"""Optimal power flow (economic dispatch with network constraints) and preventive security-constrained OPF.

Formulation (documented, deliberately conservative)
 * DC network model (PTDF from a sparse-LU of the reduced B matrix); piecewise-linear convex generator cost
   (secant segments of the quadratic c2 P^2 + c1 P + c0 from the CIM model) solved as an LP with HiGHS.
 * Load-shed variables at a very high price keep the problem feasible and make any unserved energy visible.
 * Losses are handled by iteration: the AC losses of the dispatch are fed back as extra demand.
 * Nodal prices (LMP) come from the LP duals: LMP_i = lambda + sum_l mu_l * PTDF_li.
 * SCOPF = preventive N-1 security by constraint generation: post-outage flows use LODF, violated
   (monitored line, outage) pairs are added as linear cuts until none remain. Islanding outages are not secured
   (they are reported); corrective actions and reactive/voltage constraints are NOT modelled.
 * Every result is verified with the full AC Newton-Raphson power flow (and AC N-1 for SCOPF).
It is ADVISORY: nothing here is sent to any control path.
"""
from __future__ import annotations
import time
import numpy as np
import scipy.sparse as sp
from scipy.optimize import linprog
from scipy.sparse.linalg import splu
from .network import Net, UNRATED
from . import powerflow as pf

VOLL = 5000.0        # $/MWh price of unserved energy
DEFAULT_C1 = 30.0    # $/MWh used for a generator with no cost data (reported in `defaults_used`)


def gen_costs(net: Net):
    n = len(net.g_bus)
    c2 = np.zeros(n) if net.g_c2 is None else np.nan_to_num(net.g_c2, nan=0.0)
    c1 = np.full(n, DEFAULT_C1) if net.g_c1 is None else np.where(np.isnan(net.g_c1), DEFAULT_C1, net.g_c1)
    c0 = np.zeros(n) if net.g_c0 is None else np.nan_to_num(net.g_c0, nan=0.0)
    missing = 0 if net.g_c1 is None else int(np.isnan(net.g_c1).sum())
    return c2, c1, c0, (n if net.g_c1 is None else missing)


class Grid:
    """DC grid model: dense PTDF (nl x nb, slack column = 0) and branch-terminal transfer matrix."""

    def __init__(self, net: Net):
        self.net = net
        nb, nl = net.nb, net.nl
        tap = np.where(net.tap != 0.0, np.abs(net.tap), 1.0)
        self.bl = 1.0 / (net.x * tap)
        i = np.arange(nl)
        A = sp.csr_matrix((np.r_[np.ones(nl), -np.ones(nl)], (np.r_[i, i], np.r_[net.f, net.t])), shape=(nl, nb))
        self.A = A
        B = (A.T @ sp.diags(self.bl) @ A).tocsc()
        keep = np.setdiff1d(np.arange(nb), [net.slack_bus])
        lu = splu(B[keep][:, keep].tocsc())
        X = lu.solve(np.eye(len(keep)))
        ptdf = np.zeros((nl, nb))
        ptdf[:, keep] = (sp.diags(self.bl) @ A[:, keep]) @ X
        self.ptdf = ptdf
        self.phi = ptdf @ A.T.toarray()      # (nl x nl): response of l to a unit transfer across k's terminals

    def flows(self, inj):
        return self.ptdf @ inj

    def lodf(self, k):
        den = 1.0 - self.phi[k, k]
        return None if abs(den) < 1e-9 else self.phi[:, k] / den


def _pwl_slopes(c2, c1, pmin, pmax, S):
    w = (pmax - pmin) / S
    pts = pmin[:, None] + w[:, None] * np.arange(S + 1)[None, :]
    cost = c2[:, None] * pts ** 2 + c1[:, None] * pts
    slope = (cost[:, 1:] - cost[:, :-1]) / np.where(w[:, None] > 0, w[:, None], 1.0)
    return w, slope


