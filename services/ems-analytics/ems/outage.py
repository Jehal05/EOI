"""Planned-outage security study, conflict detection and switching-plan generation. ADVISORY: nothing here operates a device.

Study: the equipment (and every concurrently planned outage) is isolated by opening its adjacent breakers on the live
topology overlay; the resulting AC base case and the full AC N-1 sweep are compared with the intact network. Verdicts:
  secure      no new islanded load, no new base-case violation, no outage that is secure on the intact network becomes insecure
  restricted  base case fine but N-1 security is lost for the listed contingencies while the outage is in force
  unsafe      islanded load, a non-converging base case or a base-case violation caused by the outage
  invalid     unknown equipment id
Switching plan: breaker steps ordered so the first step is the one with the smallest effect, each step verified with an AC
power flow (convergence, islanded load, new overloads, voltage band), followed by the field steps the CIM does not model
(isolators, earths, permit) marked manual. Restoration is the reverse. Limits: only equipment with breakers in the model
(lines, transformers; generators, loads and shunts are isolated the same way but their outage effect is a loss of injection);
no isolator / earth-switch interlocking, synchro-check or protection modelling; steps are never executed by this module.
"""
from __future__ import annotations
import hashlib
import json
import time
import numpy as np
from .network import Net, UNRATED
from . import contingency as ca, powerflow as pf

VMIN, VMAX = 0.95, 1.05


class Cim:
    def __init__(self, d: dict):
        self.d = d
        self.nodes = {n["mrid"]: n for n in d["connectivityNodes"]}
        self.vl = {v["mrid"]: v for v in d["voltageLevels"]}
        self.sub = {s["mrid"]: s for s in d["substations"]}
        self.sw = {s["mrid"]: s for s in d["switches"]}
        self.eq = {}
        for k, key in (("line", "acLineSegments"), ("transformer", "transformers")):
            for e in d.get(key, []):
                self.eq[e["mrid"]] = dict(kind=k, name=e["name"], terminals=[e["node1"], e["node2"]])
        for k, key in (("generator", "generators"), ("load", "loads"), ("shunt", "shunts")):
            for e in d.get(key, []):
                self.eq[e["mrid"]] = dict(kind=k, name=e["name"], terminals=[e["node"]])
        self.adj: dict[str, list[str]] = {}
        for s in d["switches"]:
            for n in (s["node1"], s["node2"]):
                self.adj.setdefault(n, []).append(s["mrid"])

    def location(self, mrid):
        s = self.sw[mrid]
        for n in (s["node1"], s["node2"]):
            node = self.nodes.get(n)
            if node and node.get("voltageLevel") in self.vl:
                v = self.vl[node["voltageLevel"]]
                return self.sub.get(v["substation"], {}).get("name", v["substation"]), v["nominalKV"]
        return None, None

    def isolation(self, mrid):
        """Switches that isolate the equipment: (breakers, other switch types) adjacent to each terminal."""
        e = self.eq.get(mrid)
        if e is None:
            return None
        out = []
        for t in e["terminals"]:
            for sm in self.adj.get(t, []):
                s = self.sw[sm]
                out.append(dict(switch=sm, type=s["type"], name=s["name"], terminal=t))
        out.sort(key=lambda x: (x["type"] != "Breaker", x["switch"]))
        return out


_NET_CACHE: dict = {}


def _net(loader, open_set):
    key = tuple(sorted(open_set))
    if key not in _NET_CACHE:
        if len(_NET_CACHE) > 12:
            _NET_CACHE.pop(next(iter(_NET_CACHE)))
        _NET_CACHE[key] = loader(list(key))
    return _NET_CACHE[key]


def clear_cache():
    _NET_CACHE.clear()


def _violations(net: Net, sol):
    rated = (net.rate > 0) & (net.rate < UNRATED)
    over = {net.br_id[k]: float(sol["loading_pct"][k]) for k in range(net.nl) if rated[k] and sol["loading_pct"][k] > 100.0}
    live = np.ones(net.nb, dtype=bool)
    live[sol["dead_buses"]] = False
    vv = {net.bus_name[i]: float(sol["vm"][i]) for i in range(net.nb) if live[i] and not (VMIN - 1e-4 <= sol["vm"][i] <= VMAX + 1e-4)}
    return over, vv


