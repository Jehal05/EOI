import numpy as np
from ems.network import Net
from ems import powerflow as pf, contingency as ca


def test_n1_runs_and_ranks_ieee118(bb118):
    net = Net.from_busbranch(bb118)
    r = ca.run_n1(net, workers=1)
    assert r["cases"] == 186
    sev = [x["severity"] for x in r["results"]]
    assert sev == sorted(sev, reverse=True)
    assert r["n_nonconverged"] == 0


def test_parallel_equals_serial(bb118):
    net = Net.from_busbranch(bb118)
    a = ca.run_n1(net, workers=1)
    b = ca.run_n1(net, workers=2)
    key = lambda r: {x["outage"]: (round(x["severity"], 6), x["converged"]) for x in r["results"]}
    assert key(a) == key(b)


def test_radial_outage_loses_load_ieee14(bb14):
    """Bus 8 is fed only by transformer 7-8 and has no load in IEEE-14; losing the line islands a generator.
    Bus 14's loss of both feeds would lose load; check that a single outage at a radial load bus is handled."""
    net = Net.from_busbranch(bb14)
    r = ca.run_n1(net)
    islanding = [x for x in r["results"] if x["islanded"]]
    assert islanding, "IEEE-14 has radial elements whose outage islands the network"
    for x in islanding:
        assert x["converged"]


def test_branch_outage_matches_independent_oracle(bb118):
    """Compare one post-contingency solution against the oracle solving the modified case."""
    import importlib
    from pypower.api import runpf, ppoption
    case = importlib.import_module("pypower.case118").case118()
    net = Net.from_busbranch(bb118)
    k = 40
    br_idx = int(net.br_id[k][2:]) - 1
    case["branch"][br_idx, 10] = 0  # out of service in the oracle
    ref, ok = runpf(case, ppoption(VERBOSE=0, OUT_ALL=0))
    assert ok
    mine = pf.solve(net.without_branches([k]))
    assert mine["converged"]
    assert np.max(np.abs(mine["vm"] - ref["bus"][:, 7])) < 1e-6
    assert abs(mine["losses_mw"] - (ref["gen"][:, 1].sum() - ref["bus"][:, 2].sum())) < 1e-4