def solve_dc_opf(net: Net, grid: Grid | None = None, *, loss_mw: float = 0.0, cuts=None, segments: int = 16,
                 line_scale: float = 1.0, load_scale: float = 1.0, active=None) -> dict:
    """One LP solve. `cuts` is a list of (row_vector_over_bus_injections, limit_mw, kind) post-contingency constraints."""
    g = grid or Grid(net)
    nb, ng = net.nb, len(net.g_bus)
    c2, c1, c0, missing = gen_costs(net)
    pmin, pmax = net.g_pmin.copy(), np.maximum(net.g_pmax, net.g_pmin)
    S = segments
    w, slope = _pwl_slopes(c2, c1, pmin, pmax, S)
    nvar_g = ng * S
    pd = net.pd * load_scale
    load_bus = np.where(pd > 0)[0]
    nu = len(load_bus)
    nvar = nvar_g + nu
    cvec = np.r_[slope.ravel(), np.full(nu, VOLL)]
    bounds = [(0.0, float(w[gi])) for gi in range(ng) for _ in range(S)] + [(0.0, float(pd[b])) for b in load_bus]
    # injection = Gmap @ (pmin + delta) + Umap @ u - pd
    M = np.zeros((nb, nvar))
    for gi in range(ng):
        M[net.g_bus[gi], gi * S:(gi + 1) * S] = 1.0
    for j, b in enumerate(load_bus):
        M[b, nvar_g + j] = 1.0
    inj0 = np.zeros(nb)
    np.add.at(inj0, net.g_bus, pmin)
    inj0 -= pd
    Aeq = np.ones((1, nvar))
    Aeq[0, :] = 1.0
    beq = np.array([pd.sum() + loss_mw - pmin.sum()])
    rated = (net.rate > 0) & (net.rate < UNRATED)
    lim = net.rate * line_scale
    rows_mask = rated if active is None else (rated & active)       # `active` = lazily generated line limits
    F = g.ptdf[rows_mask] @ M
    off = g.ptdf[rows_mask] @ inj0
    rows = [F, -F]
    rhs = [lim[rows_mask] - off, lim[rows_mask] + off]
    cut_meta = []
    if cuts:
        for vec, limit, meta in cuts:
            f = vec @ M
            o = vec @ inj0
            rows += [f[None, :], -f[None, :]]
            rhs += [np.array([limit - o]), np.array([limit + o])]
            cut_meta.append(meta)
    Aub = np.vstack(rows)
    bub = np.concatenate(rhs)
    r = linprog(cvec, A_ub=Aub, b_ub=bub, A_eq=Aeq, b_eq=beq, bounds=bounds, method="highs")
    if r.status != 0:
        return dict(ok=False, status=int(r.status), message=r.message)
    x = r.x
    pg = pmin + x[:nvar_g].reshape(ng, S).sum(axis=1)
    shed = np.zeros(nb)
    shed[load_bus] = x[nvar_g:]
    inj = np.zeros(nb)
    np.add.at(inj, net.g_bus, pg)
    inj += shed - pd
    flow = g.flows(inj)
    cost_gen = float((c2 * pg ** 2 + c1 * pg + c0).sum())
    lam = float(r.eqlin.marginals[0])
    nr = int(rows_mask.sum())
    mu_up = np.zeros(net.nl)
    mu_lo = np.zeros(net.nl)
    mu_up[rows_mask] = r.ineqlin.marginals[:nr]
    mu_lo[rows_mask] = r.ineqlin.marginals[nr:2 * nr]
    lmp = lam + g.ptdf.T @ (mu_up - mu_lo)
    cut_mu = []
    if cuts:
        cm = r.ineqlin.marginals[2 * nr:]
        cut_mu = [float(cm[2 * i] + cm[2 * i + 1]) for i in range(len(cuts))]
        lmp = lmp + sum((cm[2 * i] - cm[2 * i + 1]) * (cuts[i][0]) for i in range(len(cuts)))
    return dict(ok=True, pg=pg, shed=shed, inj=inj, flow=flow, cost=cost_gen, lp_objective=float(r.fun) + float((c2 * pmin ** 2 + c1 * pmin).sum()) + float(c0.sum()),
                lam=lam, lmp=lmp, mu_up=mu_up, mu_lo=mu_lo, rated=rated, unserved_mw=float(shed.sum()), cut_mu=cut_mu,
                n_cuts=len(cuts or []), defaults_used=missing, cut_meta=cut_meta)


