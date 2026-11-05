"""Dynamic security assessment: voltage (loading margin), transient (swing-equation time domain, critical clearing time)
and small-signal (eigenvalue / damping) stability. ADVISORY: nothing here drives control.

Voltage (VSA)
 * Loading margin: all loads and all generator P are scaled by (1 + lam); AC Newton-Raphson at each step with warm start
   and Q limits, step halving to 0.1 % of the base loading. The margin is the largest converged lam (a lower bound of the
   true nose within the step tolerance). Weakest bus = lowest voltage at the last converged point. N-1 margins for the
   most-loaded branches are computed the same way after the outage.
Transient (TSA)
 * Classical model: each generator is a constant voltage E behind x'd (0.3 pu on S = Pmax/0.85, synthetic), swing equation
   with inertia H (5 s synthetic) and damping D; loads are constant impedance at the pre-fault voltage; the network is
   Kron-reduced to the generator internal nodes for the pre-fault, fault (bolted three-phase fault at a bus) and
   post-fault (optionally one incident branch tripped) conditions. RK4 integration. A case is unstable when any rotor
   angle leaves the centre of inertia by more than 180 degrees. CCT is found by bisection on the clearing time.
Small-signal (SSA)
 * The same classical model linearised at the equilibrium: x = [d delta, d omega], A = [[0, w0 I], [-M^-1 K, -M^-1 D]],
   K = dPe/ddelta from the reduced post-fault network. Modes are the eigenvalues of A (frequency, damping ratio) and
   participation is the right-eigenvector magnitude of the angle states.
Limits: classical model only (no exciters, PSS, governors, flux decay or saturation); constant-impedance loads; dynamic
data are synthetic defaults because the CIM model carries none; three-phase faults only; no protection modelling; the
voltage margin ignores dynamic load behaviour, OLTC action and over-excitation limiters other than the steady-state Q limits.
"""
from __future__ import annotations
import time
import numpy as np
import scipy.sparse as sp
import scipy.sparse.linalg  # noqa: F401  (sp.linalg.splu)
from .network import Net, UNRATED
from . import powerflow as pf

F0 = 50.0
DYN = dict(H=5.0, xd1=0.3, D=2.0, t_end=3.0, dt=0.01, s_factor=0.85)


# ------------------------------------------------------------------ voltage stability
def _scaled(net: Net, lam: float, base_gp):
    n = net.copy()
    n.pd = net.pd * (1 + lam); n.qd = net.qd * (1 + lam)
    n.g_p = base_gp * (1 + lam)
    return n


def loading_margin(net: Net, *, enforce_q=True, vmin=0.5, tol=1e-3, lam_max=3.0, step0=0.05, curve=True):
    """Largest scaling lam of loads+generation with a converged AC power flow. Returns dict(lam, margin_mw, points, weakest_bus...)."""
    base = pf.solve(net, enforce_q_limits=enforce_q)
    if not base["converged"]:
        return dict(converged=False, lam=0.0, margin_mw=0.0, points=[])
    gp0 = net.g_p.copy()
    load0 = float(net.pd.sum())
    lam, V, step = 0.0, base["V"], step0
    pts = [(0.0, base["vm"].copy())]
    last = base
    while lam < lam_max and step > tol:
        n2 = _scaled(net, lam + step, gp0)
        s = pf.solve(n2, V0=V, enforce_q_limits=enforce_q)
        ok = s["converged"] and np.nanmin(s["vm"][s["vm"] > 0]) > vmin
        if ok:
            lam += step; V = s["V"]; last = s
            pts.append((lam, s["vm"].copy()))
        else:
            step /= 2.0
    vm = last["vm"]
    live = vm > 0
    order = np.argsort(np.where(live, vm, 9.0))
    dv = np.abs(pts[-1][1] - pts[max(0, len(pts) - 3)][1]) if len(pts) > 2 else np.zeros_like(vm)
    wk = int(np.argmax(np.where(live, dv, -1.0)))                 # bus whose voltage moves most near the nose
    out = dict(converged=True, lam=round(lam, 4), margin_mw=round(lam * load0, 1), margin_pct=round(100 * lam, 1), base_load_mw=round(load0, 1),
               collapse_vm_min=round(float(vm[live].min()), 4), weakest_bus=int(wk), weakest_bus_name=net.bus_name[wk],
               lowest_voltage_buses=[dict(bus=net.bus_name[i], vm=round(float(vm[i]), 4)) for i in order[:5]], q_limited_buses=len(last["q_limited_buses"]),
               steps=len(pts))
    if curve:
        out["pv_curve"] = [dict(lam=round(l, 4), vm=round(float(v[wk]), 4)) for l, v in pts]
    return out


