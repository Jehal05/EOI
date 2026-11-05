"""Weighted-least-squares (WLS) AC state estimator with bad-data processing.

Measurement kinds: vm (pu), pinj/qinj (MW/MVAr bus injection, generation minus load),
pf/qf (from-end branch flow), pt/qt (to-end branch flow).
State: bus voltage angles (reference fixed at 0) and magnitudes.
Bad data: chi-square test on the objective J, then largest-normalized-residual
(LNR) identification and removal. Normalized residuals use the exact residual
covariance diag(R - H G^-1 H^T), computed densely: fine for the sizes tested here
(documented limit; large systems need a sparse selected-inverse)."""
from __future__ import annotations
from dataclasses import dataclass
import numpy as np
import scipy.sparse as sp
from scipy.sparse.linalg import splu
from scipy.stats import chi2, norm
from .network import Net
from .powerflow import dsbus_dv

KINDS = ("vm", "pinj", "qinj", "pf", "qf", "pt", "qt")


@dataclass
class Meas:
    kind: str
    idx: int          # bus index (vm, pinj, qinj) or branch index (pf, qf, pt, qt)
    value: float
    sigma: float      # standard deviation, same units as value
    id: str = ""


def _branch_derivs(net: Net, Yf, Yt, V):
    Cf, Ct = net.conn()
    Vnorm = V / np.abs(V)
    dV = sp.diags(V)
    dVn = sp.diags(Vnorm)
    out = {}
    for tag, Y, C in (("f", Yf, Cf), ("t", Yt, Ct)):
        I = Y @ V
        Vb = C @ V
        dSa = 1j * (sp.diags(np.conj(I)) @ C @ dV - sp.diags(Vb) @ (Y @ dV).conj())
        dSm = sp.diags(Vb) @ (Y @ dVn).conj() + sp.diags(np.conj(I)) @ C @ dVn
        out[tag] = (dSm.tocsr(), dSa.tocsr(), Vb * np.conj(I))
    return out


class SEResult(dict):
    pass


def _group(ms: list[Meas]):
    """Positions and element indices of each measurement kind (computed once per round)."""
    g = {}
    for pos, m in enumerate(ms):
        g.setdefault(m.kind, ([], []))
        g[m.kind][0].append(pos)
        g[m.kind][1].append(m.idx)
    return {k: (np.array(a, dtype=int), np.array(b, dtype=int)) for k, (a, b) in g.items()}


def _model(net: Net, Ybus, Yf, Yt, groups, nmeas, x, nonref):
    """Vectorised measurement function h(x) and Jacobian H (rows in measurement order)."""
    nb = net.nb
    va = np.zeros(nb)
    va[nonref] = x[: len(nonref)]
    vm = x[len(nonref):]
    V = vm * np.exp(1j * va)
    dVm, dVa = dsbus_dv(Ybus, V)
    Sbus = V * np.conj(Ybus @ V)
    br = _branch_derivs(net, Yf, Yt, V)
    base = net.base
    h = np.zeros(nmeas)
    blocks, order = [], []
    for k, (pos, idx) in groups.items():
        if k == "vm":
            h[pos] = vm[idx]
            Ra = sp.csr_matrix((len(idx), nb))
            Rm = sp.csr_matrix((np.ones(len(idx)), (np.arange(len(idx)), idx)), shape=(len(idx), nb))
        elif k in ("pinj", "qinj"):
            part = np.real if k == "pinj" else np.imag
            h[pos] = part(Sbus[idx]) * base
            Ra, Rm = part(dVa[idx]) * base, part(dVm[idx]) * base
        else:
            dSm, dSa, S = br["f" if k in ("pf", "qf") else "t"]
            part = np.real if k in ("pf", "pt") else np.imag
            h[pos] = part(S[idx]) * base
            Ra, Rm = part(dSa[idx]) * base, part(dSm[idx]) * base
        blocks.append(sp.hstack([Ra[:, nonref], Rm], format="csr"))
        order.append(pos)
    H = sp.vstack(blocks, format="csr")
    perm = np.argsort(np.concatenate(order))  # restore original measurement order
    return h, H[perm]


