#!/usr/bin/env python3
"""Measured EMS scale benchmark on synthetic networks built by tiling the public IEEE-118 case.

Tiles are joined in a binary tree by synthetic tie lines. Reports power-flow, state-estimation and N-1 timings on
THIS host (see the printed CPU/RAM). N-1 at large sizes is timed on a random sample of outages and
the full-screen time is an explicit EXTRAPOLATION (cases x mean case time / workers), labelled as such.
Usage: bench_scale.py [tiles...]   (default 1 9 43 85  ->  118 / 1062 / 5074 / 10030 buses)"""
import copy, json, os, platform, sys, time
import numpy as np
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "services", "ems-analytics"))
from ems.network import Net
from ems import powerflow as pf, state_estimator as se

ROOT = os.path.join(os.path.dirname(__file__), "..")


def tiled(bb, k):
    nb0, nl0 = len(bb["buses"]), len(bb["branches"])
    out = dict(baseMVA=bb["baseMVA"], buses=[], branches=[], gens=[], islands=[dict(hasSlack=True, refBus=bb["islands"][0]["refBus"])])
    for t in range(k):
        for b in bb["buses"]:
            nb = dict(b); nb["name"] = f"{b['name']}#{t}"; out["buses"].append(nb)
        for r in bb["branches"]:
            nr = dict(r); nr["mrid"] = f"{r['mrid']}#{t}"; nr["from"] += t * nb0; nr["to"] += t * nb0; out["branches"].append(nr)
        for g in bb["gens"]:
            ng = dict(g); ng["mrid"] = f"{g['mrid']}#{t}"; ng["bus"] += t * nb0; out["gens"].append(ng)
        if t:   # synthetic tie lines to the parent tile in a binary tree (shallow, meshed by 3 parallel ties)
            parent = (t - 1) // 2
            for j, (a, b) in enumerate(((10, 10), (60, 60), (90, 90))):
                out["branches"].append(dict(mrid=f"TIE{t}-{j}", **{"from": parent * nb0 + a, "to": t * nb0 + b},
                                            r=0.001, x=0.01, b=0.02, tap=0.0, shift=0.0, rate=3000.0))
    return out


def t_ms(f):
    t0 = time.perf_counter(); r = f(); return r, (time.perf_counter() - t0) * 1000


def main():
    tiles = [int(a) for a in sys.argv[1:]] or [1, 9, 43, 85]
    bb = json.load(open(os.path.join(ROOT, "data", "ieee118.busbranch.json")))
    print(f"host: {platform.processor() or platform.machine()} cpus={os.cpu_count()}  python={platform.python_version()}")
    rows = []
    for k in tiles:
        net = Net.from_busbranch(tiled(bb, k))
        row = dict(buses=net.nb, branches=net.nl)
        flat, row["pf_flatstart_ms"] = t_ms(lambda: pf.solve(net))
        row["pf_flatstart_converged"] = bool(flat["converged"])
        # real-time operation always warm-starts from the previous solution; emulate with the tiled 118-bus solution
        v118 = pf.solve(Net.from_busbranch(bb))["V"]
        V0 = np.tile(v118, k)
        sol, row["pf_ms"] = t_ms(lambda: pf.solve(net, V0=V0))
        row["pf_converged"] = bool(sol["converged"]); row["pf_iters"] = sol["iterations"]
        ms = se.simulate_measurements(net, sol, np.random.default_rng(1))
        row["measurements"] = len(ms)
        try:
            e, row["se_ms"] = t_ms(lambda: se.estimate(net, ms, bad_data=False))
            row["se_converged"] = bool(e.get("converged")); row["se_observable"] = bool(e.get("observable"))
            if e.get("observable"):
                row["se_rms_vm_err"] = float(np.sqrt(np.mean((e["vm"] - sol["vm"]) ** 2)))
        except Exception as ex:
            row["se_error"] = repr(ex)[:120]
        # N-1 on a random sample of outages (warm-started from the base solution)
        rng = np.random.default_rng(5)
        sample = rng.choice(net.nl, size=min(40, net.nl), replace=False)
        tt = []
        for j in sample:
            n2 = net.without_branches([int(j)])
            _, d = t_ms(lambda: pf.solve(n2, V0=sol["V"]))
            tt.append(d)
        row["n1_case_ms_mean"] = float(np.mean(tt)); row["n1_case_ms_p95"] = float(np.percentile(tt, 95))
        row["n1_full_screen_s_extrapolated_1core"] = net.nl * row["n1_case_ms_mean"] / 1000.0
        rows.append(row)
        print(json.dumps({k2: (round(v, 3) if isinstance(v, float) else v) for k2, v in row.items()}), flush=True)
    json.dump(dict(host=dict(cpus=os.cpu_count(), machine=platform.machine()), rows=rows), open(os.path.join(ROOT, "docs", "bench_scale.json"), "w"), indent=1)


if __name__ == "__main__":
    main()
