#!/usr/bin/env python3
"""End-to-end verification of the live stack (field-sim -> outstation -> IEC104 -> FEP -> scada-core A/B)."""
import sys, time, json, subprocess, os, signal, httpx
sys.path.insert(0, os.path.dirname(__file__))
from gs_client import Sess, A, B

ok = fail = 0
def check(name, cond, detail=""):
    global ok, fail
    if cond: ok += 1; print(f"  PASS  {name}")
    else: fail += 1; print(f"  FAIL  {name}  {detail}")

def wait(fn, t=15, step=0.3):
    t0 = time.time()
    while time.time() - t0 < t:
        v = fn()
        if v: return v
        time.sleep(step)
    return None

print("== authentication")
r = httpx.post(A + "/api/login", json={"user": "alice", "password": "wrong-password-xx"}); check("bad password -> 401", r.status_code == 401)
r = httpx.get(A + "/api/snapshot"); check("no token -> 401", r.status_code == 401)
r = httpx.get(A + "/api/snapshot", headers={"Authorization": "Bearer abc.def"}); check("garbage token -> 401", r.status_code == 401)
alice, bob, carol, north = Sess("alice"), Sess("bob"), Sess("carol"), Sess("north")
check("alice logged in", alice.tok.count(".") == (2 if alice.tok.startswith("v2.") else 1))
r = bob.post("/api/command", json={"tag": "BRK-BR0010-F.STATE", "action": "open", "reason": "x", "totp": "000000"}); check("viewer cannot command -> 403", r.status_code == 403)
r = bob.post("/api/ingest", content=b""); check("viewer cannot ingest -> 403", r.status_code == 403)

print("== telemetry and RTDB")
s = alice.get("/api/snapshot").json()
check("all 1707 points present on A", len(s["pts"]) == 1707, len(s["pts"]))
sb = Sess("bob", B).get("/api/snapshot").json()
check("all 1707 points present on standby B", len(sb["pts"]) == 1707, len(sb["pts"]))
m = alice.get("/api/metrics").json()
check("no telemetry dropped", m["dropped"] == 0, m["dropped"])
print(f"       latency field-push -> core apply: p50={m['latency_ms_p50']} ms p95={m['latency_ms_p95']} ms p99={m['latency_ms_p99']} ms max={m['latency_ms_max']} ms (n={m['latency_samples']})")

print("== control path (select-before-operate, MFA, audit-before-action)")
tag = "BRK-BR0010-F.STATE"
def pos(sess, tg): 
    return next(p for p in sess.get("/api/snapshot").json()["pts"] if p["t"] == tg).get("s")
check("breaker starts closed", pos(alice, tag) == 2)
r = alice.post("/api/command", json={"tag": tag, "action": "open", "reason": "e2e", "totp": "123456"}); check("wrong TOTP -> 403", r.status_code == 403 and "second factor" in r.text, r.text)
r = alice.post("/api/command", json={"tag": tag, "action": "open", "reason": "", "totp": alice.code()}); check("empty reason -> 400", r.status_code == 400, r.text)
r = alice.command("BR0010-F.P", "open"); check("analog tag not controllable -> 404", r.status_code == 404, r.text)
r = north.command(tag, "open"); check("jurisdiction: Northern-only operator cannot operate outside region -> 403", r.status_code == 403 and "jurisdiction" in r.text, r.text)
r = alice.command(tag, "close"); check("interlock: close on closed breaker -> 409", r.status_code == 409, r.text)
tt = alice.code(); r = alice.post("/api/command", json={"tag": tag, "action": "open", "reason": "e2e open", "totp": tt})
check("operator opens breaker -> 200", r.status_code == 200, r.text)
r2 = alice.post("/api/command", json={"tag": "BRK-BR0020-F.STATE", "action": "open", "reason": "replay", "totp": tt}); check("reused one-time code rejected", r2.status_code == 403, r2.text)
check("telemetry confirms OPEN within 6 s", wait(lambda: pos(alice, tag) == 1, 6) is not None)
soe = alice.get("/api/soe?limit=50").json()["events"]
check("SOE records command + execution + commanded state change", all(any(k in e["text"] for e in soe) for k in ("OPEN commanded by alice", "executed", "(commanded)")))
al = alice.get("/api/alarms").json()["active"]
check("commanded operation raised no TRIP alarm", not any(a["type"] == "TRIP" for a in al), al)

