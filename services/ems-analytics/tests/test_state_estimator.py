import numpy as np
from ems.network import Net
from ems import powerflow as pf
from ems import state_estimator as se


def _setup(bb, seed, gross=None):
    net = Net.from_busbranch(bb)
    sol = pf.solve(net)
    ms = se.simulate_measurements(net, sol, np.random.default_rng(seed), gross=gross)
    return net, sol, ms


def _err(net, sol, est):
    ev = est["vm"] - sol["vm"]
    ea = (est["va_deg"] - sol["va_deg"] + sol["va_deg"][net.slack_bus]) - 0  # est ref angle is 0
    ea = est["va_deg"] - (sol["va_deg"] - sol["va_deg"][net.slack_bus])
    return float(np.sqrt(np.mean(ev ** 2))), float(np.sqrt(np.mean(ea ** 2)))


def test_filters_noise_ieee14(bb14):
    net, sol, ms = _setup(bb14, 1)
    est = se.estimate(net, ms)
    assert est["observable"] and est["converged"]
    rms_v, rms_a = _err(net, sol, est)
    assert rms_v < 0.004 / 2          # better than a single raw voltage measurement (sigma 0.004)
    assert rms_a < 0.1                # degrees
    assert not est["bad_data_detected"]


def test_clean_data_passes_chi_square_most_of_the_time(bb118):
    flagged = 0
    runs = 20
    for s in range(runs):
        net, sol, ms = _setup(bb118, 100 + s)
        est = se.estimate(net, ms, bad_data=False)
        flagged += est["bad_data_detected"]
    assert flagged <= 3  # alpha=1% -> expect ~0-1; allow slack for finite-sample behaviour


def test_gross_error_detected_identified_and_removed(bb118):
    bad = "PF:BR0050"
    net, sol, ms = _setup(bb118, 7, gross={bad: 40.0})
    raw = se.estimate(net, ms, bad_data=False)
    assert raw["bad_data_detected"]
    fixed = se.estimate(net, ms, bad_data=True)
    assert [r["id"] for r in fixed["removed"]][:1] == [bad]
    assert not fixed["bad_data_detected"]
    rv_raw, _ = _err(net, sol, raw)
    rv_fix, _ = _err(net, sol, fixed)
    assert rv_fix < rv_raw
    assert rv_fix < 0.002


def test_multiple_gross_errors(bb118):
    bads = {"PINJ:Busbar 12": 35.0, "VM:Busbar 77": -30.0, "QT:BR0100": 40.0}
    net, sol, ms = _setup(bb118, 11, gross=bads)
    fixed = se.estimate(net, ms)
    found = {r["id"] for r in fixed["removed"]}
    assert set(bads) <= found, f"missed {set(bads) - found}"
    assert not fixed["bad_data_detected"]


def test_unobservable_detected(bb14):
    net, sol, ms = _setup(bb14, 3)
    only_v = [m for m in ms if m.kind == "vm"]
    est = se.estimate(net, only_v)
    assert est["observable"] is False


def _split(bb, drop):
    net = Net.from_busbranch(bb)
    return net.without_branches([net.br_id.index(b) for b in drop])


def test_islanded_network_estimated_per_island(bb118):
    # find a branch pair that splits the system by trying radial branches
    base = Net.from_busbranch(bb118)
    drop = None
    for k in range(base.nl):
        n2 = base.without_branches([k])
        if len(n2.island_ref) > 1:
            drop = [base.br_id[k]]
            break
    assert drop, "IEEE-118 has radial branches"
    net = _split(bb118, drop)
    sol = pf.solve(net)
    ms = se.simulate_measurements(net, sol, np.random.default_rng(3))
    est = se.estimate_network(net, ms)
    assert est["observable"] and est["converged"]
    live = [i for i in range(net.nb) if net.bus_name[i] not in est["dead_buses"]]
    assert np.max(np.abs(est["vm"][live] - sol["vm"][live])) < 0.02
    for i in range(net.nb):
        if net.bus_name[i] in est["dead_buses"]:
            assert est["vm"][i] == 0.0


def test_estimate_network_single_island_matches_estimate(bb14):
    net, sol, ms = _setup(bb14, 1)
    a, b = se.estimate(net, ms), se.estimate_network(net, ms)
    assert np.allclose(a["vm"], b["vm"]) and b["dead_buses"] == []
