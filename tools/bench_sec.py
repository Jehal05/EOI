#!/usr/bin/env python3
"""Measured security-enhancement benchmark on IEEE-118 and a tiled ~1k-bus synthetic network (see docs/BENCHMARKS.md)."""
import json, os, sys, time, warnings
warnings.filterwarnings("ignore")
ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, os.path.join(ROOT, "services", "ems-analytics")); sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ems.network import Net
from ems import security
from bench_scale import tiled

if __name__ == "__main__":
    bb = json.load(open(os.path.join(ROOT, "data", "ieee118.busbranch.json")))
    rows = []
    for k in [int(a) for a in sys.argv[1:]] or [1, 9]:
        net = Net.from_busbranch(tiled(bb, k))
        t0 = time.perf_counter(); r = security.plan(net, emergency=1.15); dt = time.perf_counter() - t0
        row = dict(buses=net.nb, branches=net.nl, status=r["status"], critical_before=r["before"]["critical"], critical_after=r["after"]["critical"],
                   excess_before_pct=round(r["before"]["total_excess_pct"]), excess_after_pct=round(r["after"]["total_excess_pct"]),
                   worst_before_pct=round(r["before"]["worst_loading_pct"]), worst_after_pct=round(r["after"]["worst_loading_pct"]),
                   redispatch_mw=round(r.get("redispatch_mw") or 0), shed_mw=round(r.get("shed_mw") or 0, 1), cost_change_per_h=round(r.get("cost_change_per_h") or 0),
                   switching_actions=sum(a["type"] == "switching" for a in r["actions"]), wall_s=round(dt, 1))
        rows.append(row); print(json.dumps(row), flush=True)
        json.dump(rows, open(os.path.join(ROOT, "docs", "bench_sec.json"), "w"), indent=1)