print("== uncommanded trip: first-out, grouping, ack")
fs = "http://127.0.0.1:8091"
httpx.post(fs + "/event", json={"restore": [tag.replace(".STATE", "")]})        # put BR0010-F back (truth), no alarm expected? it IS uncommanded
time.sleep(5)
alice.post("/api/alarms/ack", json={"all": True})
httpx.post(fs + "/event", json={"trip": ["BRK-BR0030-F", "BRK-BR0030-T"]})
trip = wait(lambda: [a for a in alice.get("/api/alarms").json()["active"] if a["type"] == "TRIP"], 8)
check("uncommanded trip raises TRIP alarms", bool(trip))
fo = [a for a in (trip or []) if a.get("firstOut")]
check("exactly one first-out alarm", len(fo) == 1, [a["tag"] for a in trip or []])
st = alice.get("/api/alarms").json()["stats"]
print(f"       alarm stats: {st}")
n = alice.post("/api/alarms/ack", json={"all": True}).json()["acked"]; check("ack-all acknowledges", n > 0, n)
httpx.post(fs + "/event", json={"restore": ["BRK-BR0030-F", "BRK-BR0030-T", "BRK-BR0010-F"]})

print("== replication to standby")
time.sleep(3)
bobB = Sess("bob", B)
a_state = {a["id"]: a["state"] for a in alice.get("/api/alarms").json()["active"]}
b_state = {a["id"]: a["state"] for a in bobB.get("/api/alarms").json()["active"]}
sa = len(alice.get("/api/soe?limit=2000").json()["events"]); sb_ = len(bobB.get("/api/soe?limit=2000").json()["events"])
check("standby saw commands/acks (SOE has commanded-open text)", any("commanded by alice" in e["text"] for e in bobB.get("/api/soe?limit=2000").json()["events"]))
r = bobB.get("/api/me").json(); check("B reports standby", r["leader"] is False)
r = Sess("alice", B).command("BRK-BR0040-F.STATE", "open"); check("standby refuses commands -> 409", r.status_code == 409, r.text)

print("== global AI/ML switch and usage audit")
adm = Sess("admin")
check("AI advisory is on by default", alice.get("/api/ai").json()["enabled"] is True)
r = alice.post("/api/ai/switch", json={"enabled": False, "reason": "test off"}); check("operator cannot switch AI off -> 403", r.status_code == 403, r.text)
r = adm.post("/api/ai/switch", json={"enabled": False}); check("switch needs a reason -> 400", r.status_code == 400, r.text)
r = alice.post("/api/ai/use", json={"feature": "forecast", "target": "e2e", "detail": "on"}); check("permitted use is recorded -> 200", r.status_code == 200, r.text)
r = adm.post("/api/ai/switch", json={"enabled": False, "reason": "e2e switch-off test"}); check("admin switches AI off", r.status_code == 200 and r.json()["enabled"] is False, r.text)
r = alice.post("/api/ai/use", json={"feature": "forecast", "target": "e2e", "detail": "off"}); check("use while off -> 409", r.status_code == 409, r.text)
time.sleep(2)
check("switch replicated to standby", Sess("bob", B).get("/api/ai").json()["enabled"] is False)
ev = [e["text"] for e in alice.get("/api/soe?limit=2000").json()["events"]]
check("switch and use are in the audit trail with the user and reason", any("DISABLED by admin (e2e switch-off test)" in t for t in ev) and any("used by alice: forecast e2e" in t for t in ev))
r = adm.post("/api/ai/switch", json={"enabled": True, "reason": "e2e switch-on test"}); check("admin switches AI back on", r.status_code == 200 and r.json()["enabled"] is True, r.text)
check("use allowed again", alice.post("/api/ai/use", json={"feature": "forecast", "target": "e2e"}).status_code == 200)

