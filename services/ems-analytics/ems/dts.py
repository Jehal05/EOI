"""Dispatcher training simulator (DTS). SIMULATION ONLY: a session owns a private copy of the network, never reads or writes live data
and never reaches the core, so nothing a trainee does can affect operations.

Model: a session holds the CIM-derived network plus the trainee's and scenario's changes (branches out, generators tripped or re-dispatched,
load scaled or shed). Time advances in event-aligned segments; each segment is one AC power flow (the slack absorbs imbalance, no frequency
dynamics: use the load-shed/AGC tools for those). Scenario events: branch_trip (a fault that blocks reclosing until `clear_after`), gen_trip,
load_scale. `stuck` branches refuse to open (failed breaker). Violations: loading > 100 % of the rating, voltage outside 0.95-1.05 pu
(+-1e-4), slack above its maximum, load lost to islanding.
Score (0-100, a TRAINING AID, not a certification; every term is returned): 50 x (1 - insecure_seconds / duration)
 + 20 if the final state is secure + 15 x (1 - lost_load_MWs / (5 % of load x duration)) + 15 - 5 x harmful actions - 1 x refused actions (min 0).
 A harmful action raises the violation count or sheds a critical-class load.
"""
from __future__ import annotations
import time, uuid
import numpy as np
from .network import Net
from . import powerflow as pf, shed as shedm

VMIN, VMAX, TOL = 0.95, 1.05, 1e-4
MAX_SESSIONS, TTL_S = 50, 2 * 3600


def library(net: Net) -> list[dict]:
    base = pf.solve(net)
    order = [int(k) for k in np.argsort(-np.where(base["rated"], base["loading_pct"], 0))]
    ids = [net.br_id[k] for k in order]
    big = max(range(len(net.g_id)), key=lambda i: net.g_p[i])
    return [
        dict(id="line-trip", name="Line trip", description=f"The most loaded line ({ids[0]}) trips on a fault. Restore a secure state.", duration=600,
             events=[dict(t=30, kind="branch_trip", id=ids[0])], stuck=[]),
        dict(id="gen-trip", name="Generator trip", description=f"The largest unit ({net.g_id[big]}, {net.g_p[big]:.0f} MW) trips. The slack picks up the loss; relieve the network.", duration=600,
             events=[dict(t=30, kind="gen_trip", id=net.g_id[big])], stuck=[]),
        dict(id="double", name="Double event", description=f"Two heavily loaded lines ({ids[0]}, {ids[1]}) trip 60 s apart.", duration=900,
             events=[dict(t=30, kind="branch_trip", id=ids[0]), dict(t=90, kind="branch_trip", id=ids[1])], stuck=[]),
        dict(id="surge", name="Load surge", description="Demand rises 12 % everywhere at t=30 s, then 6 % more at t=150 s.", duration=600,
             events=[dict(t=30, kind="load_scale", id="*", factor=1.12), dict(t=150, kind="load_scale", id="*", factor=1.06)], stuck=[]),
        dict(id="stuck-breaker", name="Stuck breaker", description=f"{ids[0]} trips and the breaker of {ids[2]} will not open.", duration=600,
             events=[dict(t=30, kind="branch_trip", id=ids[0])], stuck=[ids[2]]),
    ]


