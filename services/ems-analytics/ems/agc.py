"""Multi-area Automatic Generation Control simulator and regulation dispatch. ADVISORY / SIMULATION: it never issues a setpoint.

Model (documented simplifications)
 * Areas: model-driven spectral partition (same as the corridor monitor). Each area is a coherent block with one
   frequency; area-to-area stiffness comes from the DC network by Kron-reducing the susceptance matrix onto one
   representative bus per area (exact for a DC network with coherent areas).
 * Per area i (MW, per-unit frequency deviation w, angle d):  M_i dw/dt = dPm_i - dPload_i - D_i w - dPtie_i,
   dd/dt = 2 pi f0 w, dPtie_i = sum_j T_ij (d_i - d_j).  M_i = 2 H sum(Pmax) (H on the MW rating), D_i = d_load * load_i.
 * D_i = d_load * load + d_gen * sum(Pmax) (load damping plus machine damping). Per unit: droop governor (gain Pmax/R) with dead band and valve limits, first-order governor (Tg) and turbine (Tt), tripped units drop out.
 * AGC every `agc_period` s: ACE_i = dPtie_i + beta_i w_i (tie-line bias control, beta_i = sum of unit droop gains + D_i),
   PI with deadband and anti-windup, allocated to units by participation factors (range / equal / economic) within unit
   limits and AGC ramp rates (`allocate`).
 * CIM has no dynamic data: H, R, Tg, Tt, damping, ramp rates are synthetic defaults, overridable per request, and reported.
Limits: no network losses, voltage or reactive effects, no reheat/hydro turbine detail, no frequency-dependent loads beyond D,
no under-frequency load shedding, no inter-area oscillation modelling beyond the lumped areas, no CPS1/CPS2/BAAL scoring
(ACE statistics are reported instead).
"""
from __future__ import annotations
import time
import numpy as np
from .network import Net
from . import tcm

F0 = 50.0

DEFAULTS = dict(H=5.0, R=0.05, Tg=0.2, Tt=0.5, d_load=1.0, d_gen=1.0, gov_deadband_hz=0.03, ramp_agc_pct_min=3.0, kp=0.2, ki=0.05, agc_period=4.0, deadband_mw=0.2, dt=0.05, mode="range")


def allocate(delta, up, dn, weights):
    """Split `delta` MW over units with weights, never exceeding per-unit room (up for +, dn for -). Returns (x, unallocated)."""
    x = np.zeros(len(weights))
    if abs(delta) < 1e-12:
        return x, 0.0
    room = np.maximum(up if delta > 0 else dn, 0.0)
    w = np.where(room > 1e-12, np.maximum(weights, 0.0), 0.0)
    rem = abs(delta)
    for _ in range(len(weights) + 1):
        sw = w.sum()
        if sw <= 1e-15 or rem <= 1e-12:
            break
        give = np.minimum(rem * w / sw, room - x)
        x += give
        rem -= give.sum()
        w = np.where(x >= room - 1e-12, 0.0, w)
    return np.sign(delta) * x, float(max(rem, 0.0)) * np.sign(delta)


def kron_ties(net: Net, area: np.ndarray, reps: list[int]):
    """Equivalent area-to-area susceptance matrix (pu) by Kron reduction onto the representative buses."""
    nb = net.nb
    tap = np.where(net.tap != 0.0, np.abs(net.tap), 1.0)
    b = 1.0 / (net.x * tap)
    B = np.zeros((nb, nb))
    for k in range(net.nl):
        f, t = net.f[k], net.t[k]
        if area[f] < 0 or area[t] < 0:
            continue
        B[f, f] += b[k]; B[t, t] += b[k]; B[f, t] -= b[k]; B[t, f] -= b[k]
    live = np.where(area >= 0)[0]
    keep = np.array(reps)
    elim = np.setdiff1d(live, keep)
    Bkk = B[np.ix_(keep, keep)]
    if len(elim):
        Bke = B[np.ix_(keep, elim)]
        Bee = B[np.ix_(elim, elim)]
        Bkk = Bkk - Bke @ np.linalg.solve(Bee, Bke.T)
    Beq = -Bkk.copy()
    np.fill_diagonal(Beq, 0.0)
    return Beq


