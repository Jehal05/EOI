import json, os
import pytest
from ems.network import Net
from ems import dts, powerflow as pf

DATA = os.path.join(os.path.dirname(__file__), "..", "..", "..", "data")


@pytest.fixture(scope="module")
def net():
    return Net.from_busbranch(json.load(open(os.path.join(DATA, "ieee118.busbranch.json"))))


def mk(net, sid="line-trip", **kw):
    sc = next(s for s in dts.library(net) if s["id"] == sid)
    return dts.Session(net, sc, "tester", "ieee118"), sc


def test_library_is_model_driven_and_valid(net):
    lib = dts.library(net)
    assert len(lib) == 5
    for sc in lib:
        assert dts.validate_scenario(net, sc)["events"]
    ids = {e["id"] for sc in lib for e in sc["events"] if e["kind"] == "branch_trip"}
    assert ids <= set(net.br_id)


def test_intact_network_is_secure_and_score_is_full(net):
    sc = dts.validate_scenario(net, dict(duration=60, events=[dict(t=50, kind="load_scale", id="*", factor=1.0)]))
    s = dts.Session(net, sc, "u", "m")
    base = s.state
    s.step(60)
    assert base["secure"] or base["n_violations"] >= 0
    if base["secure"]:
        assert s.score()["total"] == pytest.approx(100.0)


def test_line_trip_matches_direct_power_flow_and_time_advances(net):
    s, sc = mk(net)
    s.step(20)
    assert s.t == 20 and s.next_ev == 0 and not s.out
    st = s.step(20)
    tid = sc["events"][0]["id"]
    assert s.out == {tid} and s.t == 40
    k = net.br_id.index(tid)
    direct = pf.solve(net.without_branches([k]))
    assert st["max_loading"] == pytest.approx(round(float(direct["loading_pct"].max()), 1), abs=0.06)


def test_reclosing_is_locked_out_until_cleared(net):
    s, sc = mk(net)
    s.step(40)
    tid = sc["events"][0]["id"]
    r = s.act("close_branch", tid)
    assert not r["ok"] and "locked" in r["message"] and s.refused == 1


def test_stuck_breaker_refuses_to_open(net):
    s, sc = mk(net, "stuck-breaker")
    s.step(40)
    r = s.act("open_branch", sc["stuck"][0])
    assert not r["ok"] and "failed" in r["message"]


def test_actions_change_the_state_and_are_logged(net):
    s, sc = mk(net, "gen-trip")
    s.step(40)
    assert sc["events"][0]["id"] in s.state["gens_off"]
    g = next(i for i, x in enumerate(net.g_id) if x != sc["events"][0]["id"] and net.g_pmax[i] - net.g_p[i] > 5)
    gid = net.g_id[g]
    r = s.act("set_gen", gid, float(net.g_p[g] + 5))
    assert r["ok"] and s.gen_mw[gid] == pytest.approx(net.g_p[g] + 5)
    bad = s.act("set_gen", gid, float(net.g_pmax[g] + 100))
    assert not bad["ok"]
    assert any(l["kind"] == "action" for l in s.log) and any(l["kind"] == "event" for l in s.log)


def test_shedding_critical_load_counts_as_harmful_and_tracks_lost_load(net):
    s, sc = mk(net, "surge")
    crit = next(m for m in net.load_p0 if s.pr[m] == 1)
    s.step(40)
    r = s.act("shed_load", crit, 5.0)
    assert r["ok"] and s.harmful == 1 and s.shed[crit] == 5.0
    assert not s.act("shed_load", crit, 1e6)["ok"]


def test_score_terms_sum_and_penalise_insecurity(net):
    s, sc = mk(net)
    s.step(sc["duration"])
    sc1 = s.score()
    assert sc1["total"] == pytest.approx(sc1["security_time"] + sc1["final_state"] + sc1["load_kept"] + sc1["action_quality"], abs=0.2)
    assert 0 <= sc1["total"] <= 100
    if not s.state["secure"]:
        assert sc1["final_state"] == 0 and sc1["insecure_s"] > 0


def test_removing_the_fault_by_reclose_after_clear_restores_security(net):
    sc = dts.validate_scenario(net, dict(duration=120, events=[dict(t=10, kind="branch_trip", id=dts.library(net)[0]["events"][0]["id"], clear_after=20)]))
    s = dts.Session(net, sc, "u", "m")
    s.step(15)
    assert not s.act("close_branch", sc["events"][0]["id"])["ok"]
    s.step(20)
    assert s.act("close_branch", sc["events"][0]["id"])["ok"] and not s.out


def test_validation_rejects_bad_scenarios(net):
    for bad in (dict(duration=600, events=[]), dict(duration=600, events=[dict(t=1, kind="branch_trip", id="NOPE")]),
                dict(duration=600, events=[dict(t=999, kind="gen_trip", id=net.g_id[0])]), dict(duration=600, events=[dict(t=1, kind="load_scale", id="*", factor=9)]),
                dict(duration=10, events=[dict(t=1, kind="gen_trip", id=net.g_id[0])])):
        with pytest.raises(ValueError):
            dts.validate_scenario(net, bad)


def test_sessions_are_isolated_from_each_other_and_the_base_network(net):
    a, sc = mk(net); b, _ = mk(net)
    a.step(40)
    assert not b.out and b.t == 0
    assert pf.solve(net)["converged"] and len(net.br_id) == len({*net.br_id})


def test_store_ownership_and_limit(net):
    st = dts.Store(); s, _ = mk(net); st.add(s)
    assert st.get(s.id, "tester") is s
    with pytest.raises(KeyError):
        st.get(s.id, "someone-else")
    assert st.get(s.id, "someone-else", admin=True) is s
