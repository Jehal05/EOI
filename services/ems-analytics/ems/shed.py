"""Load-shed support: under-frequency load shedding (UFLS) assessment and rotating-roster planning. ADVISORY ONLY.

UFLS assessment (single aggregate frequency, documented simplifications)
 * 2 H S dw/dt = dPm - dPload - D w   (w = per-unit frequency deviation, S = online MW rating, D = d_load*load + d_gen*S in MW per pu)
 * governor: droop gain S/R with a dead band, response limited by the smaller of the unit headroom and a spinning-reserve cap (reserve_pct of S), first-order governor
   (Tg) and turbine (Tt) lags. The tripped units' output is lost instantly.
 * relays: each stage trips once when f has been below its pick-up for its delay, and removes `frac` of the pre-event load at once.
 * the stage table is an ILLUSTRATIVE default, NOT a grid-code setting; replace it with the utility's table (`stages`).
 * H, R, Tg, Tt, damping are synthetic defaults (the CIM carries no dynamic data) and are reported in the result.
Rotating roster: for a generation deficit, pick loads to shed each period so that nobody is shed twice before everyone eligible
 has been shed once (lowest accumulated energy-not-served first), never touching critical loads, and check every period's result
 with an AC power flow (generation reduced pro rata by the deficit; the slack absorbs the remainder).
Limits: no feeder topology (a CIM load is treated as one sheddable block), no breaker or SCADA command is issued, priority classes are
 SYNTHETIC unless supplied, no voltage-dependent load, no df/dt relays, no islanding.
"""
from __future__ import annotations
import dataclasses, hashlib, time
import numpy as np
from .network import Net
from . import powerflow as pf

F0 = 50.0
DEFAULTS = dict(H=5.0, R=0.05, Tg=0.2, Tt=0.5, d_load=1.0, d_gen=1.0, gov_deadband_hz=0.03, reserve_pct=5.0, dt=0.02)
STAGES = [dict(f=49.2, delay=0.2, frac=0.05), dict(f=49.0, delay=0.2, frac=0.07), dict(f=48.8, delay=0.2, frac=0.08), dict(f=48.6, delay=0.2, frac=0.10)]
CLASSES = {1: "critical", 2: "essential", 3: "commercial / industrial", 4: "residential / agricultural"}


def assign_priority(loads: dict, overrides: dict | None = None) -> dict:
    """mrid -> class 1..4. SYNTHETIC deterministic placeholder (hash of the id) unless overridden: 8 % critical, 17 % essential, 40 % commercial, 35 % residential."""
    out = {}
    for m in loads:
        v = int(hashlib.sha256(m.encode()).hexdigest()[:8], 16) % 100
        out[m] = 1 if v < 8 else 2 if v < 25 else 3 if v < 65 else 4
    for m, c in (overrides or {}).items():
        if m in out and int(c) in CLASSES:
            out[m] = int(c)
    return out


def priority_table(net: Net, overrides=None):
    pr = assign_priority(net.load_p0, overrides)
    rows = [dict(load=m, bus=net.bus_name[net.load_bus[m]], mw=round(p, 2), cls=pr[m]) for m, p in net.load_p0.items()]
    summ = {c: dict(name=n, loads=sum(1 for r in rows if r["cls"] == c), mw=round(sum(r["mw"] for r in rows if r["cls"] == c), 1)) for c, n in CLASSES.items()}
    return dict(loads=rows, classes=summ, total_mw=round(sum(net.load_p0.values()), 1), synthetic=not overrides or len(overrides) < len(rows))


def _units(net: Net):
    on = net.g_pmax > 0
    return np.where(on)[0]


