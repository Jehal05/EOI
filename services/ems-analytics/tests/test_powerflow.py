"""Power flow validation.
1. Against published IEEE 14-bus solution values (widely reproduced in the literature).
2. Against an independent open-source implementation (pypower, used here ONLY as a test oracle).
3. Against the equations themselves: residual mismatch must be ~0."""
import numpy as np
import pytest
from ems.network import Net
from ems import powerflow as pf


def test_ieee14_published_solution(bb14):
    net = Net.from_busbranch(bb14)
    r = pf.solve(net)
    assert r["converged"] and r["iterations"] <= 6
    vm = r["vm"]
    va = r["va_deg"]
    # published IEEE-14 results (|V| pu, angle deg), tolerance covers rounding in the literature
    pub_vm = [1.060, 1.045, 1.010, 1.019, 1.020, 1.070, 1.062, 1.090, 1.056, 1.051, 1.057, 1.055, 1.050, 1.036]
    pub_va = [0.0, -4.98, -12.72, -10.33, -8.78, -14.22, -13.37, -13.36, -14.94, -15.10, -14.79, -15.07, -15.16, -16.04]
    assert np.max(np.abs(vm - pub_vm)) < 2e-3
    assert np.max(np.abs(va - pub_va)) < 0.06
    assert abs(r["losses_mw"] - 13.39) < 0.05


def _oracle(case_fn, enforce_q=False):
    from pypower.api import runpf, ppoption
    res, ok = runpf(case_fn(), ppoption(VERBOSE=0, OUT_ALL=0, ENFORCE_Q_LIMS=int(enforce_q)))
    assert ok
    return res


@pytest.mark.parametrize("model,casefn", [("14", "case14"), ("118", "case118")])
def test_matches_independent_oracle(model, casefn, bb14, bb118):
    import importlib
    case = getattr(importlib.import_module(f"pypower.{casefn}"), casefn)
    bb = bb14 if model == "14" else bb118
    net = Net.from_busbranch(bb)
    mine = pf.solve(net)
    ref = _oracle(case)
    assert mine["converged"]
    assert np.max(np.abs(mine["vm"] - ref["bus"][:, 7])) < 1e-6
    # angles are only defined relative to the reference bus (the source case fixes its slack at 30 deg,
    # this solver at 0 deg), so compare angle differences to the slack bus
    refbus = net.slack_bus
    assert np.max(np.abs((mine["va_deg"] - mine["va_deg"][refbus]) - (ref["bus"][:, 8] - ref["bus"][refbus, 8]))) < 1e-4
    # branch flows: map by mRID BRxxxx -> row in the source branch table
    order = [int(m[2:]) - 1 for m in net.br_id]
    assert np.max(np.abs(mine["pf"] - ref["branch"][order, 13])) < 1e-3
    assert np.max(np.abs(mine["qf"] - ref["branch"][order, 14])) < 1e-3
    assert np.max(np.abs(mine["pt"] - ref["branch"][order, 15])) < 1e-3
    loss_ref = float(np.sum(ref["gen"][:, 1]) - np.sum(ref["bus"][:, 2]))
    assert abs(mine["losses_mw"] - loss_ref) < 1e-4
    # generator dispatch (slack picks up the imbalance)
    gorder = [int(m[1:]) - 1 for m in net.g_id]
    assert np.max(np.abs(mine["gen_p"] - ref["gen"][gorder, 1])) < 1e-3
    assert np.max(np.abs(mine["gen_q"] - ref["gen"][gorder, 2])) < 1e-3


def test_residual_is_zero(bb118):
    net = Net.from_busbranch(bb118)
    r = pf.solve(net)
    Ybus, _, _ = net.ybus()
    S = r["V"] * np.conj(Ybus @ r["V"]) * net.base
    # P balance at every non-slack bus; Q balance at PQ buses
    pg = np.zeros(net.nb)
    np.add.at(pg, net.g_bus, net.g_p)
    nonref = net.bus_type != 3
    assert np.max(np.abs((S.real - (pg - net.pd))[nonref])) < 1e-5
    pq = net.bus_type == 1
    qg = np.zeros(net.nb)
    np.add.at(qg, net.g_bus, net.g_q)
    assert np.max(np.abs((S.imag - (qg - net.qd))[pq])) < 1e-5


def test_q_limit_enforcement_changes_solution_and_respects_limits(bb118):
    net = Net.from_busbranch(bb118)
    a = pf.solve(net)
    b = pf.solve(net, enforce_q_limits=True)
    assert b["converged"]
    for bus in b["q_limited_buses"]:
        idx = np.where(net.g_bus == bus)[0]
        qmax, qmin = net.g_qmax[idx].sum(), net.g_qmin[idx].sum()
        q = b["gen_q"][idx].sum()
        assert qmin - 1e-3 <= q <= qmax + 1e-3
    if b["q_limited_buses"]:
        assert np.max(np.abs(a["vm"] - b["vm"])) > 1e-6


def test_island_split_with_dead_island(bb14):
    """Remove every branch at bus 8 (a radial generator bus via transformer 7-8): bus 8 has a gen so it self-slacks."""
    net = Net.from_busbranch(bb14)
    idx = [i for i in range(net.nl) if 7 in (net.f[i], net.t[i])]
    n2 = net.without_branches(idx)
    r = pf.solve(n2)
    assert r["converged"]
    assert len(set(n2.island.tolist())) == 2
    # a generator-less island would be dead: isolate bus 14 (index 13) which has load only
    idx = [i for i in range(net.nl) if 13 in (net.f[i], net.t[i])]
    n3 = net.without_branches(idx)
    r3 = pf.solve(n3)
    assert r3["converged"] and 13 in r3["dead_buses"]
    assert abs(r3["lost_load_mw"] - 14.9) < 1e-9


def test_token_cross_language_vector(monkeypatch):
    """Same token vector verified by the Rust, Go and Java services."""
    import subprocess, os
    from ems import auth
    monkeypatch.setenv("EMS_SECRET", "contract-test-secret-0123456789")
    vec = subprocess.check_output(["python3", os.path.join(os.path.dirname(__file__), "..", "..", "..", "tools", "token_vector.py")]).decode().strip()
    c = auth.verify(vec)
    assert c["sub"] == "vector" and c["role"] == "operator"