def normalized_residuals(H, G, w, r, n_state, dense_limit=3000, k_candidates=60):
    """Normalized residuals rn_i = |r_i| / sqrt(sigma_i^2 - h_i G^-1 h_i^T).
    Up to `dense_limit` states the full inverse is formed (exact for every measurement). Above it, forming G^-1 is
    O(n^2) memory, so the diagonal of H G^-1 H^T is computed only for the `k_candidates` measurements with the largest
    weighted raw residual |r_i|*sqrt(w_i), one sparse solve with the LU factors each; the others get rn = 0.
    That is exact for the candidates but can miss a bad measurement whose raw residual is masked by leverage (it is the
    usual practical screening, not a proof); the caller reports which method was used."""
    sig2 = 1.0 / w
    rn = np.zeros(len(r))
    lu = splu(G)
    if n_state <= dense_limit:
        Ginv = lu.solve(np.eye(n_state))
        hgh = np.asarray(H.multiply(H @ Ginv).sum(axis=1)).ravel()
        omega = sig2 - hgh
        ok = omega > 1e-9 * sig2
        rn[ok] = np.abs(r[ok]) / np.sqrt(omega[ok])
        return rn, "dense"
    cand = np.argsort(-np.abs(r) * np.sqrt(w))[:k_candidates]
    Hc = H[cand].toarray().T                      # n_state x K
    X = lu.solve(Hc)                              # G^-1 h_i for each candidate
    hgh = np.einsum("ij,ij->j", Hc, X)
    omega = sig2[cand] - hgh
    ok = omega > 1e-9 * sig2[cand]
    rn[cand[ok]] = np.abs(r[cand[ok]]) / np.sqrt(omega[ok])
    return rn, "sparse-candidates"


def estimate(net: Net, meas: list[Meas], max_iter=25, tol=1e-7, bad_data=True, alpha=0.01,
             lnr_threshold=3.0, max_removals=15, x0=None, dense_limit=3000, k_candidates=60, local_test=True, local_min=8.0) -> SEResult:
    """Estimate the state of a single electrically connected island."""
    nb = net.nb
    refs = np.where(net.bus_type == 3)[0]
    if len(refs) != 1:
        raise ValueError("estimate() needs an island with exactly one reference bus")
    nonref = np.array([i for i in range(nb) if i != refs[0]])
    Ybus, Yf, Yt = net.ybus()
    active = list(meas)
    removed = []
    n_state = len(nonref) + nb
    x_init = np.r_[np.zeros(len(nonref)), np.ones(nb)] if x0 is None else x0.copy()
    iters_total = 0
    for _round in range(max_removals + 1):
        x = x_init.copy()
        if len(active) < n_state:
            return SEResult(observable=False, converged=False, reason=f"{len(active)} measurements < {n_state} states",
                            removed=removed)
        groups = _group(active)
        w = np.array([1.0 / (m.sigma ** 2) for m in active])
        z = np.array([m.value for m in active])
        W = sp.diags(w)
        converged = False
        try:
            for it in range(max_iter):
                h, H = _model(net, Ybus, Yf, Yt, groups, len(active), x, nonref)
                G = (H.T @ W @ H).tocsc()
                lu = splu(G)
                dx = lu.solve(H.T @ (w * (z - h)))
                x = x + dx
                iters_total += 1
                if np.max(np.abs(dx)) < tol:
                    converged = True
                    break
        except RuntimeError:
            return SEResult(observable=False, converged=False, reason="gain matrix singular: network not observable",
                            removed=removed)
        h, H = _model(net, Ybus, Yf, Yt, groups, len(active), x, nonref)
        r = z - h
        J = float(np.sum(w * r * r))
        dof = len(active) - n_state
        thr = float(chi2.ppf(1 - alpha, dof)) if dof > 0 else float("inf")
        G = (H.T @ W @ H).tocsc()
        # normalized residuals: exact dense inverse up to `dense_limit` states, otherwise sparse candidate screening
        rn, rn_method = normalized_residuals(H, G, w, r, n_state, dense_limit=dense_limit, k_candidates=k_candidates)
        suspect = int(np.argmax(rn)) if len(rn) else -1
        # Chi-square alone loses power as the system grows (one 15-sigma error adds 225 to J, but J's own spread is
        # sqrt(2*dof) ~ 140 at 10k degrees of freedom). So a single measurement whose normalized residual exceeds the
        # Bonferroni-corrected two-sided limit for this many measurements is also treated as bad data.
        # local_min: live data from the field simulator shows systematic 7-sigma model mismatches on a few points while the
        # global test passes; removing those on clean data is a false alarm, so the local gate never goes below 8 sigma.
        z_local = max(lnr_threshold, local_min, float(norm.isf(alpha / (2 * max(len(active), 1))))) if local_test else float("inf")
        if bad_data and converged and (J > thr and rn[suspect] > lnr_threshold or rn[suspect] > z_local):
            m = active.pop(suspect)
            removed.append(dict(id=m.id, kind=m.kind, value=m.value, estimate=float(h[suspect]),
                                normalized_residual=float(rn[suspect])))
            x_init = x  # warm start next round
            continue
        va = np.zeros(nb)
        va[nonref] = x[: len(nonref)]
        vm = x[len(nonref):]
        order = np.argsort(-rn)[:10]
        return SEResult(
            observable=True, converged=converged, iterations=iters_total,
            vm=vm, va_deg=np.degrees(va), J=J, dof=int(dof), chi2_threshold=thr,
            bad_data_detected=bool(J > thr), removed=removed, n_meas=len(active),
            rn_method=rn_method,
            top_residuals=[dict(id=active[i].id, kind=active[i].kind, residual=float(r[i]),
                                normalized=float(rn[i])) for i in order],
        )
    return SEResult(observable=True, converged=False, reason="bad-data removal limit reached", removed=removed)


