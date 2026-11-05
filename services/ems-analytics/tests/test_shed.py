import json, os
import numpy as np
import pytest
from ems.network import Net
from ems import shed

DATA = os.path.join(os.path.dirname(__file__), "..", "..", "..", "data")


@pytest.fixture(scope="module")
def net118():
    return Net.from_busbranch(json.load(open(os.path.join(DATA, "ieee118.busbranch.json"))))


def sys_(S=10000.0, load=8000.0, p=None):
    pm = np.full(10, S / 10)
    pg = np.full(10, load / 10) if p is None else p
    return load, pg, pm


def test_initial_rocof_matches_swing_equation():
    load, pg, pm = sys_()
    r = shed.simulate(load, pg, pm, [0], duration=2.0, params=dict(dt=0.005))
    lost = float(pg[0]); S = float(pm[1:].sum())
    expected = -50.0 * lost / (2 * 5.0 * S)
    assert r["rocof0_hz_s"] == pytest.approx(expected, rel=0.10)


def test_steady_state_matches_droop_formula():
    load, pg, pm = sys_(S=20000.0, load=8000.0)                 # large headroom so the governor never saturates
    p = dict(gov_deadband_hz=0.0, reserve_pct=100.0)
    r = shed.simulate(load, pg, pm, [0], duration=120.0, params=p)
    S = float(pm[1:].sum()); D = 1.0 * load + 1.0 * S; gain = S / 0.05
    ss = 50.0 * (-float(pg[0]) / (D + gain))
    assert r["f_final"] - 50.0 == pytest.approx(ss, rel=0.02)


def test_governor_dead_band_and_reserve_cap_limit_the_response():
    load, pg, pm = sys_(S=10000.0, load=8000.0)
    big = shed.simulate(load, pg, pm, [0, 1, 2], duration=60.0, params=dict(reserve_pct=100.0))
    capped = shed.simulate(load, pg, pm, [0, 1, 2], duration=60.0, params=dict(reserve_pct=2.0))
    assert capped["f_final"] < big["f_final"] - 0.2


def test_ufls_arrests_decline_and_stage_times_are_ordered():
    load, pg, pm = sys_(S=10000.0, load=8000.0)
    no = shed.simulate(load, pg, pm, [0, 1, 2], duration=60.0)
    yes = shed.simulate(load, pg, pm, [0, 1, 2], stages=shed.STAGES, duration=60.0)
    assert yes["f_nadir"] > no["f_nadir"] + 0.5 and yes["f_final"] > no["f_final"]
    fired = [s["fired_at"] for s in yes["stages"] if s["fired_at"] is not None]
    assert fired and fired == sorted(fired)
    assert yes["shed_mw"] == pytest.approx(sum(s["frac"] for s in yes["stages"] if s["fired_at"] is not None) * load, rel=1e-6)


def test_no_stage_fires_when_frequency_stays_above_pickup():
    load, pg, pm = sys_(S=20000.0, load=8000.0)
    r = shed.simulate(load, pg, pm, [0], stages=shed.STAGES, duration=60.0, params=dict(reserve_pct=100.0))
    assert r["shed_mw"] == 0 and all(s["fired_at"] is None for s in r["stages"])


def test_final_frequency_after_shedding_matches_analytic():
    load, pg, pm = sys_(S=20000.0, load=8000.0)
    st = [dict(f=49.9, delay=0.0, frac=0.05)]
    p = dict(gov_deadband_hz=0.0, reserve_pct=100.0, d_load=0.0)   # no load damping: shed does not change D
    r = shed.simulate(load, pg, pm, [0, 1, 2], stages=st, duration=150.0, params=p)
    S = float(pm[3:].sum()); D = 1.0 * S; gain = S / 0.05
    ss = 50.0 * ((-float(pg[:3].sum()) + 0.05 * load) / (D + gain))
    assert r["shed_mw"] == pytest.approx(400.0) and r["f_final"] - 50.0 == pytest.approx(ss, rel=0.03)


