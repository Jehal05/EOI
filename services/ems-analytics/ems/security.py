"""Security enhancement: remedial-action planning against N-1 overloads (ADVISORY, never executed automatically).

Workflow
 1. Assess: AC N-1 sweep on the present operating point (live generation/load overrides applied). A contingency is
    CRITICAL when a monitored branch exceeds `emergency` x its rating afterwards.
 2. Plan (any subset of: switching, redispatch, load shed):
      - switching: greedy, opens one in-service branch at a time when it lowers the DC post-contingency overload
        metric without islanding or creating base-case violations (max `max_switching` actions);
      - redispatch: LP over up/down generator moves (minimum redispatch cost, move limits) with linear post-contingency
        constraints anchored on the AC post-contingency flows and DC sensitivities (PTDF/LODF);
      - load shed: last resort inside the same LP at a high price.
 3. Verify: the plan is applied to the network and a full AC N-1 sweep is repeated; rounds repeat (re-linearising on
    the new AC flows) up to `rounds` times. The report states before/after honestly, including what remains.
Limits: thermal overloads only (no voltage/stability remedies); DC sensitivities are an approximation, hence the AC
verification loop; islanding outages cannot be remedied; switching candidates are line OPENINGS only.
"""
from __future__ import annotations
import time
import numpy as np
from scipy.optimize import linprog
from .network import Net, UNRATED
from . import powerflow as pf, contingency as ca
from .opf import Grid, gen_costs

SHED_PRICE = 2000.0       # $/MWh, remedial load shed is the last resort
MOVE_ADDER = 0.5          # $/MWh penalty on every MW moved (suppresses needless churn)


def apply_overrides(net: Net, gen_p: dict | None = None, load_p: dict | None = None):
    """Return a copy of `net` with live generator P (by unit id) and load P (by load id) applied. Q follows P for loads."""
    n = net.copy()
    used = dict(gens=0, loads=0)
    if gen_p:
        for i, g in enumerate(n.g_id):
            if g in gen_p and np.isfinite(gen_p[g]):
                n.g_p[i] = float(np.clip(gen_p[g], min(n.g_pmin[i], 0.0), max(n.g_pmax[i], 0.0) * 1.0001 + 1e-9))
                used["gens"] += 1
    if load_p:
        delta = np.zeros(n.nb)
        for mrid, v in load_p.items():
            if mrid in n.load_bus and np.isfinite(v):
                delta[n.load_bus[mrid]] += float(v) - n.load_p0.get(mrid, 0.0)
                used["loads"] += 1
        old = n.pd.copy()
        n.pd = np.maximum(n.pd + delta, 0.0)
        ratio = np.where(old > 1e-9, n.pd / np.where(old > 1e-9, old, 1.0), 1.0)
        n.qd = n.qd * ratio
    return n, used


