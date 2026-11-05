#!/usr/bin/env python3
"""Measured OPF / SCOPF benchmark on the public IEEE-118 case and a tiled ~1k-bus synthetic network (see docs/BENCHMARKS.md)."""
import json, os, sys, time, warnings
warnings.filterwarnings("ignore")
ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, os.path.join(ROOT, "services", "ems-analytics")); sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ems.network import Net
from ems import opf
from bench_scale import tiled

def t(f):
    t0 = time.perf_counter(); r = f(); return r, time.perf_counter() - t0

if __name__ == "__main__":
    bb = json.load(open(os.path.join(ROOT, "data", "ieee118.busbranch.json")))
    rows = []
    for k in [int(a) for a in sys.argv[1:]] or [1, 9]:
        net = Net.from_busbranch(tiled(bb, k))
        g, t_grid = t(lambda: opf.Grid(net))
        lp, t_lp = t(lambda: opf.solve_dc_opf(net, g))
        o, t_opf = t(lambda: opf.run_opf(net, verify=True))
        s, t_sc = t(lambda: opf.run_scopf(net, verify=True, verify_n1=(k == 1)))
        row = dict(buses=net.nb, branches=net.nl, gens=len(net.g_bus), grid_build_s=round(t_grid, 3), one_lp_s=round(t_lp, 3),
                   opf_with_ac_verify_s=round(t_opf, 2), opf_cost=o["cost_per_h"], opf_unserved_mw=o["unserved_mw"],
                   scopf_s=round(t_sc, 2), scopf_iterations=s["iterations"], scopf_cuts=s["n_cuts"], scopf_cost_of_security=s["security_cost_per_h"],
                   scopf_unserved_mw=s["unserved_mw"], islanding_not_secured=s["n_islanding_not_secured"], ac_n1=s.get("ac_n1"))
        rows.append(row); print(json.dumps(row), flush=True)
    json.dump(rows, open(os.path.join(ROOT, "docs", "bench_opf.json"), "w"), indent=1)
