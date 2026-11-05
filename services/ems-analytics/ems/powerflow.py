"""AC power flow: Newton-Raphson in polar form with sparse Jacobian.
Written from scratch from the standard power-flow equations; validated against
an independent open-source implementation in tests (see tests/test_powerflow.py)."""
from __future__ import annotations
import numpy as np
import scipy.sparse as sp
from scipy.sparse.linalg import spsolve
from .network import Net, UNRATED


def dsbus_dv(Ybus, V):
    """Partial derivatives of complex bus injections w.r.t. |V| and angle."""
    Ibus = Ybus @ V
    diagV = sp.diags(V)
    diagIbus = sp.diags(Ibus)
    diagVnorm = sp.diags(V / np.abs(V))
    dS_dVm = diagV @ (Ybus @ diagVnorm).conj() + diagIbus.conj() @ diagVnorm
    dS_dVa = 1j * diagV @ (diagIbus - Ybus @ diagV).conj()
    return dS_dVm.tocsr(), dS_dVa.tocsr()


class PFResult(dict):
    pass


def _solve_nr(net: Net, Ybus, V0, Sspec_p, Sspec_q, types, tol, max_iter, linesearch=False):
    """Core NR loop. types: 3=REF, 2=PV, 1=PQ per bus. Returns (V, converged, iters, mismatch)."""
    ref = np.where(types == 3)[0]
    pv = np.where(types == 2)[0]
    pq = np.where(types == 1)[0]
    pvpq = np.r_[pv, pq]
    V = V0.copy()
    Sspec = Sspec_p + 1j * Sspec_q

    def mismatch(V):
        mis = V * np.conj(Ybus @ V) - Sspec
        return np.r_[mis[pvpq].real, mis[pq].imag]

    F = mismatch(V)
    norm = np.linalg.norm(F, np.inf) if len(F) else 0.0
    it = 0
    while norm > tol and it < max_iter:
        it += 1
        dVm, dVa = dsbus_dv(Ybus, V)
        J11 = dVa[pvpq][:, pvpq].real
        J12 = dVm[pvpq][:, pq].real
        J21 = dVa[pq][:, pvpq].imag
        J22 = dVm[pq][:, pq].imag
        J = sp.bmat([[J11, J12], [J21, J22]], format="csc")
        dx = spsolve(J, -F)
        if not np.all(np.isfinite(dx)):
            return V, False, it, np.inf
        va0 = np.angle(V)
        vm0 = np.abs(V)
        step = 1.0
        f0 = float(F @ F)
        while True:
            va = va0.copy(); vm = vm0.copy()
            va[pvpq] += step * dx[: len(pvpq)]
            vm[pq] = np.maximum(vm[pq] + step * dx[len(pvpq):], 0.05) if linesearch else vm[pq] + step * dx[len(pvpq):]
            Vn = vm * np.exp(1j * va)
            Fn = mismatch(Vn)
            if not linesearch or float(Fn @ Fn) < f0 * (1 - 1e-4 * step) or step < 1.0 / 64:
                break
            step *= 0.5          # backtrack: halve the Newton step until the mismatch norm falls
        V, F = Vn, Fn
        norm = np.linalg.norm(F, np.inf)
    return V, bool(norm <= tol), it, float(norm)


def _solve_lm(net: Net, Ybus, V0, Sp, Sq, types, tol, max_iter):
    """Levenberg-Marquardt: minimise |F|^2 with an adaptive damping term. Slow but converges from poor starts
    where Newton's method and its line-search variant fail; used only as the last resort."""
    ref = np.where(types == 3)[0]; pv = np.where(types == 2)[0]; pq = np.where(types == 1)[0]
    pvpq = np.r_[pv, pq]
    Sspec = Sp + 1j * Sq
    V = V0.copy()
    def mis(V):
        m = V * np.conj(Ybus @ V) - Sspec
        return np.r_[m[pvpq].real, m[pq].imag]
    F = mis(V); lam = 1e-2; it = 0
    while np.linalg.norm(F, np.inf) > tol and it < max_iter:
        it += 1
        dVm, dVa = dsbus_dv(Ybus, V)
        J = sp.bmat([[dVa[pvpq][:, pvpq].real, dVm[pvpq][:, pq].real], [dVa[pq][:, pvpq].imag, dVm[pq][:, pq].imag]], format="csc")
        A = (J.T @ J).tocsc(); g = J.T @ F
        improved = False
        for _ in range(12):
            dx = spsolve(A + lam * sp.identity(A.shape[0], format="csc"), -g)
            va = np.angle(V); vm = np.abs(V)
            va[pvpq] += dx[: len(pvpq)]; vm[pq] = np.maximum(vm[pq] + dx[len(pvpq):], 0.05)
            Vn = vm * np.exp(1j * va); Fn = mis(Vn)
            if np.isfinite(Fn).all() and Fn @ Fn < F @ F:
                V, F = Vn, Fn; lam = max(lam / 5, 1e-9); improved = True; break
            lam *= 5
        if not improved:
            break
    n = float(np.linalg.norm(F, np.inf)) if len(F) else 0.0
    return V, n <= tol, it, n