print("== planned outages: server-side study, two-person approval, plan")
now = int(time.time() * 1000)
body = {"equipment": ["BR0001", "BR0015"], "start": now + 3600_000, "end": now + 3 * 3600_000, "reason": "e2e planned work"}
r = bob.post("/api/outages", json=body); check("viewer cannot request an outage -> 403", r.status_code == 403, r.text)
r = alice.post("/api/outages", json=dict(body, equipment=["NOPE;DROP"])); check("bad equipment id -> 400", r.status_code == 400, r.text)
r = alice.post("/api/outages", json=body); check("operator requests outage; server runs the study", r.status_code == 200 and r.json().get("study", {}).get("verdict") in ("secure", "restricted", "unsafe"), r.text[:300])
oj = r.json() if r.status_code == 200 else {}
on = oj.get("n")
verdict = oj.get("study", {}).get("verdict")
print(f"       outage {oj.get('id')} verdict {verdict}, new critical {oj.get('study', {}).get('n_new_critical')}")
if on:
    r = alice.post("/api/outages/decision", json={"n": on, "decision": "approve", "note": "self"}); check("operator cannot decide -> 403", r.status_code == 403, r.text)
    r = carol.post("/api/outages/decision", json={"n": on, "decision": "approve", "note": "ok by carol"})
    if verdict == "secure":
        check("engineer approves a secure study", r.status_code == 200, r.text)
    else:
        check("non-secure study needs admin override -> 403/409", r.status_code in (403, 409), r.text)
        r = adm.post("/api/outages/decision", json={"n": on, "decision": "approve", "note": "override test", "override": True}); check("admin override approves", r.status_code == 200, r.text)
    r = adm.post("/api/outages/decision", json={"n": on, "decision": "approve", "note": "again"}); check("cannot decide twice -> 409", r.status_code == 409, r.text)
    r = alice.post("/api/outages/plan", json={"n": on, "mode": "isolate"}); check("isolation plan returned with fingerprint", r.status_code == 200 and r.json().get("plan_hash") and r.json().get("steps"), r.text[:200])
    r = alice.post("/api/outages/start", json={"n": on}); check("start work", r.status_code == 200, r.text)
    r = alice.post("/api/outages/complete", json={"n": on}); check("complete work", r.status_code == 200, r.text)
    r = alice.post("/api/outages/cancel", json={"n": on}); check("cannot cancel a completed outage -> 409", r.status_code == 409, r.text)
    time.sleep(2)
    sbo = Sess("bob", B).get("/api/outages").json()["outages"]
    check("outage replicated to standby", any(o["n"] == on and o["status"] == "completed" for o in sbo))
    ev = [e["text"] for e in alice.get("/api/soe?limit=2000").json()["events"]]
    check("outage lifecycle is in the SOE", any(oj.get("id", "?") in t for t in ev))
r = alice.post("/api/outages", json=body); n2 = r.json().get("n")
r = alice.post("/api/outages/decision", json={"n": n2, "decision": "approve", "note": "me myself"}); check("requester cannot approve own request -> 403", r.status_code == 403, r.text)
alice.post("/api/outages/cancel", json={"n": n2})

print("== operator log, shift handover, communications status")
r = bob.post("/api/log", json={"category": "general", "text": "viewer note"}); check("viewer cannot write the log -> 403", r.status_code == 403, r.text)
r = alice.post("/api/log", json={"category": "handover", "text": "forged"}); check("cannot forge a handover category -> 400", r.status_code == 400, r.text)
r = alice.post("/api/log", json={"category": "abnormal", "text": "e2e: relay alarm at SS-0001 under investigation", "ref": "SS-0001"}); check("operator writes a log entry", r.status_code == 200, r.text)
n1 = r.json().get("n")
r = alice.post("/api/log", json={"category": "general", "text": "e2e correction: SS-0002 not SS-0001", "amends": n1}); check("correction references the original entry", r.status_code == 200, r.text)
r = alice.post("/api/log", json={"category": "general", "text": "bad amend", "amends": 99999999}); check("amending an unknown entry -> 404", r.status_code == 404, r.text)
ents = bob.get("/api/log?limit=5").json()["entries"]
check("viewer can read the log, newest first, with the amendment link", ents[0].get("amends") == n1 and ents[1]["n"] == n1 and ents[1]["text"].startswith("e2e: relay"), ents[:2])
check("search finds entries", len(bob.get("/api/log?q=relay%20alarm").json()["entries"]) >= 1 and all(e["category"] == "abnormal" for e in bob.get("/api/log?category=abnormal").json()["entries"]))
time.sleep(2)
check("log replicated to standby", any(e["n"] == n1 for e in Sess("bob", B).get("/api/log?limit=20").json()["entries"]))
d = alice.get("/api/handover/draft").json()
check("handover snapshot comes from server state (alarms, outages, comms, AI switch)", all(k in d for k in ("alarms", "outages", "comms", "ai_enabled", "notable_log")) and any(e["n"] == n1 for e in d["notable_log"]), list(d))
pend = bob.get("/api/handover").json().get("pending")
if pend: carol.post("/api/handover/accept", json={"n": pend["n"]}); alice.post("/api/handover/accept", json={"n": pend["n"]})
r = bob.post("/api/handover", json={"notes": "viewer handover"}); check("viewer cannot hand over -> 403", r.status_code == 403, r.text)
r = alice.post("/api/handover", json={"notes": "ok", "to": "alice"}); check("short notes -> 400", r.status_code == 400, r.text)
r = alice.post("/api/handover", json={"notes": "Quiet shift. Relay test at SS-0001 pending; see log.", "to": "north"}); check("outgoing operator issues a handover", r.status_code == 200 and r.json()["handover"]["status"] == "pending", r.text[:200])
hn = r.json().get("handover", {}).get("n")
r = alice.post("/api/handover", json={"notes": "second one"}); check("only one open handover -> 409", r.status_code == 409, r.text)
r = alice.post("/api/handover/accept", json={"n": hn}); check("outgoing operator cannot accept own handover -> 403", r.status_code == 403, r.text)
r = carol.post("/api/handover/accept", json={"n": hn}); check("handover addressed to another user cannot be accepted by carol -> 403", r.status_code == 403, r.text)
r = north.post("/api/handover/accept", json={"n": hn, "note": "taken over"}); check("addressed operator accepts", r.status_code == 200, r.text)
h = bob.get("/api/handover").json()
check("handover is accepted with acceptor and snapshot", h["pending"] is None and h["history"][0]["status"] == "accepted" and h["history"][0]["acceptedBy"] == "north" and h["history"][0]["snapshot"]["alarms"] is not None, h["history"][:1])
time.sleep(2)
check("handover replicated to standby", Sess("bob", B).get("/api/handover").json()["history"][0]["status"] == "accepted")
soe = [e["text"] for e in alice.get("/api/soe?limit=3000").json()["events"]]
check("log and handover are in the sequence of events", any("LOG #" in t and "[abnormal]" in t for t in soe) and any("SHIFT HANDOVER" in t and "accepted by north" in t for t in soe))
cm = bob.get("/api/comms").json()
check("comms status lists every substation and the feed heartbeat", len(cm["substations"]) >= 100 and cm["feed"]["monitored"] and not cm["feed"]["lost"] and all(x["state"] == "ok" for x in cm["substations"]), cm["feed"])

