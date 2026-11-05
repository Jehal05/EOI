import itertools
import numpy as np
from ems.network import Net
from ems import powerflow as pf, contingency as ca, n2


def _ac_all(net, base, pairs, vmin=0.95, vmax=1.05):
    bo, bv = ca.violations(net, base, vmin, vmax)
    ca._init(net, base["V"], ({b: l for b, l in bo}, {b: v for b, v in bv}), vmin, vmax, False)
    return {tuple(int(x) for x in p): ca._one(tuple(int(x) for x in p)) for p in pairs}


def _dc_flows(d, inj):
    th = np.zeros(d.net.nb)
    th[d.keep] = d.lu.solve(inj[d.keep])
    return d.bl * (d.A @ th)


def test_dc_lodf_closed_form_equals_dc_resolve(bb14):
    """With DC-consistent base flows, the 2-outage closed form must equal re-solving the DC network."""
    net = Net.from_busbranch(bb14)
    dc = n2.DC(net)
    rng = np.random.default_rng(4)
    inj = rng.normal(size=net.nb) * 30
    inj[net.slack_bus] = 0
    inj[net.slack_bus] = -inj.sum()
    f0 = _dc_flows(dc, inj)
    checked = 0
    for a, b in itertools.combinations(range(net.nl), 2):
        sub = net.without_branches([a, b])
        if len(sub.island_ref) > 1:
            continue
        P = dc.phi([a, b])
        D = (1 - P[a, 0]) * (1 - P[b, 1]) - P[a, 1] * P[b, 0]
        da = ((1 - P[b, 1]) * f0[a] + P[a, 1] * f0[b]) / D
        db = (P[b, 0] * f0[a] + (1 - P[a, 0]) * f0[b]) / D
        post = f0 + P[:, 0] * da + P[:, 1] * db
        keep = [k for k in range(net.nl) if k not in (a, b)]
        ref = _dc_flows(n2.DC(sub), inj)
        assert np.allclose(post[keep], ref, atol=1e-8 * max(1.0, np.abs(ref).max())), (a, b)
        checked += 1
    assert checked >= 100
    print("closed-form vs DC re-solve: pairs checked =", checked)


def _rank_quality(net, base, pairs, vmin=0.95, vmax=1.05):
    from scipy.stats import spearmanr
    dc = n2.DC(net)
    sev, isl, dcmax = n2.dc_screen(net, base, dc, pairs)
    ac = _ac_all(net, base, pairs, vmin, vmax)
    keys = [tuple(int(x) for x in p) for p in pairs]
    sel = [i for i in range(len(keys)) if not isl[i] and ac[keys[i]]["converged"] and ac[keys[i]]["max_loading_pct"] is not None]
    acs = np.array([ac[keys[i]]["max_loading_pct"] for i in sel])
    dcs = np.array([dcmax[i] for i in sel])
    rho = spearmanr(dcs, acs)[0]
    nt = max(5, len(sel) // 10)                       # AC worst 10%
    acw = set(np.argsort(-acs)[:nt])
    dcw = set(np.argsort(-dcs)[: 2 * nt])             # found within the DC worst 20%
    recall = len(acw & dcw) / nt
    # islanding detection must be exact
    ac_isl = np.array([ac[k]["islanded"] for k in keys])
    return rho, recall, nt, float(np.abs(acs - dcs).mean()), bool((ac_isl == isl).all())


def test_n2_quality_vs_full_ac_ieee14(bb14):
    net = Net.from_busbranch(bb14)
    base = pf.solve(net)
    pairs = np.array(list(itertools.combinations(range(net.nl), 2)))
    rho, recall, nt, mae, isl_ok = _rank_quality(net, base, pairs)
    print(f"IEEE-14 exhaustive {len(pairs)} pairs: spearman={rho:.3f} recall(AC worst10% in DC worst20%)={recall:.2f} "
          f"MAE loading={mae:.1f}pp islanding exact={isl_ok}")
    assert isl_ok and rho >= 0.9 and recall >= 0.8


def test_n2_quality_vs_full_ac_ieee118_candidates(bb118):
    net = Net.from_busbranch(bb118)
    base = pf.solve(net)
    dc = n2.DC(net)
    pairs, _ = n2.candidates(net, base, dc, 25)
    assert len(pairs) < net.nl * (net.nl - 1) // 2 / 5          # real reduction of the search space
    rho, recall, nt, mae, isl_ok = _rank_quality(net, base, pairs)
    print(f"IEEE-118 {len(pairs)} candidate pairs of {net.nl*(net.nl-1)//2}: spearman={rho:.3f} "
          f"recall={recall:.2f} (n={nt}) MAE loading={mae:.1f}pp islanding exact={isl_ok}")
    assert isl_ok and rho >= 0.8 and recall >= 0.7


def test_run_n2_end_to_end_and_islanding(bb118):
    net = Net.from_busbranch(bb118)
    r = n2.run_n2(net, workers=1, k_top=20, verify=30)
    assert r["n_ac_verified"] >= 30 and r["n_candidates"] < r["n_pairs_total"]
    ver = [x for x in r["results"] if x["ac_verified"]]
    assert [x["rank_key"] for x in ver] == sorted((x["rank_key"] for x in ver), reverse=True)
    assert all("dc_severity" in x for x in r["results"])


def test_split_network_screens_main_island(bb118):
    net = Net.from_busbranch(bb118)
    # open a radial branch to create a split, N-2 must still run on the energised main island
    for k in range(net.nl):
        n1 = net.without_branches([k])
        if len(n1.island_ref) > 1:
            break
    r = n2.run_n2(n1, workers=1, k_top=10, verify=10)
    assert r["n_candidates"] > 0