def vsa(net: Net, *, n1_top=8, enforce_q=True, warn_pct=10.0) -> dict:
    t0 = time.perf_counter()
    base = loading_margin(net, enforce_q=enforce_q)
    if not base["converged"]:
        return dict(status="base_not_converged")
    sol = pf.solve(net)
    rated = (net.rate > 0) & (net.rate < UNRATED)
    load = np.where(rated, sol["mva_max"] / np.where(rated, net.rate, 1.0), sol["mva_max"] / max(float(sol["mva_max"].max()), 1.0))
    cand = [int(k) for k in np.argsort(-load)[:n1_top]]
    rows = []
    for k in cand:
        n2 = net.without_branches([k])
        if len(n2.island_ref) > len(net.island_ref):
            continue
        r = loading_margin(n2, enforce_q=enforce_q, curve=False)
        if r.get("converged"):
            rows.append(dict(outage=net.br_id[k], margin_pct=r["margin_pct"], margin_mw=r["margin_mw"], weakest_bus=r["weakest_bus_name"]))
    rows.sort(key=lambda r: r["margin_pct"])
    worst = min([base["margin_pct"]] + [r["margin_pct"] for r in rows])
    return dict(status="ok" if worst >= warn_pct else "low_margin", base=base, n1=rows, worst_margin_pct=worst, warn_pct=warn_pct,
               wall_seconds=round(time.perf_counter() - t0, 2),
               notes=["Margin = largest uniform scaling of loads and generation with a converged AC power flow (lower bound within 0.1 %).",
                      "Steady-state Q limits only; no OLTC, over-excitation limiters or dynamic loads."])


# ------------------------------------------------------------------ transient / small-signal
class Dyn:
    """Classical-model data and reduced networks for one operating point."""

    def __init__(self, net: Net, params=None, sol=None):
        self.p = {**DYN, **(params or {})}
        p = self.p
        self.net = net
        sol = sol or pf.solve(net)
        if not sol["converged"]:
            raise ValueError("base power flow did not converge")
        self.sol = sol
        main = net.island == net.island[net.slack_bus]
        gi = np.where(main[net.g_bus])[0]
        self.gi = gi
        self.ng = len(gi)
        gb = net.g_bus[gi]
        self.gb = gb
        V = sol["V"]
        S = (sol["gen_p"][gi] + 1j * sol["gen_q"][gi]) / net.base
        I = np.conj(S / V[gb])
        S_mva = np.maximum(np.maximum(net.g_pmax[gi], np.abs(sol["gen_p"][gi])), 10.0) / p["s_factor"]
        self.S = S_mva
        self.xd1 = p["xd1"] * net.base / S_mva                     # pu on system base
        self.E = V[gb] + 1j * self.xd1 * I
        self.delta0 = np.angle(self.E)
        self.Em = np.abs(self.E)
        H = np.full(self.ng, p["H"]) if np.isscalar(p["H"]) else np.asarray(p["H"], float)
        self.H = H
        self.M = 2.0 * H * S_mva / net.base                         # s, on system base (M dw/dt = Pm - Pe - D w, w in pu)
        self.D = p["D"] * S_mva / net.base
        Ybus, _, _ = net.ybus()
        self.Y0 = Ybus.tocsr()
        self.V = V
        yl = (net.pd - 1j * net.qd) / net.base / np.maximum(np.abs(V), 1e-6) ** 2
        yl = np.where(main, yl, 0)
        self.yl = yl
        # Pm from the electrical power in the pre-fault reduced network so that the initial state is an exact equilibrium
        self.Ypre = self._reduce(self.Y0)
        self.Pm = self._pe(self.delta0, self.Ypre)

    def _reduce(self, Yb):
        """Kron-reduce (network + constant-impedance loads + generator internal nodes) onto the generator internal nodes."""
        net = self.net
        nb, ng = net.nb, self.ng
        main = np.where(net.island == net.island[net.slack_bus])[0]
        pos = -np.ones(nb, dtype=int); pos[main] = np.arange(len(main))
        Yn = (Yb + sp.diags(self.yl)).tocsr()[main][:, main].tolil()
        n = len(main)
        Y = sp.lil_matrix((n + ng, n + ng), dtype=complex)
        Y[:n, :n] = Yn
        for i in range(ng):
            yg = 1.0 / (1j * self.xd1[i]); b = pos[self.gb[i]]
            Y[n + i, n + i] += yg; Y[b, b] += yg; Y[n + i, b] -= yg; Y[b, n + i] -= yg
        Y = Y.tocsc()
        lu = sp.linalg.splu(Y[:n, :n].tocsc())
        Ygl = Y[n:, :n]; Ylg = Y[:n, n:]
        X = lu.solve(Ylg.toarray())
        return (Y[n:, n:].toarray() - Ygl @ X)

    def fault_net(self, bus, branch=None, zf=1e-4):
        Y = self.Y0.tolil().copy()
        Y[bus, bus] += 1.0 / (1j * zf)
        return Y.tocsr()

    def post_net(self, trip):
        if trip is None:
            return self.Y0
        n2 = self.net.without_branches([trip])
        return n2.ybus()[0].tocsr()

    def _pe(self, delta, Yr):
        E = self.Em * np.exp(1j * delta)
        return np.real(E * np.conj(Yr @ E))

    def K(self, delta, Yr):
        """dPe_i/ddelta_j for the classical model."""
        E = self.Em
        G, B = Yr.real, Yr.imag
        dd = delta[:, None] - delta[None, :]
        Kij = E[:, None] * E[None, :] * (G * np.sin(dd) - B * np.cos(dd))        # i != j
        np.fill_diagonal(Kij, 0.0)
        Kii = -Kij.sum(axis=1)
        np.fill_diagonal(Kij, Kii)
        return Kij


