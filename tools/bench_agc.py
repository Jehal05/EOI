#!/usr/bin/env python3
"""Measured AGC simulation benchmark on IEEE-118 and a tiled ~1k-bus network (see docs/BENCHMARKS.md). Dynamics parameters are synthetic."""
import json, os, sys, time, warnings
import numpy as np
warnings.filterwarnings("ignore")
ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, os.path.join(ROOT, "services", "ems-analytics")); sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ems.network import Net
from ems import agc
from bench_scale import tiled

if __name__ == "__main__":
    bb = json.load(open(os.path.join(ROOT, "data", "ieee118.busbranch.json")))
    rows = []
    for k in [int(a) for a in sys.argv[1:]] or [1, 9]:
        net = Net.from_busbranch(tiled(bb, k))
        for parts in (2, 4):
            t0 = time.perf_counter(); S = agc.Setup(net, parts); t_setup = time.perf_counter() - t0
            for label, ev in (("load step 3 % of area 0", [dict(t=5, kind="load_step", area=0, mw=round(0.03 * float(S.load[0]), 1))]),
                              ("trip of largest unit in area 0", [dict(t=5, kind="gen_trip", unit=S.ids[int(np.where(S.g_area == 0)[0][np.argmax(S.p0[S.g_area == 0])])])])):
                on = agc.simulate(net, parts=parts, events=ev, duration=600, agc=True, setup=S)
                off = agc.simulate(net, parts=parts, events=ev, duration=600, agc=False, setup=S)
                row = dict(buses=net.nb, units=len(S.units), areas=parts, event=label, mw=ev[0].get("mw"), setup_s=round(t_setup, 2), sim_s_per_run=on["wall_seconds"],
                           nadir_hz_no_agc=min(a["nadir_hz"] for a in off["areas"]), nadir_hz_agc=min(a["nadir_hz"] for a in on["areas"]),
                           final_dev_hz_no_agc=max(abs(a["final_dev_hz"]) for a in off["areas"]), final_dev_hz_agc=max(abs(a["final_dev_hz"]) for a in on["areas"]),
                           final_tie_dev_mw_agc=max(abs(a["final_tie_dev_mw"]) for a in on["areas"]), final_ace_mw_agc=max(abs(a["final_ace_mw"]) for a in on["areas"]),
                           analytic_primary_hz=on["reference"]["primary_only_steady_state_hz"], measured_primary_hz=round(float(np.mean([a["final_dev_hz"] for a in off["areas"]])), 4),
                           ace_settle_s=[a["ace_settle_s"] for a in on["areas"]], units_at_limit=on["units_at_limit"])
                rows.append(row); print(json.dumps(row), flush=True)
    json.dump(rows, open(os.path.join(ROOT, "docs", "bench_agc.json"), "w"), indent=1)
