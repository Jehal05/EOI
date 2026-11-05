import importlib
import numpy as np
import pytest
from ems.network import Net, UNRATED
from ems import opf, powerflow as pf


def _case_row(net):
    """cim-pds lists lines before transformers, so map our branch order to the pypower case rows by id (BR0001 = row 0)."""
    return np.array([int(b[2:]) - 1 for b in net.br_id])


def _pypower_dc(case_mod, net: Net, scale_rate=1.0):
    """Reference: pypower's DC-OPF (exact quadratic costs, PIPS) on the same case with the SAME branch limits."""
    from pypower.api import rundcopf, ppoption
    case = getattr(importlib.import_module(f"pypower.{case_mod}"), case_mod)()
    rated = (net.rate > 0) & (net.rate < UNRATED)
    row = _case_row(net)
    lim = np.zeros(len(case["branch"]))
    lim[row] = np.where(rated, net.rate * scale_rate, 0.0)
    case["branch"][:, 5] = lim
    r = rundcopf(case, ppoption(VERBOSE=0, OUT_ALL=0, PDIPM_MAX_IT=300))
    r = r[0] if isinstance(r, tuple) else r
    assert r["success"], "reference solver did not converge"
    return float(r["f"]), r["gen"][:, 1]


@pytest.mark.parametrize("mod,fx", [("case14", "bb14"), ("case118", "bb118")])
def test_dc_opf_matches_pypower_objective(mod, fx, request):
    net = Net.from_busbranch(request.getfixturevalue(fx))
    ref_cost, ref_pg = _pypower_dc(mod, net)
    mine = opf.solve_dc_opf(net, segments=40)
    assert mine["ok"] and mine["unserved_mw"] < 1e-6
    rel = abs(mine["cost"] - ref_cost) / ref_cost
    print(f"{mod}: mine={mine['cost']:.2f} pypower={ref_cost:.2f} rel.diff={rel:.2e}")
    assert rel < 2e-3      # piecewise-linear cost approximation (40 segments)


def test_dc_opf_matches_pypower_under_congestion(bb118):
    net = Net.from_busbranch(bb118)
    ref_cost, _ = _pypower_dc("case118", net, scale_rate=0.75)
    mine = opf.solve_dc_opf(net, segments=60, line_scale=0.75)
    assert mine["ok"]
    cong = int(np.sum(np.abs(mine["flow"][mine["rated"]]) >= 0.999 * (net.rate[mine["rated"]] * 0.75)))
    print(f"congested: mine={mine['cost']:.2f} pypower={ref_cost:.2f} binding lines={cong}")
    assert cong >= 5                         # the test really exercises the network constraints
    assert abs(mine["cost"] - ref_cost) / ref_cost < 1e-3


def test_flows_respect_limits_and_balance(bb118):
    net = Net.from_busbranch(bb118)
    r = opf.solve_dc_opf(net, segments=16, line_scale=0.6)
    assert r["ok"]
    f = r["flow"][r["rated"]]
    assert np.all(np.abs(f) <= net.rate[r["rated"]] * 0.6 + 1e-6)
    assert r["pg"].sum() + r["shed"].sum() == pytest.approx(net.pd.sum(), abs=1e-6)
    assert np.all(r["pg"] >= net.g_pmin - 1e-9) and np.all(r["pg"] <= net.g_pmax + 1e-9)


def test_lmp_equals_marginal_cost_of_load_finite_difference(bb118):
    """LMP_i must equal d(cost)/d(load at i). Piecewise-linear cost makes this exact except at breakpoints, so
    we check the median error over buses and that most buses agree to a small tolerance."""
    net = Net.from_busbranch(bb118)
    grid = opf.Grid(net)
    s = 0.6
    base = opf.solve_dc_opf(net, grid, segments=60, line_scale=s)
    assert base["ok"]
    rng = np.random.default_rng(3)
    cand = [i for i in np.where(net.pd > 5)[0] if i != net.slack_bus]
    errs = []
    for i in rng.choice(cand, 14, replace=False):
        n2 = net.copy()
        n2.pd[i] += 0.5
        r = opf.solve_dc_opf(n2, grid, segments=60, line_scale=s)
        errs.append(abs((r["lp_objective"] - base["lp_objective"]) / 0.5 - base["lmp"][i]))
    errs = np.array(errs)
    print("LMP finite-difference abs errors ($/MWh):", np.round(np.sort(errs), 3))
    assert np.median(errs) < 0.05 and (errs < 0.5).mean() >= 0.8


