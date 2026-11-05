"""Short-circuit analysis in the style of IEC 60909 (symmetrical components, bolted faults).

Method (documented simplifications, NOT a certified IEC 60909 implementation)
 * Pre-fault loads, line charging and shunts are neglected; the pre-fault voltage is the equivalent voltage
   source c * Un / sqrt(3) (c = 1.10 for maximum, 0.95 for minimum fault currents).
 * Generators are the sub-transient reactance x''d behind the bus (no KG impedance correction, no network-
   transformer KT correction, no motor contribution, no partial-load/tap compensation).
 * Negative sequence = positive sequence for the network; x2 = x''d for generators.
 * Zero sequence is SYNTHETIC: lines r0 = 3 r1, x0 = 3 x1; every transformer is treated as a through-connected
   grounded-star/grounded-star unit with z0 = z1; generators are solidly grounded with x0 = 0.5 x''d.
   The public IEEE cases contain no zero-sequence data, so zero-sequence results are illustrative.
 * Generator ratings and x''d are SYNTHETIC unless the model supplies them: S = max(Pmax, Qmax, 10 MVA)/0.85,
   x''d = 0.18 pu on S. Peak factor kappa = 1.02 + 0.98 exp(-3 R/X) from the Thevenin R/X (method B-like).
Fault types: 3ph, LG (phase a), LL (b-c), LLG (b-c-earth).
"""
from __future__ import annotations
import time
import numpy as np
import scipy.sparse as sp
from scipy.sparse.linalg import splu
from .network import Net

A_OP = np.exp(2j * np.pi / 3)
XDPP = 0.18
GEN_PF = 0.85
FAULT_TYPES = ("3ph", "lg", "ll", "llg")


def gen_xdpp_pu(net: Net, xdpp: float = XDPP) -> np.ndarray:
    s = np.maximum.reduce([net.g_pmax, net.g_qmax, np.full(len(net.g_bus), 10.0)]) / GEN_PF
    return xdpp * net.base / s


def breaker_rating_ka(kv: float) -> float:
    """SYNTHETIC interrupting-rating classes by nominal voltage (for the duty screen only)."""
    return 31.5 if kv < 72.5 else 40.0 if kv <= 145 else 50.0 if kv <= 245 else 63.0


def _seq_ybus(net: Net, zero: bool, xdpp: float, x2_ratio: float = 1.0):
    n = net.copy()
    n.b[:] = 0.0
    n.gs[:] = 0.0
    n.bs[:] = 0.0
    if zero:
        line = n.tap == 0.0
        n.r = np.where(line & (n.shift == 0.0), 3.0 * n.r, n.r)
        n.x = np.where(line & (n.shift == 0.0), 3.0 * n.x, n.x)
        n.tap = np.zeros(n.nl)       # zero sequence ignores off-nominal tap and phase shift (synthetic YNyn)
        n.shift = np.zeros(n.nl)
    Y, Yf, Yt = n.ybus()
    xg = gen_xdpp_pu(net, xdpp)
    xg = xg * (0.5 if zero else x2_ratio)
    Yg = sp.csr_matrix((1.0 / (1j * xg), (net.g_bus, net.g_bus)), shape=(net.nb, net.nb))
    return (Y + Yg).tocsc(), Yf, Yt


class SeqModel:
    """Factorised sequence networks of ONE energised island."""

    def __init__(self, net: Net, xdpp: float = XDPP, x2_ratio: float = 1.0):
        self.net = net
        self.Y = {}
        self.Yf = {}
        self.Yt = {}
        self.lu = {}
        for name, zero in (("1", False), ("2", False), ("0", True)):
            if name == "2":
                Y, Yf, Yt = _seq_ybus(net, False, xdpp, x2_ratio)
            else:
                Y, Yf, Yt = _seq_ybus(net, zero, xdpp)
            self.Y[name], self.Yf[name], self.Yt[name] = Y, Yf, Yt
            self.lu[name] = splu(Y)

    def zcol(self, seq: str, cols):
        cols = np.asarray(cols, dtype=int)
        rhs = np.zeros((self.net.nb, len(cols)), dtype=complex)
        rhs[cols, np.arange(len(cols))] = 1.0
        return self.lu[seq].solve(rhs)

    def zdiag(self, seq: str, buses, block: int = 256):
        out = np.zeros(len(buses), dtype=complex)
        for s in range(0, len(buses), block):
            idx = np.asarray(buses[s:s + block], dtype=int)
            out[s:s + len(idx)] = self.zcol(seq, idx)[idx, np.arange(len(idx))]
        return out


def seq_currents(kind: str, z1, z2, z0, c: float):
    """Sequence currents (Ia0, Ia1, Ia2) injected into the fault, phase a reference, Zf = 0."""
    z1, z2, z0 = np.asarray(z1), np.asarray(z2), np.asarray(z0)
    if kind == "3ph":
        i1 = c / z1
        return 0 * i1, i1, 0 * i1
    if kind == "lg":
        i = c / (z0 + z1 + z2)
        return i, i, i
    if kind == "ll":
        i1 = c / (z1 + z2)
        return 0 * i1, i1, -i1
    if kind == "llg":
        zp = z2 * z0 / (z2 + z0)
        i1 = c / (z1 + zp)
        return -i1 * z2 / (z2 + z0), i1, -i1 * z0 / (z2 + z0)
    raise ValueError(kind)


def phase_currents(i0, i1, i2):
    return i0 + i1 + i2, i0 + A_OP ** 2 * i1 + A_OP * i2, i0 + A_OP * i1 + A_OP ** 2 * i2


