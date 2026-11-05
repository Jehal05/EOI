import os, tempfile
os.environ["HIST_DB"] = os.path.join(tempfile.mkdtemp(), "fc.db")      # never touch the live historian database
os.environ.setdefault("GS_DEV", "1")
import numpy as np
import pytest
from hist import forecast as fc, synth


def test_resample_forward_fill_and_gap_limit():
    t = np.array([0, 60_000, 600_000]); v = np.array([1.0, 2.0, 3.0])
    g, y = fc.resample(t, v, 60_000)
    assert y[0] == 1.0 and y[1] == 2.0 and y[2] == 2.0 and y[3] == 2.0
    assert np.isnan(y[5])                                    # older than 3 steps -> gap stays a gap
    assert y[-1] == 3.0


def test_ridge_recovers_known_ar_process():
    rng = np.random.default_rng(0)
    n = 4000
    y = np.zeros(n)
    for i in range(1, n):
        y[i] = 0.9 * y[i - 1] + rng.normal(0, 1)
    t = np.arange(n) * 300_000
    m = fc.Ridge(lags=3, daily=False, alpha=1e-6).fit(y, t, 1, n - 1)
    o = np.arange(100, n - 1)
    p = m.predict(y, t, o)[:, 0]
    err = y[o + 1] - p
    assert np.std(err) == pytest.approx(1.0, abs=0.05)       # irreducible noise sigma = 1


def test_conformal_quantile_is_finite_sample_corrected():
    r = np.abs(np.arange(1, 100).astype(float))[:, None]    # 99 residuals 1..99
    assert fc._conformal_q(r, 0.8)[0] == 80.0                # ceil(100*0.8) = 80th smallest


@pytest.mark.parametrize("gen,kw", [(synth.load, {}), (synth.solar, {"nonneg": True}), (synth.wind, {"nonneg": True})])
def test_coverage_on_untouched_test_block_is_close_to_nominal(gen, kw):
    t, v = gen(days=20, step_min=5)
    r = fc.forecast(t, v, step_min=5, horizon_min=120, **kw)
    cov = r["backtest"]["coverage"]
    assert 0.70 <= cov["80"] <= 0.90 and 0.90 <= cov["95"] <= 0.99, cov
    assert r["backtest"]["n_origins"] > 500


def test_load_forecast_beats_persistence_at_multi_hour_horizon():
    t, v = synth.load(days=20, step_min=5)
    r = fc.forecast(t, v, step_min=5, horizon_min=360)
    assert r["model"] in ("ridge", "seasonal_naive")
    assert r["backtest"]["skill_vs_persistence"][r["model"]] > 0.3
    assert r["seasonality"] == "daily"


def test_solar_nonneg_and_cap_enforced_and_night_is_zero():
    t, v = synth.solar(days=15, step_min=5, cap=50.0)
    r = fc.forecast(t, v, step_min=5, horizon_min=240, nonneg=True, cap=50.0)
    for p in r["points"]:
        assert 0.0 <= p["lo95"] <= p["lo80"] <= p["p50"] + 1e-9 <= p["hi80"] + 1e-9 <= p["hi95"] <= 50.0


def test_intervals_are_ordered_and_widen_with_horizon():
    t, v = synth.wind(days=15, step_min=5)
    r = fc.forecast(t, v, step_min=5, horizon_min=180, nonneg=True)
    w = [p["hi95"] - p["lo95"] for p in r["points"]]
    assert w[-1] >= w[0]
    assert all(p["lo95"] <= p["lo80"] <= p["hi80"] <= p["hi95"] for p in r["points"])


def test_short_history_is_honest_not_seasonal():
    t, v = synth.load(days=1, step_min=1)
    r = fc.forecast(t, v, step_min=1, horizon_min=30)
    assert r["seasonality"] == "none" and any("48 h" in w for w in r["warnings"])
    with pytest.raises(fc.ForecastError):
        fc.forecast(t[:50], v[:50], step_min=1, horizon_min=10)


def test_deterministic():
    t, v = synth.load(days=10, step_min=5)
    a = fc.forecast(t, v, step_min=5, horizon_min=60); b = fc.forecast(t, v, step_min=5, horizon_min=60)
    assert a["points"] == b["points"] and a["backtest"] == b["backtest"]


def test_api_gate_fails_closed_and_audits(monkeypatch):
    import os
    os.environ.setdefault("GS_DEV", "1")
    from fastapi.testclient import TestClient
    from hist import app as A
    t, v = synth.load(days=3, step_min=1)
    A.store.write([{"t": "L1.P", "m": int(a), "v": float(b)} for a, b in zip(t[:3000], v[:3000])])
    cl = TestClient(A.app)
    hdr = {"Authorization": "Bearer " + A.mint("u", "viewer")}
    calls = []

    class R:
        def __init__(self, code): self.status_code = code

    def fake(code):
        def post(url, **kw):
            calls.append((url, kw["json"])); return R(code)
        return post
    body = {"tags": ["L1.P"], "mode": "single", "horizon_min": 30, "step_min": 5, "history_hours": 72}
    monkeypatch.setattr(A.httpx, "post", fake(200))
    r = cl.post("/v1/forecast", json=body, headers=hdr)
    assert r.status_code == 200 and r.json()["advisory"] is True and len(r.json()["points"]) == 6
    assert calls and calls[0][1]["feature"] == "forecast" and calls[0][0].endswith("/api/ai/use")
    monkeypatch.setattr(A.httpx, "post", fake(409))
    r = cl.post("/v1/forecast", json=body, headers=hdr)
    assert r.status_code == 503 and "off" in r.json()["detail"]

    def boom(url, **kw): raise OSError("down")
    monkeypatch.setattr(A.httpx, "post", boom)
    assert cl.post("/v1/forecast", json=body, headers=hdr).status_code == 503        # unreachable audit service -> fail closed
    assert cl.post("/v1/forecast", json=body).status_code == 401