class Setup:
    def __init__(self, net: Net, parts=2, params=None):
        self.p = {**DEFAULTS, **(params or {})}
        self.net = net
        area = tcm.partition(net, parts)
        g_area = np.where(area[net.g_bus] >= 0, area[net.g_bus], -1)
        self.area = area
        self.na = int(area.max()) + 1
        self.units = np.where(g_area >= 0)[0]
        self.g_area = g_area[self.units]
        pmax = net.g_pmax[self.units].astype(float)
        self.pmax, self.pmin = pmax, np.minimum(net.g_pmin[self.units], net.g_p[self.units]).astype(float)
        self.p0 = net.g_p[self.units].astype(float)
        self.ids = [net.g_id[i] for i in self.units]
        self.load = np.array([net.pd[area == a].sum() for a in range(self.na)])
        reps = []
        for a in range(self.na):
            gi = np.where(self.g_area == a)[0]
            reps.append(int(net.g_bus[self.units[gi[np.argmax(pmax[gi])]]]) if len(gi) else int(np.where(area == a)[0][0]))
        self.reps = reps
        self.Beq = kron_ties(net, area, reps)
        self.T = self.Beq * net.base                              # MW per rad
        p = self.p
        self.M = np.array([2 * p["H"] * pmax[self.g_area == a].sum() for a in range(self.na)])
        self.D = p["d_load"] * self.load + p["d_gen"] * np.array([pmax[self.g_area == a].sum() for a in range(self.na)])
        self.Kg = pmax / p["R"]
        self.beta = np.array([self.Kg[self.g_area == a].sum() + self.D[a] for a in range(self.na)])
        self.econ_w = None
        if net.g_c2 is not None:
            c2 = np.nan_to_num(net.g_c2[self.units], nan=0.0)
            self.econ_w = 1.0 / (2 * c2 * np.maximum(pmax, 1.0) + 1.0)


