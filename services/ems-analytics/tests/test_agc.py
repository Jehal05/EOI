import numpy as np
import pytest
from ems.network import Net
from ems import agc, tcm


def test_allocate_properties():
    rng = np.random.default_rng(0)
    for _ in range(300):
        n = rng.integers(1, 12)
        up, dn, w = rng.uniform(0, 20, n), rng.uniform(0, 20, n), rng.uniform(0, 5, n)
        delta = rng.uniform(-80, 80)
        x, un = agc.allocate(delta, up, dn, w)
        room = up if delta > 0 else dn
        assert (np.abs(x) <= room + 1e-9).all()
        assert (np.sign(x) * np.sign(delta) >= 0).all()
        assert x.sum() + un == pytest.approx(delta, abs=1e-8)
        feasible = abs(delta) <= (room[w > 0]).sum() - 1e-9
        if feasible and (w > 0).all():
            assert abs(un) < 1e-8                       # everything placed when it fits


def test_kron_equivalent_matches_dc_solve(bb14):
    net = Net.from_busbranch(bb14)
    S = agc.Setup(net, parts=2)
    a, b = S.reps
    nb = net.nb
    tap = np.where(net.tap != 0.0, np.abs(net.tap), 1.0)
    B = np.zeros((nb, nb))
    for k in range(net.nl):
        f, t = net.f[k], net.t[k]; y = 1 / (net.x[k] * tap[k])
        B[f, f] += y; B[t, t] += y; B[f, t] -= y; B[t, f] -= y
    inj = np.zeros(nb); inj[a], inj[b] = 1.0, -1.0
    keep = [i for i in range(nb) if i != b]
    th = np.zeros(nb); th[keep] = np.linalg.solve(B[np.ix_(keep, keep)], inj[keep])
    assert S.Beq[0, 1] == pytest.approx(1.0 / (th[a] - th[b]), rel=1e-9)


def _net(bb, parts=2):
    return Net.from_busbranch(bb)


def test_primary_only_steady_state_matches_analytic(bb118):
    net = _net(bb118)
    S = agc.Setup(net, 2)
    ev = [dict(t=1.0, kind="load_step", area=0, mw=50.0)]
    r = agc.simulate(net, parts=2, events=ev, duration=120, agc=False, setup=S)
    dev = np.mean([a["final_dev_hz"] for a in r["areas"]])
    assert dev == pytest.approx(r["reference"]["primary_only_steady_state_hz"], rel=0.01)
    assert r["areas"][0]["final_dev_hz"] == pytest.approx(r["areas"][1]["final_dev_hz"], abs=2e-4)     # one synchronous frequency


def test_initial_rocof_matches_inertia(bb118):
    net = _net(bb118)
    S = agc.Setup(net, 2)
    r = agc.simulate(net, parts=2, events=[dict(t=0.0, kind="load_step", area=0, mw=100.0)], duration=4, agc=False, setup=S, params=dict(dt=0.01))
    f = np.array(r["series"]["f"])[:, 0]; t = np.array(r["series"]["t"])
    rocof = (f[3] - f[0]) / (t[3] - t[0])
    # first moments: only the (small) tie response and governor lag differ from -dP/M
    assert rocof == pytest.approx(-100.0 * agc.F0 / S.M[0], rel=0.1)       # local swing equation before ties/governors act


def test_energy_balance_primary(bb118):
    net = _net(bb118)
    S = agc.Setup(net, 2, dict(gov_deadband_hz=0.0))
    r = agc.simulate(net, parts=2, events=[dict(t=1.0, kind="load_step", area=1, mw=40.0)], duration=150, agc=False, setup=S, params=dict(gov_deadband_hz=0.0))
    df = np.mean([a["final_dev_hz"] for a in r["areas"]]) / agc.F0
    # unit response + damping response = the load step
    assert (-df * S.Kg.sum() + -df * S.D.sum()) == pytest.approx(40.0, rel=0.02)


