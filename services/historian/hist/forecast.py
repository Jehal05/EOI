"""Short-term load / renewable forecasting with calibrated prediction intervals. ADVISORY: never feeds a control path.

Method (deliberately simple and inspectable; no opaque model)
 * Series: 1-minute aggregates (or any regular series) resampled to `step` with forward fill (gaps > 3 steps stay NaN).
 * Candidates, all multi-horizon: persistence; seasonal-naive (daily, when >= 2 days of history); ridge regression
   (direct multi-output) on recent lags + daily Fourier terms (only when >= 2 days of history).
 * Chronological split 60 / 20 / 20 (train / validation / test). The model with the lowest validation MAE is chosen.
 * Intervals: split-conformal on the VALIDATION residuals per horizon (finite-sample corrected empirical quantile),
   i.e. no distributional assumption; coverage is then MEASURED on the untouched test block and reported.
 * The final model is refit on train+validation before forecasting (intervals reuse the validation residual quantiles).
Limits: no weather / calendar-holiday inputs; intervals are marginal per horizon; quantiles assume the residual process is
stationary between validation and use; with short history only persistence-type skill is available and the output says so.
"""
from __future__ import annotations
import math
import numpy as np

MIN_SAMPLES = 120
LEVELS = (0.80, 0.95)


class ForecastError(ValueError):
    pass


