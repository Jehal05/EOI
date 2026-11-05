import numpy as np
import pytest
from ems.network import Net, UNRATED
from ems import powerflow as pf, security as sec
from ems.opf import Grid


def test_apply_overrides_sets_generation_and_load(bb118):
    net = Net.from_busbranch(bb118)
    gid = net.g_id[3]
    lid = next(iter(net.load_bus))
    b = net.load_bus[lid]
    p_old = net.load_p0[lid]
    n2, used = sec.apply_overrides(net, {gid: 77.0}, {lid: p_old + 10.0})
    assert used == dict(gens=1, loads=1)
    assert n2.g_p[3] == pytest.approx(77.0)
    assert n2.pd[b] == pytest.approx(net.pd[b] + 10.0)
    assert n2.qd[b] / net.qd[b] == pytest.approx(n2.pd[b] / net.pd[b])
    assert net.g_p[3] != 77.0                      # original untouched
    n3, used3 = sec.apply_overrides(net, {"NOPE": 1.0}, {"NOPE": 1.0})
    assert used3 == dict(gens=0, loads=0)


def test_switching_dc_identity_lodf_equals_independent_solve(bb118):
    """Opening line c must change flows exactly as LODF predicts (checked against a physical removal)."""
    net = Net.from_busbranch(bb118)
    g = Grid(net)
    inj = sec._dc_inj(net)
    f = g.flows(inj)
    checked = 0
    for c in range(0, net.nl, 9):
        lv = g.lodf(c)
        if lv is None:
            continue
        post = f + lv * f[c]
        sub = net.without_branches([c])
        ref = Grid(sub).flows(sec._dc_inj(sub))
        keep = [j for j in range(net.nl) if j != c]
        assert np.allclose(post[keep], ref, atol=1e-7), c
        checked += 1
    assert checked >= 15


def test_sensitivities_track_ac_response(bb118):
    """A 10 MW transfer between two units: the DC sensitivities must predict the AC change in post-outage flow."""
    net = Net.from_busbranch(bb118)
    base = pf.solve(net)
    mat0 = sec.ACMatrix(net, base["V"])
    n2 = net.copy()
    i, j = 5, 40
    n2.g_p[i] += 10.0
    n2.g_p[j] -= 10.0
    b2 = pf.solve(n2)
    mat1 = sec.ACMatrix(n2, b2["V"])
    g = Grid(net)
    ratios = []
    for k in range(0, net.nl, 4):
        if mat0.status[k] != "ok" or g.lodf(k) is None:
            continue
        lv = g.lodf(k)
        dvec = np.zeros(net.nb)
        dvec[net.g_bus[i]] += 10.0
        dvec[net.g_bus[j]] -= 10.0
        pred = (g.ptdf + np.outer(lv, g.ptdf[k])) @ dvec
        act = mat1.P[k] - mat0.P[k]
        for l in np.argsort(-np.abs(pred))[:3]:
            if l != k and abs(pred[l]) > 1.0 and np.isfinite(act[l]):
                ratios.append(abs(act[l] - pred[l]) / abs(pred[l]))
    ratios = np.array(ratios)
    print(f"DC-sensitivity vs AC response: n={len(ratios)} median rel.err={np.median(ratios):.3f} p90={np.percentile(ratios, 90):.3f}")
    assert len(ratios) > 30 and np.median(ratios) < 0.15


def test_plan_reduces_ac_overload_and_respects_limits(bb118):
    net = Net.from_busbranch(bb118)
    r = sec.plan(net, emergency=1.15, rounds=4)
    b, a = r["before"], r["after"]
    print(f"before: critical={b['critical']} excess={b['total_excess_pct']:.0f}%  after: critical={a['critical']} excess={a['total_excess_pct']:.0f}%  "
          f"redispatch={r['redispatch_mw']} MW shed={r['shed_mw']} MW cost={r['cost_change_per_h']}/h status={r['status']}")
    assert r["status"] in ("secured", "improved")
    assert a["total_excess_pct"] < 0.2 * b["total_excess_pct"]
    assert a["critical"] < b["critical"] and a["base_converged"] and a["base_overloads"] == 0
    gens = {g: i for i, g in enumerate(net.g_id)}
    d_total = 0.0
    for act in r["actions"]:
        if act["type"] == "redispatch":
            i = gens[act["element"]]
            assert net.g_pmin[i] - 1e-6 <= act["to_mw"] <= net.g_pmax[i] + 1e-6
            assert act["to_mw"] - act["from_mw"] == pytest.approx(act["delta_mw"], abs=0.02)
            d_total += act["delta_mw"]
    shed = sum(a_["mw"] for a_ in r["actions"] if a_["type"] == "load_shed")
    assert d_total == pytest.approx(-shed, abs=0.5 + 0.01 * len(r["actions"]))     # generation change = -shed (balance)


def test_secure_system_needs_no_actions(bb118):
    r = sec.plan(Net.from_busbranch(bb118), emergency=20.0)
    assert r["status"] == "secure" and r["actions"] == []


def test_switching_only_and_move_limit(bb118):
    net = Net.from_busbranch(bb118)
    r = sec.plan(net, emergency=1.15, use_redispatch=False, use_shed=False, use_switching=True)
    assert all(a["type"] == "switching" for a in r["actions"])
    for a in r["actions"]:
        sub = net.without_branches([net.br_id.index(a["element"])])
        assert len(sub.island_ref) == len(net.island_ref)               # switching never islands
    r2 = sec.plan(net, emergency=1.15, use_switching=False, use_shed=False, max_move_mw=30.0, rounds=3)
    assert all(abs(a["delta_mw"]) <= 30.0 + 1e-6 for a in r2["actions"] if a["type"] == "redispatch")
    assert not any(a["type"] == "load_shed" for a in r2["actions"])


def test_matrix_matches_n1_sweep(bb118):
    """The AC matrix must agree with the existing N-1 engine on loading for every converged outage."""
    from ems import contingency as ca
    net = Net.from_busbranch(bb118)
    base = pf.solve(net)
    m = sec.ACMatrix(net, base["V"])
    r = ca.run_n1(net)
    mx = {c["outage"]: c["max_loading_pct"] for c in r["results"] if c["converged"] and c["max_loading_pct"] is not None}
    _, load = m.excess_pct(1.15)
    for k in range(0, net.nl, 7):
        if net.br_id[k] in mx and m.status[k] != "nonconverged":
            assert np.nanmax(load[k]) == pytest.approx(mx[net.br_id[k]], rel=1e-6)
