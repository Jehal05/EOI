"""Transmission corridor capability monitor (TCM): transfer capability and margin across inter-area corridors. ADVISORY.

Definitions (documented, conservative)
 * Areas: model-driven spectral partition of the live island (Fiedler bisection on 1/x weights, repeated); a corridor is
   the set of branches joining two areas, evaluated in each direction. Explicit source/sink bus sets are also accepted.
 * Transfer: a source area raises generation, a sink area lowers it, each in proportion to its headroom / ramp-down
   (to Pmax / Pmin). `t` MW of transfer moves t MW from source units to sink units.
 * DC step: closed form. Branch l after outage k carries F0[l] + LODF[l,k] F0[k] + t (s[l] + LODF[l,k] s[k]),
   s = PTDF d. The smallest t at which any monitored branch reaches its limit (base: rating; N-1: n1_limit x rating) is
   the DC transfer limit; the generation headroom is a further cap. Constraints already violated at t=0 (pre-existing)
   are excluded and counted (that is a security-enhancement matter, not a transfer limit); islanding outages are skipped.
 * AC step: the DC limit is verified on the AC model (base thermal + voltage band, plus AC N-1 on the most critical
   outages) and reduced by bisection until the AC checks pass. The AC value is the reported incremental capability.
 * ATC = incremental capability - TRM, TRM = trm_pct x total transfer capability (base interface flow + incremental).
Limits: thermal and voltage-band limits only (no stability / voltage-collapse limits); AC N-1 covers the top
`ac_n1_top` DC-critical outages, not every outage; MW (not MVA) is used in the DC step; ratings are model data.
"""
from __future__ import annotations
import time
import numpy as np
import scipy.sparse as sp
from scipy.sparse.csgraph import connected_components
from .network import Net, UNRATED
from . import powerflow as pf
from .opf import Grid


def partition(net: Net, parts: int = 3):
    """Split the live island containing the slack bus into `parts` areas. Returns (area index per bus, -1 for dead buses)."""
    main = np.where(net.island == net.island[net.slack_bus])[0]
    area = np.full(net.nb, -1, dtype=int)
    area[main] = 0
    w = 1.0 / np.abs(net.x)
    for _ in range(max(0, parts - 1)):
        sizes = np.bincount(area[area >= 0])
        # split the largest area that can still be split
        for a in np.argsort(-sizes):
            idx = np.where(area == a)[0]
            if len(idx) < 4:
                continue
            pos = {b: i for i, b in enumerate(idx)}
            m = [(pos[f], pos[t], wk) for f, t, wk in zip(net.f, net.t, w) if f in pos and t in pos]
            if not m:
                continue
            i, j, ww = map(np.array, zip(*m))
            n = len(idx)
            W = sp.coo_matrix((np.r_[ww, ww], (np.r_[i, j], np.r_[j, i])), shape=(n, n)).tocsr()
            nc, lab = connected_components(W, directed=False)
            if nc > 1:
                side = lab == np.bincount(lab).argmax()
            else:
                L = np.diag(np.asarray(W.sum(axis=1)).ravel()) - W.toarray()
                vec = np.linalg.eigh(L)[1][:, 1]
                side = vec > 0
                if min(side.sum(), (~side).sum()) < 0.1 * n:
                    side = vec > np.median(vec)
            if side.all() or (~side).all():
                continue
            area[idx[~side]] = area.max() + 1
            break
    return area


def corridors(net: Net, area: np.ndarray):
    out = []
    na = int(area.max()) + 1
    for a in range(na):
        for b in range(na):
            if a == b:
                continue
            ks = [k for k in range(net.nl) if area[net.f[k]] == a and area[net.t[k]] == b]
            ks_r = [k for k in range(net.nl) if area[net.f[k]] == b and area[net.t[k]] == a]
            if ks or ks_r:
                out.append((a, b, ks, ks_r))
    return out


def interface_flow(net: Net, pflow, ks_fwd, ks_rev):
    return float(sum(pflow[k] for k in ks_fwd) - sum(pflow[k] for k in ks_rev))


def transfer_vector(net: Net, src_buses, snk_buses, g_p=None):
    """Per-generator MW change per MW of transfer and the cap on transfer from generation limits."""
    gp = net.g_p if g_p is None else g_p
    src = np.isin(net.g_bus, src_buses)
    snk = np.isin(net.g_bus, snk_buses)
    up = np.where(src, np.maximum(net.g_pmax - gp, 0.0), 0.0)
    dn = np.where(snk, np.maximum(gp - np.minimum(net.g_pmin, gp), 0.0), 0.0)
    cap = float(min(up.sum(), dn.sum()))
    dg = np.zeros(len(gp))
    if cap > 1e-9:
        dg = up / up.sum() - dn / dn.sum()
    return dg, cap, float(up.sum()), float(dn.sum())