class ACMatrix:
    """AC post-outage flows for every single-branch outage at the present operating point.
    P[k, l] = from-end active flow on l after k opened, S[k, l] = max-end MVA; NaN for the outaged branch itself."""

    def __init__(self, net: Net, V0=None):
        nl = net.nl
        self.net = net
        self.P = np.full((nl, nl), np.nan)
        self.S = np.full((nl, nl), np.nan)
        self.status = np.array(["ok"] * nl, dtype=object)       # ok | islanding | nonconverged
        self.lost = np.zeros(nl)
        for k in range(nl):
            n2 = net.without_branches([k])
            sol = pf.solve(n2, V0=V0)
            keep = [j for j in range(nl) if j != k]
            if len(n2.island_ref) > len(net.island_ref):
                self.status[k] = "islanding"
                self.lost[k] = sol["lost_load_mw"]
            if not sol["converged"]:
                self.status[k] = "nonconverged"
                continue
            self.P[k, keep] = sol["pf"]
            self.S[k, keep] = sol["mva_max"]

    def excess_pct(self, emergency: float):
        rated = (self.net.rate > 0) & (self.net.rate < UNRATED)
        with np.errstate(invalid="ignore"):
            load = self.S / np.where(rated, self.net.rate, np.inf)[None, :] * 100.0
        ex = np.nan_to_num(load - emergency * 100.0, nan=0.0)
        ex[:, ~rated] = 0.0
        ex[self.status == "islanding"] = np.where(ex[self.status == "islanding"] > 0, ex[self.status == "islanding"], 0.0)
        return np.maximum(ex, 0.0), load

    def summary(self, emergency: float):
        ex, load = self.excess_pct(emergency)
        ok = self.status != "nonconverged"
        crit_k = np.where((ex.max(axis=1) > 1e-6) & ok)[0]
        return dict(cases=int(self.net.nl), critical=int(len(crit_k)), n_overloads=int((ex > 1e-6).sum()), total_excess_pct=float(ex.sum()),
                    worst_loading_pct=float(np.nanmax(np.where(ex > 1e-6, load, 0.0))) if len(crit_k) else 0.0,
                    nonconverged=int((self.status == "nonconverged").sum()), islanding_outages=int((self.status == "islanding").sum()))

    def critical(self, emergency: float, top=15):
        ex, load = self.excess_pct(emergency)
        net = self.net
        out = []
        for k in np.argsort(-ex.max(axis=1)):
            if ex[k].max() <= 1e-6 or self.status[k] == "nonconverged":
                break
            ls = np.where(ex[k] > 1e-6)[0]
            ls = ls[np.argsort(-load[k, ls])]
            out.append(dict(outage=net.br_id[k], branches=[net.br_id[k]], worst_loading_pct=round(float(load[k, ls[0]]), 1), islanded=bool(self.status[k] == "islanding"),
                            lost_load_mw=round(float(self.lost[k]), 1), overloads=[dict(branch=net.br_id[l], loading_pct=round(float(load[k, l]), 1)) for l in ls[:5]]))
            if len(out) >= top:
                break
        return out


VIOL_PENALTY = 1000.0     # $/MW of unrelieved post-contingency flow in the LP (elastic constraints)