def simulate(net: Net, *, parts=2, events=None, duration=300.0, agc=True, params=None, out_points=300, setup: Setup | None = None) -> dict:
    t0 = time.perf_counter()
    S = setup or Setup(net, parts, params)
    p = S.p
    na, nu = S.na, len(S.units)
    dt = p["dt"]
    steps = int(round(duration / dt))
    ev = sorted(events or [], key=lambda e: e["t"])
    a_g = S.g_area
    onehot = np.zeros((na, nu)); onehot[a_g, np.arange(nu)] = 1.0
    ex_g = 1 - np.exp(-dt / p["Tg"]); ex_t = 1 - np.exp(-dt / p["Tt"])
    Pv = S.p0.copy(); Pm = S.p0.copy(); Pref = S.p0.copy(); alive = np.ones(nu, dtype=bool)
    w = np.zeros(na); d = np.zeros(na)
    dload = np.zeros(na); ramps = []                   # active load ramps: (area, t_start, duration, mw)
    I = np.zeros(na)
    next_agc = 0.0
    db_pu = p["gov_deadband_hz"] / F0
    ramp_u = p["ramp_agc_pct_min"] / 100.0 / 60.0 * S.pmax         # MW/s per unit
    weights_cache = None
    nrec = max(1, steps // out_points)
    rec = dict(t=[], f=[], ace=[], tie=[], pgen=[], pload=[])
    ace = np.zeros(na); ei = 0
    unalloc = np.zeros(na)
    ace_hist = []
    for k in range(steps + 1):
        t = k * dt
        while ei < len(ev) and ev[ei]["t"] <= t + 1e-9:
            e = ev[ei]; ei += 1
            if e["kind"] == "load_step":
                dload[e["area"]] += e["mw"]
            elif e["kind"] == "load_ramp":
                ramps.append((e["area"], e["t"], e["duration"], e["mw"]))
            elif e["kind"] == "gen_trip":
                u = S.ids.index(e["unit"]) if e["unit"] in S.ids else None
                if u is not None:
                    alive[u] = False; Pv[u] = 0.0; Pm[u] = 0.0
        dl = dload.copy()
        for a, ts, du, mw in ramps:
            if t > ts:
                dl[a] += mw * min(1.0, (t - ts) / du)
        dtie = S.T.sum(axis=1) * d - S.T @ d if False else (S.T.sum(axis=1) * d - S.T @ d)
        # AGC
        if t >= next_agc - 1e-9:
            next_agc += p["agc_period"]
            ace = dtie + S.beta * w
            ace_hist.append(ace.copy())
            if agc:
                for a in range(na):
                    idx = np.where((a_g == a) & alive)[0]
                    if not len(idx):
                        continue
                    e_a = ace[a] if abs(ace[a]) > p["deadband_mw"] else 0.0
                    I[a] += p["ki"] * e_a * p["agc_period"]
                    u_des = -(p["kp"] * e_a + I[a])                    # desired total regulation (MW) relative to base schedule
                    cur = float((Pref[idx] - S.p0[idx]).sum())
                    delta = u_des - cur
                    up = np.minimum(S.pmax[idx] - Pref[idx], ramp_u[idx] * p["agc_period"])
                    dn = np.minimum(Pref[idx] - S.pmin[idx], ramp_u[idx] * p["agc_period"])
                    if p["mode"] == "equal":
                        wt = np.ones(len(idx))
                    elif p["mode"] == "economic" and S.econ_w is not None:
                        wt = S.econ_w[idx]
                    else:
                        wt = np.maximum(up if delta > 0 else dn, 0.0) + 1e-9
                    x, un = allocate(delta, up, dn, wt)
                    Pref[idx] += x
                    unalloc[a] = un
                    if abs(un) > 1e-9 and abs(delta) > 0:
                        I[a] += un * 0.5                                 # anti-windup (back-calculation)
        # unit dynamics
        wu = w[a_g]
        wg = np.sign(wu) * np.maximum(np.abs(wu) - db_pu, 0.0)                 # governor dead band
        tgt = np.clip(Pref - S.Kg * wg, S.pmin, S.pmax)
        Pv = np.where(alive, Pv + ex_g * (tgt - Pv), 0.0)
        Pm = np.where(alive, Pm + ex_t * (Pv - Pm), 0.0)
        dPm = onehot @ (Pm - S.p0 * alive) + onehot @ (S.p0 * alive) - onehot @ S.p0   # trip removes the unit's whole base output
        dtie = S.T.sum(axis=1) * d - S.T @ d
        dw = (dPm - dl - S.D * w - dtie) / S.M
        w = w + dt * dw
        d = d + dt * 2 * np.pi * F0 * w
        if k % nrec == 0:
            rec["t"].append(t); rec["f"].append((F0 * (1 + w)).tolist()); rec["ace"].append(ace.tolist()); rec["tie"].append(dtie.tolist())
            rec["pgen"].append(float(Pm.sum() - S.p0.sum())); rec["pload"].append(float(dl.sum()))
    f = np.array(rec["f"]); T_ = np.array(rec["t"]); AC = np.array(rec["ace"]); TI = np.array(rec["tie"])
    tail = T_ >= duration - min(30.0, duration / 4)
    kp = []
    first_ev = ev[0]["t"] if ev else 0.0
    for a in range(na):
        fa = f[:, a]
        post = T_ >= first_ev
        i_min = int(np.argmin(np.where(post, fa, 1e9)))
        db = max(p["deadband_mw"] * 2, 0.01 * max(abs(e.get("mw", 0.0)) for e in ev) if ev else 1.0)
        in_band = np.abs(AC[:, a]) <= db
        settle = None
        for i in range(len(T_) - 1, -1, -1):
            if not in_band[i]:
                settle = float(T_[min(i + 1, len(T_) - 1)]) - first_ev if i < len(T_) - 1 else None
                break
        else:
            settle = 0.0
        kp.append(dict(area=a, buses=int((S.area == a).sum()), units=int((S.g_area == a).sum()), load_mw=round(float(S.load[a]), 1), beta_mw_per_hz=round(float(S.beta[a] / F0), 1),
                       nadir_hz=round(float(fa[i_min]), 4), nadir_time_s=round(float(T_[i_min]), 1), final_dev_hz=round(float(fa[tail].mean() - F0), 4),
                       max_abs_ace_mw=round(float(np.abs(AC[:, a]).max()), 2), final_ace_mw=round(float(AC[tail, a].mean()), 2),
                       final_tie_dev_mw=round(float(TI[tail, a].mean()), 2), ace_settle_s=None if settle is None else round(settle, 1),
                       ace_rms_mw=round(float(np.sqrt((AC[post, a] ** 2).mean())), 2)))
    # analytic references
    tot_dl = sum(e["mw"] for e in ev if e["kind"] == "load_step") + sum(e["mw"] for e in ev if e["kind"] == "load_ramp")
    trip = sum(float(S.p0[S.ids.index(e["unit"])]) for e in ev if e["kind"] == "gen_trip" and e["unit"] in S.ids)
    lost_gov = sum(float(S.Kg[S.ids.index(e["unit"])]) for e in ev if e["kind"] == "gen_trip" and e["unit"] in S.ids)
    beta_tot = float(S.beta.sum()) - lost_gov
    dpu = p["gov_deadband_hz"] / F0
    kg_tot = float(S.Kg.sum()) - lost_gov
    d_tot = float(S.D.sum())
    w_ss = -(tot_dl + trip) / d_tot                               # inside the dead band only load/machine damping responds
    if abs(w_ss) > dpu:                                           # outside it, governors supply the part beyond the band
        w_ss = -((tot_dl + trip) + np.sign(tot_dl + trip) * kg_tot * dpu) / beta_tot
    ref = dict(primary_only_steady_state_hz=round(F0 * w_ss, 4), initial_rocof_hz_s=round(F0 * (-(tot_dl + trip)) / float(S.M.sum()), 4) if ev and ev[0]["kind"] != "load_ramp" else None,
               note="valid for a single event with no saturation; compare with a primary-only (agc=false) run")
    q = len(T_) // 4
    amp = lambda a_, b_: float(np.abs(f[a_:b_] - f[a_:b_].mean(axis=0)).max()) if b_ > a_ else 0.0
    stability = "ok"
    if np.abs(f - F0).max() > 3.0 or (amp(3 * q, len(T_)) > 0.02 and amp(3 * q, len(T_)) > 1.5 * amp(2 * q, 3 * q)):
        stability = "oscillatory_unstable"
    sat = int(((Pref >= S.pmax - 1e-6) | (Pref <= S.pmin + 1e-6))[alive].sum())
    out = dict(advisory=True, simulation=True, agc=agc, stability=stability, duration_s=duration, dt=dt, areas=kp, reference=ref, units_at_limit=sat, unallocated_mw=[round(float(x), 2) for x in unalloc],
               params={k: S.p[k] for k in S.p}, ties_mw_per_rad=[[round(float(v), 1) for v in r] for r in S.T],
               series=dict(t=[round(x, 2) for x in rec["t"]], f=np.round(f, 5).tolist(), ace=np.round(AC, 3).tolist(), tie=np.round(TI, 3).tolist()),
               wall_seconds=round(time.perf_counter() - t0, 2),
               notes=["Simulation only: nothing is sent to any unit or control path.", "Dynamic parameters are synthetic defaults (the CIM model carries none).", "Coherent areas, DC ties, no losses/voltage; no CPS1/BAAL scoring."]
               + (["The lumped model is oscillatory-unstable for this area split (fast inter-area mode with lagged governors); result is NOT credible. Use fewer areas or slower governor settings."] if stability != "ok" else []))
    return out