def lodf_all(grid: Grid):
    den = 1.0 - np.diag(grid.phi)
    bridge = np.abs(den) < 1e-9
    L = grid.phi / np.where(bridge, 1.0, den)[None, :]
    return L, bridge


def dc_limits(grid: Grid, F0, d_inj, rate, rated, n1_limit=1.0, skip_outage=None):
    """Closed-form DC transfer limits. Returns dict(base=(t, l), n1=(t, l, k), n_preexisting, order) with t=inf if unconstrained."""
    L, bridge = lodf_all(grid)
    s = grid.ptdf @ d_inj
    nl = len(F0)
    lim_b = np.where(rated, rate, np.inf)
    lim_n = lim_b * n1_limit

    def tmax(a, b, lim):
        """smallest t >= 0 with |a + b t| = lim (inf if never); entries already beyond limit at t=0 -> nan (pre-existing)."""
        with np.errstate(divide="ignore", invalid="ignore"):
            t_pos = (lim - a) / b
            t_neg = (-lim - a) / b
            t = np.where(b > 0, t_pos, np.where(b < 0, t_neg, np.inf))
        t = np.where(np.abs(a) > lim + 1e-9, np.nan, t)
        t = np.where(np.isfinite(lim), t, np.inf)
        return np.where(t < 0, np.inf, t)

    tb = tmax(F0, s, lim_b)
    pre = int(np.isnan(tb).sum())
    tb = np.where(np.isnan(tb), np.inf, tb)
    lb = int(np.argmin(tb))
    A = F0[None, :] + L.T * F0[:, None]          # A[k, l]
    B = s[None, :] + L.T * s[:, None]            # B[k, l]
    T = tmax(A, B, np.broadcast_to(lim_n[None, :], A.shape))
    mask = bridge.copy()
    if skip_outage is not None:
        mask |= skip_outage
    T[mask, :] = np.inf
    T[np.arange(nl), np.arange(nl)] = np.inf     # the outaged branch itself carries nothing
    pre_n = int(np.isnan(T).sum())
    T = np.where(np.isnan(T), np.inf, T)
    flat = int(np.argmin(T))
    k, l = divmod(flat, nl)
    return dict(base=(float(tb[lb]), lb), n1=(float(T[k, l]), l, k), n_preexisting=pre + pre_n, T=T, tb=tb, s=s)


def _viol(net: Net, sol, vmin, vmax, lim, exclude_b=(), exclude_v=()):
    rated = (net.rate > 0) & (net.rate < UNRATED)
    over = [k for k in range(net.nl) if rated[k] and sol["mva_max"][k] > lim * net.rate[k] + 1e-6 and k not in exclude_b]
    live = np.ones(net.nb, dtype=bool)
    live[sol["dead_buses"]] = False
    vv = [i for i in range(net.nb) if live[i] and (sol["vm"][i] < vmin - 1e-4 or sol["vm"][i] > vmax + 1e-4) and i not in exclude_v]
    return over, vv


def _with_transfer(net, dg, t):
    n2 = net.copy()
    n2.g_p = net.g_p + t * dg
    return n2