def _redispatch_lp(net: Net, grid: Grid, base_sol: dict, mat: ACMatrix, emergency: float, *, margin: float, max_move: float | None,
                   allow_shed: bool, max_rows: int = 900, moved=None):
    ng = len(net.g_bus)
    c2, c1, _, _ = gen_costs(net)
    p0 = net.g_p.copy()
    marg = 2 * c2 * p0 + c1
    up_max = np.maximum(net.g_pmax - p0, 0.0)
    dn_max = np.maximum(p0 - net.g_pmin, 0.0)
    if max_move is not None:                    # the limit applies to the CUMULATIVE move of a unit over all rounds
        mv = np.zeros(ng) if moved is None else moved
        up_max = np.minimum(up_max, np.maximum(max_move - mv, 0.0))
        dn_max = np.minimum(dn_max, np.maximum(max_move + mv, 0.0))
    load_bus = np.where(net.pd > 0)[0] if allow_shed else np.array([], dtype=int)
    nu = len(load_bus)
    Gm = np.zeros((net.nb, ng))
    Gm[net.g_bus, np.arange(ng)] = 1.0
    Um = np.zeros((net.nb, nu))
    Um[load_bus, np.arange(nu)] = 1.0
    Dm = np.hstack([Gm, -Gm, Um])
    rated = (net.rate > 0) & (net.rate < UNRATED)
    cand = []                                   # (priority, sens, rhs, kind, l, k)
    Pf0, S0 = base_sol["pf"], base_sol["mva_max"]
    for l in np.where(rated & (S0 > 0.85 * net.rate))[0]:
        rho = net.rate[l] * (1 - margin) / S0[l]
        sg = 1.0 if Pf0[l] >= 0 else -1.0
        cand.append((S0[l] / net.rate[l] * 10, sg * (grid.ptdf[l] @ Dm), (rho - 1.0) * abs(Pf0[l]), "base", l, -1))
    for k in range(net.nl):
        if mat.status[k] != "ok":
            continue
        lv = grid.lodf(k)
        if lv is None:
            continue
        ratio = mat.S[k] / np.where(rated, net.rate, np.inf)
        for l in np.where(np.nan_to_num(ratio, nan=0.0) > 0.9 * emergency)[0]:
            p, s_ = mat.P[k, l], mat.S[k, l]
            if s_ <= 0:
                continue
            rho = emergency * net.rate[l] * (1 - margin) / s_
            sg = 1.0 if p >= 0 else -1.0
            cand.append((ratio[l], sg * ((grid.ptdf[l] + lv[l] * grid.ptdf[k]) @ Dm), (rho - 1.0) * abs(p), "post", l, k))
    cand.sort(key=lambda c: -c[0])
    cand = cand[:max_rows]
    nr = len(cand)
    # variables: up, dn, shed, elastic(nr)
    cvec = np.r_[marg + MOVE_ADDER, -marg + MOVE_ADDER, np.full(nu, SHED_PRICE), np.full(nr, VIOL_PENALTY)]
    bounds = [(0, float(u)) for u in up_max] + [(0, float(d)) for d in dn_max] + [(0, float(net.pd[b])) for b in load_bus] + [(0, None)] * nr
    if nr == 0:
        return dict(ok=True, up=np.zeros(ng), dn=np.zeros(ng), shed=np.zeros(nu), load_bus=load_bus, n_constraints=0)
    A = np.zeros((nr, 2 * ng + nu + nr))
    b = np.zeros(nr)
    for i, (_p, sens, rhs, _kd, _l, _k) in enumerate(cand):
        A[i, :2 * ng + nu] = sens
        A[i, 2 * ng + nu + i] = -1.0
        b[i] = rhs
    Aeq = np.zeros((1, 2 * ng + nu + nr))
    Aeq[0, :ng], Aeq[0, ng:2 * ng], Aeq[0, 2 * ng:2 * ng + nu] = 1.0, -1.0, 1.0
    r = linprog(cvec, A_ub=A, b_ub=b, A_eq=Aeq, b_eq=np.zeros(1), bounds=bounds, method="highs")
    if r.status != 0:
        return dict(ok=False, message=str(r.message), n_constraints=nr)
    x = r.x
    return dict(ok=True, up=x[:ng], dn=x[ng:2 * ng], shed=x[2 * ng:2 * ng + nu], load_bus=load_bus, n_constraints=nr,
                unrelieved_mw=float(x[2 * ng + nu:].sum()))


def _dc_inj(net: Net):
    inj = np.zeros(net.nb)
    np.add.at(inj, net.g_bus, net.g_p)
    inj -= net.pd
    return inj


def _excess_metric(net: Net, grid: Grid, inj, emergency: float, outages: list[str]):
    """Sum over given outages (by id) of post-outage DC overload above emergency*rate, plus base-case overload."""
    ids = {b: i for i, b in enumerate(net.br_id)}
    rated = (net.rate > 0) & (net.rate < UNRATED)
    f = grid.flows(inj)
    tot = float(np.maximum(np.abs(f) - net.rate, 0.0)[rated].sum())
    base = tot
    for o in outages:
        k = ids.get(o)
        if k is None:
            continue
        lv = grid.lodf(k)
        if lv is None:
            continue
        post = f + lv * f[k]
        post[k] = 0.0
        ex = np.maximum(np.abs(post) - emergency * net.rate, 0.0)
        ex[~rated] = 0.0
        tot += float(ex.sum())
    return tot, base


