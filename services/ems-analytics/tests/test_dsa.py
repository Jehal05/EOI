import numpy as np
import pytest
from ems.network import Net
from ems import dsa, powerflow as pf


def two_bus(pl=50.0, ql=0.0, x=0.5, pg=0.0):
    """Slack generator (bus 0) feeding a load (bus 1) over a lossless line, base 100 MVA."""
    bus = lambda i, t, pd, qd: dict(idx=i, name=f"B{i+1}", baseKV=132.0, pd=pd, qd=qd, gs=0.0, bs=0.0, type=t, island=0, nodes=[f"CN{i}"], substation=f"SS{i}")
    return {"baseMVA": 100.0, "buses": [bus(0, 3, 0.0, 0.0), bus(1, 1, pl, ql)],
            "branches": [dict(mrid="BR0001", kind="line", **{"from": 0, "to": 1}, r=0.0, x=x, b=0.0, tap=0.0, shift=0.0, rate=500.0)],
            "gens": [dict(mrid="G001", bus=0, p=pl, q=0.0, pmax=1000.0, pmin=0.0, qmax=1e4, qmin=-1e4, vset=1.0)],
            "loads": [dict(mrid="L0002", bus=1, p=pl, q=ql)], "dropped": [], "modelId": "t", "version": 1,
            "islands": [dict(id=0, buses=[0, 1], dead=False, genMaxMW=1000.0, hasSlack=True, loadMW=pl, refBus=0)]}


def smib(pg=80.0, x=0.4):
    """Generator at bus 0 (finite inertia) and an 'infinite' machine at bus 1 (huge rating and inertia)."""
    bb = two_bus(pl=0.0, x=x)
    bb["buses"][1]["type"] = 3; bb["buses"][0]["type"] = 2
    bb["gens"] = [dict(mrid="G001", bus=0, p=pg, q=10.0, pmax=100.0, pmin=0.0, qmax=1e4, qmin=-1e4, vset=1.0),
                  dict(mrid="G002", bus=1, p=-pg, q=0.0, pmax=1e6, pmin=-1e6, qmax=1e6, qmin=-1e6, vset=1.0)]
    bb["loads"] = []
    bb["islands"][0].update(refBus=1, loadMW=0.0)
    return bb


# ---------------------------------------------------------------- voltage stability
def test_loading_margin_matches_closed_form_two_bus():
    net = Net.from_busbranch(two_bus(pl=50.0, x=0.5))
    r = dsa.loading_margin(net, enforce_q=False, tol=2e-4, lam_max=2.0, step0=0.1)
    pmax_mw = 100.0 * 1.0 ** 2 / (2 * 0.5)               # E^2 / (2 X) for a unity-power-factor load
    assert (1 + r["lam"]) * 50.0 == pytest.approx(pmax_mw, rel=0.01)
    assert r["lam"] <= pmax_mw / 50.0 - 1 + 1e-6          # never beyond the true nose


def test_loading_margin_closed_form_with_reactive_load():
    net = Net.from_busbranch(two_bus(pl=40.0, ql=20.0, x=0.4))
    r = dsa.loading_margin(net, enforce_q=False, tol=2e-4, lam_max=3.0, step0=0.1)
    phi = np.arctan2(20.0, 40.0)                          # constant power factor scaling
    pmax = 100.0 / (2 * 0.4) * np.cos(phi) / (1 + np.sin(phi))
    assert (1 + r["lam"]) * 40.0 == pytest.approx(pmax, rel=0.015)


def test_vsa_118_margin_is_a_real_limit(bb118):
    net = Net.from_busbranch(bb118)
    r = dsa.vsa(net, n1_top=4)
    b = r["base"]
    assert b["converged"] and b["margin_pct"] > 5 and b["pv_curve"][0]["lam"] == 0.0
    assert pf.solve(dsa._scaled(net, b["lam"], net.g_p), enforce_q_limits=True)["converged"]
    beyond = pf.solve(dsa._scaled(net, b["lam"] + 0.15, net.g_p), enforce_q_limits=True)
    assert (not beyond["converged"]) or beyond["vm"][beyond["vm"] > 0].min() < 0.5 or b["lam"] >= 2.99
    assert all(x["margin_pct"] > 0 for x in r["n1"])


# ---------------------------------------------------------------- transient stability
def test_equilibrium_is_stationary(bb118):
    net = Net.from_busbranch(bb118)
    dy = dsa.Dyn(net)
    ok, sep, _, _ = dsa._simulate(dy, dy.Ypre, dy.Ypre, 0.0, 2.0, 0.01)
    Hs = dy.H * dy.S
    init = float(np.abs(dy.delta0 - (Hs * dy.delta0).sum() / Hs.sum()).max())
    assert ok and abs(sep - init) < 1e-6          # nothing moves