def analyze(net: Net, *, parts: int = 3, trm_pct: float = 5.0, n1_limit: float = 1.0, vmin=0.95, vmax=1.05, ac_n1_top: int = 12,
            source_buses=None, sink_buses=None, grid: Grid | None = None, base_sol=None, bisect_iters: int = 8) -> dict:
    t0 = time.perf_counter()
    base = base_sol if base_sol is not None else pf.solve(net)
    if not base["converged"]:
        return dict(status="base_not_converged", corridors=[])
    grid = grid or Grid(net)
    F0 = np.asarray(base["pf"], dtype=float)
    rated = (net.rate > 0) & (net.rate < UNRATED)
    area = partition(net, parts)
    if source_buses is not None and sink_buses is not None:
        a_s, a_k = np.asarray(source_buses, int), np.asarray(sink_buses, int)
        ks_f = [k for k in range(net.nl) if net.f[k] in set(a_s) and net.t[k] in set(a_k)]
        ks_r = [k for k in range(net.nl) if net.f[k] in set(a_k) and net.t[k] in set(a_s)]
        cors = [(0, 1, ks_f, ks_r)]
        areas = {0: a_s, 1: a_k}
    else:
        cors = corridors(net, area)
        areas = {a: np.where(area == a)[0] for a in range(int(area.max()) + 1)}
    over0, vv0 = _viol(net, base, vmin, vmax, 1.0)
    pre_n1: dict = {}                      # outage -> (overloaded branches, voltage-violating buses) already present at t = 0

    def pre_of(k):
        if k not in pre_n1:
            n3 = net.without_branches([k])
            s3 = pf.solve(n3, V0=base.get("V"))
            pre_n1[k] = _viol(n3, s3, vmin, vmax, n1_limit) if s3["converged"] else ([], [])
        return pre_n1[k]
    out = []
    for a, b, kf, kr in cors:
        dg, cap, up, dn = transfer_vector(net, areas[a], areas[b])
        flow0 = interface_flow(net, F0, kf, kr)
        row = dict(from_area=int(a), to_area=int(b), from_buses=int(len(areas[a])), to_buses=int(len(areas[b])),
                   branches=[net.br_id[k] for k in kf + kr], n_branches=len(kf) + len(kr), base_flow_mw=round(flow0, 1),
                   interface_rating_mva=round(float(sum(net.rate[k] for k in kf + kr if rated[k])), 0),
                   gen_headroom_mw=round(up, 1), gen_rampdown_mw=round(dn, 1))
        if cap <= 1e-6:
            row.update(status="no_generation_capacity", dc_mw=0.0, ac_mw=0.0, atc_mw=0.0, ttc_mw=round(abs(flow0), 1), trm_mw=0.0, limit=dict(type="generation", element=None, outage=None))
            out.append(row)
            continue
        d_inj = np.zeros(net.nb)
        np.add.at(d_inj, net.g_bus, dg)
        lim = dc_limits(grid, F0, d_inj, net.rate, rated, n1_limit)
        cands = [("generation", cap, None, None), ("thermal_base", lim["base"][0], net.br_id[lim["base"][1]], None),
                 ("thermal_n1", lim["n1"][0], net.br_id[lim["n1"][1]], net.br_id[lim["n1"][2]])]
        typ, t_dc, el, ou = min(cands, key=lambda c: c[1])
        t_dc = float(min(t_dc, cap))
        # critical outages by smallest DC limit, for the AC N-1 check
        worst = np.argsort(lim["T"].min(axis=1))[:ac_n1_top]
        worst = [int(k) for k in worst if np.isfinite(lim["T"][k].min())]

        def ac_ok(t):
            n2 = _with_transfer(net, dg, t)
            sol = pf.solve(n2, V0=base.get("V"))
            if not sol["converged"]:
                return False, "nonconverged", None, None
            ob, vb = _viol(n2, sol, vmin, vmax, 1.0, over0, vv0)
            if ob:
                return False, "thermal_base", net.br_id[ob[0]], None
            if vb:
                return False, "voltage", net.bus_name[vb[0]], None
            for k in worst:
                n3 = n2.without_branches([k])
                s3 = pf.solve(n3, V0=sol.get("V"))
                if not s3["converged"]:
                    continue
                if len(n3.island_ref) > len(n2.island_ref):
                    continue
                po, pv = pre_of(k)
                o3, v3 = _viol(n3, s3, vmin, vmax, n1_limit, po, pv)
                if o3:
                    return False, "thermal_n1", net.br_id[o3[0]], net.br_id[k]
                if v3:
                    return False, "voltage_n1", net.bus_name[v3[0]], net.br_id[k]
            return True, None, None, None

        ok, why, el2, ou2 = ac_ok(t_dc)
        t_ac, nsolve = t_dc, 1
        if not ok:
            lo, hi = 0.0, t_dc
            typ, el, ou = why, el2, ou2
            for _ in range(bisect_iters):
                mid = 0.5 * (lo + hi)
                ok, w2, e2, o2 = ac_ok(mid)
                nsolve += 1
                if ok:
                    lo = mid
                else:
                    hi, typ, el, ou = mid, w2, e2, o2
            t_ac = lo
        ttc = abs(flow0) + t_ac
        trm = trm_pct / 100.0 * ttc
        atc = max(0.0, t_ac - trm)
        row.update(status="ok", dc_mw=round(t_dc, 1), ac_mw=round(t_ac, 1), ttc_mw=round(ttc, 1), trm_mw=round(trm, 1), atc_mw=round(atc, 1),
                   utilization_pct=round(100.0 * abs(flow0) / ttc, 1) if ttc > 0 else None,
                   limit=dict(type=typ, element=el, outage=ou), preexisting_excluded=lim["n_preexisting"], ac_checks=nsolve,
                   level="low" if atc < 0.1 * ttc else ("watch" if atc < 0.25 * ttc else "ok"))
        out.append(row)
    out.sort(key=lambda r: r.get("atc_mw", 0.0))
    return dict(status="ok", areas=[dict(area=int(a), buses=int(len(v)), gens=int(np.isin(net.g_bus, v).sum())) for a, v in areas.items()],
                corridors=out, trm_pct=trm_pct, n1_limit=n1_limit, wall_seconds=round(time.perf_counter() - t0, 2),
                notes=["Advisory. Thermal and voltage-band limits only; no stability or voltage-collapse limit.",
                       f"AC N-1 checked on the {ac_n1_top} most critical outages per corridor, not all outages.",
                       "Pre-existing violations are excluded from limits and counted (see security enhancement)."])
