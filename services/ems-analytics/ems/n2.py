"""N-2 (double branch outage) screening: DC/LODF pre-screen, then AC verification of the worst cases.

Pipeline
 1. Candidate pairs = all pairs among the K most critical branches (by DC N-1 impact and base loading)
    + all pairs of branches that share a bus (adjacent / common-corridor outages).
 2. Vectorised DC screen. Post-outage flows use the generalised line-outage-distribution factors,
        (I - Phi_MM) d = f_M ,   f'_l = f_l + Phi_lM d
    where Phi are DC PTDFs between branch end-points and f_M are the BASE-CASE AC active flows
    (hybrid: AC pre-outage flow, DC redistribution). Pairs that make (I - Phi_MM) singular island the network.
 3. AC Newton-Raphson verification of the top `verify` pairs (plus every islanding pair, capped).
The DC screen is a ranking device only; results always show the AC verdict beside the DC estimate.
"""
from __future__ import annotations
import itertools
import time
import numpy as np
import multiprocessing as mp
import scipy.sparse as sp
from scipy.sparse.linalg import splu
from .network import Net
from . import powerflow as pf, contingency as ca


class DC:
    """Sparse-LU DC model with lazily computed PTDF columns (branch-terminal transfers)."""

    def __init__(self, net: Net):
        self.net = net
        nb, nl = net.nb, net.nl
        tap = np.where(net.tap != 0.0, np.abs(net.tap), 1.0)
        self.bl = 1.0 / (net.x * tap)
        i = np.arange(nl)
        self.A = sp.csr_matrix((np.r_[np.ones(nl), -np.ones(nl)], (np.r_[i, i], np.r_[net.f, net.t])), shape=(nl, nb))
        B = (self.A.T @ sp.diags(self.bl) @ self.A).tocsc()
        keep = np.setdiff1d(np.arange(nb), [net.slack_bus])
        self.keep = keep
        self.lu = splu(B[keep][:, keep].tocsc())

    def phi(self, cols):
        """Flow response of ALL branches to a unit transfer across each branch in `cols` -> (nl, len(cols))."""
        cols = np.asarray(cols, dtype=int)
        rhs = np.zeros((self.net.nb, len(cols)))
        rhs[self.net.f[cols], np.arange(len(cols))] += 1.0
        rhs[self.net.t[cols], np.arange(len(cols))] -= 1.0
        th = np.zeros_like(rhs)
        th[self.keep] = self.lu.solve(rhs[self.keep])
        return self.bl[:, None] * (self.A @ th)


def _excess(flow, rate, rated, base_load):
    """Sum of NEW overload (percentage points above max(100, base loading))."""
    load = np.abs(flow) / np.where(rated, rate, 1.0) * 100.0
    ex = np.maximum(0.0, load - np.maximum(100.0, base_load[None, :] if flow.ndim == 2 else base_load))
    return (ex * rated).sum(axis=-1), load


def candidates(net: Net, base: dict, dc: DC, k_top: int, chunk: int = 400):
    nl = net.nl
    rated = base["rated"]
    base_load = base["loading_pct"]
    crit = np.zeros(nl)
    fM = base["pf"]
    for s in range(0, nl, chunk):  # DC N-1 criticality of each branch (impact on the others)
        cols = np.arange(s, min(nl, s + chunk))
        P = dc.phi(cols)
        diag = P[cols, np.arange(len(cols))]
        den = np.where(np.abs(1 - diag) < 1e-9, np.inf, 1 - diag)
        post = base["pf"][:, None] + P * (fM[cols] / den)[None, :]
        post[cols, np.arange(len(cols))] = 0.0
        crit[cols] = _excess(post.T, net.rate, rated, base_load)[0]
    score = crit + base_load * rated * 0.01
    top = np.argsort(-score)[:k_top]
    pairs = set(itertools.combinations(sorted(int(i) for i in top), 2))
    by_bus = {}
    for k in range(nl):
        by_bus.setdefault(int(net.f[k]), []).append(k)
        by_bus.setdefault(int(net.t[k]), []).append(k)
    for ks in by_bus.values():
        for a, b in itertools.combinations(sorted(ks), 2):
            pairs.add((a, b))
    return np.array(sorted(pairs), dtype=int).reshape(-1, 2), crit