def _switching(net: Net, crit: list, emergency: float, max_actions: int, name_to_idx_fn):
    """Greedy line-opening search on the DC model. Returns (actions, new_net)."""
    actions = []
    cur = net
    outages = [c["outage"] for c in crit[:25] if len(c["branches"]) == 1]
    for _ in range(max_actions):
        grid = Grid(cur)
        inj = _dc_inj(cur)
        base_metric, base_only = _excess_metric(cur, grid, inj, emergency, outages)
        if base_metric <= 1e-6:
            break
        f = grid.flows(inj)
        rated = (cur.rate > 0) & (cur.rate < UNRATED)
        # candidate lines: strongest opposing effect on currently overloaded monitored lines (LODF x flow)
        post_over = {}
        ids = {b: i for i, b in enumerate(cur.br_id)}
        for o in outages:
            k = ids.get(o)
            if k is None:
                continue
            lv = grid.lodf(k)
            if lv is None:
                continue
            post = f + lv * f[k]
            ex = np.where(rated, np.abs(post) - emergency * cur.rate, -1)
            ex[k] = -1
            for l in np.where(ex > 0)[0]:
                post_over[l] = max(post_over.get(l, 0.0), float(ex[l]))
        cand = []
        for c in range(cur.nl):
            lv = grid.lodf(c)
            if lv is None or abs(f[c]) < 1e-6:
                continue
            gain = 0.0
            for l, ex in post_over.items():
                if l != c:
                    delta = lv[l] * f[c]
                    gain += max(0.0, -np.sign(f[l]) * delta)
            if gain > 0:
                cand.append((gain, c))
        cand.sort(reverse=True)
        best = None
        for _gain, c in cand[:30]:
            if cur.br_id[c] in outages:
                continue
            n2 = cur.without_branches([c])
            if len(n2.island_ref) > len(cur.island_ref):
                continue
            g2 = Grid(n2)
            m2, b2 = _excess_metric(n2, g2, _dc_inj(n2), emergency, [o for o in outages if o != cur.br_id[c]])
            if b2 > base_only + 1e-6:                      # must not worsen normal-state overloads
                continue
            if best is None or m2 < best[0]:
                best = (m2, c, n2)
        if best is None or best[0] > 0.95 * base_metric:
            break
        actions.append(dict(type="switching", action="open", element=cur.br_id[best[1]], from_bus=cur.bus_name[cur.f[best[1]]],
                            to_bus=cur.bus_name[cur.t[best[1]]], reason=f"DC post-contingency overload metric {base_metric:.0f} -> {best[0]:.0f} MW"))
        cur = best[2]
    return actions, cur


def _crit_ids(mat: ACMatrix, emergency: float):
    return [c for c in mat.critical(emergency, top=60)]