def isolating_breakers(cim: Cim, equipment, live_open):
    br = []
    unknown = []
    for e in equipment:
        iso = cim.isolation(e)
        if iso is None:
            unknown.append(e)
            continue
        br += [x["switch"] for x in iso if x["type"] == "Breaker" and x["switch"] not in live_open]
    return sorted(set(br)), unknown


def study(cim: Cim, loader, equipment, concurrent=(), live_open=(), max_new=10) -> dict:
    """loader(open_list) -> Net with that breaker overlay."""
    t0 = time.perf_counter()
    live_open = list(live_open)
    br, unknown = isolating_breakers(cim, equipment, set(live_open))
    if unknown:
        return dict(verdict="invalid", unknown_equipment=unknown, isolating_breakers=br, notes=["Unknown equipment id(s): " + ", ".join(unknown)])
    conc_br = []
    for group in concurrent:
        b, _ = isolating_breakers(cim, group, set(live_open))
        conc_br += b
    open_out = sorted(set(live_open) | set(br) | set(conc_br))
    net_i, net_o = _net(loader, live_open), _net(loader, open_out)
    si, so = pf.solve(net_i), pf.solve(net_o)
    notes = []
    if not so["converged"]:
        return dict(verdict="unsafe", islanded_load_mw=None, base_violations=[dict(kind="power flow", detail="base case does not converge with the outage")],
                    new_critical=[], isolating_breakers=br, n_concurrent=len(list(concurrent)), notes=["AC power flow did not converge in the outage state."],
                    wall_seconds=round(time.perf_counter() - t0, 2))
    oi, vi = _violations(net_i, si)
    oo, vo = _violations(net_o, so)
    islanded = max(0.0, float(so["lost_load_mw"]) - float(si["lost_load_mw"]))
    # a planned load outage legitimately loses that load
    own = 0.0
    for e in list(equipment) + [x for g in concurrent for x in g]:
        if cim.eq.get(e, {}).get("kind") == "load":
            own += float(next((l["p"] for l in cim.d["loads"] if l["mrid"] == e), 0.0))
    islanded = max(0.0, islanded - own)
    base_v = [dict(kind="overload", detail=f"{b} at {l:.0f} %") for b, l in sorted(oo.items(), key=lambda t: -t[1]) if b not in oi or l > oi[b] + 1.0]
    base_v += [dict(kind="voltage", detail=f"{b} at {v:.3f} pu") for b, v in vo.items() if b not in vi]
    n1_i = {r["outage"]: r for r in ca.run_n1(net_i, base_sol=si)["results"]}
    n1_o = ca.run_n1(net_o, base_sol=so)["results"]
    new = []
    for r in n1_o:
        if r["severity"] <= 0:
            continue
        before = n1_i.get(r["outage"])
        if before is None or before["severity"] <= 0:
            new.append(dict(outage=r["outage"], severity=round(r["severity"], 1), islanded=r["islanded"], lost_load_mw=round(r["lost_load_mw"], 1),
                            worst=(r["overloads"][0] if r["overloads"] else None)))
    new.sort(key=lambda r: -r["severity"])
    if islanded > 0.5 or base_v:
        verdict = "unsafe"
    elif new:
        verdict = "restricted"
    else:
        verdict = "secure"
    if not br:
        notes.append("All isolating breakers are already open.")
    notes.append("Breaker-isolated equipment is treated as out of service (open-ended); no isolator or earth-switch modelling.")
    return dict(verdict=verdict, islanded_load_mw=round(islanded, 1), base_violations=base_v[:10], new_critical=new[:max_new], n_new_critical=len(new),
                worst_loading_pct=round(float(so["loading_pct"].max()), 1), isolating_breakers=br, n_concurrent=len(list(concurrent)), notes=notes,
                wall_seconds=round(time.perf_counter() - t0, 2))