def dc_screen(net: Net, base: dict, dc: DC, pairs: np.ndarray):
    """Vectorised DC/LODF estimate for every pair. Returns severity, islanding flag, max loading."""
    nl = net.nl
    cols = np.unique(pairs)
    P = dc.phi(cols)                      # (nl, |cols|)
    pos = {int(c): i for i, c in enumerate(cols)}
    ia = np.array([pos[int(a)] for a in pairs[:, 0]])
    ib = np.array([pos[int(b)] for b in pairs[:, 1]])
    ma, mb = pairs[:, 0], pairs[:, 1]
    Paa, Pbb = P[ma, ia], P[mb, ib]
    Pab, Pba = P[ma, ib], P[mb, ia]       # response on a to transfer across b, and vice versa
    D = (1 - Paa) * (1 - Pbb) - Pab * Pba
    isl = (np.abs(D) < 1e-7) | (np.abs(1 - Paa) < 1e-9) | (np.abs(1 - Pbb) < 1e-9)
    Dsafe = np.where(isl, 1.0, D)
    fa, fb = base["pf"][ma], base["pf"][mb]
    da = ((1 - Pbb) * fa + Pab * fb) / Dsafe
    db = (Pba * fa + (1 - Paa) * fb) / Dsafe
    flow = base["pf"][None, :] + P[:, ia].T * da[:, None] + P[:, ib].T * db[:, None]
    flow[np.arange(len(pairs)), ma] = 0.0
    flow[np.arange(len(pairs)), mb] = 0.0
    sev, load = _excess(flow, net.rate, base["rated"], base["loading_pct"])
    sev = np.where(isl, 1e5, sev)
    return sev, isl, np.where(isl, np.nan, (load * base["rated"]).max(axis=1))


def run_n2(net: Net, workers: int = 1, vmin=0.95, vmax=1.05, k_top: int = 40, verify: int = 60,
           enforce_q_limits=False, base_sol=None, dc_only: bool = False, runner=None) -> dict:
    t0 = time.perf_counter()
    if len(net.island_ref) > 1:      # open switches split the network: screen the energised main island only
        net, _ = net.subnet(int(net.island[net.slack_bus]))
        base_sol = None
    tb = time.perf_counter()
    base = base_sol or pf.solve(net, enforce_q_limits=enforce_q_limits)
    base_ms = (time.perf_counter() - tb) * 1000.0
    if not base["converged"]:
        raise RuntimeError("base case did not converge")
    dc = DC(net)
    t1 = time.perf_counter()
    pairs, _crit = candidates(net, base, dc, k_top)
    sev, isl, dcmax = dc_screen(net, base, dc, pairs)
    t2 = time.perf_counter()
    order = np.argsort(-sev)
    n_isl = int(isl.sum())
    pick = list(order[:verify])
    pick += [i for i in np.where(isl)[0] if i not in set(pick)][: max(0, verify)]  # islanding pairs always verified (capped)
    rows = [dict(outage=f"{net.br_id[a]}+{net.br_id[b]}", branches=[net.br_id[a], net.br_id[b]],
                 dc_severity=float(sev[i]), dc_max_loading_pct=None if np.isnan(dcmax[i]) else float(dcmax[i]),
                 dc_islanding=bool(isl[i])) for i, (a, b) in enumerate(pairs)]
    ac = {}
    if not dc_only and pick:
        bo, bv = ca.violations(net, base, vmin, vmax)
        args = (net, base["V"], ({b: l for b, l in bo}, {b: v for b, v in bv}), vmin, vmax, enforce_q_limits)
        jobs = [tuple(int(x) for x in pairs[i]) for i in pick]
        # Parallelism only pays when the AC batch is big: a forked worker inherits the parent's BLAS/SuperLU thread
        # state and can deadlock, so large batches use a clean 'forkserver' pool and small ones run in-process.
        est_s = len(jobs) * max(base_ms, 1.0) / 1000.0 * 2.0
        if runner is not None:
            res = runner([list(j) for j in jobs])
        elif workers > 1 and est_s > 1.5:
            with ca.mp_context().Pool(workers, initializer=ca._init, initargs=args) as pool:
                res = pool.map(ca._one, jobs, chunksize=max(1, len(jobs) // (workers * 4)))
        else:
            ca._init(*args)
            res = [ca._one(j) for j in jobs]
        ac = {r["outage"]: r for r in res}
    for r in rows:
        a = ac.get(r["outage"])
        r["ac_verified"] = a is not None
        if a:
            r.update({k: a[k] for k in ("converged", "islanded", "lost_load_mw", "overloads", "voltage_violations",
                                        "max_loading_pct", "min_vm", "severity")})
        r["rank_key"] = r["severity"] if a else -1.0
    verified = sorted((r for r in rows if r["ac_verified"]), key=lambda r: -r["rank_key"])
    rest = sorted((r for r in rows if not r["ac_verified"]), key=lambda r: -r["dc_severity"])
    wall = time.perf_counter() - t0
    return dict(
        n_branches=net.nl, n_pairs_total=net.nl * (net.nl - 1) // 2, n_candidates=len(pairs), n_ac_verified=len(ac),
        n_islanding_candidates=n_isl, dc_screen_seconds=round(t2 - t1, 4), dc_build_seconds=round(t1 - t0, 4),
        wall_seconds=round(wall, 3), workers=workers, k_top=k_top,
        n_with_violations=sum(1 for r in verified if r["severity"] > 0),
        results=verified + rest,
        method="DC/LODF pre-screen (AC base flows) + AC verification; ranking is advisory until AC verified",
    )