def test_priority_table_is_deterministic_and_complete(net118):
    a = shed.priority_table(net118); b = shed.priority_table(net118)
    assert a == b and sum(c["loads"] for c in a["classes"].values()) == len(net118.load_p0)
    assert sum(c["mw"] for c in a["classes"].values()) == pytest.approx(a["total_mw"], abs=1.0)
    m = next(iter(net118.load_p0))
    o = shed.priority_table(net118, {m: 1})
    assert next(r for r in o["loads"] if r["load"] == m)["cls"] == 1


def test_ufls_scenarios_on_118_report_with_and_without(net118):
    r = shed.ufls(net118)
    assert len(r["scenarios"]) >= 2 and r["synthetic_dynamics"] and r["stage_table_is_default"]
    big = max(r["scenarios"], key=lambda s: s["lost_mw"])
    assert big["with_ufls"]["f_nadir"] > big["without"]["f_nadir"] and big["with_ufls"]["shed_mw"] > 0
    assert len({s["lost_mw"] for s in r["scenarios"]}) == len(r["scenarios"])


def test_roster_meets_deficit_never_sheds_critical_and_rotates(net118):
    r = shed.roster(net118, deficit_mw=150.0, hours=12, ac_check=False)
    pr = shed.assign_priority(net118.load_p0)
    assert r["feasible"]
    for p in r["periods"]:
        assert abs(p["shed_mw"] - 150.0) <= 8.0 or p["shortfall_mw"] == 0.0
        assert all(pr[m] >= 3 for m in p["loads"])
    assert sum(r["ens_mwh"].values()) == pytest.approx(r["total_ens_mwh"], abs=0.2 * len(r["ens_mwh"]))
    assert sum(sum(net118.load_p0[m] for m in p["loads"]) for p in r["periods"]) == pytest.approx(r["total_ens_mwh"], abs=0.5)
    s = shed.roster(net118, deficit_mw=150.0, hours=12, rotate=False, ac_check=False)
    assert r["loads_ever_shed"] > 5 * s["loads_ever_shed"] and r["jain_fairness"] > 5 * s["jain_fairness"] and r["max_ens_mwh"] < s["max_ens_mwh"]


def test_roster_is_deterministic_and_flags_infeasible(net118):
    a = shed.roster(net118, deficit_mw=200.0, hours=4, ac_check=False); b = shed.roster(net118, deficit_mw=200.0, hours=4, ac_check=False)
    assert a == {**b, "wall_seconds": a["wall_seconds"]}
    big = shed.roster(net118, deficit_mw=9000.0, hours=2, ac_check=False)
    assert not big["feasible"] and big["periods"][0]["shortfall_mw"] > 0


def test_roster_ac_check_matches_independent_power_flow(net118):
    import dataclasses
    from ems import powerflow as pf
    r = shed.roster(net118, deficit_mw=150.0, hours=2)
    p = r["periods"][0]
    pd2, qd2 = net118.pd.copy(), net118.qd.copy()
    for m in p["loads"]:                                        # remove each shed load from its bus, keep the power factor of the rest
        b = net118.load_bus[m]
        keep = 1.0 - net118.load_p0[m] / net118.pd[b] if net118.load_p0[m] < net118.pd[b] else 0.0
        pd2[b] *= keep; qd2[b] *= keep
    gp = net118.g_p * (1 - 150.0 / net118.g_p.sum())
    s = pf.solve(dataclasses.replace(net118, pd=pd2, qd=qd2, g_p=gp))
    assert p["ac"]["converged"] and p["ac"]["max_loading_pct"] == pytest.approx(float(s["loading_pct"].max()), abs=0.06)
    assert r["ac_base"]["converged"] and r["ac_base"]["max_loading_pct"] == pytest.approx(float(pf.solve(net118)["loading_pct"].max()), abs=0.06)