def _kappa(z1):
    rx = np.abs(z1.real) / np.maximum(np.abs(z1.imag), 1e-12)
    return np.minimum(2.0, 1.02 + 0.98 * np.exp(-3.0 * rx))


def analyze(net: Net, buses=None, c: float = 1.10, xdpp: float = XDPP, x2_ratio: float = 1.0) -> dict:
    """Fault levels at the requested buses (default: every energised bus)."""
    t0 = time.perf_counter()
    want = set(range(net.nb)) if buses is None else {int(b) for b in buses}
    rows = {}
    for k, rb in net.island_ref.items():
        isl = np.where(net.island == k)[0]
        sel = [i for i in isl if i in want]
        if rb is None or not sel:
            continue
        sub, bmap = net.subnet(k)
        if len(sub.g_bus) == 0:
            continue
        m = SeqModel(sub, xdpp, x2_ratio)
        loc = np.array([int(np.where(bmap == i)[0][0]) for i in sel])
        z1, z2, z0 = (m.zdiag(s, loc) for s in ("1", "2", "0"))
        ibase = net.base / (np.sqrt(3.0) * net.base_kv[np.array(sel)])          # kA per pu
        out = {}
        for kind in FAULT_TYPES:
            i0, i1, i2 = seq_currents(kind, z1, z2, z0, c)
            ia, ib, ic = phase_currents(i0, i1, i2)
            out[kind] = {"3ph": np.abs(ia), "lg": np.abs(ia), "ll": np.abs(ib), "llg": np.maximum(np.abs(ib), np.abs(ic))}[kind] * ibase
            if kind == "llg":
                out["llg_earth"] = np.abs(3 * i0) * ibase
        kap = _kappa(z1)
        for j, bi in enumerate(sel):
            rating = breaker_rating_ka(float(net.base_kv[bi]))
            mx = max(out[kd][j] for kd in FAULT_TYPES)
            rows[int(bi)] = dict(
                bus=net.bus_name[bi], kv=float(net.base_kv[bi]), z1_pu=[float(z1[j].real), float(z1[j].imag)],
                z0_pu=[float(z0[j].real), float(z0[j].imag)], x_over_r=float(abs(z1[j].imag) / max(abs(z1[j].real), 1e-12)),
                ik3_ka=float(out["3ph"][j]), ik_lg_ka=float(out["lg"][j]), ik_ll_ka=float(out["ll"][j]),
                ik_llg_ka=float(out["llg"][j]), ik_llg_earth_ka=float(out["llg_earth"][j]),
                kappa=float(kap[j]), ip_ka=float(kap[j] * np.sqrt(2.0) * out["3ph"][j]), sk_mva=float(np.sqrt(3.0) * net.base_kv[bi] * out["3ph"][j]),
                max_ka=float(mx), rating_ka=rating, duty_pct=float(mx / rating * 100.0))
    order = sorted(rows.values(), key=lambda r: -r["duty_pct"])
    return dict(c=c, n_buses=len(rows), seconds=round(time.perf_counter() - t0, 4),
                n_over_rating=sum(1 for r in order if r["duty_pct"] > 100.0), results=order,
                assumptions="IEC 60909-style, bolted faults, c-factor source, loads neglected; zero sequence and generator data synthetic")


def fault_detail(net: Net, bus: int, kind: str, c: float = 1.10, xdpp: float = XDPP, top: int = 15) -> dict:
    """Bus voltages (3 phases) and branch currents during a fault at `bus` (energised island only)."""
    k = int(net.island[bus])
    if net.island_ref.get(k) is None:
        raise ValueError("bus is not energised")
    sub, bmap = net.subnet(k)
    b = int(np.where(bmap == bus)[0][0])
    m = SeqModel(sub, xdpp)
    z = {s: m.zcol(s, [b])[:, 0] for s in ("1", "2", "0")}
    i0, i1, i2 = seq_currents(kind, z["1"][b], z["2"][b], z["0"][b], c)
    v1 = c - z["1"] * i1
    v2 = -z["2"] * i2
    v0 = -z["0"] * i0
    va, vb, vc = phase_currents(v0, v1, v2)
    cur = {}
    for s, v in (("1", v1), ("2", v2), ("0", v0)):
        cur[s] = (m.Yf[s] @ v, m.Yt[s] @ v)
    out = []
    for end, idx in (("F", 0), ("T", 1)):
        ia, ib, ic = phase_currents(cur["0"][idx], cur["1"][idx], cur["2"][idx])
        ibase = net.base / (np.sqrt(3.0) * sub.base_kv[sub.f if end == "F" else sub.t])
        mx = np.maximum.reduce([np.abs(ia), np.abs(ib), np.abs(ic)]) * ibase
        out.append(mx)
    mx = np.maximum(out[0], out[1])
    order = np.argsort(-mx)[:top]
    return dict(bus=net.bus_name[bus], kind=kind, c=c,
                fault_current_ka=float(max(np.abs(x) for x in phase_currents(i0, i1, i2)) * net.base / (np.sqrt(3.0) * net.base_kv[bus])),
                buses=[dict(bus=sub.bus_name[i], va=float(abs(va[i])), vb=float(abs(vb[i])), vc=float(abs(vc[i]))) for i in np.argsort([min(abs(va[i]), abs(vb[i]), abs(vc[i])) for i in range(sub.nb)])[:top]],
                branches=[dict(branch=sub.br_id[i], ka=float(mx[i])) for i in order])
