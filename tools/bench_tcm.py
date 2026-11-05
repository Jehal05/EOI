#!/usr/bin/env python3
"""Measured corridor-capability benchmark on IEEE-118 and a tiled ~1k-bus synthetic network (see docs/BENCHMARKS.md)."""
import json, os, sys, time, warnings
warnings.filterwarnings("ignore")
ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, os.path.join(ROOT, "services", "ems-analytics")); sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ems.network import Net
from ems import tcm
from bench_scale import tiled

if __name__ == "__main__":
    bb = json.load(open(os.path.join(ROOT, "data", "ieee118.busbranch.json")))
    rows = []
    for k in [int(a) for a in sys.argv[1:]] or [1, 9]:
        net = Net.from_busbranch(tiled(bb, k))
        for n1 in (1.0, 1.15):
            t0 = time.perf_counter(); r = tcm.analyze(net, parts=3, n1_limit=n1); dt = time.perf_counter() - t0
            cs = r["corridors"]
            row = dict(buses=net.nb, branches=net.nl, n1_limit=n1, corridors=len(cs), wall_s=round(dt, 1),
                       atc_mw=[c["atc_mw"] for c in cs], ac_mw=[c["ac_mw"] for c in cs], dc_mw=[c["dc_mw"] for c in cs],
                       limits=[c["limit"]["type"] for c in cs], preexisting_excluded=[c.get("preexisting_excluded") for c in cs])
            rows.append(row); print(json.dumps(row), flush=True)
            json.dump(rows, open(os.path.join(ROOT, "docs", "bench_tcm.json"), "w"), indent=1)
