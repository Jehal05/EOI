#!/usr/bin/env python3
"""Measured N-2 screening benchmark (see docs/BENCHMARKS.md). Synthetic tiled IEEE-118; run as a script file."""
import json, os, sys, time, warnings
warnings.filterwarnings("ignore")
ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, os.path.join(ROOT, "services", "ems-analytics")); sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ems.network import Net
from ems import n2, powerflow as pf
from bench_scale import tiled

if __name__ == "__main__":
    bb = json.load(open(os.path.join(ROOT, "data", "ieee118.busbranch.json")))
    rows = []
    for k in [int(a) for a in sys.argv[1:]] or [1, 9]:
        net = Net.from_busbranch(tiled(bb, k))
        r = n2.run_n2(net, workers=2, k_top=40, verify=60)
        d = n2.run_n2(net, workers=1, k_top=40, verify=1, dc_only=True)
        row = dict(buses=net.nb, branches=net.nl, all_pairs=r["n_pairs_total"], candidates=r["n_candidates"],
                   ac_verified=r["n_ac_verified"], dc_build_s=d["dc_build_seconds"], dc_screen_s=d["dc_screen_seconds"],
                   total_wall_s=r["wall_seconds"])
        rows.append(row); print(json.dumps(row), flush=True)
    json.dump(rows, open(os.path.join(ROOT, "docs", "bench_n2.json"), "w"), indent=1)