def test_agc_restores_frequency_and_ties_and_disturbed_area_picks_up(bb118):
    net = _net(bb118)
    S = agc.Setup(net, 2)
    ev = [dict(t=5.0, kind="load_step", area=0, mw=100.0)]
    off = agc.simulate(net, parts=2, events=ev, duration=600, agc=False, setup=S)
    on = agc.simulate(net, parts=2, events=ev, duration=600, agc=True, setup=S)
    assert abs(off["areas"][0]["final_dev_hz"]) > 0.01
    for a in on["areas"]:
        assert abs(a["final_dev_hz"]) < 0.004
        assert abs(a["final_tie_dev_mw"]) < 1.0 and abs(a["final_ace_mw"]) < 1.0
    assert on["areas"][0]["nadir_hz"] >= off["areas"][0]["nadir_hz"] - 1e-6
    assert abs(on["areas"][0]["final_dev_hz"]) < abs(off["areas"][0]["final_dev_hz"]) / 5


def test_gen_trip_and_saturation_reported(bb118):
    net = _net(bb118)
    S = agc.Setup(net, 2)
    big = int(np.argmax(S.p0))
    r = agc.simulate(net, parts=2, events=[dict(t=2.0, kind="gen_trip", unit=S.ids[big])], duration=400, agc=True, setup=S)
    assert min(a["nadir_hz"] for a in r["areas"]) < agc.F0
    # impossible disturbance (exceeds all regulating room): ACE cannot be restored and this is visible
    huge = agc.simulate(net, parts=2, events=[dict(t=2.0, kind="load_step", area=0, mw=3000.0)], duration=600, agc=True, setup=S)
    assert huge["areas"][0]["final_dev_hz"] < -0.05 or huge["units_at_limit"] > 0


def test_modes_all_work_and_respect_limits(bb118):
    net = _net(bb118)
    for mode in ("range", "equal", "economic"):
        r = agc.simulate(net, parts=2, events=[dict(t=2.0, kind="load_step", area=1, mw=30.0)], duration=300, agc=True, params=dict(mode=mode))
        assert abs(r["areas"][1]["final_ace_mw"]) < 2.0


def test_deterministic(bb118):
    net = _net(bb118)
    ev = [dict(t=2.0, kind="load_ramp", area=0, mw=30.0, duration=60.0)]
    a = agc.simulate(net, parts=3, events=ev, duration=120); b = agc.simulate(net, parts=3, events=ev, duration=120)
    assert a["series"] == b["series"]



def test_instability_is_detected_not_hidden(bb118):
    net = _net(bb118)
    r = agc.simulate(net, parts=4, events=[dict(t=5, kind="load_step", area=0, mw=40.0)], duration=240, agc=False, params=dict(d_gen=0.0, gov_deadband_hz=0.0))
    assert r["stability"] == "oscillatory_unstable" and any("NOT credible" in n for n in r["notes"])
    ok = agc.simulate(net, parts=4, events=[dict(t=5, kind="load_step", area=0, mw=40.0)], duration=240, agc=False)
    assert ok["stability"] == "ok"


def test_governor_deadband_shifts_steady_state_as_analytic(bb118):
    net = _net(bb118)
    S0 = agc.Setup(net, 2, dict(gov_deadband_hz=0.0)); S1 = agc.Setup(net, 2, dict(gov_deadband_hz=0.03))
    ev = [dict(t=1.0, kind="load_step", area=0, mw=80.0)]
    a = agc.simulate(net, parts=2, events=ev, duration=150, agc=False, setup=S0, params=dict(gov_deadband_hz=0.0))
    b = agc.simulate(net, parts=2, events=ev, duration=150, agc=False, setup=S1)
    assert abs(b["areas"][0]["final_dev_hz"]) > abs(a["areas"][0]["final_dev_hz"])
    assert b["areas"][0]["final_dev_hz"] == pytest.approx(b["reference"]["primary_only_steady_state_hz"], rel=0.02)
