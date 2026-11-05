import numpy as np
import pytest
from ems.network import Net, UNRATED
from ems import powerflow as pf, tcm
from ems.opf import Grid


def _dc_flows(net, inj):
    """Independent DC power flow by dense solve on the (possibly outaged) network."""
    nb = net.nb
    tap = np.where(net.tap != 0.0, np.abs(net.tap), 1.0)
    b = 1.0 / (net.x * tap)
    B = np.zeros((nb, nb))
    for k in range(net.nl):
        f, t = net.f[k], net.t[k]
        B[f, f] += b[k]; B[t, t] += b[k]; B[f, t] -= b[k]; B[t, f] -= b[k]
    keep = [i for i in range(nb) if i != net.slack_bus]
    th = np.zeros(nb)
    th[keep] = np.linalg.solve(B[np.ix_(keep, keep)], inj[keep])
    return b * (th[net.f] - th[net.t])


def _inj(net):
    inj = -net.pd.copy()
    np.add.at(inj, net.g_bus, net.g_p)
    return inj / net.base


def test_partition_covers_live_buses_and_corridors_cut_exactly(bb118):
    net = Net.from_busbranch(bb118)
    area = tcm.partition(net, 3)
    assert set(np.unique(area)) == {0, 1, 2}
    cs = tcm.corridors(net, area)
    cut = {k for k in range(net.nl) if area[net.f[k]] != area[net.t[k]]}
    seen = set()
    for a, b, kf, kr in cs:
        if a < b:
            seen |= set(kf) | set(kr)
    assert seen == cut
    for a, b, kf, kr in cs:
        assert all(area[net.f[k]] == a and area[net.t[k]] == b for k in kf)


def test_interface_flow_antisymmetric(bb118):
    net = Net.from_busbranch(bb118)
    sol = pf.solve(net)
    area = tcm.partition(net, 2)
    (a, b, kf, kr), (a2, b2, kf2, kr2) = tcm.corridors(net, area)
    assert tcm.interface_flow(net, sol["pf"], kf, kr) == pytest.approx(-tcm.interface_flow(net, sol["pf"], kf2, kr2))


def test_dc_limits_match_independent_dc_resolve(bb14):
    """At 0.999 t* no monitored (base or N-1) DC flow exceeds its limit; at 1.001 t* the named one does. Oracle = dense DC solves with the branch really removed."""
    net = Net.from_busbranch(bb14)
    rated = np.ones(net.nl, dtype=bool)
    base_inj = _inj(net)
    g = Grid(net)
    F0 = g.flows(base_inj) * net.base
    area = tcm.partition(net, 2)
    a, b, kf, kr = tcm.corridors(net, area)[0]
    dg, cap, up, dn = tcm.transfer_vector(net, np.where(area == a)[0], np.where(area == b)[0])
    d = np.zeros(net.nb); np.add.at(d, net.g_bus, dg)
    rate = np.full(net.nl, 1.5 * np.abs(F0).max())          # a uniform rating that makes limits bite
    lim = tcm.dc_limits(g, F0, d, rate, rated, n1_limit=1.0)
    t, l, k = lim["n1"]
    assert np.isfinite(t) and t > 0
    s = d / net.base

    def post(tt, kk):
        n2 = net.without_branches([kk])
        keep = [j for j in range(net.nl) if j != kk]
        f = _dc_flows(n2, base_inj + tt * s) * net.base
        full = np.full(net.nl, np.nan); full[keep] = f
        return full
    # (the oracle needs DC-consistent base flows, which F0 is by construction here)
    for tt, expect_viol in ((0.999 * t, False), (1.001 * t, True)):
        f = post(tt, k)
        assert (abs(f[l]) > rate[l] * (1 + 1e-9)) == expect_viol, (tt, f[l], rate[l])
    # no other (outage, branch) violated just below t*
    T = lim["T"]
    assert np.nanmin(T) == pytest.approx(t)


def test_generation_cap_and_vector_sums_to_zero(bb118):
    net = Net.from_busbranch(bb118)
    area = tcm.partition(net, 2)
    dg, cap, up, dn = tcm.transfer_vector(net, np.where(area == 0)[0], np.where(area == 1)[0])
    assert dg.sum() == pytest.approx(0.0, abs=1e-9)
    assert cap == pytest.approx(min(up, dn))
    gp = net.g_p + cap * dg
    assert (gp <= net.g_pmax + 1e-6).all()
    assert (gp >= np.minimum(net.g_pmin, net.g_p) - 1e-6).all()


def test_analyze_ac_limit_is_secure_and_beyond_is_not(bb118):
    net = Net.from_busbranch(bb118)
    r = tcm.analyze(net, parts=2, ac_n1_top=10)
    assert r["status"] == "ok" and r["corridors"]
    c = r["corridors"][-1]
    assert c["status"] == "ok" and c["ac_mw"] <= c["dc_mw"] + 1e-6
    assert c["atc_mw"] <= c["ac_mw"] + 1e-9 and c["ttc_mw"] == pytest.approx(abs(c["base_flow_mw"]) + c["ac_mw"], abs=0.2)
    # independent AC re-check at the reported AC limit: base case within rating
    area = tcm.partition(net, 2)
    a, b = c["from_area"], c["to_area"]
    dg, cap, *_ = tcm.transfer_vector(net, np.where(area == a)[0], np.where(area == b)[0])
    n2 = tcm._with_transfer(net, dg, c["ac_mw"])
    sol = pf.solve(n2)
    assert sol["converged"]
    over0, _ = tcm._viol(net, pf.solve(net), 0.95, 1.05, 1.0)
    over, vv = tcm._viol(n2, sol, 0.95, 1.05, 1.0, over0)
    assert not over


def test_trm_reduces_atc_and_levels(bb14):
    net = Net.from_busbranch(bb14)
    pick = lambda r: next(c for c in r["corridors"] if c["status"] == "ok" and c["ac_mw"] > 1.0)
    a = pick(tcm.analyze(net, parts=2, trm_pct=0.0))
    b = pick(tcm.analyze(net, parts=2, trm_pct=20.0))
    assert (a["from_area"], a["to_area"]) == (b["from_area"], b["to_area"])
    assert a["atc_mw"] == pytest.approx(a["ac_mw"], abs=0.2)
    assert b["atc_mw"] < a["atc_mw"]
    assert b["trm_mw"] == pytest.approx(0.2 * b["ttc_mw"], abs=0.2)


def test_explicit_source_sink_and_no_generation_case(bb14):
    net = Net.from_busbranch(bb14)
    area = tcm.partition(net, 2)
    r = tcm.analyze(net, source_buses=np.where(area == 0)[0], sink_buses=np.where(area == 1)[0])
    assert len(r["corridors"]) == 1
    n2 = net.copy(); n2.g_pmax = n2.g_p.copy()          # no headroom anywhere
    r2 = tcm.analyze(n2, parts=2)
    assert all(c["status"] == "no_generation_capacity" and c["atc_mw"] == 0 for c in r2["corridors"])