def _simulate(dy: Dyn, Yf, Yp, t_clear, t_end, dt, keep=None):
    n = dy.ng
    d = dy.delta0.copy(); w = np.zeros(n)
    M, D, Pm = dy.M, dy.D, dy.Pm
    w0 = 2 * np.pi * F0
    def f(d, w, Y):
        return w0 * w, (Pm - dy._pe(d, Y) - D * w) / M
    steps = int(round(t_end / dt))
    maxsep = 0.0
    traj = []
    Hs = dy.H * dy.S
    for k in range(steps + 1):
        t = k * dt
        Y = Yf if t < t_clear - 1e-12 else Yp
        if keep is not None and k % keep == 0:
            coi = (Hs * d).sum() / Hs.sum()
            traj.append((t, d - coi, w.copy()))
        k1d, k1w = f(d, w, Y)
        k2d, k2w = f(d + 0.5 * dt * k1d, w + 0.5 * dt * k1w, Y)
        k3d, k3w = f(d + 0.5 * dt * k2d, w + 0.5 * dt * k2w, Y)
        k4d, k4w = f(d + dt * k3d, w + dt * k3w, Y)
        d = d + dt / 6 * (k1d + 2 * k2d + 2 * k3d + k4d)
        w = w + dt / 6 * (k1w + 2 * k2w + 2 * k3w + k4w)
        coi = (Hs * d).sum() / Hs.sum()
        sep = float(np.abs(d - coi).max())
        maxsep = max(maxsep, sep)
        if sep > np.pi:
            return False, maxsep, t, traj
    return True, maxsep, t_end, traj


def _reduced_fault_and_post(dy, bus, trip):
    Yf = dy._reduce(dy.fault_net(bus))
    Yp = dy._reduce(dy.post_net(trip))
    return Yf, Yp


def simulate_fault(dy: Dyn, bus: int, t_clear: float, trip=None, t_end=None, with_traj=False):
    Yf, Yp = _reduced_fault_and_post(dy, bus, trip)
    ok, sep, tend, traj = _simulate(dy, Yf, Yp, t_clear, t_end or dy.p["t_end"], dy.p["dt"], keep=5 if with_traj else None)
    return dict(stable=ok, max_angle_sep_deg=round(float(np.degrees(sep)), 1), time_to_loss_s=None if ok else round(tend, 3)), traj, (Yf, Yp)


def critical_clearing_time(dy: Dyn, bus: int, trip=None, t_max=1.0, tol=0.002, mats=None):
    Yf, Yp = mats or _reduced_fault_and_post(dy, bus, trip)
    run = lambda tc: _simulate(dy, Yf, Yp, tc, dy.p["t_end"], dy.p["dt"])[0]
    if not run(1e-3):
        return 0.0
    if run(t_max):
        return None                                               # stable even for t_max
    lo, hi = 1e-3, t_max
    while hi - lo > tol:
        mid = 0.5 * (lo + hi)
        if run(mid):
            lo = mid
        else:
            hi = mid
    return round(lo, 4)


def _worst_branch(net, sol, bus):
    cand = [k for k in range(net.nl) if net.f[k] == bus or net.t[k] == bus]
    if not cand:
        return None
    return max(cand, key=lambda k: sol["mva_max"][k])