def _state(net: Net, sol, ref_over, ref_vv, ref_lost):
    over, vv = _violations(net, sol)
    new_over = {b: l for b, l in over.items() if b not in ref_over or l > ref_over[b] + 1.0}
    new_vv = {b: v for b, v in vv.items() if b not in ref_vv}
    lost = max(0.0, float(sol["lost_load_mw"]) - ref_lost)
    return dict(converged=bool(sol["converged"]), islanded_load_mw=round(lost, 1), max_loading_pct=round(float(sol["loading_pct"].max()), 1),
                new_overloads=[dict(branch=b, loading_pct=round(l, 0)) for b, l in sorted(new_over.items(), key=lambda t: -t[1])[:5]], new_voltage_violations=len(new_vv))


def _safe(st):
    return st["converged"] and st["islanded_load_mw"] <= 0.5 and not st["new_overloads"] and st["new_voltage_violations"] == 0


MANUAL_ISOLATE = ["Obtain the permit to work and confirm the work window with the neighbouring control centre (not modelled).",
                  "Open and lock the line / transformer isolators at every terminal (the CIM carries no isolators).",
                  "Prove the equipment dead with a voltage detector at the work location.",
                  "Apply the earths and record their positions (the CIM carries no earthing switches)."]
MANUAL_RESTORE = ["Confirm the permit is cancelled, personnel are clear and the earths are removed (not modelled).",
                  "Close the isolators at every terminal (not modelled).",
                  "Check the equipment with a pre-energisation test as per local practice (not modelled)."]


def plan(cim: Cim, loader, equipment, mode="isolate", live_open=()) -> dict:
    t0 = time.perf_counter()
    live_open = list(live_open)
    unknown = [e for e in equipment if cim.eq.get(e) is None]
    if unknown:
        return dict(status="invalid", unknown_equipment=unknown, steps=[])
    ref_net = _net(loader, live_open)
    ref = pf.solve(ref_net)
    ref_over, ref_vv = _violations(ref_net, ref)
    ref_lost = float(ref["lost_load_mw"])
    # the outage itself is the reference for "restore", the intact network for "isolate"
    brk, _ = isolating_breakers(cim, equipment, set(live_open))
    if mode == "restore":
        all_iso = [x["switch"] for e in equipment for x in cim.isolation(e) if x["type"] == "Breaker"]
        brk = sorted(set(s for s in all_iso if s in set(live_open)))
    steps, cur = [], set(live_open)
    pending = list(brk)
    action = "close" if mode == "restore" else "open"
    # restore reference = fully intact network (live open set without these breakers)
    if mode == "restore":
        intact_open = sorted(set(live_open) - set(brk))
        ref_net = _net(loader, intact_open)
        ref = pf.solve(ref_net)
        ref_over, ref_vv = _violations(ref_net, ref)
        ref_lost = float(ref["lost_load_mw"])
    all_safe = True
    seq = 0
    if mode == "isolate":
        for t in MANUAL_ISOLATE[:1]:
            seq += 1; steps.append(dict(seq=seq, manual=True, in_model=False, text=t))
    else:
        for t in MANUAL_RESTORE:
            seq += 1; steps.append(dict(seq=seq, manual=True, in_model=False, text=t))
    while pending:
        best = None
        for sm in pending:
            trial = (cur | {sm}) if action == "open" else (cur - {sm})
            net = _net(loader, trial)
            st = _state(net, pf.solve(net), ref_over, ref_vv, ref_lost)
            key = (not _safe(st), st["islanded_load_mw"], len(st["new_overloads"]), st["max_loading_pct"], sm)
            if best is None or key < best[0]:
                best = (key, sm, st, trial)
        _, sm, st, cur = best
        pending.remove(sm)
        loc, kv = cim.location(sm)
        seq += 1
        steps.append(dict(seq=seq, manual=False, in_model=True, action=action, switch=sm, tag=f"{sm}.STATE", name=cim.sw[sm]["name"], substation=loc, kv=kv, check=st, safe=_safe(st),
                          interlock="synchro-check at the closing end (not modelled)" if action == "close" else None))
        all_safe &= _safe(st)
    if mode == "isolate":
        for t in MANUAL_ISOLATE[1:]:
            seq += 1; steps.append(dict(seq=seq, manual=True, in_model=False, text=t))
    digest = hashlib.sha256(json.dumps([(s.get("switch"), s.get("action"), s.get("text")) for s in steps], sort_keys=True).encode()).hexdigest()[:16]
    return dict(status="ok" if all_safe else "review", mode=mode, equipment=list(equipment), steps=steps, n_breaker_steps=len(brk), plan_hash=digest,
                wall_seconds=round(time.perf_counter() - t0, 2),
                notes=["Advisory: execute each breaker step through the normal select-before-operate command path; nothing is sent from here.",
                       "Each breaker step was checked with an AC power flow against the " + ("intact" if mode == "isolate" else "restored") + " network.",
                       "Equipment with one terminal open is modelled out of service, so later breaker steps change nothing electrically."])