def solve_lazy(net: Net, grid: Grid, *, line_scale: float = 1.0, max_rounds: int = 40, add_per_round: int = 150, state: dict | None = None, **kw) -> dict:
    """Constraint generation for the normal-state line limits: solve with a small active set, add violated lines, repeat.
    `state['active']` persists across calls (SCOPF) so later solves start from the lines that mattered before."""
    rated = (net.rate > 0) & (net.rate < UNRATED)
    st = state if state is not None else {}
    active = st.setdefault("active", np.zeros(net.nl, dtype=bool))
    lim = net.rate * line_scale
    res = None
    for rnd in range(max_rounds):
        res = solve_dc_opf(net, grid, line_scale=line_scale, active=active, **kw)
        if not res["ok"]:
            return res
        ex = np.abs(res["flow"]) - lim
        ex[~rated | active] = -1.0
        bad = np.where(ex > 1e-6)[0]
        if len(bad) == 0:
            res["lazy_rounds"] = rnd + 1
            res["active_lines"] = int(active.sum())
            return res
        bad = bad[np.argsort(-ex[bad])][:add_per_round]
        active[bad] = True
    res["lazy_rounds"] = max_rounds
    res["active_lines"] = int(active.sum())
    return res


def ac_check(net: Net, pg, vmin=0.95, vmax=1.05, enforce_q=True) -> dict:
    n2 = net.copy()
    n2.g_p = np.array(pg, dtype=float)
    sol = pf.solve(n2, enforce_q_limits=enforce_q)
    out = dict(converged=bool(sol["converged"]), losses_mw=float(sol["losses_mw"]))
    if not sol["converged"]:
        return out
    over = [(net.br_id[k], float(sol["loading_pct"][k])) for k in range(net.nl) if sol["rated"][k] and sol["loading_pct"][k] > 100.0]
    live = np.ones(net.nb, dtype=bool)
    live[sol["dead_buses"]] = False
    vv = [(net.bus_name[i], float(sol["vm"][i])) for i in range(net.nb) if live[i] and (sol["vm"][i] < vmin or sol["vm"][i] > vmax)]
    slack_g = int(np.where(net.g_bus == net.slack_bus)[0][0]) if (net.g_bus == net.slack_bus).any() else None
    out.update(overloads=[dict(branch=b, loading_pct=round(l, 1)) for b, l in sorted(over, key=lambda t: -t[1])][:20], n_overloads=len(over),
               voltage_violations=[dict(bus=b, vm=round(v, 4)) for b, v in vv][:20], n_voltage_violations=len(vv),
               min_vm=float(sol["vm"][live].min()), max_vm=float(sol["vm"][live].max()),
               slack_gen_mw=None if slack_g is None else float(sol["gen_p"][slack_g]),
               slack_gen_pmax=None if slack_g is None else float(net.g_pmax[slack_g]),
               q_limited_buses=len(sol["q_limited_buses"]))
    out["_sol"] = sol
    return out