def validate_scenario(net: Net, sc: dict) -> dict:
    out = dict(id=str(sc.get("id", ""))[:40] or "custom", name=str(sc.get("name", "Custom scenario"))[:60], description=str(sc.get("description", ""))[:300],
               duration=float(sc.get("duration", 600)), events=[], stuck=[])
    if not 30 <= out["duration"] <= 7200:
        raise ValueError("duration must be 30 to 7200 s")
    evs = sc.get("events", [])
    if not 1 <= len(evs) <= 50:
        raise ValueError("1 to 50 events required")
    for e in evs:
        k, t = e.get("kind"), float(e.get("t", -1))
        if not 0 <= t <= out["duration"]:
            raise ValueError("event time outside the scenario")
        if k == "branch_trip":
            if e.get("id") not in net.br_id:
                raise ValueError(f"unknown branch {e.get('id')}")
            out["events"].append(dict(t=t, kind=k, id=e["id"], clear_after=float(e["clear_after"]) if e.get("clear_after") is not None else None))
        elif k == "gen_trip":
            if e.get("id") not in net.g_id:
                raise ValueError(f"unknown generator {e.get('id')}")
            out["events"].append(dict(t=t, kind=k, id=e["id"]))
        elif k == "load_scale":
            f = float(e.get("factor", 1))
            if not 0.2 <= f <= 3 or (e.get("id") != "*" and e.get("id") not in net.load_p0):
                raise ValueError("bad load_scale event")
            out["events"].append(dict(t=t, kind=k, id=e["id"], factor=f))
        else:
            raise ValueError(f"unknown event kind {k}")
    for s in sc.get("stuck", []):
        if s not in net.br_id:
            raise ValueError(f"unknown branch {s}")
        out["stuck"].append(s)
    out["events"].sort(key=lambda e: e["t"])
    return out