def segments(requests):
    """Sweep line over [start, end) windows. requests: list of dict(id, equipment, start, end). Returns time segments with the active ids."""
    pts = sorted({r["start"] for r in requests} | {r["end"] for r in requests})
    segs = []
    for a, b in zip(pts, pts[1:]):
        act = [r for r in requests if r["start"] <= a and r["end"] >= b]
        if len(act) > 0:
            segs.append(dict(start=a, end=b, ids=[r["id"] for r in act], equipment=[r["equipment"] for r in act]))
    return segs


RANK = {"secure": 0, "restricted": 1, "unsafe": 2, "invalid": 3}


def schedule(cim: Cim, loader, requests, live_open=(), max_segments=40) -> dict:
    """Study every distinct overlapping set of outages. A CONFLICT is a segment whose combined verdict is worse than the
    worst verdict of any member studied alone (the overlap itself creates the problem)."""
    t0 = time.perf_counter()
    segs = segments(requests)[:max_segments]
    by_id = {r["id"]: r for r in requests}
    single = {}
    out, cache = [], {}
    for s in segs:
        for i in s["ids"]:
            if i not in single:
                single[i] = study(cim, loader, by_id[i]["equipment"], live_open=live_open, max_new=3)["verdict"]
        key = tuple(sorted(s["ids"]))
        if key not in cache:
            eqs = s["equipment"]
            cache[key] = study(cim, loader, eqs[0], concurrent=eqs[1:], live_open=live_open, max_new=5) if len(eqs) > 1 else None
        r = cache[key]
        verdict = r["verdict"] if r else single[s["ids"][0]]
        worst_single = max((RANK[single[i]] for i in s["ids"]), default=0)
        out.append(dict(**s, verdict=verdict, escalated=RANK[verdict] > worst_single, singles={str(i): single[i] for i in s["ids"]},
                        islanded_load_mw=None if not r else r.get("islanded_load_mw"), n_new_critical=0 if not r else r.get("n_new_critical", 0)))
    conflicts = [s for s in out if s["escalated"]]
    return dict(segments=out, conflicts=conflicts, n_segments=len(out), wall_seconds=round(time.perf_counter() - t0, 2),
                notes=["A conflict is an overlap whose combined topology is less secure than the worst member alone."])


def suggest(cim: Cim, loader, equipment, duration_ms, earliest, latest, existing, step_ms=3_600_000, live_open=(), want=3, max_try=24):
    """First start times in [earliest, latest] where adding the outage does not make the combined state worse than the outage alone."""
    alone = RANK[study(cim, loader, equipment, live_open=live_open, max_new=1)["verdict"]]
    found, tried = [], 0
    t = earliest
    while t + duration_ms <= latest and len(found) < want and tried < max_try:
        conc = [e["equipment"] for e in existing if e["start"] < t + duration_ms and e["end"] > t]
        if conc:
            r = study(cim, loader, equipment, concurrent=conc, live_open=live_open, max_new=1)
            tried += 1
            ok = RANK[r["verdict"]] <= max([alone] + [RANK[study(cim, loader, c, live_open=live_open, max_new=1)["verdict"]] for c in conc])
        else:
            ok = True
        if ok:
            found.append(dict(start=t, end=t + duration_ms, concurrent=len(conc)))
        t += step_ms
    return dict(suggestions=found, tried=tried, verdict_alone=[k for k, v in RANK.items() if v == alone][0])
