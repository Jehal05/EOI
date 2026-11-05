#!/usr/bin/env python3
"""Field / physics simulator.

Plays the role of the physical grid behind the outstation: it owns the TRUTH of breaker
positions, applies a synthetic load profile, solves AC power flow (our own ems library) and
pushes measurements + statuses to the outstation simulator (/ingest). Breaker commands arrive from
the outstation (/command) and change the truth, which then flows back up through
outstation -> IEC 104 -> FEP -> SCADA. Also supports scripted events (trip / cascade).

Simulation only: no auth on this port, bind to loopback.
"""
import os, sys, time, math, threading, random, json
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "services", "ems-analytics"))
import httpx, numpy as np, uvicorn
from fastapi import FastAPI, HTTPException
from ems.network import Net
from ems.powerflow import solve
from ems import auth as gsauth

CIM = os.environ.get("CIM_URL", "http://127.0.0.1:8081")
MODEL = os.environ.get("GS_MODEL", "ieee118")
OUTST = os.environ.get("OUTSTATION_URL", "http://127.0.0.1:8090")
TICK = float(os.environ.get("TICK_S", "2.0"))
SECRET = os.environ.get("GS_SECRET") or ("dev-secret-do-not-use-in-prod" if os.environ.get("GS_DEV") == "1" else None)
if not SECRET:
    sys.exit("GS_SECRET required (GS_DEV=1 for development)")


if os.environ.get("GS_SECRET"):
    os.environ["EMS_SECRET"] = os.environ["GS_SECRET"]
else:
    os.environ["EMS_DEV"] = "1"


def token():
    return gsauth.mint("field-sim", "viewer", ["*"], 600)


class Field:
    def __init__(self):
        self.lock = threading.RLock()
        self.http = httpx.Client(timeout=20)
        h = {"Authorization": "Bearer " + token()}
        self.points = self.http.get(f"{CIM}/models/{MODEL}/pointlist", headers=h).json()["points"]
        self.cim = self.http.get(f"{CIM}/models/{MODEL}", headers=h).json()
        self.open = {s["mrid"] for s in self.cim["switches"] if s["open"]}
        self.by_tag = {p["tag"]: p for p in self.points}
        self.cache = {}
        self.t0 = time.time()
        self.load_scale = 1.0
        self.noise = 0.002
        self.last = {}
        self.tick_ms = 0.0
        self.ticks = 0
        self.failures = 0
        self.rng = random.Random(42)
        self.scenario = None
        self.bias = {}
        self.events = []

    # --- topology (via cim-pds operational overlay; cached per breaker-state set)
    def net_for(self, open_set):
        key = tuple(sorted(open_set))
        if key not in self.cache:
            h = {"Authorization": "Bearer " + token()}
            q = f"?open={','.join(key)}" if key else ""
            bb = self.http.get(f"{CIM}/models/{MODEL}/busbranch{q}", headers=h).json()
            net = Net.from_busbranch(bb)
            bus_of_node = {n: b["idx"] for b in bb["buses"] for n in b["nodes"]}
            self.cache = {key: (bb, net, bus_of_node)} if len(self.cache) > 8 else {**self.cache, key: (bb, net, bus_of_node)}
        return self.cache[key]

    def profile(self, now):
        # synthetic daily shape compressed to a 10-minute cycle: base 1.0 +/- 12%
        ph = ((now - self.t0) / 600.0) * 2 * math.pi
        return self.load_scale * (1.0 + 0.12 * math.sin(ph) + 0.03 * math.sin(3 * ph + 1.0))

    def step(self):
        t = time.time()
        with self.lock:
            open_set = set(self.open)
            bb, net, bus_of_node = self.net_for(open_set)
            k = self.profile(t)
            net = net.copy()
            net.pd = net.pd * k
            net.qd = net.qd * k
            net.g_p = net.g_p * k          # dispatch follows load; slack picks up the losses
            res = solve(net, enforce_q_limits=True)
            vals = []
            ok = res["converged"]

            def A(tag, v):
                p = self.by_tag.get(tag)
                if p is None:
                    return
                v = float(v) * (1 + self.rng.gauss(0, self.noise)) if v else 0.0
                v += self.bias.get(tag, 0.0)
                vals.append({"ca": p["ca"], "ioa": p["ioa"], "v": round(v, 4), "q": 0 if ok else 0x40})

            for tag, p in self.by_tag.items():
                if p["kind"] == "status":
                    vals.append({"ca": p["ca"], "ioa": p["ioa"], "dpi": 1 if p["element"] in open_set else 2})
            for b in bb["buses"]:
                kv = b["baseKV"]
                dead = b["idx"] in res["dead_buses"]
                for nd in b["nodes"]:
                    A(f"{nd}.V", 0.0 if dead else res["vm"][b["idx"]] * kv)
            br_idx = {m: i for i, m in enumerate(net.br_id)}
            for p in self.points:
                if p["kind"] != "analog" or not p["tag"].startswith("BR"):
                    continue
            brs = {x["mrid"] for x in bb["branches"]}
            for tag, p in self.by_tag.items():
                if p["kind"] != "analog" or not tag.startswith("BR"):
                    continue
                el = p["element"]
                end = tag.split("-")[1].split(".")[0]
                q = tag.rsplit(".", 1)[1]
                if el not in br_idx or f"BRK-{el}-{end}" in open_set:
                    A(tag, 0.0)
                    continue
                i = br_idx[el]
                A(tag, {("F", "P"): res["pf"], ("F", "Q"): res["qf"], ("T", "P"): res["pt"], ("T", "Q"): res["qt"]}[(end, q)][i])
            gi = {g: i for i, g in enumerate(net.g_id)}
            for tag, p in self.by_tag.items():
                if p["kind"] != "analog" or not tag.startswith("G"):
                    continue
                el, q = p["element"], tag.rsplit(".", 1)[1]
                if el in gi and f"BRK-{el}" not in open_set:
                    A(tag, res["gen_p"][gi[el]] if q == "P" else res["gen_q"][gi[el]])
                else:
                    A(tag, 0.0)
            for l in bb["loads"]:
                on = f"BRK-{l['mrid']}" not in open_set and l["bus"] not in res["dead_buses"]
                A(f"{l['mrid']}.P", l["p"] * k if on else 0.0)
                A(f"{l['mrid']}.Q", l["q"] * k if on else 0.0)
            self.last = {"t": t, "scale": k, "converged": ok, "losses_mw": res["losses_mw"], "dead_buses": len(res["dead_buses"]),
                         "lost_load_mw": res["lost_load_mw"], "open_breakers": sorted(open_set), "iterations": res["iterations"]}
        try:
            self.http.post(f"{OUTST}/ingest", json={"values": vals}, timeout=10).raise_for_status()
        except Exception as e:
            self.failures += 1
            self.last["push_error"] = str(e)
        self.tick_ms = (time.time() - t) * 1000
        self.ticks += 1

    def set_breaker(self, mrid, open_):
        with self.lock:
            if mrid not in {s["mrid"] for s in self.cim["switches"]}:
                raise KeyError(mrid)
            (self.open.add if open_ else self.open.discard)(mrid)
            self.events.append({"t": time.time(), "breaker": mrid, "open": open_})

    def run(self):
        while True:
            t = time.time()
            try:
                self.step()
            except Exception as e:
                self.failures += 1
                self.last = {"error": repr(e)}
            time.sleep(max(0.0, TICK - (time.time() - t)))