def run_opf(net: Net, *, loss_iterations: int = 2, verify: bool = True, vmin=0.95, vmax=1.05, segments: int = 16,
            line_scale: float = 1.0) -> dict:
    t0 = time.perf_counter()
    if len(net.island_ref) > 1:
        net, _ = net.subnet(int(net.island[net.slack_bus]))
    grid = Grid(net)
    loss = 0.0
    res = None
    ac = None
    hist = []
    for it in range(max(1, loss_iterations + 1)):
        res = solve_lazy(net, grid, loss_mw=loss, segments=segments, line_scale=line_scale)
        if not res["ok"]:
            return dict(ok=False, status=res["status"], message=str(res["message"]))
        if not verify and it == 0:
            break
        ac = ac_check(net, res["pg"], vmin, vmax)
        hist.append(dict(iteration=it, loss_assumed_mw=round(loss, 3), ac_losses_mw=ac.get("losses_mw")))
        if not ac["converged"] or it == loss_iterations or abs(ac["losses_mw"] - loss) < 0.05:
            break
        loss = ac["losses_mw"]
    c2, c1, c0, _ = gen_costs(net)
    ref_cost = float((c2 * net.g_p ** 2 + c1 * net.g_p + c0).sum())
    ac_out = None if ac is None else {k: v for k, v in ac.items() if k != "_sol"}
    return _pack(net, res, ac_out, ref_cost, hist, time.perf_counter() - t0, kind="opf")


def _pack(net, res, ac, ref_cost, hist, wall, kind, extra=None):
    flows = res["flow"]
    lim = net.rate
    cong = [dict(branch=net.br_id[k], flow_mw=round(float(flows[k]), 2), limit_mw=float(lim[k]), loading_pct=round(abs(flows[k]) / lim[k] * 100, 1),
                 shadow_price=round(float(res["mu_up"][k] - res["mu_lo"][k]), 3)) for k in range(net.nl)
            if res["rated"][k] and abs(flows[k]) >= 0.999 * lim[k]]
    cong.sort(key=lambda r: -abs(r["shadow_price"]))
    gens = [dict(id=net.g_id[i], bus=net.bus_name[net.g_bus[i]], p_mw=round(float(res["pg"][i]), 2), pmin=float(net.g_pmin[i]), pmax=float(net.g_pmax[i]),
                 p_ref_mw=float(net.g_p[i])) for i in range(len(net.g_bus))]
    lmp = res["lmp"]
    out = dict(ok=True, kind=kind, cost_per_h=round(res["cost"], 2), reference_cost_per_h=round(ref_cost, 2), unserved_mw=round(res["unserved_mw"], 3),
               system_lambda=round(res["lam"], 3), lmp_min=round(float(lmp.min()), 3), lmp_max=round(float(lmp.max()), 3),
               n_congested=len(cong), congested=cong[:25], generators=gens,
               shed=[dict(bus=net.bus_name[i], mw=round(float(res['shed'][i]), 2)) for i in np.where(res['shed'] > 1e-6)[0]],
               lmp=[dict(bus=net.bus_name[i], lmp=round(float(lmp[i]), 3)) for i in range(net.nb)],
               ac=ac, loss_iterations=hist, wall_seconds=round(wall, 3), defaults_used=res["defaults_used"],
               assumptions="DC-network LP with piecewise-linear costs, AC-loss iteration and AC verification; advisory only")
    if extra:
        out.update(extra)
    return out


