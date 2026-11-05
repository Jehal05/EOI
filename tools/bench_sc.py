#!/usr/bin/env python3
"""Measured short-circuit benchmark (all-bus fault levels) on tiled synthetic networks; see docs/BENCHMARKS.md."""
import json, os, sys, time, warnings
warnings.filterwarnings("ignore")
ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, os.path.join(ROOT, "services", "ems-analytics")); sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ems.network import Net
from ems import shortcircuit as sc
from bench_scale import tiled
if __name__ == "__main__":
    bb = json.load(open(os.path.join(ROOT, "data", "ieee118.busbranch.json")))
    rows = []
    for k in [int(a) for a in sys.argv[1:]] or [1, 9, 43]:
        net = Net.from_busbranch(tiled(bb, k))
        t0 = time.perf_counter(); r = sc.analyze(net); dt = time.perf_counter() - t0
        row = dict(buses=net.nb, branches=net.nl, all_bus_seconds=round(dt, 3), buses_analysed=r["n_buses"], max_ik_ka=round(max(x["max_ka"] for x in r["results"]), 1))
        rows.append(row); print(json.dumps(row), flush=True)
    json.dump(rows, open(os.path.join(ROOT, "docs", "bench_sc.json"), "w"), indent=1)