def simulate(load_mw: float, p_gen: np.ndarray, pmax: np.ndarray, trip: list[int], *, stages=None, params=None, duration=60.0, out_points=240) -> dict:
    """Aggregate frequency response after losing generator indices `trip`. stages=None: no UFLS."""
    q = dict(DEFAULTS, **(params or {}))
    keep = np.ones(len(p_gen), bool); keep[trip] = False
    lost = float(p_gen[trip].sum())
    S = float(pmax[keep].sum())
    head = min(float(np.maximum(pmax[keep] - p_gen[keep], 0.0).sum()), q["reserve_pct"] / 100.0 * S)
    M = 2.0 * q["H"] * S                        # MW s per pu
    D = q["d_load"] * load_mw + q["d_gen"] * S
    gain = S / q["R"]                           # MW per pu frequency
    db = q["gov_deadband_hz"] / F0
    dt = q["dt"]; n = int(duration / dt)
    w = 0.0; pg = 0.0; pt = 0.0                 # governor valve (MW), turbine output (MW)
    shed = 0.0; shed_frac_total = 0.0
    below = [0.0] * len(stages or []); fired = [None] * len(stages or [])
    ts, fs, shs = [], [], []
    nadir, tn, rocof0 = 0.0, 0.0, None
    stride = max(1, n // out_points)
    for k in range(n):
        t = k * dt
        eff = 0.0 if abs(w) <= db else (w + db if w < -db else w - db)
        target = float(np.clip(-gain * eff, -head, head))
        pg += dt / q["Tg"] * (target - pg)
        pt += dt / q["Tt"] * (pg - pt)
        dP = pt - lost + shed                    # shed load reduces the demand (adds to the balance)
        dw = (dP - D * w) / M
        if rocof0 is None:
            rocof0 = dw * F0
        w += dt * dw
        f = F0 * (1 + w)
        for i, st in enumerate(stages or []):
            if fired[i] is None:
                below[i] = below[i] + dt if f < st["f"] else 0.0
                if below[i] >= st["delay"]:
                    fired[i] = t; shed += st["frac"] * load_mw; shed_frac_total += st["frac"]
        if f - F0 < nadir:
            nadir, tn = f - F0, t
        if k % stride == 0:
            ts.append(round(t, 3)); fs.append(round(f, 4)); shs.append(round(shed, 2))
    return dict(lost_mw=round(lost, 1), online_mw=round(S, 1), headroom_mw=round(head, 1), f_nadir=round(F0 + nadir, 3), t_nadir=round(tn, 2), f_final=round(f, 3),
                rocof0_hz_s=round(float(rocof0), 4), shed_mw=round(shed, 1), shed_pct=round(100 * shed_frac_total, 1),
                stages=[dict(f=s["f"], delay=s["delay"], frac=s["frac"], fired_at=None if fired[i] is None else round(fired[i], 2)) for i, s in enumerate(stages or [])],
                t=ts, f=fs, shed_curve=shs, beta_mw_per_hz=round((D + gain) / F0, 1))


def ufls(net: Net, *, scenarios=None, stages=None, params=None, nadir_min=48.8, final_min=49.5, duration=60.0, use_ufls=True) -> dict:
    t0 = time.perf_counter()
    on = _units(net)
    p, pm = net.g_p[on].astype(float), net.g_pmax[on].astype(float)
    load = float(sum(net.load_p0.values())) or float(net.pd.sum())
    order = np.argsort(-p)
    if not scenarios:
        tot = float(p.sum())
        scenarios = [dict(name="Loss of the largest unit", units=[int(order[0])]), dict(name="Loss of the two largest units", units=[int(order[0]), int(order[1])])] + \
                    [dict(name=f"Loss of {int(x)} % of generation", pct=x) for x in (15, 25, 35)]
    stg = STAGES if stages is None else stages
    rows, seen = [], set()
    for sc in scenarios:
        if "largest" in sc:
            trip = [int(i) for i in order[:max(1, min(int(sc["largest"]), len(order) - 1))]]
        elif "units" in sc:
            trip = list(sc["units"])
        else:                                   # largest units first until the percentage is reached
            need, trip, acc = sc["pct"] / 100.0 * float(p.sum()), [], 0.0
            for i in order:
                if acc >= need:
                    break
                trip.append(int(i)); acc += float(p[i])
        if tuple(sorted(trip)) in seen:
            continue                            # units are large, so neighbouring percentages can mean the same trip set
        seen.add(tuple(sorted(trip)))
        a = simulate(load, p, pm, trip, stages=None, params=params, duration=duration)
        b = simulate(load, p, pm, trip, stages=stg, params=params, duration=duration) if use_ufls else None
        res = b or a
        ok = res["f_nadir"] >= nadir_min and res["f_final"] >= final_min
        rows.append(dict(name=sc["name"], units_tripped=len(trip), lost_mw=a["lost_mw"], lost_pct=round(100 * a["lost_mw"] / float(p.sum()), 1),
                         without=a, with_ufls=b, passes=bool(ok), headroom_ok=a["headroom_mw"] >= 0))
    for r in rows:                              # keep the payload small: curves only for the first scenario of each kind
        pass
    return dict(scenarios=rows, load_mw=round(load, 1), params=dict(DEFAULTS, **(params or {})), stages=stg, criteria=dict(nadir_min=nadir_min, final_min=final_min),
                online_units=int(len(on)), synthetic_dynamics=True, stage_table_is_default=stages is None, wall_seconds=round(time.perf_counter() - t0, 2))


def _jain(x):
    x = np.asarray(x, float)
    return float(x.sum() ** 2 / (len(x) * (x ** 2).sum())) if len(x) and (x ** 2).sum() > 0 else 1.0


def _check(net: Net, shed_by_bus: dict, deficit: float):
    pd2, qd2 = net.pd.copy(), net.qd.copy()
    for b, mw in shed_by_bus.items():
        f = 1.0 - min(1.0, mw / net.pd[b]) if net.pd[b] > 0 else 1.0
        pd2[b] = net.pd[b] * f; qd2[b] = net.qd[b] * f
    gp = net.g_p * (1.0 - deficit / max(1e-9, float(net.g_p.sum())))
    n2 = dataclasses.replace(net, pd=pd2, qd=qd2, g_p=gp)
    s = pf.solve(n2)
    rated = s["rated"]
    over = [(n2.br_id[k], float(s["loading_pct"][k])) for k in range(n2.nl) if rated[k] and s["loading_pct"][k] > 100.0]
    live = np.ones(n2.nb, bool); live[s["dead_buses"]] = False
    vbad = [(n2.bus_name[i], float(s["vm"][i])) for i in range(n2.nb) if live[i] and not (0.9 - 1e-4 <= s["vm"][i] <= 1.1 + 1e-4)]
    return dict(converged=bool(s["converged"]), max_loading_pct=round(float(s["loading_pct"].max()), 1), overloads=len(over), voltage_violations=len(vbad),
                worst_overload=None if not over else max(over, key=lambda t: t[1])[0])


def roster(net: Net, *, deficit_mw: float, hours: int = 12, period_h: float = 1.0, overrides=None, min_class: int = 3, tolerance_pct: float = 5.0, rotate: bool = True, ac_check: bool = True) -> dict:
    t0 = time.perf_counter()
    pr = assign_priority(net.load_p0, overrides)
    elig = [m for m in net.load_p0 if pr[m] >= min_class and net.load_p0[m] > 0]
    cap = sum(net.load_p0[m] for m in elig)
    n_per = max(1, int(round(hours / period_h)))
    ens = {m: 0.0 for m in elig}
    periods, first = [], None
    tol = max(1.0, tolerance_pct / 100.0 * deficit_mw)
    checks = {}
    base_ac = _check(net, {}, 0.0) if ac_check else None

    def ac_of(key):
        if key not in checks:
            by_bus = {}
            for m in key:
                b = net.load_bus[m]; by_bus[b] = by_bus.get(b, 0.0) + net.load_p0[m]
            checks[key] = _check(net, by_bus, deficit_mw)
        return checks[key]

    def pick(order, skip):
        chosen, acc = [], 0.0
        for m in order:
            if acc >= deficit_mw - 0.5:
                break
            if m not in skip and acc + net.load_p0[m] <= deficit_mw + tol:
                chosen.append(m); acc += net.load_p0[m]
        for m in order:                         # nothing small enough left: accept a larger overshoot rather than a shortfall
            if acc >= deficit_mw - tol:
                break
            if m not in skip and m not in chosen:
                chosen.append(m); acc += net.load_p0[m]
        return chosen, acc

    for k in range(n_per):
        if rotate:
            order = sorted(elig, key=lambda m: (ens[m], -pr[m], m))
        else:                                   # static baseline: always the lowest-priority, then largest, loads
            order = sorted(elig, key=lambda m: (-pr[m], -net.load_p0[m], m))
        skip, tries = set(), 0
        chosen, acc = pick(order, skip)
        ac = ac_of(tuple(sorted(chosen))) if ac_check else None
        while ac_check and (not ac["converged"] or ac["overloads"] > base_ac["overloads"]) and tries < 4 and chosen:
            skip.add(max(chosen, key=lambda m: net.load_p0[m])); tries += 1        # heuristic: drop the biggest block and re-pick
            chosen, acc = pick(order, skip)
            ac = ac_of(tuple(sorted(chosen)))
        for m in chosen:
            ens[m] += net.load_p0[m] * period_h
        per = dict(k=k, start_h=round(k * period_h, 2), loads=chosen, shed_mw=round(acc, 1), shortfall_mw=round(max(0.0, deficit_mw - acc), 1))
        if ac_check:
            per["ac"] = ac; per["ac_retries"] = tries
        periods.append(per)
    served = [ens[m] for m in elig]
    ever = sum(1 for m in elig if ens[m] > 0)
    return dict(deficit_mw=deficit_mw, hours=hours, period_h=period_h, rotate=rotate, min_class=min_class, eligible_loads=len(elig), eligible_mw=round(cap, 1),
                feasible=cap >= deficit_mw - tol, periods=periods, loads_ever_shed=ever, jain_fairness=round(_jain(served), 3),
                max_ens_mwh=round(max(served), 1) if served else 0.0, ens_mwh={m: round(v, 1) for m, v in ens.items() if v > 0}, total_ens_mwh=round(sum(served), 1),
                classes={m: pr[m] for m in elig}, synthetic_priorities=not overrides, ac_checked=ac_check, ac_base=base_ac, wall_seconds=round(time.perf_counter() - t0, 2))
