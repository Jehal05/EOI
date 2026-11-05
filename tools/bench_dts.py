#!/usr/bin/env python3
"""DTS timing on IEEE-118 and a 9x tiled ~1k-bus network (raw: docs/bench_dts.json)."""
import json, os, sys, time, warnings
warnings.filterwarnings("ignore")
ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, os.path.join(ROOT, "services", "ems-analytics")); sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ems.network import Net
from ems import dts
from bench_scale import tiled
bb = json.load(open(os.path.join(ROOT, "data", "ieee118.busbranch.json"))); rows = []
for k in (1, 9):
    net = Net.from_busbranch(tiled(bb, k))
    if not net.load_p0:
        net.load_bus = {f"L{b}": int(b) for b in range(net.nb) if net.pd[b] > 0}; net.load_p0 = {f"L{b}": float(net.pd[b]) for b in range(net.nb) if net.pd[b] > 0}
    t0 = time.perf_counter(); lib = dts.library(net); tl = time.perf_counter() - t0
    sc = dts.validate_scenario(net, next(s for s in lib if s["id"] == "double"))
    t0 = time.perf_counter(); s = dts.Session(net, sc, "u", "m"); tc = time.perf_counter() - t0
    ts = []
    for _ in range(int(sc["duration"] // 60)):
        t0 = time.perf_counter(); s.step(60); ts.append(time.perf_counter() - t0)
    t0 = time.perf_counter(); s.act("open_branch", net.br_id[5]); ta = time.perf_counter() - t0
    rows.append(dict(buses=net.nb, library_s=round(tl, 2), session_create_s=round(tc, 2), step_60s_ms=round(1000 * sorted(ts)[len(ts) // 2], 0), action_ms=round(1000 * ta, 0), final_score=s.score()["total"]))
    print(rows[-1], flush=True)
json.dump(rows, open(os.path.join(ROOT, "docs", "bench_dts.json"), "w"), indent=1)
