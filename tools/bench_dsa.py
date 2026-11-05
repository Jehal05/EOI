#!/usr/bin/env python3
"""Measured dynamic-security benchmark on IEEE-118 and a tiled ~1k-bus network (see docs/BENCHMARKS.md). Dynamic data are synthetic."""
import json, os, sys, time, warnings
import numpy as np
warnings.filterwarnings("ignore")
ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, os.path.join(ROOT, "services", "ems-analytics")); sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ems.network import Net
from ems import dsa
from bench_scale import tiled

if __name__ == "__main__":
    bb = json.load(open(os.path.join(ROOT, "data", "ieee118.busbranch.json")))
    rows = []
    for k in [int(a) for a in sys.argv[1:]] or [1, 9]:
        net = Net.from_busbranch(tiled(bb, k))
        row = dict(buses=net.nb, branches=net.nl)
        t0 = time.perf_counter(); v = dsa.vsa(net, n1_top=4); row["vsa_s"] = round(time.perf_counter() - t0, 1)
        row.update(vsa_margin_pct=v["base"]["margin_pct"], vsa_worst_n1_pct=v["worst_margin_pct"], vsa_weakest=v["base"]["weakest_bus_name"], vsa_pf_steps=v["base"]["steps"])
        t0 = time.perf_counter(); dy = dsa.Dyn(net); row["tsa_setup_s"] = round(time.perf_counter() - t0, 2); row["units"] = dy.ng
        nf = 118 if k == 1 else 12
        t0 = time.perf_counter(); r = dsa.tsa(net, t_clear=0.12, cct_top=6 if k == 1 else 3, max_buses=nf); dt = time.perf_counter() - t0
        ccts = [x["cct_s"] for x in r["results"] if x.get("cct_s") is not None]
        row.update(tsa_faults=r["n_faults"], tsa_unstable=r["n_unstable"], tsa_s=round(dt, 1), tsa_s_per_fault=round(dt / max(r["n_faults"], 1), 2),
                   tsa_min_cct_s=min(ccts) if ccts else None, tsa_max_angle_deg=max(x["max_angle_sep_deg"] for x in r["results"]))
        t0 = time.perf_counter(); s = dsa.ssa(net); row["ssa_s"] = round(time.perf_counter() - t0, 2)
        row.update(ssa_modes=s["n_modes"], ssa_min_damping=s["min_damping_ratio"], ssa_poor=s["n_poorly_damped"], ssa_freq_hz=s["freq_range_hz"], ssa_status=s["status"])
        rows.append(row); print(json.dumps(row), flush=True)
    json.dump(rows, open(os.path.join(ROOT, "docs", "bench_dsa.json"), "w"), indent=1)