class Session:
    def __init__(self, net: Net, scenario: dict, user: str, model: str):
        self.id = uuid.uuid4().hex[:12]
        self.net, self.sc, self.user, self.model = net, scenario, user, model
        self.created = time.time(); self.t = 0.0; self.ended = False
        self.out: set[str] = set(); self.locked: dict[str, float] = {}          # branch -> time before which reclosing is refused (inf = never)
        self.gen_off: set[str] = set(); self.gen_mw: dict[str, float] = {}
        self.scale: dict[str, float] = {}; self.shed: dict[str, float] = {}
        self.pr = shedm.assign_priority(net.load_p0)
        self.log: list[dict] = []; self.next_ev = 0
        self.insecure_s = 0.0; self.lost_mws = 0.0; self.actions = 0; self.harmful = 0; self.refused = 0
        self.state = self._solve()
        self.prev_viol = {v["key"] for v in self.state["violations"]}

    # ---------------------------------------------------------------- network
    def _build(self) -> Net:
        n = self.net.copy()
        keep = [i for i, g in enumerate(n.g_id) if g not in self.gen_off]
        for a in ("g_bus", "g_p", "g_q", "g_qmax", "g_qmin", "g_vset", "g_pmax", "g_pmin"):
            setattr(n, a, getattr(n, a)[keep])
        for a in ("g_c2", "g_c1", "g_c0"):
            if getattr(n, a, None) is not None:
                setattr(n, a, getattr(n, a)[keep])
        n.g_id = [n.g_id[i] for i in keep]
        for i, g in enumerate(n.g_id):
            if g in self.gen_mw:
                n.g_p[i] = self.gen_mw[g]
        pd, qd = self.net.pd.copy(), self.net.qd.copy()
        f_all = self.scale.get("*", 1.0)
        pd *= f_all; qd *= f_all
        for m, f in self.scale.items():
            if m != "*":
                b = self.net.load_bus[m]; d = self.net.load_p0[m] * (f - 1.0); pd[b] += d; qd[b] += d * (self.net.qd[b] / self.net.pd[b] if self.net.pd[b] else 0)
        for m, mw in self.shed.items():
            b = self.net.load_bus[m]; frac = mw / pd[b] if pd[b] > 0 else 0
            qd[b] -= qd[b] * min(1.0, frac); pd[b] = max(0.0, pd[b] - mw)
        n.pd, n.qd = pd, qd
        idx = [k for k, b in enumerate(n.br_id) if b in self.out]
        if idx:
            return n.without_branches(idx)
        n.analyze_islands()
        return n

    def _solve(self) -> dict:
        n = self._build()
        s = pf.solve(n)
        viol = []
        for k in range(n.nl):
            if s["rated"][k] and s["loading_pct"][k] > 100.0 + 1e-6:
                viol.append(dict(kind="overload", id=n.br_id[k], value=round(float(s["loading_pct"][k]), 1), key=f"ov:{n.br_id[k]}"))
        live = np.ones(n.nb, bool); live[s["dead_buses"]] = False
        for i in range(n.nb):
            if live[i] and not (VMIN - TOL <= s["vm"][i] <= VMAX + TOL):
                viol.append(dict(kind="voltage", id=n.bus_name[i], value=round(float(s["vm"][i]), 4), key=f"v:{n.bus_name[i]}"))
        slack = n.slack_bus
        sg = [i for i, b in enumerate(n.g_bus) if b == slack]
        slack_mw = float(s["gen_p"][sg].sum()) if sg else 0.0
        slack_max = float(n.g_pmax[sg].sum()) if sg else 0.0
        if sg and slack_mw > slack_max + 1e-6:
            viol.append(dict(kind="slack", id=n.bus_name[slack], value=round(slack_mw, 1), key="slack"))
        lost = float(s["lost_load_mw"])
        if lost > 0.05:
            viol.append(dict(kind="load_lost", id="islanding", value=round(lost, 1), key="lost"))
        top = sorted(((n.br_id[k], float(s["loading_pct"][k])) for k in range(n.nl) if s["rated"][k]), key=lambda x: -x[1])[:8]
        return dict(t=round(self.t, 1), secure=not viol and bool(s["converged"]), converged=bool(s["converged"]), violations=sorted(viol, key=lambda v: -v["value"] if v["kind"] == "overload" else 0)[:40],
                    n_violations=len(viol), max_loading=round(float(s["loading_pct"].max()), 1), top_loading=[dict(id=i, pct=round(p, 1)) for i, p in top],
                    vm_min=round(float(s["vm"][live].min()), 4), vm_max=round(float(s["vm"][live].max()), 4), lost_load_mw=round(lost, 1),
                    slack_mw=round(slack_mw, 1), slack_max=round(slack_max, 1), out_branches=sorted(self.out), gens_off=sorted(self.gen_off),
                    shed={m: round(v, 1) for m, v in self.shed.items()}, losses_mw=round(float(s["losses_mw"]), 1))

    def _note(self, kind: str, text: str):
        self.log.append(dict(t=round(self.t, 1), kind=kind, text=text))

    def _refresh(self):
        self.state = self._solve()
        cur = {v["key"]: v for v in self.state["violations"]}
        for k, v in cur.items():
            if k not in self.prev_viol:
                self._note("alarm", f"{v['kind']} {v['id']} {v['value']}")
        for k in self.prev_viol - set(cur):
            self._note("clear", f"cleared {k}")
        self.prev_viol = set(cur)

    # ---------------------------------------------------------------- time
    def step(self, dt: float) -> dict:
        if self.ended:
            raise ValueError("session has ended")
        end = min(self.sc["duration"], self.t + dt)
        evs = self.sc["events"]
        while True:
            changed = False
            while self.next_ev < len(evs) and evs[self.next_ev]["t"] <= self.t + 1e-9:
                self._apply(evs[self.next_ev]); self.next_ev += 1; changed = True
            if changed:
                self._refresh()
            if self.t >= end - 1e-9:
                break
            seg_end = min(end, evs[self.next_ev]["t"] if self.next_ev < len(evs) else float("inf"))
            d = seg_end - self.t
            self.insecure_s += d if not self.state["secure"] else 0.0
            self.lost_mws += d * self.state["lost_load_mw"]
            self.t = seg_end
        self.state["t"] = round(self.t, 1)
        return self.state

    def _apply(self, e):
        k = e["kind"]
        if k == "branch_trip":
            self.out.add(e["id"]); self.locked[e["id"]] = self.t + e["clear_after"] if e.get("clear_after") is not None else float("inf")
            self._note("event", f"FAULT: {e['id']} tripped")
        elif k == "gen_trip":
            self.gen_off.add(e["id"]); self._note("event", f"TRIP: generator {e['id']} lost")
        elif k == "load_scale":
            self.scale[e["id"]] = self.scale.get(e["id"], 1.0) * e["factor"]; self._note("event", f"LOAD: {e['id']} x{e['factor']}")

    # ---------------------------------------------------------------- trainee actions
    def act(self, action: str, target: str, value: float | None = None) -> dict:
        if self.ended:
            raise ValueError("session has ended")
        before = self.state["n_violations"]
        res = dict(ok=True, message="")
        if action == "open_branch":
            if target not in self.net.br_id: raise ValueError("unknown branch")
            if target in self.out: res = dict(ok=False, message="already open")
            elif target in self.sc["stuck"]: res = dict(ok=False, message="breaker failed to operate")
            else: self.out.add(target)
        elif action == "close_branch":
            if target not in self.net.br_id: raise ValueError("unknown branch")
            if target not in self.out: res = dict(ok=False, message="already closed")
            elif self.t < self.locked.get(target, 0.0): res = dict(ok=False, message="locked out: fault not cleared")
            else: self.out.discard(target)
        elif action == "set_gen":
            if target not in self.net.g_id or target in self.gen_off: raise ValueError("unknown or tripped generator")
            i = self.net.g_id.index(target); cur = self.gen_mw.get(target, float(self.net.g_p[i])); v = float(value)
            lo, hi = float(self.net.g_pmin[i]), float(self.net.g_pmax[i])
            if not lo - 1e-9 <= v <= hi + 1e-9: res = dict(ok=False, message=f"outside {lo:.0f} to {hi:.0f} MW")
            elif abs(v - cur) > 0.2 * hi + 1e-9: res = dict(ok=False, message="a single move is limited to 20 % of the rating")
            else: self.gen_mw[target] = v
        elif action == "shed_load":
            if target not in self.net.load_p0: raise ValueError("unknown load")
            v = float(value); room = self.net.load_p0[target] * self.scale.get("*", 1.0) * self.scale.get(target, 1.0) - self.shed.get(target, 0.0)
            if not 0 < v <= room + 1e-9: res = dict(ok=False, message=f"0 to {room:.1f} MW can be shed")
            else:
                self.shed[target] = self.shed.get(target, 0.0) + v
                if self.pr[target] == 1: self.harmful += 1; res["message"] = "critical load shed"
        else:
            raise ValueError("unknown action")
        self.actions += 1
        if not res["ok"]:
            self.refused += 1
        else:
            self._refresh()
            if self.state["n_violations"] > before: self.harmful += 1; res["message"] = (res["message"] + " violations increased").strip()
        self._note("action", f"{action} {target}{'' if value is None else ' ' + str(value)}: {'ok' if res['ok'] else 'REFUSED ' + res['message']}{'; ' + res['message'] if res['ok'] and res['message'] else ''}")
        res["state"] = self.state
        return res

    def score(self) -> dict:
        D = self.sc["duration"]
        tl = sum(self.net.load_p0.values()) or float(self.net.pd.sum())
        a = 50 * max(0.0, 1 - self.insecure_s / D)
        b = 20.0 if self.state["secure"] else 0.0
        c = 15 * max(0.0, 1 - self.lost_mws / (0.05 * tl * D))
        d = max(0.0, 15 - 5 * self.harmful - 1 * self.refused)
        return dict(total=round(a + b + c + d, 1), security_time=round(a, 1), final_state=b, load_kept=round(c, 1), action_quality=round(d, 1), insecure_s=round(self.insecure_s, 1),
                    duration_s=D, lost_load_mws=round(self.lost_mws, 1), actions=self.actions, harmful=self.harmful, refused=self.refused, final_secure=self.state["secure"])


class Store:
    def __init__(self):
        self.s: dict[str, Session] = {}

    def add(self, s: Session):
        now = time.time()
        for k in [k for k, v in self.s.items() if now - v.created > TTL_S]:
            del self.s[k]
        if len(self.s) >= MAX_SESSIONS:
            raise OverflowError("too many training sessions")
        self.s[s.id] = s

    def get(self, sid: str, user: str, admin=False) -> Session:
        s = self.s.get(sid)
        if s is None or (s.user != user and not admin):
            raise KeyError(sid)
        return s


STORE = Store()