def _smib_dyn(**kw):
    net = Net.from_busbranch(smib(**kw))
    return net, dsa.Dyn(net, dict(H=[5.0, 1e5], D=0.0, dt=0.002, t_end=3.0))


def test_smib_cct_matches_equal_area_criterion():
    net, dy = _smib_dyn()
    Pm = dy.Pm[0]
    B12 = abs(dy.Ypre[0, 1].imag)
    Pmax = dy.Em[0] * dy.Em[1] * B12
    d0 = dy.delta0[0] - dy.delta0[1]
    assert Pm == pytest.approx(Pmax * np.sin(d0), rel=1e-6)
    dc = np.arccos((np.pi - 2 * d0) * np.sin(d0) - np.cos(d0))
    tc = np.sqrt(2 * dy.M[0] * (dc - d0) / (2 * np.pi * dsa.F0 * Pm))
    cct = dsa.critical_clearing_time(dy, 0, None, tol=0.0005)
    assert cct == pytest.approx(tc, abs=0.004)
    for f, stable in ((0.9, True), (1.1, False)):
        r, _, _ = dsa.simulate_fault(dy, 0, f * tc)
        assert r["stable"] is stable


def test_118_cct_brackets_stability_and_longer_is_worse(bb118):
    net = Net.from_busbranch(bb118)
    r = dsa.tsa(net, fault_buses=[10, 20, 30], t_clear=0.1, trip_branch=False, cct_top=3, params=dict(dt=0.01, t_end=2.0))
    dy = dsa.Dyn(net, dict(dt=0.01, t_end=2.0))
    for x in r["results"]:
        c = x["cct_s"]
        if c is None or c < 0.02:
            continue
        a = dsa.simulate_fault(dy, x["bus_idx"], 0.85 * c)[0]; b = dsa.simulate_fault(dy, x["bus_idx"], 1.15 * c + 0.004)[0]
        assert a["stable"] and not b["stable"]
    assert r["trajectory"]["angle_deg"] and len(r["trajectory"]["t"]) > 10


# ---------------------------------------------------------------- small signal
def test_K_matrix_matches_finite_difference(bb14):
    net = Net.from_busbranch(bb14)
    dy = dsa.Dyn(net)
    K = dy.K(dy.delta0, dy.Ypre)
    h = 1e-6
    for j in range(dy.ng):
        d = dy.delta0.copy(); d[j] += h
        num = (dy._pe(d, dy.Ypre) - dy._pe(dy.delta0, dy.Ypre)) / h
        assert np.allclose(K[:, j], num, atol=1e-4)
    assert np.allclose(K.sum(axis=1), 0, atol=1e-9)         # rigid-body invariance


def test_smib_swing_mode_matches_analytic_frequency_and_damping():
    net = Net.from_busbranch(smib())
    r = dsa.ssa(net, params=dict(H=[5.0, 1e5], D=0.0))
    dy = dsa.Dyn(net, dict(H=[5.0, 1e5], D=0.0))
    K = dy.Em[0] * dy.Em[1] * abs(dy.Ypre[0, 1].imag) * np.cos(dy.delta0[0] - dy.delta0[1])
    fn = np.sqrt(2 * np.pi * dsa.F0 * K / dy.M[0]) / (2 * np.pi)
    f = [m["freq_hz"] for m in r["modes"] if m["freq_hz"] > 0.05]
    assert f and f[0] == pytest.approx(fn, rel=0.005)
    assert abs(r["modes"][0]["damping_ratio"]) < 1e-6
    d = dsa.ssa(net, params=dict(H=[5.0, 1e5], D=4.0))
    dd = dsa.Dyn(net, dict(H=[5.0, 1e5], D=4.0))
    wn = np.sqrt(2 * np.pi * dsa.F0 * K / dd.M[0])
    zeta = dd.D[0] / (2 * dd.M[0] * wn)                        # d2(delta)/dt2 = -(w0 K/M) delta - (D/M) d(delta)/dt
    got = [m["damping_ratio"] for m in d["modes"] if m["freq_hz"] > 0.05][0]
    assert got == pytest.approx(zeta, rel=0.01)


def test_118_modes_are_stable_with_damping_and_undamped_without(bb118):
    net = Net.from_busbranch(bb118)
    a = dsa.ssa(net)
    assert a["n_unstable"] == 0 and a["min_damping_ratio"] > 0
    z = dsa.ssa(net, params=dict(D=0.0))
    assert abs(z["min_damping_ratio"]) < 1e-5 and z["n_unstable"] == 0
    assert 0.2 < a["freq_range_hz"][0] and a["freq_range_hz"][1] < 40
