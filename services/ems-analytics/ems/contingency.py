"""N-1 AC contingency analysis (branch outages) with parallel execution.
Only violations that are NEW relative to the base case are attributed to an outage."""
from __future__ import annotations
import time
import numpy as np
import multiprocessing as mp
from .network import Net
from . import powerflow as pf


def mp_context():
    """Clean worker processes: forkserver where it exists (Linux, macOS), spawn on Windows."""
    return mp.get_context("forkserver" if "forkserver" in mp.get_all_start_methods() else "spawn")

_G = {}


def _init(net, V0, base_viol, vmin, vmax, enforce_q):
    _G.update(net=net, V0=V0, base=base_viol, vmin=vmin, vmax=vmax, q=enforce_q)


def violations(net: Net, sol: dict, vmin: float, vmax: float):
    over = [(net.br_id[k], float(sol["loading_pct"][k])) for k in range(net.nl) if sol["rated"][k] and sol["loading_pct"][k] > 100.0]
    live = np.ones(net.nb, dtype=bool)
    live[sol["dead_buses"]] = False
    vv = [(net.bus_name[i], float(sol["vm"][i])) for i in range(net.nb) if live[i] and (sol["vm"][i] < vmin or sol["vm"][i] > vmax)]
    return over, vv


def _one(k):
    """Evaluate one outage; k is a branch index (N-1) or a tuple/list of indices (N-k)."""
    ks = [int(k)] if np.isscalar(k) else [int(i) for i in k]
    net, V0 = _G["net"], _G["V0"]
    t0 = time.perf_counter()
    n2 = net.without_branches(ks)
    sol = pf.solve(n2, V0=V0, enforce_q_limits=_G["q"])
    ms = (time.perf_counter() - t0) * 1000.0
    out = dict(outage="+".join(net.br_id[i] for i in ks), branches=[net.br_id[i] for i in ks],
               from_bus=net.bus_name[net.f[ks[0]]], to_bus=net.bus_name[net.t[ks[0]]],
               converged=bool(sol["converged"]), islanded=len(n2.island_ref) > len(net.island_ref),
               lost_load_mw=float(sol["lost_load_mw"]), solve_ms=ms)
    if not sol["converged"]:
        out.update(overloads=[], voltage_violations=[], max_loading_pct=None, min_vm=None, max_vm=None, severity=1e6)
        return out
    over, vv = violations(n2, sol, _G["vmin"], _G["vmax"])
    base_o, base_v = _G["base"]
    over = [(b, l) for b, l in over if b not in base_o or l > base_o[b] + 1.0]  # exclude pre-existing, tolerance 1%
    vv = [(b, v) for b, v in vv if b not in base_v]
    live = np.ones(n2.nb, dtype=bool)
    live[sol["dead_buses"]] = False
    vm = sol["vm"][live]
    sev = (sum(l - 100.0 for _, l in over) + 25.0 * len(vv) + 2.0 * sol["lost_load_mw"]
           + 100.0 * len(sol["nonconverged_islands"]))
    out.update(
        overloads=[dict(branch=b, loading_pct=round(l, 1)) for b, l in sorted(over, key=lambda t: -t[1])],
        voltage_violations=[dict(bus=b, vm=round(v, 4)) for b, v in vv],
        max_loading_pct=float(np.max(sol["loading_pct"])) if net.nl > 1 else 0.0,
        min_vm=float(vm.min()) if len(vm) else None, max_vm=float(vm.max()) if len(vm) else None,
        severity=float(sev))
    return out


def run_n1(net: Net, workers: int = 1, vmin=0.95, vmax=1.05, enforce_q_limits=False, base_sol=None, runner=None) -> dict:
    t0 = time.perf_counter()
    base = base_sol or pf.solve(net, enforce_q_limits=enforce_q_limits)
    if not base["converged"]:
        raise RuntimeError("base case did not converge")
    bo, bv = violations(net, base, vmin, vmax)
    base_viol = ({b: l for b, l in bo}, {b: v for b, v in bv})
    args = (net, base["V"], base_viol, vmin, vmax, enforce_q_limits)
    idx = list(range(net.nl))
    if runner is not None:      # distributed evaluation: runner(list of branch-index lists) -> results in the same order
        res = runner([[k] for k in idx])
    elif workers > 1:
        ctx = mp_context()      # a forked worker inherits the parent threads' BLAS state and can deadlock
        with ctx.Pool(workers, initializer=_init, initargs=args) as pool:
            res = pool.map(_one, idx, chunksize=max(1, len(idx) // (workers * 8)))
    else:
        _init(*args)
        res = [_one(k) for k in idx]
    res.sort(key=lambda r: -r["severity"])
    wall = time.perf_counter() - t0
    return dict(
        cases=len(res), workers=workers, wall_seconds=wall, mean_case_ms=float(np.mean([r["solve_ms"] for r in res])),
        base=dict(losses_mw=base["losses_mw"], violations=dict(overloads=len(bo), voltage=len(bv))),
        n_nonconverged=sum(not r["converged"] for r in res),
        n_with_violations=sum(1 for r in res if r["severity"] > 0), results=res,
    )
