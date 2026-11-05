import numpy as np
import pytest
from ems.network import Net
from ems import shortcircuit as sc

A = sc.A_OP


def two_bus(x_line=0.1, pmax=85.0, kv=132.0):
    """Gen (S = 100 MVA -> x''d = 0.18 pu) at bus 0, reactive line to bus 1, no load."""
    return Net.from_busbranch(dict(
        baseMVA=100.0,
        buses=[dict(name="B0", baseKV=kv, pd=0, qd=0, gs=0, bs=0), dict(name="B1", baseKV=kv, pd=0, qd=0, gs=0, bs=0)],
        branches=[dict(mrid="L01", **{"from": 0, "to": 1}, r=0.0, x=x_line, b=0.0, tap=0.0, shift=0.0, rate=100.0)],
        gens=[dict(mrid="G0", bus=0, p=50, q=0, qmax=10, qmin=-10, vset=1.0, pmax=pmax, pmin=0)],
        islands=[dict(hasSlack=True, refBus=0)]))


def test_two_bus_hand_calculation():
    """Hand calc: Z1=Z2=j(0.18+0.10)=j0.28, Z0=j(0.09+0.30)=j0.39, c=1.1, Ibase=100/(sqrt3*132)=0.43740 kA."""
    r = sc.analyze(two_bus(), [1], c=1.1)["results"][0]
    ib = 100.0 / (np.sqrt(3) * 132.0)
    z1, z2, z0 = 0.28, 0.28, 0.39
    assert r["ik3_ka"] == pytest.approx(1.1 / z1 * ib, rel=1e-9)
    assert r["ik_lg_ka"] == pytest.approx(3 * 1.1 / (z0 + z1 + z2) * ib, rel=1e-9)
    assert r["ik_ll_ka"] == pytest.approx(np.sqrt(3) * 1.1 / (z1 + z2) * ib, rel=1e-9)
    zp = z2 * z0 / (z2 + z0)
    i1 = 1.1 / (z1 + zp)
    i1v = complex(0, -1) * i1  # phase irrelevant for magnitudes
    i2 = -i1 * z0 / (z2 + z0)
    i0 = -i1 * z2 / (z2 + z0)
    assert r["ik_llg_earth_ka"] == pytest.approx(abs(3 * i0) * ib, rel=1e-9)
    assert r["ik_llg_ka"] == pytest.approx(max(abs(i0 + A ** 2 * i1 + A * i2), abs(i0 + A * i1 + A ** 2 * i2)) * ib, rel=1e-9)
    assert r["x_over_r"] > 1e6   # purely reactive network
    assert r["kappa"] == pytest.approx(2.0, abs=1e-6)   # R/X -> 0 gives the maximum peak factor


def _phase_domain(kind, z0, z1, z2, c):
    """Independent derivation: build Zabc = T Zseq T^-1 and impose the fault boundary conditions directly."""
    T = np.array([[1, 1, 1], [1, A ** 2, A], [1, A, A ** 2]])          # [Ia,Ib,Ic] = T [I0,I1,I2]
    Z = T @ np.diag([z0, z1, z2]) @ np.linalg.inv(T)
    E = c * np.array([1, A ** 2, A])
    # unknowns: I (3) ; equations: V = E - Z I ; plus boundary conditions
    M = np.zeros((3, 3), dtype=complex)
    rhs = np.zeros(3, dtype=complex)
    if kind == "3ph":
        M, rhs = Z, E
    elif kind == "lg":      # Va=0, Ib=Ic=0
        M = np.array([Z[0], [0, 1, 0], [0, 0, 1]], dtype=complex)
        rhs = np.array([E[0], 0, 0])
    elif kind == "ll":      # Ia=0, Ic=-Ib, Vb=Vc
        M = np.array([[1, 0, 0], [0, 1, 1], Z[1] - Z[2]], dtype=complex)
        rhs = np.array([0, 0, E[1] - E[2]])
    elif kind == "llg":     # Ia=0, Vb=0, Vc=0
        M = np.array([[1, 0, 0], Z[1], Z[2]], dtype=complex)
        rhs = np.array([0, E[1], E[2]])
    return np.linalg.solve(M, rhs)


