import os, tempfile, time
from hist.store import Store


def mk():
    return Store(os.path.join(tempfile.mkdtemp(), "h.db"))


def test_raw_and_aggregate_roundtrip():
    s = mk()
    t0 = 1_700_000_000_000
    pts = [{"t": "X.P", "m": t0 + i * 1000, "v": float(i)} for i in range(120)]      # 2 minutes, 1 Hz
    assert s.write(pts) == 120
    r = s.trend("X.P", t0, t0 + 120_000)
    assert r["resolution"] == "raw" and len(r["points"]) == 120
    r = s.trend("X.P", t0 - 3_600_000, t0 + 3_600_000)           # >15 min -> aggregate
    assert r["resolution"].startswith("1m")
    tot = sum(p["n"] for p in r["points"])
    assert tot == 120
    assert min(p["min"] for p in r["points"]) == 0.0 and max(p["max"] for p in r["points"]) == 119.0
    avg = sum(p["avg"] * p["n"] for p in r["points"]) / tot
    assert abs(avg - 59.5) < 1e-9


def test_status_points_and_unknown_tag():
    s = mk()
    s.write([{"t": "BRK.STATE", "m": 1000, "s": 2}, {"t": "BRK.STATE", "m": 2000, "s": 1}])
    r = s.trend("BRK.STATE", 0, 5000)
    assert [p["avg"] for p in r["points"]] == [2.0, 1.0]
    assert s.trend("NOPE", 0, 5000)["points"] == []


def test_decimation_keeps_envelope():
    s = mk()
    t0 = 1_700_000_000_000
    s.write([{"t": "Y", "m": t0 + i * 1000, "v": 1000.0 if i == 777 else 1.0} for i in range(3000)])
    r = s.trend("Y", t0, t0 + 3_000_000, max_points=100)           # 50 min -> aggregates, decimated
    assert len(r["points"]) <= 100
    assert max(p["max"] for p in r["points"]) == 1000.0            # the spike survives decimation


def test_idempotent_on_same_timestamp():
    s = mk()
    s.write([{"t": "Z", "m": 5000, "v": 1.0}])
    s.write([{"t": "Z", "m": 5000, "v": 2.0}])
    assert len(s.trend("Z", 0, 9000)["points"]) == 1


def test_persistence_across_restart():
    d = tempfile.mkdtemp(); p = os.path.join(d, "h.db")
    s = Store(p); s.write([{"t": "K", "m": 1, "v": 7.0}]); del s
    s2 = Store(p)
    assert s2.trend("K", 0, 10)["points"][0]["avg"] == 7.0