def _dc_start(net: Net, V0, Sp, types):
    """Voltage angles from a DC power flow (B' theta = P): a far better start than zero angle on a large meshed system."""
    nb = net.nb
    xt = net.x * np.where(net.tap == 0, 1.0, net.tap)
    bij = 1.0 / np.where(np.abs(xt) < 1e-6, 1e-6, xt)
    A = sp.csr_matrix((np.r_[np.ones(net.nl), -np.ones(net.nl)], (np.r_[np.arange(net.nl), np.arange(net.nl)], np.r_[net.f, net.t])), shape=(net.nl, nb))
    B = (A.T @ sp.diags(bij) @ A).tocsc()
    free = np.where(types != 3)[0]
    P = Sp - (np.pi / 180.0) * 0  # shifts ignored: start value only
    th = np.zeros(nb)
    try:
        th[free] = spsolve(B[free][:, free], P[free])
    except Exception:
        return V0
    if not np.all(np.isfinite(th)):
        return V0
    th = np.clip(th, -np.pi / 2, np.pi / 2)
    return np.abs(V0) * np.exp(1j * th)


def robust_nr(net, Ybus, V0, Sp, Sq, types, tol, max_iter):
    """Strategy chain for a cold start. Returns (V, converged, iters, mismatch, strategy)."""
    a = _solve_nr(net, Ybus, V0, Sp, Sq, types, tol, max_iter)
    if a[1]:
        return (*a, "newton")
    vflat = np.abs(V0).astype(complex)
    for name, start, ls in (("newton+linesearch", V0, True), ("dc-start+linesearch", _dc_start(net, V0, Sp, types), True)):
        r = _solve_nr(net, Ybus, start, Sp, Sq, types, tol, max_iter, linesearch=ls)
        if r[1]:
            return (*r, name)
    r = _solve_lm(net, Ybus, _dc_start(net, V0, Sp, types), Sp, Sq, types, tol, max(max_iter, 60))
    return (*r, "levenberg-marquardt" if r[1] else "failed")


