#!/usr/bin/env python3
"""Operator log / handover / comms API timings on the live stack (raw: docs/bench_logbook.json)."""
import json, os, statistics, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from gs_client import Sess

def pct(v, p): v = sorted(v); return round(v[min(len(v) - 1, int(len(v) * p))] * 1000, 1)
def timed(f, n):
    ts = []
    for _ in range(n):
        t0 = time.perf_counter(); r = f(); ts.append(time.perf_counter() - t0); assert r.status_code == 200, r.text
    return dict(n=n, p50_ms=pct(ts, 0.5), p95_ms=pct(ts, 0.95), max_ms=round(max(ts) * 1000, 1))

a, c = Sess("alice"), Sess("carol")
out = {}
out["log_append"] = timed(lambda: a.post("/api/log", json={"category": "general", "text": "bench entry %d" % time.time_ns()}), 300)
out["log_read_200"] = timed(lambda: a.get("/api/log?limit=200"), 50)
out["log_search"] = timed(lambda: a.get("/api/log?q=bench&limit=200"), 50)
out["comms_118_substations"] = timed(lambda: a.get("/api/comms"), 100)
out["handover_draft"] = timed(lambda: a.get("/api/handover/draft"), 50)
p = c.get("/api/handover").json()["pending"]
if p: a.post("/api/handover/accept", json={"n": p["n"]})
r = a.post("/api/handover", json={"notes": "bench handover"}); assert r.status_code == 200, r.text
t0 = time.perf_counter(); r2 = c.post("/api/handover/accept", json={"n": r.json()["handover"]["n"]}); out["handover_accept_ms"] = round((time.perf_counter() - t0) * 1000, 1)
out["log_entries_after"] = len(a.get("/api/log?limit=1000").json()["entries"])
print(json.dumps(out, indent=1)); json.dump(out, open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "docs", "bench_logbook.json"), "w"), indent=1)