def plan(net: Net, *, emergency: float = 1.15, use_switching: bool = True, use_redispatch: bool = True, use_shed: bool = True,
         max_switching: int = 3, max_move_mw: float | None = None, rounds: int = 5, margin: float = 0.02) -> dict:
    t0 = time.perf_counter()
    if len(net.island_ref) > 1:
        net, _ = net.subnet(int(net.island[net.slack_bus]))
    base = pf.solve(net)
    if not base["converged"]:
        raise RuntimeError("base case did not converge")
    mat0 = ACMatrix(net, base["V"])
    sweep0 = mat0.summary(emergency)
    out = dict(emergency=emergency, before=sweep0, critical_before=mat0.critical(emergency), actions=[], notes=[],
               semantics="critical = an outage leaves a branch above emergency x rating (pre-existing overloads included)")
    if sweep0["critical"] == 0:
        out.update(status="secure", after=sweep0, wall_seconds=round(time.perf_counter() - t0, 3), notes=["no N-1 outage overloads a branch beyond the emergency rating"])
        return out
    actions, cur = [], net
    mat = mat0
    if use_switching:
        sw, cur2 = _switching(net, _crit_ids(mat0, emergency), emergency, max_switching, None)
        if sw:
            b2 = pf.solve(cur2)
            if b2["converged"]:
                m2 = ACMatrix(cur2, b2["V"])
                if m2.summary(emergency)["total_excess_pct"] < sweep0["total_excess_pct"]:
                    actions, cur, mat = sw, cur2, m2
                else:
                    out["notes"].append("switching candidates were rejected: AC verification showed no net improvement")
    cur = cur.copy()
    moved = np.zeros(len(net.g_bus))
    shed_total = np.zeros(net.nb)
    cur_metric = mat.summary(emergency)["total_excess_pct"]
    rounds_used = 0
    if use_redispatch:
        for rnd in range(rounds):
            base_c = pf.solve(cur)
            lp = _redispatch_lp(cur, Grid(cur), base_c, mat, emergency, margin=margin, max_move=max_move_mw, allow_shed=use_shed, moved=moved)
            if not lp["ok"]:
                out["notes"].append(f"redispatch LP not solved in round {rnd + 1}: {lp.get('message')}")
                break
            d = lp["up"] - lp["dn"]
            if np.abs(d).sum() + lp["shed"].sum() < 1e-6:
                out["notes"].append("linear model sees no further feasible relief")
                break
            accepted = False
            for alpha in (1.0, 0.5, 0.25):                       # trust-region style step reduction on AC re-check
                trial = cur.copy()
                trial.g_p = cur.g_p + alpha * d
                dshed = np.zeros(net.nb)
                for j, b in enumerate(lp["load_bus"]):
                    s_ = alpha * lp["shed"][j]
                    if s_ > 1e-6:
                        ratio = max(trial.pd[b] - s_, 0.0) / max(trial.pd[b], 1e-9)
                        trial.pd[b] -= s_
                        trial.qd[b] *= ratio
                        dshed[b] = s_
                tb = pf.solve(trial)
                if not tb["converged"]:
                    continue
                tm = ACMatrix(trial, tb["V"])
                tmet = tm.summary(emergency)["total_excess_pct"]
                if tmet < cur_metric - 1e-6:
                    cur, mat, cur_metric = trial, tm, tmet
                    moved += alpha * d
                    shed_total += dshed
                    accepted = True
                    rounds_used = rnd + 1
                    break
            if not accepted:
                out["notes"].append("no step reduced the AC overload metric; stopped")
                break
            if mat.summary(emergency)["critical"] == 0:
                break
    c2, c1, c0, _ = gen_costs(net)
    cost_fn = lambda p: float((c2 * p ** 2 + c1 * p + c0).sum())
    for i in np.where(np.abs(moved) > 0.05)[0]:
        actions.append(dict(type="redispatch", action="increase" if moved[i] > 0 else "decrease", element=net.g_id[i], bus=net.bus_name[net.g_bus[i]],
                            from_mw=round(float(net.g_p[i]), 2), to_mw=round(float(net.g_p[i] + moved[i]), 2), delta_mw=round(float(moved[i]), 2),
                            reason="relieves post-contingency overloads (AC-anchored sensitivities)"))
    for b in np.where(shed_total > 0.05)[0]:
        actions.append(dict(type="load_shed", action="shed", element=net.bus_name[b], mw=round(float(shed_total[b]), 2),
                            reason="last resort: redispatch alone cannot relieve the overloads"))
    sweep1 = mat.summary(emergency)
    base1 = pf.solve(cur)
    after = dict(sweep1, base_converged=bool(base1["converged"]),
                 base_overloads=int(((base1["loading_pct"] > 100.0) & base1["rated"]).sum()) if base1["converged"] else None)
    status = "secured" if sweep1["critical"] == 0 else ("improved" if sweep1["total_excess_pct"] < sweep0["total_excess_pct"] else "not_improved")
    out.update(status=status, after=after, critical_after=mat.critical(emergency, top=10), actions=actions,
               redispatch_mw=round(float(np.abs(moved).sum() / 2), 2), shed_mw=round(float(shed_total.sum()), 2),
               cost_change_per_h=round(cost_fn(cur.g_p) - cost_fn(net.g_p), 2), rounds=rounds_used,
               wall_seconds=round(time.perf_counter() - t0, 3),
               advisory="Not executed. Any action must go through the normal authorised command path.")
    return out