F = Field()
app = FastAPI(title="field-sim", version="0.1.0")


@app.get("/health")
def health():
    return {"status": "ok", "service": "field-sim", "ticks": F.ticks, "tick_ms": round(F.tick_ms, 1), "failures": F.failures}


@app.get("/state")
def state():
    return F.last


@app.post("/command")
def command(c: dict):
    p = next((p for p in F.points if p["ca"] == c.get("ca") and p["ioa"] == c.get("ioa") and p["kind"] == "status"), None)
    if not p:
        raise HTTPException(404, "no such status point")
    F.set_breaker(p["element"], c.get("dcs") == 1)   # DCS 1=OFF(open) 2=ON(close)
    return {"ok": True, "breaker": p["element"]}


@app.post("/event")
def event(e: dict):
    """{"trip": ["BRK-BR0010-F","BRK-BR0010-T"]} / {"restore": [...]} / {"load_scale": 1.1}"""
    for b in e.get("trip", []):
        F.set_breaker(b, True)
    for b in e.get("restore", []):
        F.set_breaker(b, False)
    for tag, d in (e.get("bias") or {}).items():       # {"bias": {"BR0005-F.P": 25.0}} ; 0 clears
        if d: F.bias[tag] = float(d)
        else: F.bias.pop(tag, None)
    if "load_scale" in e:
        F.load_scale = float(e["load_scale"])
    return {"ok": True}


@app.post("/scenario/cascade")
def cascade(s: dict):
    """Trip the given branch ids (both ends) one after another, `gap_s` seconds apart."""
    ids, gap = s.get("branches", []), float(s.get("gap_s", 5))

    def go():
        for br in ids:
            time.sleep(gap)
            F.set_breaker(f"BRK-{br}-F", True)
            F.set_breaker(f"BRK-{br}-T", True)
    threading.Thread(target=go, daemon=True).start()
    return {"ok": True, "scheduled": ids}


if __name__ == "__main__":
    threading.Thread(target=F.run, daemon=True).start()
    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("PORT", "8091")), log_level="warning")