def test_congestion_separates_prices(bb118):
    net = Net.from_busbranch(bb118)
    r = opf.solve_dc_opf(net, segments=16, line_scale=0.5)
    assert r["lmp"].max() - r["lmp"].min() > 1.0
    free = opf.solve_dc_opf(net, segments=16, line_scale=50.0)
    assert free["lmp"].max() - free["lmp"].min() < 1e-6        # no congestion: one system price


def test_load_shed_visible_when_capacity_short(bb14):
    net = Net.from_busbranch(bb14)
    r = opf.solve_dc_opf(net, load_scale=3.0)
    assert r["ok"] and r["unserved_mw"] > 1.0


def _dc_flows_after_outage(net, k, inj):
    """Independent DC solve with branch k physically removed (no LODF involved)."""
    sub = net.without_branches([k])
    g = opf.Grid(sub)
    return g.flows(inj), sub


def test_scopf_is_n1_secure_by_independent_dc_resolve(bb118):
    net = Net.from_busbranch(bb118)
    r = opf.run_scopf(net, emergency=1.0, verify=False, segments=12)
    assert r["ok"] and r["n_cuts"] > 0
    pg = np.array([g["p_mw"] for g in r["generators"]])
    inj = np.zeros(net.nb)
    np.add.at(inj, net.g_bus, pg)
    inj -= net.pd
    for sh in r["shed"]:                       # unserved load is part of the dispatch the LP secured
        inj[[i for i, n in enumerate(net.bus_name) if n == sh["bus"]][0]] += sh["mw"]
    grid = opf.Grid(net)
    rated = (net.rate > 0) & (net.rate < UNRATED)
    worst = 0.0
    checked = 0
    for k in range(net.nl):
        if grid.lodf(k) is None:
            continue
        sub = net.without_branches([k])
        f_sub = opf.Grid(sub).flows(inj)
        keep = [j for j in range(net.nl) if j != k]
        lim = net.rate[keep]
        rt = rated[keep]
        worst = max(worst, float(np.max((np.abs(f_sub) - lim)[rt])))
        checked += 1
    print(f"SCOPF: {checked} outages re-solved independently, worst excess = {worst:.4f} MW, cuts={r['n_cuts']}")
    assert worst < 0.5 and checked > 150


def test_scopf_costs_at_least_unsecured_and_equal_when_slack(bb14):
    net = Net.from_busbranch(bb14)
    r = opf.run_scopf(net, emergency=1.0, verify=False)
    assert r["security_cost_per_h"] >= -1e-6
    loose = opf.run_scopf(net, emergency=50.0, verify=False)
    assert loose["n_cuts"] == 0 and abs(loose["security_cost_per_h"]) < 1e-6


def test_ac_verification_converges_and_reports(bb118):
    net = Net.from_busbranch(bb118)
    r = opf.run_opf(net)
    assert r["ac"]["converged"] and r["ac"]["losses_mw"] > 0
    assert r["loss_iterations"][-1]["ac_losses_mw"] > 0


def test_branch_flows_equal_pypower_for_same_dispatch(bb118):
    """My PTDF flows must equal pypower's solved branch flows for the dispatch pypower found (mapped by branch id)."""
    from pypower.api import rundcopf, ppoption
    import importlib as il
    net = Net.from_busbranch(bb118)
    case = il.import_module("pypower.case118").case118()
    case["branch"][:, 5] = 0.0
    r = rundcopf(case, ppoption(VERBOSE=0, OUT_ALL=0, PDIPM_MAX_IT=300))
    r = r[0] if isinstance(r, tuple) else r
    inj = np.zeros(net.nb)
    np.add.at(inj, net.g_bus, r["gen"][:, 1])
    inj -= net.pd
    mine = opf.Grid(net).flows(inj)
    ref = r["branch"][_case_row(net), 13]
    assert np.abs(mine - ref).max() < 1e-4, np.abs(mine - ref).max()


def test_lazy_line_limits_equal_full_formulation(bb118):
    net = Net.from_busbranch(bb118)
    grid = opf.Grid(net)
    for sc in (0.75, 0.62):
        full = opf.solve_dc_opf(net, grid, segments=16, line_scale=sc)
        lazy = opf.solve_lazy(net, grid, segments=16, line_scale=sc)
        assert full["ok"] and lazy["ok"]
        assert lazy["lp_objective"] == pytest.approx(full["lp_objective"], rel=1e-9)
        assert lazy["active_lines"] < int(full["rated"].sum())          # really a smaller LP
        assert np.all(np.abs(lazy["flow"][lazy["rated"]]) <= net.rate[lazy["rated"]] * sc + 1e-6)
        assert np.allclose(lazy["lmp"], full["lmp"], atol=1e-6) or lazy["unserved_mw"] > 0
