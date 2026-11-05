"""Item 17: robust cold start and sparse bad-data identification at transmission-system scale (synthetic tiled IEEE-118)."""
import json, os, sys, time
import numpy as np
ROOT = os.path.join(os.path.dirname(__file__), "..", "..", "..")
sys.path.insert(0, os.path.join(ROOT, "tools"))
import bench_scale
from ems.network import Net
from ems import powerflow as pf, state_estimator as se

BB = json.load(open(os.path.join(ROOT, "data", "ieee118.busbranch.json")))


def test_cold_start_converges_at_10k_buses_and_matches_warm_solution():
    net = Net.from_busbranch(bench_scale.tiled(BB, 85))
    assert net.nb > 10000
    cold = pf.solve(net)
    assert cold["converged"], cold["max_mismatch"]
    assert cold["strategy"] != "newton"        # plain flat-start Newton diverges here; a fallback did the work
    warm = pf.solve(net, V0=cold["V"])
    assert warm["converged"] and warm["iterations"] <= 2
    assert np.max(np.abs(warm["vm"] - cold["vm"])) < 1e-8
    assert cold["max_mismatch"] <= 1e-9


def test_easy_case_still_uses_plain_newton():
    r = pf.solve(Net.from_busbranch(BB))
    assert r["strategy"] == "newton" and r["converged"]


def test_hopeless_case_reports_failure_not_garbage():
    d = json.loads(json.dumps(BB))
    for b in d["buses"]:
        b["pd"] *= 30; b["qd"] *= 30        # far beyond the maximum loadability
    r = pf.solve(Net.from_busbranch(d))
    assert r["converged"] is False


def test_sparse_screening_matches_dense_on_candidates_and_finds_the_gross_error():
    net = Net.from_busbranch(BB)
    sol = pf.solve(net)
    gross = {"PF:BR0050": 25.0}
    ms = se.simulate_measurements(net, sol, np.random.default_rng(3), gross=gross)
    dense = se.estimate(net, ms, bad_data=False)
    sparse = se.estimate(net, ms, bad_data=False, dense_limit=0, k_candidates=40)
    assert dense["rn_method"] == "dense" and sparse["rn_method"] == "sparse-candidates"
    assert dense["top_residuals"][0]["id"] == "PF:BR0050" == sparse["top_residuals"][0]["id"]
    assert abs(dense["top_residuals"][0]["normalized"] - sparse["top_residuals"][0]["normalized"]) < 1e-6
    # the full iterative removal gives the same removal with either method
    d2 = se.estimate(net, ms); s2 = se.estimate(net, ms, dense_limit=0, k_candidates=40)
    assert [x["id"] for x in d2["removed"]] == [x["id"] for x in s2["removed"]] == ["PF:BR0050"]


def test_bad_data_identified_on_a_5000_bus_system_with_the_sparse_path():
    net = Net.from_busbranch(bench_scale.tiled(BB, 43))
    assert net.nb > 5000
    sol = pf.solve(net)
    ms = se.simulate_measurements(net, sol, np.random.default_rng(5), gross={"PF:BR0050#20": 30.0, "VM:" + net.bus_name[3000]: 15.0})
    t0 = time.perf_counter()
    r = se.estimate(net, ms)
    dt = time.perf_counter() - t0
    assert r["rn_method"] == "sparse-candidates" and r["converged"]
    assert {x["id"] for x in r["removed"]} == {"PF:BR0050#20", "VM:" + net.bus_name[3000]}, r["removed"]
    assert not r["bad_data_detected"]
    assert dt < 120
    print(f"5074-bus SE with bad-data identification: {dt:.1f} s")


def test_clean_data_at_scale_is_not_falsely_rejected():
    net = Net.from_busbranch(bench_scale.tiled(BB, 43))
    sol = pf.solve(net)
    wrong = 0
    for seed in range(4):
        r = se.estimate(net, se.simulate_measurements(net, sol, np.random.default_rng(100 + seed)))
        wrong += len(r["removed"])
    print("false removals over 4 clean 5074-bus runs:", wrong)
    assert wrong <= 1        # Bonferroni at alpha = 0.01 per run: expected 0.04 per run
