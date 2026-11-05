#!/usr/bin/env python3
"""Outage study / switching plan timing (see docs/BENCHMARKS.md).
118 bus: measured end-to-end through the live EMS API (CIM node-breaker model).
~1k bus: the CIM model exists only for IEEE-14/57/118, so the tiled bus-branch network is used to MEASURE the two
building blocks (AC power flow, full AC N-1); the study time is then ESTIMATED as 2 x N-1 + 2 x PF (marked as such)."""
import json, os, sys, time, warnings, statistics
warnings.filterwarnings("ignore")
ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, os.path.join(ROOT, "services", "ems-analytics")); sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import httpx
from ems.network import Net
from ems import contingency as ca, powerflow as pf
from bench_scale import tiled

from gs_client import Sess
_S = Sess("carol")

def live(path, body, n=3):
    ts = []
    for _ in range(n):
        t0 = time.perf_counter(); r = _S.h.post("/ems" + path, json=body, timeout=300); ts.append(time.perf_counter() - t0)
        r.raise_for_status()
    return r.json(), ts

if __name__ == "__main__":
    out = {}
    body = dict(model="ieee118", equipment=["BR0001"])
    s, ts = live("/v1/outage-study", body); out["study_118_s"] = [round(t, 2) for t in ts]; out["study_118_verdict"] = s["verdict"]
    p, ts = live("/v1/switching-plan", dict(body, mode="isolate")); out["plan_118_s"] = [round(t, 2) for t in ts]; out["plan_118_steps"] = len(p["steps"]); out["plan_118_status"] = p["status"]
    sc, ts = live("/v1/outage-schedule", dict(model="ieee118", requests=[dict(id=1, equipment=["BR0001"], start=0, end=10), dict(id=2, equipment=["BR0015"], start=5, end=15)]), n=1)
    out["schedule_118_s"] = [round(t, 2) for t in ts]
    bb = json.load(open(os.path.join(ROOT, "data", "ieee118.busbranch.json")))
    for k in (1, 9):
        net = Net.from_busbranch(tiled(bb, k))
        t0 = time.perf_counter(); sol = pf.solve(net); tpf = time.perf_counter() - t0
        t0 = time.perf_counter(); ca.run_n1(net, base_sol=sol); tn1 = time.perf_counter() - t0
        out[f"buses_{net.nb}"] = dict(pf_s=round(tpf, 2), n1_s=round(tn1, 1), study_estimate_s=round(2 * tn1 + 2 * tpf, 1), estimate=True)
    print(json.dumps(out, indent=1)); json.dump(out, open(os.path.join(ROOT, "docs", "bench_outage.json"), "w"), indent=1)