def run_scopf(net: Net, *, emergency: float = 1.15, max_iter: int = 30, cuts_per_iter: int = 80, tol_mw: float = 0.05,
              loss_iterations: int = 2, verify: bool = True, vmin=0.95, vmax=1.05, segments: int = 16, verify_n1: bool = True) -> dict:
    """Preventive N-1 SCOPF. Post-outage limit = emergency x normal rating."""
    t0 = time.perf_counter()
    if len(net.island_ref) > 1:
        net, _ = net.subnet(int(net.island[net.slack_bus]))
    grid = Grid(net)
    nl = net.nl
    rated = (net.rate > 0) & (net.rate < UNRATED)
    lodf = {}
    isl = []
    for k in range(nl):
        v = grid.lodf(k)
        if v is None:
            isl.append(net.br_id[k])
        else:
            lodf[k] = v
    # unsecured reference for the cost of security
    state = {}
    base = solve_lazy(net, grid, segments=segments, state=state)
    if not base["ok"]:
        return dict(ok=False, status=base["status"], message=str(base["message"]))
    loss = 0.0
    cuts, keys = [], set()
    res, ac, hist, iters = None, None, [], 0
    for outer in range(loss_iterations + 1):
        for it in range(max_iter):
            iters = it + 1
            res = solve_lazy(net, grid, loss_mw=loss, cuts=cuts, segments=segments, state=state)
            if not res["ok"]:
                return dict(ok=False, status=res["status"], message=str(res["message"]), n_cuts=len(cuts))
            f = res["flow"]
            viol = []
            for k, lv in lodf.items():
                post = f + lv * f[k]
                post[k] = 0.0
                ex = np.abs(post) - net.rate * emergency
                ex[~rated] = -1.0
                ex[k] = -1.0
                for l in np.where(ex > tol_mw)[0]:
                    viol.append((float(ex[l]), int(l), k, 1.0 if post[l] > 0 else -1.0))
            new = [v for v in sorted(viol, reverse=True) if (v[1], v[2], v[3]) not in keys][:cuts_per_iter]
            if not new:
                break
            for ex, l, k, sg in new:
                keys.add((l, k, sg))
                vec = sg * (grid.ptdf[l] + lodf[k][l] * grid.ptdf[k])
                cuts.append((vec, float(net.rate[l] * emergency), (net.br_id[l], net.br_id[k], sg)))
        if not verify:
            break
        ac = ac_check(net, res["pg"], vmin, vmax)
        hist.append(dict(iteration=outer, loss_assumed_mw=round(loss, 3), ac_losses_mw=ac.get("losses_mw")))
        if not ac["converged"] or outer == loss_iterations or abs(ac["losses_mw"] - loss) < 0.05:
            break
        loss = ac["losses_mw"]
    c2, c1, c0, _ = gen_costs(net)
    ref_cost = float((c2 * net.g_p ** 2 + c1 * net.g_p + c0).sum())
    ac_out = None if ac is None else {k: v for k, v in ac.items() if k != "_sol"}
    n1 = None
    if verify and verify_n1 and ac and ac["converged"]:
        n1 = _ac_n1(net, res["pg"], base["pg"], emergency, vmin, vmax)
    binding = sorted(({"monitored": m[0], "outage": m[1], "price": round(abs(mu), 3)} for m, mu in zip(res["cut_meta"], res["cut_mu"]) if abs(mu) > 1e-6),
                     key=lambda r: -r["price"])[:15]
    extra = dict(secured=True, active_line_limits=int(state['active'].sum()), emergency_factor=emergency, iterations=iters, n_cuts=len(cuts), unsecured_cost_per_h=round(base["cost"], 2),
                 security_cost_per_h=round(res["cost"] - base["cost"], 2), islanding_outages_not_secured=isl[:30], n_islanding_not_secured=len(isl),
                 binding_cuts=binding, ac_n1=n1)
    return _pack(net, res, ac_out, ref_cost, hist, time.perf_counter() - t0, kind="scopf", extra=extra)


def _ac_n1(net, pg_sec, pg_unsec, emergency, vmin, vmax):
    """AC N-1 comparison of the secured and the unsecured dispatch (violations above the emergency rating)."""
    from . import contingency as ca
    out = {}
    for name, pg in (("secured", pg_sec), ("unsecured", pg_unsec)):
        n2 = net.copy()
        n2.g_p = np.array(pg, dtype=float)
        r = ca.run_n1(n2, workers=1, vmin=vmin, vmax=vmax)
        bad = 0
        worst = 0.0
        for c in r["results"]:
            ov = [o["loading_pct"] for o in c["overloads"] if o["loading_pct"] > emergency * 100.0]
            if ov:
                bad += 1
                worst = max(worst, max(ov))
        out[name] = dict(cases=r["cases"], cases_over_emergency=bad, worst_loading_pct=round(worst, 1), nonconverged=r["n_nonconverged"])
    return out