def tsa(net: Net, *, fault_buses=None, t_clear=0.12, trip_branch=True, cct_top=6, params=None, max_buses=None, traj_bus=None) -> dict:
    t0 = time.perf_counter()
    dy = Dyn(net, params)
    main = np.where(net.island == net.island[net.slack_bus])[0]
    buses = list(main) if fault_buses is None else [int(b) for b in fault_buses]
    if max_buses:
        buses = buses[:max_buses]
    rows = []
    for b in buses:
        trip = _worst_branch(net, dy.sol, b) if trip_branch else None
        if trip is not None and len(net.without_branches([trip]).island_ref) > len(net.island_ref):
            trip = None                                            # never trip a radial/bridge branch: it would island the network
        r, _, mats = simulate_fault(dy, b, t_clear, trip)
        rows.append(dict(bus=net.bus_name[b], bus_idx=int(b), tripped=None if trip is None else net.br_id[trip], **r, _m=mats))
    rows.sort(key=lambda r: (r["stable"], -r["max_angle_sep_deg"]))
    for r in rows[:cct_top]:
        r["cct_s"] = critical_clearing_time(dy, r["bus_idx"], None, mats=r["_m"])
        r["cct_margin_ms"] = None if r["cct_s"] is None else round(1000 * (r["cct_s"] - t_clear), 0)
    for r in rows:
        r.pop("_m", None)
    out = dict(status="ok", clearing_time_s=t_clear, trip_branch=trip_branch, n_faults=len(rows), n_unstable=int(sum(not r["stable"] for r in rows)),
               results=rows, units=dy.ng, params=dy.p, wall_seconds=round(time.perf_counter() - t0, 2),
               notes=["Classical model: constant E behind x'd, constant-impedance loads, bolted three-phase faults; no exciters, PSS or governors.",
                      "Dynamic data are synthetic defaults (the CIM model carries none).",
                      "CCT is for the same post-fault network as the listed case; None means stable up to the search limit."])
    wb = rows[0]["bus_idx"] if traj_bus is None else traj_bus
    trip = _worst_branch(net, dy.sol, wb) if trip_branch else None
    if trip is not None and len(net.without_branches([trip]).island_ref) > len(net.island_ref):
        trip = None
    r, traj, _ = simulate_fault(dy, wb, t_clear, trip, t_end=min(dy.p["t_end"], 3.0), with_traj=True)
    idx = np.argsort(-dy.H * dy.S)[:6] if dy.ng > 6 else np.arange(dy.ng)
    out["trajectory"] = dict(bus=net.bus_name[wb], t=[round(x[0], 3) for x in traj], units=[net.g_id[dy.gi[i]] for i in idx],
                             angle_deg=[[round(float(np.degrees(x[1][i])), 2) for i in idx] for x in traj])
    return out


def ssa(net: Net, *, trip=None, params=None, damping_warn=0.05, top=12) -> dict:
    t0 = time.perf_counter()
    dy = Dyn(net, params)
    Yp = dy._reduce(dy.post_net(trip)) if trip is not None else dy.Ypre
    K = dy.K(dy.delta0, Yp)
    n = dy.ng
    w0 = 2 * np.pi * F0
    Minv = 1.0 / dy.M
    A = np.zeros((2 * n, 2 * n))
    A[:n, n:] = w0 * np.eye(n)
    A[n:, :n] = -(Minv[:, None] * K)
    A[n:, n:] = -np.diag(Minv * dy.D)
    lam, R = np.linalg.eig(A)
    modes = []
    for i in range(2 * n):
        l = lam[i]
        if l.imag <= 1e-6:
            continue                                               # one of each conjugate pair; real modes are reported below
        zeta = -l.real / abs(l)
        part = np.abs(R[:n, i]); part = part / part.max()
        top_u = np.argsort(-part)[:3]
        modes.append(dict(freq_hz=round(float(l.imag / (2 * np.pi)), 4), damping_ratio=round(float(zeta), 4), real=round(float(l.real), 4),
                          participants=[dict(unit=net.g_id[dy.gi[j]], rel=round(float(part[j]), 2)) for j in top_u]))
    real_modes = sorted(float(l.real) for l in lam if abs(l.imag) <= 1e-6)
    modes.sort(key=lambda m: m["damping_ratio"])
    unstable = [m for m in modes if m["real"] > 1e-6] + ([dict(freq_hz=0.0, damping_ratio=None, real=r) for r in real_modes if r > 1e-6])
    poor = [m for m in modes if m["damping_ratio"] < damping_warn]
    return dict(status="unstable" if unstable else ("poorly_damped" if poor else "ok"), n_modes=len(modes), units=n, modes=modes[:top], n_poorly_damped=len(poor),
               n_unstable=len(unstable), min_damping_ratio=modes[0]["damping_ratio"] if modes else None, damping_warn=damping_warn,
               freq_range_hz=[min(m["freq_hz"] for m in modes), max(m["freq_hz"] for m in modes)] if modes else None, params=dy.p,
               wall_seconds=round(time.perf_counter() - t0, 2),
               notes=["Classical model linearised at the operating point; damping comes from the synthetic damping coefficient only (no exciters/PSS).",
                      "Participation is the normalised right-eigenvector magnitude of the rotor-angle states."])