def simulate_measurements(net: Net, sol: dict, rng: np.random.Generator, sig_v=0.004, sig_p=0.8, sig_q=0.8,
                          gross: dict | None = None) -> list[Meas]:
    """Build a full measurement set from a solved power flow plus Gaussian noise.
    gross: {measurement_id: error_in_sigmas} adds a gross error to chosen measurements."""
    Ybus, _, _ = net.ybus()
    S = sol["V"] * np.conj(Ybus @ sol["V"]) * net.base
    ms = []
    for i in range(net.nb):
        ms.append(Meas("vm", i, sol["vm"][i], sig_v, f"VM:{net.bus_name[i]}"))
        ms.append(Meas("pinj", i, S[i].real, sig_p, f"PINJ:{net.bus_name[i]}"))
        ms.append(Meas("qinj", i, S[i].imag, sig_q, f"QINJ:{net.bus_name[i]}"))
    for k in range(net.nl):
        ms.append(Meas("pf", k, sol["pf"][k], sig_p, f"PF:{net.br_id[k]}"))
        ms.append(Meas("qf", k, sol["qf"][k], sig_q, f"QF:{net.br_id[k]}"))
        ms.append(Meas("pt", k, sol["pt"][k], sig_p, f"PT:{net.br_id[k]}"))
        ms.append(Meas("qt", k, sol["qt"][k], sig_q, f"QT:{net.br_id[k]}"))
    for m in ms:
        m.value += rng.normal(0.0, m.sigma)
        if gross and m.id in gross:
            m.value += gross[m.id] * m.sigma
    return ms


_BUS_KINDS = ("vm", "pinj", "qinj")


def estimate_network(net: Net, meas: list[Meas], **kw) -> SEResult:
    """Island-aware state estimation for a network that may be split by open switches.

    Each live island is estimated independently with its own reference bus; de-energised
    islands (no generation) are reported as dead buses (vm = 0). Measurements are routed
    to the island that owns their bus/branch. Results are merged into full-system arrays."""
    if len(np.where(net.bus_type == 3)[0]) == 1 and len(net.island_ref) == 1:
        res = estimate(net, meas, **kw)
        if res.get("observable"):
            res["dead_buses"] = []
        return res
    nb = net.nb
    vm, va = np.zeros(nb), np.zeros(nb)
    dead, parts = [], []
    for k, rb in net.island_ref.items():
        buses = np.where(net.island == k)[0]
        if rb is None:
            dead += buses.tolist()
            continue
        if len(buses) < 2:  # lone energised bus: nothing to estimate beyond its voltage
            b = int(buses[0])
            vv = [m.value for m in meas if m.kind == "vm" and m.idx == b]
            vm[b] = float(vv[0]) if vv else 1.0
            continue
        sub, bmap = net.subnet(k)
        bnew = {int(o): i for i, o in enumerate(bmap)}
        bidx = np.where(net.island[net.f] == k)[0]
        brnew = {int(o): i for i, o in enumerate(bidx)}
        sm = []
        for m in meas:
            tab = bnew if m.kind in _BUS_KINDS else brnew
            if m.idx in tab:
                sm.append(Meas(m.kind, tab[m.idx], m.value, m.sigma, m.id))
        r = estimate(sub, sm, **kw)
        if not r.get("observable"):
            return SEResult(observable=False, converged=False,
                            reason=f"island {k} ({len(buses)} buses): {r.get('reason')}", removed=r.get("removed", []))
        vm[bmap] = r["vm"]
        va[bmap] = r["va_deg"]
        parts.append(r)
    if not parts:
        return SEResult(observable=False, converged=False, reason="no live island", removed=[])
    dof = sum(p["dof"] for p in parts)
    alpha = kw.get("alpha", 0.01)
    top = sorted((t for p in parts for t in p["top_residuals"]), key=lambda t: -t["normalized"])[:10]
    return SEResult(
        observable=True, converged=all(p["converged"] for p in parts),
        iterations=max(p["iterations"] for p in parts), vm=vm, va_deg=va,
        J=sum(p["J"] for p in parts), dof=int(dof),
        chi2_threshold=float(chi2.ppf(1 - alpha, dof)) if dof > 0 else float("inf"),
        bad_data_detected=any(p["bad_data_detected"] for p in parts),
        removed=[x for p in parts for x in p["removed"]], n_meas=sum(p["n_meas"] for p in parts),
        top_residuals=top, dead_buses=[net.bus_name[i] for i in dead],
    )
