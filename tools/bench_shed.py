#!/usr/bin/env python3
"""Load-shed benchmark on IEEE-118 and a 9x tiled ~1k-bus network (raw: docs/bench_shed.json). Synthetic dynamics and priorities."""
import json, os, sys, time, warnings
warnings.filterwarnings("ignore")
ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, os.path.join(ROOT, "services", "ems-analytics")); sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ems.network import Net
from ems import shed
from bench_scale import tiled

if __name__ == "__main__":
    bb = json.load(open(os.path.join(ROOT, "data", "ieee118.busbranch.json")))
    rows = []
    for k in (1, 9):
        net = Net.from_busbranch(tiled(bb, k))
        if not net.load_p0:                       # tiled bus-branch data has no CIM loads: one block per load bus
            net.load_bus = {f"L{b}": int(b) for b in range(net.nb) if net.pd[b] > 0}; net.load_p0 = {f"L{b}": float(net.pd[b]) for b in range(net.nb) if net.pd[b] > 0}
        t0 = time.perf_counter(); u = shed.ufls(net); tu = time.perf_counter() - t0
        sc = [dict(name=s["name"], lost_mw=s["lost_mw"], lost_pct=s["lost_pct"], nadir_no=s["without"]["f_nadir"], nadir_ufls=s["with_ufls"]["f_nadir"], final_ufls=s["with_ufls"]["f_final"],
                   shed_pct=s["with_ufls"]["shed_pct"], stages_fired=sum(1 for x in s["with_ufls"]["stages"] if x["fired_at"] is not None), passes=s["passes"]) for s in u["scenarios"]]
        load = sum(net.load_p0.values()); deficit = round(0.03 * load)
        out = {}
        for rot in (True, False):
            t0 = time.perf_counter(); r = shed.roster(net, deficit_mw=deficit, hours=12, rotate=rot); dt = time.perf_counter() - t0
            out["rotating" if rot else "static"] = dict(wall_s=round(dt, 2), jain=r["jain_fairness"], loads_ever=r["loads_ever_shed"], eligible=r["eligible_loads"], max_ens_mwh=r["max_ens_mwh"],
                                                    periods_with_new_overloads=sum(1 for p in r["periods"] if p["ac"]["overloads"] > r["ac_base"]["overloads"]), ac_retries=sum(p["ac_retries"] for p in r["periods"]))
        row = dict(buses=net.nb, loads=len(net.load_p0), load_mw=round(load), ufls_wall_s=round(tu, 2), scenarios=sc, deficit_mw=deficit, roster=out)
        rows.append(row); print(json.dumps(row), flush=True)
    json.dump(rows, open(os.path.join(ROOT, "docs", "bench_shed.json"), "w"), indent=1)