@pytest.mark.parametrize("kind", ["3ph", "lg", "ll", "llg"])
def test_symmetrical_components_match_independent_phase_domain(kind):
    rng = np.random.default_rng(11)
    for _ in range(25):
        z1 = complex(rng.uniform(0.001, 0.05), rng.uniform(0.05, 0.4))
        z2 = z1 * rng.uniform(0.9, 1.1)
        z0 = complex(rng.uniform(0.003, 0.2), rng.uniform(0.1, 1.2))
        i0, i1, i2 = sc.seq_currents(kind, z1, z2, z0, 1.1)
        ia, ib, ic = sc.phase_currents(i0, i1, i2)
        ref = _phase_domain(kind, z0, z1, z2, 1.1)
        assert np.allclose([ia, ib, ic], ref, atol=1e-9), (kind, [ia, ib, ic], ref)


def test_zbus_diagonal_equals_dense_inverse_ieee14(bb14):
    net = Net.from_busbranch(bb14)
    m = sc.SeqModel(net)
    for s in ("1", "2", "0"):
        Zd = np.linalg.inv(m.Y[s].toarray())
        assert np.allclose(m.zdiag(s, list(range(net.nb))), np.diag(Zd), atol=1e-10)


def test_known_identities_ieee14(bb14):
    net = Net.from_busbranch(bb14)
    r = sc.analyze(net)
    assert r["n_buses"] == net.nb
    for x in r["results"]:
        assert x["ik_ll_ka"] == pytest.approx(np.sqrt(3) / 2 * x["ik3_ka"], rel=1e-9)     # Z1 = Z2
        assert x["ip_ka"] >= np.sqrt(2) * x["ik3_ka"] * 1.02 - 1e-9 and x["kappa"] <= 2.0
        assert x["sk_mva"] > 0


def test_monotonicity_parallel_line_and_generator_loss(bb118):
    net = Net.from_busbranch(bb118)
    base = {x["bus"]: x["ik3_ka"] for x in sc.analyze(net)["results"]}
    # remove one generator -> fault levels cannot rise anywhere
    n2 = net.copy()
    keep = np.arange(len(n2.g_bus)) != 5
    for k in ("g_bus", "g_p", "g_q", "g_qmax", "g_qmin", "g_vset", "g_pmax", "g_pmin"):
        setattr(n2, k, getattr(net, k)[keep])
    n2.g_id = [g for g, kp in zip(net.g_id, keep) if kp]
    n2.analyze_islands()
    less = {x["bus"]: x["ik3_ka"] for x in sc.analyze(n2)["results"]}
    assert all(less[b] <= base[b] + 1e-9 for b in less)
    assert any(less[b] < base[b] - 1e-6 for b in less)
    # duplicate every branch of the first 10 -> fault levels cannot fall
    n3 = net.copy()
    for k in ("f", "t", "r", "x", "b", "tap", "shift", "rate"):
        v = getattr(net, k)
        setattr(n3, k, np.r_[v, v[:10]])
    n3.br_id = net.br_id + [f"DUP{i}" for i in range(10)]
    n3.analyze_islands()
    more = {x["bus"]: x["ik3_ka"] for x in sc.analyze(n3)["results"]}
    assert all(more[b] >= base[b] - 1e-9 for b in more)


def test_islanded_dead_buses_are_excluded_and_split_ok(bb118):
    net = Net.from_busbranch(bb118)
    for k in range(net.nl):
        n1 = net.without_branches([k])
        if len(n1.island_ref) > 1:
            break
    r = sc.analyze(n1)
    live = [i for i in range(n1.nb) if n1.island_ref[int(n1.island[i])] is not None]
    assert r["n_buses"] == len(live)


def test_fault_detail_voltage_collapse_and_current_balance(bb14):
    net = Net.from_busbranch(bb14)
    d = sc.fault_detail(net, 8, "3ph")
    assert d["buses"][0]["va"] == pytest.approx(0.0, abs=1e-9)         # faulted bus collapses
    assert d["buses"][0]["bus"] == net.bus_name[8]
    assert d["branches"][0]["ka"] > 0
    lg = sc.fault_detail(net, 8, "lg")
    assert lg["buses"][0]["va"] == pytest.approx(0.0, abs=1e-9) and lg["buses"][0]["vb"] > 0.5