def solve_island(net: Net, V0=None, enforce_q_limits=False, tol=1e-9, max_iter=30) -> PFResult:
    """Solve one electrically connected island that has a reference bus."""
    nb, base = net.nb, net.base
    Ybus, Yf, Yt = net.ybus()
    types = net.bus_type.copy()
    # voltage setpoints at generator buses (the first gen on a bus defines it)
    vm0 = np.ones(nb)
    for g in range(len(net.g_bus)):
        vm0[net.g_bus[g]] = net.g_vset[g]
    if V0 is None:
        V0 = vm0 * np.exp(1j * 0.0)
    else:
        V0 = V0.copy()
        gb = np.unique(net.g_bus)
        V0[gb] = vm0[gb] * np.exp(1j * np.angle(V0[gb]))  # re-impose setpoints
    # scheduled injections (pu)
    pg = np.zeros(nb)
    qg = np.zeros(nb)
    np.add.at(pg, net.g_bus, net.g_p)
    np.add.at(qg, net.g_bus, net.g_q)
    Sp = (pg - net.pd) / base
    Sq = (qg - net.qd) / base

    qmax_bus = np.zeros(nb)
    qmin_bus = np.zeros(nb)
    np.add.at(qmax_bus, net.g_bus, net.g_qmax)
    np.add.at(qmin_bus, net.g_bus, net.g_qmin)
    q_limited = np.zeros(nb, dtype=bool)

    for _outer in range(25):
        V, ok, iters, mis, strategy = robust_nr(net, Ybus, V0, Sp, Sq, types, tol, max_iter)
        if not ok or not enforce_q_limits:
            break
        Scalc = V * np.conj(Ybus @ V)
        qgen_bus = Scalc.imag * base + net.qd  # MVAr produced by generators at each bus
        viol_hi = np.where((types == 2) & (qgen_bus > qmax_bus + 1e-6))[0]
        viol_lo = np.where((types == 2) & (qgen_bus < qmin_bus - 1e-6))[0]
        if len(viol_hi) == 0 and len(viol_lo) == 0:
            break
        for b in viol_hi:
            types[b] = 1
            Sq[b] = (qmax_bus[b] - net.qd[b]) / base
            q_limited[b] = True
        for b in viol_lo:
            types[b] = 1
            Sq[b] = (qmin_bus[b] - net.qd[b]) / base
            q_limited[b] = True
        V0 = V

    Scalc = V * np.conj(Ybus @ V)
    Sf = V[net.f] * np.conj(Yf @ V)
    St = V[net.t] * np.conj(Yt @ V)
    # generator dispatch results
    gp = net.g_p.copy()
    gq = np.zeros(len(net.g_bus))
    qgen_bus = Scalc.imag * base + net.qd
    for b in np.unique(net.g_bus):
        idx = np.where(net.g_bus == b)[0]
        rng = net.g_qmax[idx] - net.g_qmin[idx]
        w = rng / rng.sum() if rng.sum() > 0 else np.ones(len(idx)) / len(idx)
        gq[idx] = qgen_bus[b] * w
    for rb in np.where(types == 3)[0]:
        idx = np.where(net.g_bus == rb)[0]
        if len(idx):
            pgen_bus = Scalc[rb].real * base + net.pd[rb]
            others = gp[idx].sum() - gp[idx[0]]
            gp[idx[0]] = pgen_bus - others
    smax = np.maximum(np.abs(Sf), np.abs(St)) * base
    rated = (net.rate > 0) & (net.rate < UNRATED)
    loading = np.where(rated, smax / np.where(rated, net.rate, 1.0) * 100.0, 0.0)
    return PFResult(
        converged=ok, iterations=iters, max_mismatch=mis, V=V,
        vm=np.abs(V), va_deg=np.degrees(np.angle(V)),
        pf=Sf.real * base, qf=Sf.imag * base, pt=St.real * base, qt=St.imag * base,
        mva_max=smax, loading_pct=loading, rated=rated,
        gen_p=gp, gen_q=gq, losses_mw=float((Sf + St).real.sum() * base),
        q_limited_buses=np.where(q_limited)[0].tolist(), strategy=strategy,
    )


def solve(net: Net, V0=None, enforce_q_limits=False, tol=1e-9, max_iter=30) -> dict:
    """Solve every live island of the network and assemble full-system results."""
    nb, nl, ng = net.nb, net.nl, len(net.g_bus)
    out = dict(
        converged=True, vm=np.zeros(nb), va_deg=np.zeros(nb), V=np.zeros(nb, dtype=complex),
        pf=np.zeros(nl), qf=np.zeros(nl), pt=np.zeros(nl), qt=np.zeros(nl), mva_max=np.zeros(nl),
        loading_pct=np.zeros(nl), rated=np.zeros(nl, dtype=bool),
        gen_p=net.g_p.copy(), gen_q=np.zeros(ng), losses_mw=0.0, iterations=0,
        dead_buses=[], lost_load_mw=0.0, max_mismatch=0.0, q_limited_buses=[],
        islands_solved=0, nonconverged_islands=[], strategy="newton",
    )
    for k, rb in net.island_ref.items():
        buses = np.where(net.island == k)[0]
        if rb is None:
            out["dead_buses"] += buses.tolist()
            out["lost_load_mw"] += float(net.pd[buses].sum())
            continue
        sub, bmap = net.subnet(k)
        V0s = V0[bmap] if V0 is not None else None
        r = solve_island(sub, V0s, enforce_q_limits, tol, max_iter)
        out["islands_solved"] += 1
        if r["strategy"] != "newton": out["strategy"] = r["strategy"]
        out["iterations"] = max(out["iterations"], r["iterations"])
        out["max_mismatch"] = max(out["max_mismatch"], r["max_mismatch"])
        if not r["converged"]:
            out["converged"] = False
            out["nonconverged_islands"].append(int(k))
        out["vm"][bmap] = r["vm"]
        out["va_deg"][bmap] = r["va_deg"]
        out["V"][bmap] = r["V"]
        out["q_limited_buses"] += bmap[r["q_limited_buses"]].tolist()
        bidx = np.where(net.island[net.f] == k)[0]
        for key in ("pf", "qf", "pt", "qt", "mva_max", "loading_pct", "rated"):
            out[key][bidx] = r[key]
        gidx = np.where(net.island[net.g_bus] == k)[0] if ng else np.array([], dtype=int)
        out["gen_p"][gidx] = r["gen_p"]
        out["gen_q"][gidx] = r["gen_q"]
        out["losses_mw"] += r["losses_mw"]
    return out