def resample(t_ms, v, step_ms: int, max_gap_steps: int = 3):
    t_ms = np.asarray(t_ms, dtype=np.int64); v = np.asarray(v, dtype=float)
    if len(t_ms) < 2:
        raise ForecastError("not enough history")
    t0 = int(t_ms[0] // step_ms * step_ms); t1 = int(t_ms[-1] // step_ms * step_ms)
    grid = np.arange(t0, t1 + step_ms, step_ms, dtype=np.int64)
    idx = np.searchsorted(t_ms, grid, side="right") - 1
    out = np.where(idx >= 0, v[np.clip(idx, 0, None)], np.nan)
    age = np.where(idx >= 0, (grid - t_ms[np.clip(idx, 0, None)]) / step_ms, np.inf)
    out[age > max_gap_steps] = np.nan
    return grid, out


def _fourier(t_ms, K=3):
    ph = 2 * np.pi * ((np.asarray(t_ms) / 86_400_000.0) % 1.0)
    cols = [np.ones(len(ph))]
    for k in range(1, K + 1):
        cols += [np.sin(k * ph), np.cos(k * ph)]
    return np.column_stack(cols)


class Ridge:
    def __init__(self, lags=12, daily=True, alpha=1.0, K=3):
        self.lags, self.daily, self.alpha, self.K = lags, daily, alpha, K

    def _X(self, y, t, i0, i1):
        """Features for forecast origins i in [i0, i1): lags of y relative to y[i] plus Fourier of origin time."""
        idx = np.arange(i0, i1)
        cols = [np.ones(len(idx)), y[idx]]
        for l in range(1, self.lags + 1):
            cols.append(y[idx - l] - y[idx])
        X = np.column_stack(cols)
        if self.daily:
            X = np.hstack([X, _fourier(t[idx], self.K)[:, 1:]])
        return X

    def fit(self, y, t, H, i_end):
        i0, i1 = self.lags, i_end - H
        X = self._X(y, t, i0, i1)
        Y = np.column_stack([y[i0 + h: i1 + h] - y[i0:i1] for h in range(1, H + 1)])      # predict change from the origin value
        ok = np.isfinite(X).all(axis=1) & np.isfinite(Y).all(axis=1)
        X, Y = X[ok], Y[ok]
        if len(X) < 30:
            raise ForecastError("not enough clean training windows")
        A = X.T @ X + self.alpha * np.eye(X.shape[1])
        A[0, 0] -= self.alpha
        self.W = np.linalg.solve(A, X.T @ Y)
        return self

    def predict(self, y, t, origins):
        X = self._X(y, t, origins[0], origins[-1] + 1)
        return y[origins][:, None] + X @ self.W


def _persist(y, origins, H):
    return np.repeat(y[origins][:, None], H, axis=1)


def _seasonal(y, origins, H, per):
    out = np.empty((len(origins), H))
    for j, o in enumerate(origins):
        for h in range(1, H + 1):
            k = o + h - per * math.ceil(h / per)       # same time one (or more) period ago
            out[j, h - 1] = y[k]
    return out


def _targets(y, origins, H):
    return np.column_stack([y[origins + h] for h in range(1, H + 1)])


def _scale(y, idx, w=12):
    """Local volatility at forecast origins (std of the last `w` one-step changes); makes the intervals adaptive."""
    d = np.diff(y, prepend=y[0])
    c = np.concatenate([[0.0], np.cumsum(d)]); c2 = np.concatenate([[0.0], np.cumsum(d * d)])
    a = np.maximum(idx - w + 1, 0)
    n = (idx + 1 - a).astype(float)
    m = (c[idx + 1] - c[a]) / n
    var = np.maximum((c2[idx + 1] - c2[a]) / n - m * m, 0.0)
    return np.sqrt(var) + 1e-6 * (np.abs(y[idx]) + 1.0)


def _conformal_q(res_abs, level):
    """finite-sample corrected empirical quantile of |residual| per horizon (columns)."""
    n = res_abs.shape[0]
    k = min(n, math.ceil((n + 1) * level))
    return np.sort(res_abs, axis=0)[k - 1]


def forecast(t_ms, v, *, step_min: int = 5, horizon_min: int = 120, nonneg: bool = False, cap: float | None = None, seed_origin_stride: int = 1) -> dict:
    step = step_min * 60_000
    H = max(1, int(round(horizon_min / step_min)))
    t, y = resample(t_ms, v, step)
    # fill short NaNs by carrying the last value, then require enough data
    ok = np.isfinite(y)
    if ok.sum() < MIN_SAMPLES:
        raise ForecastError(f"need at least {MIN_SAMPLES} samples at {step_min}-minute steps, have {int(ok.sum())}")
    first = int(np.argmax(ok)); t, y = t[first:], y[first:]
    for i in range(1, len(y)):
        if not np.isfinite(y[i]) and np.isfinite(y[i - 1]):
            y[i] = y[i - 1]
    y = np.where(np.isfinite(y), y, np.nanmean(y))
    n = len(y)
    span_h = (t[-1] - t[0]) / 3_600_000.0
    per = int(round(86_400_000 / step))
    daily = span_h >= 48.0 and n > 3 * per
    lags = max(6, min(24, H // 2 + 6))
    warns = []
    if not daily:
        warns.append(f"history {span_h:.1f} h < 48 h: no daily seasonality can be learned; only short-term (lag) skill is available")
    if H > n // 5:
        raise ForecastError("horizon too long for the available history")
    n_tr, n_va = int(0.6 * n), int(0.2 * n)
    i_va0, i_te0 = n_tr, n_tr + n_va
    start = max(lags, per if daily else 0)

    def origins(a, b):
        a = max(a, start)
        return np.arange(a, b - H, max(1, seed_origin_stride))

    cand = {"persistence": lambda yy, o: _persist(yy, o, H)}
    if daily:
        cand["seasonal_naive"] = lambda yy, o: _seasonal(yy, o, H, per)
    models = {}

    def ridge_fn(daily_flag):
        def f(yy, o, _m={}):
            return _m["m"].predict(yy, t, o)
        return f
    rg = Ridge(lags=lags, daily=daily).fit(y, t, H, i_va0)
    cand["ridge"] = lambda yy, o, m=rg: m.predict(yy, t, o)
    res = {}
    for name, fn in cand.items():
        ov = origins(i_va0, i_te0)
        res[name] = np.abs(_targets(y, ov, H) - fn(y, ov))
    mae_val = {k: float(np.mean(v)) for k, v in res.items()}
    best = min(mae_val, key=mae_val.get)
    # scale-normalised split-conformal: quantiles of |residual| / local volatility on the validation block
    ov = origins(i_va0, i_te0)
    sv = _scale(y, ov)[:, None]
    q = {lv: _conformal_q(res[best] / sv, lv) for lv in LEVELS}
    # honest test-block evaluation (models fit on train only; intervals from validation)
    ot = origins(i_te0, n)
    tgt = _targets(y, ot, H)
    pt = {k: fn(y, ot) for k, fn in cand.items()}
    mae_test = {k: float(np.mean(np.abs(tgt - p))) for k, p in pt.items()}
    st = _scale(y, ot)[:, None]
    cover = {f"{int(lv * 100)}": float(np.mean(np.abs(tgt - pt[best]) <= q[lv][None, :] * st)) for lv in LEVELS}
    base = mae_test["persistence"]
    skill = {k: (1.0 - m / base if base > 0 else 0.0) for k, m in mae_test.items()}
    mae_h = np.mean(np.abs(tgt - pt[best]), axis=0)
    # refit on train+validation (ridge only) and forecast from the last observation
    if best == "ridge":
        final = Ridge(lags=lags, daily=daily).fit(y, t, H, i_te0)
        f = final.predict(y, t, np.array([n - 1]))[0]
    elif best == "seasonal_naive":
        f = _seasonal(y, np.array([n - 1]), H, per)[0]
    else:
        f = _persist(y, np.array([n - 1]), H)[0]
    s_now = float(_scale(y, np.array([n - 1]))[0])
    lo = lambda lv: f - q[lv] * s_now
    hi = lambda lv: f + q[lv] * s_now
    L = lambda a: np.maximum(a, 0.0) if nonneg else a
    U = lambda a: np.minimum(a, cap) if cap is not None else a
    pts = []
    for h in range(H):
        pts.append(dict(t=int(t[-1] + (h + 1) * step), p50=float(U(L(f[h]))),
                        lo80=float(U(L(lo(0.80)[h]))), hi80=float(U(L(hi(0.80)[h]))), lo95=float(U(L(lo(0.95)[h]))), hi95=float(U(L(hi(0.95)[h])))))
    if best == "persistence" and daily:
        warns.append("persistence beat the learned models on validation; no usable skill beyond the last value")
    if skill.get(best, 0) < 0.02 and best != "persistence":
        warns.append("learned model is not meaningfully better than persistence on the test block")
    return dict(advisory=True, model=best, step_min=step_min, horizon_min=H * step_min, n_samples=int(n), history_hours=round(span_h, 1),
                history=[dict(t=int(a), v=float(b)) for a, b in zip(t[-3 * H:], y[-3 * H:])], seasonality="daily" if daily else "none", last_t=int(t[-1]), last_value=float(y[-1]), points=pts,
                backtest=dict(n_origins=int(len(ot)), mae={k: round(v, 4) for k, v in mae_test.items()}, mae_validation={k: round(v, 4) for k, v in mae_val.items()},
                              skill_vs_persistence={k: round(v, 4) for k, v in skill.items()}, coverage={k: round(v, 4) for k, v in cover.items()},
                              nominal={f"{int(lv * 100)}": lv for lv in LEVELS}, mae_by_horizon=[round(float(x), 4) for x in mae_h[:: max(1, H // 12)]]),
                warnings=warns)