print("== deterministic replay of the event log")
c = Sess("carol")
v = c.get("/api/verify-replay").json()
check("hash chain verifies", v["chain_ok"])
check("replayed state+alarm hashes identical to live", v["identical"], v)
print(f"       {v['records']} records replayed in {v['replay_ms']} ms")
r = alice.get("/api/verify-replay"); check("operator cannot run verify (engineer+) -> 403", r.status_code == 403)

print("== EMS integration on live SCADA data")
e = wait(lambda: alice.get("/api/ems").json() or None, 40)
e = wait(lambda: (lambda d: d if d.get("se") else None)(alice.get("/api/ems").json()), 60)
if e:
    se = e["se"]
    check("SE observable on live telemetry", se.get("observable") is True, se)
    check("SE converged", se.get("converged") is True)
    print(f"       SE: {e['measurements_used']} meas, J={se.get('J')} vs chi2 threshold {se.get('chi2_threshold')}, bad_data={se.get('bad_data_detected')}, removed={se.get('removed')}, solve {se.get('solve_ms')} ms, roundtrip {e['se_roundtrip_ms']:.0f} ms")
    ca = e["contingency"]; print(f"       N-1 keys: {sorted(k for k in ca if k != 'results')}; wall {ca.get('wall_seconds')} s, cycle {e['cycle_ms']:.0f} ms")
else:
    check("EMS summary available", False)


print("== bad-data detection on live telemetry (miscalibrated transducer)")
bad_tag = "BR0020-F.P"
def latest_ts(): return alice.get("/api/ems").json().get("ts", 0)
base = wait(lambda: (lambda d: d if d.get("se") else None)(alice.get("/api/ems").json()), 60)
if base:
    print(f"       clean-data J={base['se']['J']} (dof {base['se']['dof']}, threshold {base['se']['chi2_threshold']})")
    check("clean data: nothing removed, chi-square test passes", base["se"]["removed"] == [] and base["se"]["J"] < base["se"]["chi2_threshold"], base["se"])
    t_inj = time.time() * 1000
    httpx.post(fs + "/event", json={"bias": {bad_tag: 40.0}})
    r = wait(lambda: (lambda d: d if d.get("ts", 0) > t_inj + 4000 and d.get("se", {}).get("bad_data_detected") is not None else None)(alice.get("/api/ems").json()), 150)
    httpx.post(fs + "/event", json={"bias": {bad_tag: 0}})
    if r:
        rem = r["se"].get("removed", [])
        ids = [x["id"] for x in rem]
        print(f"       injected +40 MW on {bad_tag}; removed={[(x['id'], round(x['normalized_residual'], 1)) for x in rem]} J after cleaning={r['se'].get('J')} (threshold {r['se'].get('chi2_threshold')})")
        check("largest-normalised-residual test removed exactly the faulty measurement", ids == ["PF:BR0020"], ids)
        check("estimate after cleaning passes the chi-square test", r["se"]["J"] < r["se"]["chi2_threshold"])
    else:
        check("SE cycle ran after injection", False)

print(f"\nRESULT: {ok} passed, {fail} failed")
sys.exit(1 if fail else 0)
