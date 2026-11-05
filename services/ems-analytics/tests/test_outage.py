import itertools
import json
import os
import numpy as np
import pytest
from ems.network import Net
from ems import outage, contingency as ca, powerflow as pf

DATA = os.path.join(os.path.dirname(__file__), "..", "..", "..", "data")


def make(name, bb):
    cim = outage.Cim(json.load(open(os.path.join(DATA, f"{name}.cim.json"))))
    base = Net.from_busbranch(bb)
    ids = {b: i for i, b in enumerate(base.br_id)}

    def loader(open_list):
        ks = sorted({ids[s.split("-")[1]] for s in open_list if s.startswith("BRK-") and s.split("-")[1] in ids})
        return base.without_branches(ks) if ks else base
    return cim, base, loader


@pytest.fixture(autouse=True)
def _clear():
    outage.clear_cache()


def test_isolation_finds_both_terminal_breakers_and_rejects_unknown(bb14):
    cim, base, loader = make("ieee14", bb14)
    assert [x["switch"] for x in cim.isolation("BR0001")] == ["BRK-BR0001-F", "BRK-BR0001-T"]
    assert cim.isolation("NOPE") is None
    r = outage.study(cim, loader, ["NOPE"])
    assert r["verdict"] == "invalid" and r["unknown_equipment"] == ["NOPE"]


def test_new_critical_equals_independent_pair_enumeration(bb14):
    cim, base, loader = make("ieee14", bb14)
    sb = pf.solve(base)
    single = {r["outage"]: r for r in ca.run_n1(base, base_sol=sb)["results"]}
    for k in (2, 5, 9):                                            # a few outages, compared with brute force N-2 on the same network
        eq = base.br_id[k]
        r = outage.study(cim, loader, [eq])
        n_k = base.without_branches([k])
        so = pf.solve(n_k)
        if not so["converged"] or len(n_k.island_ref) > len(base.island_ref):
            assert r["verdict"] == "unsafe"
            continue
        expect = set()
        for j in range(base.nl):
            if j == k:
                continue
            pair = ca.run_n1(n_k, base_sol=so)["results"] if False else None
        res_o = {x["outage"]: x for x in ca.run_n1(n_k, base_sol=so)["results"]}
        for j, e2 in enumerate(base.br_id):
            if j == k:
                continue
            a = res_o[e2]["severity"] > 0
            b = single[e2]["severity"] > 0
            if a and not b:
                expect.add(e2)
        got = {x["outage"] for x in r["new_critical"]}
        assert got <= expect and (len(got) == len(expect) or r["n_new_critical"] > len(r["new_critical"]))
        assert r["n_new_critical"] == len(expect)


def test_islanding_outage_is_unsafe_with_lost_load(bb118):
    cim, base, loader = make("ieee118", bb118)
    sb = pf.solve(base)
    res = ca.run_n1(base, base_sol=sb)["results"]
    isl = next(r for r in res if r["islanded"] and r["lost_load_mw"] > 1.0)
    r = outage.study(cim, loader, [isl["outage"]])
    assert r["verdict"] == "unsafe" and r["islanded_load_mw"] == pytest.approx(isl["lost_load_mw"], abs=0.2)


def test_secure_outage_has_no_findings(bb118):
    cim, base, loader = make("ieee118", bb118)
    sb = pf.solve(base)
    res = {r["outage"]: r for r in ca.run_n1(base, base_sol=sb)["results"]}
    cand = [e for e, r in res.items() if r["severity"] == 0 and not r["islanded"]][:6]
    verdicts = [outage.study(cim, loader, [e], max_new=3)["verdict"] for e in cand]
    assert set(verdicts) <= {"secure", "restricted", "unsafe"} and len(cand) > 0
    for e, v in zip(cand, verdicts):
        r = outage.study(cim, loader, [e])
        if v == "secure":
            assert r["base_violations"] == [] and r["n_new_critical"] == 0 and r["islanded_load_mw"] == 0


def test_plan_orders_by_effect_and_checks_each_step(bb14):
    cim, base, loader = make("ieee14", bb14)
    p = outage.plan(cim, loader, ["BR0001"], "isolate")
    br = [s for s in p["steps"] if not s["manual"]]
    assert [s["action"] for s in br] == ["open", "open"] and {s["switch"] for s in br} == {"BRK-BR0001-F", "BRK-BR0001-T"}
    assert all(s["tag"] == s["switch"] + ".STATE" for s in br)
    assert p["status"] == "review" and br[0]["safe"] is False                      # losing line 1-2 overloads the network
    assert any(s["manual"] and not s["in_model"] for s in p["steps"]) and p["steps"][0]["manual"]
    # the check of the first step equals a direct power flow
    ref = pf.solve(base)
    k = base.br_id.index("BR0001")
    direct = pf.solve(base.without_branches([k]))
    assert br[0]["check"]["max_loading_pct"] == pytest.approx(float(direct["loading_pct"].max()), abs=0.06)
    assert outage.plan(cim, loader, ["BR0001"], "isolate")["plan_hash"] == p["plan_hash"]


def test_restore_plan_closes_the_open_breakers(bb14):
    cim, base, loader = make("ieee14", bb14)
    live_open = ["BRK-BR0001-F", "BRK-BR0001-T"]
    p = outage.plan(cim, loader, ["BR0001"], "restore", live_open=live_open)
    br = [s for s in p["steps"] if not s["manual"]]
    assert {s["switch"] for s in br} == set(live_open) and all(s["action"] == "close" for s in br)
    assert all(s["manual"] for s in p["steps"][:3]) and br[-1]["safe"] is True and br[-1]["check"]["max_loading_pct"] == pytest.approx(float(pf.solve(base)["loading_pct"].max()), abs=0.06)


def test_segments_sweep_line():
    reqs = [dict(id=1, equipment=["A"], start=0, end=10), dict(id=2, equipment=["B"], start=5, end=15), dict(id=3, equipment=["C"], start=20, end=30)]
    s = outage.segments(reqs)
    assert [(x["start"], x["end"], x["ids"]) for x in s] == [(0, 5, [1]), (5, 10, [1, 2]), (10, 15, [2]), (20, 30, [3])]


def test_overlap_that_makes_things_worse_is_a_conflict_and_suggestion_avoids_it(bb118):
    cim, base, loader = make("ieee118", bb118)
    a, b = "BR0017", "BR0019"                                      # both feed Busbar 14 (14 MW): each restricted alone, together they island it
    va = outage.study(cim, loader, [a], max_new=1)["verdict"]; vb = outage.study(cim, loader, [b], max_new=1)["verdict"]
    vab = outage.study(cim, loader, [a], concurrent=[[b]], max_new=1)["verdict"]
    assert (va, vb, vab) == ("restricted", "restricted", "unsafe")
    assert outage.study(cim, loader, [a], concurrent=[[b]], max_new=1)["islanded_load_mw"] == pytest.approx(14.0, abs=0.2)
    reqs = [dict(id=1, equipment=[a], start=0, end=10), dict(id=2, equipment=[b], start=5, end=15)]
    sch = outage.schedule(cim, loader, reqs)
    assert [c["ids"] for c in sch["conflicts"]] == [[1, 2]]
    assert [s["escalated"] for s in sch["segments"]] == [False, True, False]
    H = 3_600_000
    sg = outage.suggest(cim, loader, [b], 5 * H, 0, 40 * H, [dict(equipment=[a], start=0, end=10 * H)], step_ms=H, want=2)
    assert sg["suggestions"] and all(s["start"] >= 10 * H for s in sg["suggestions"]) and sg["tried"] >= 5
