#!/usr/bin/env python3
"""Destructive communications-loss test on the live stack: stops the outstation, then the FEP, and checks detection,
interlock, alarms, recovery and the standby node. Run on its own (not together with other e2e suites)."""
import sys, os, time, subprocess, httpx
sys.path.insert(0, os.path.dirname(__file__))
from gs_client import Sess, A, B

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
ok = fail = 0
def check(name, cond, detail=""):
    global ok, fail
    if cond: ok += 1; print(f"  PASS  {name}")
    else: fail += 1; print(f"  FAIL  {name}  {detail}")

def wait(f, t):
    t0 = time.time()
    while time.time() - t0 < t:
        v = f()
        if v: return v, time.time() - t0
        time.sleep(0.5)
    return None, time.time() - t0

env = dict(os.environ, GS_DEV="1", CIM_PDS_DEV="1", EMS_DEV="1", CIM_DATA_DIR=os.path.join(ROOT, "data"))
def start_outstation():
    tok = subprocess.run([f"{ROOT}/services/cim-pds/target/release/cim-pds", "mint", "outstation", "viewer", "*", "86400"], env=env, capture_output=True, text=True).stdout.strip().splitlines()[-1]
    subprocess.Popen([f"{ROOT}/services/fep-gateway/bin/outstation-sim", "-model", "ieee118"], cwd=f"{ROOT}/services/fep-gateway", env=dict(env, CIM_TOKEN=tok), stdout=open("/tmp/gs/out.log", "w"), stderr=subprocess.STDOUT, start_new_session=True)
def start_fep():
    subprocess.Popen([f"{ROOT}/services/fep-gateway/bin/fep", "-model", "ieee118", "-ingest", f"{A}/api/ingest", "-ingest", f"{B}/api/ingest"], cwd=f"{ROOT}/services/fep-gateway", env=env, stdout=open("/tmp/gs/fep.log", "a"), stderr=subprocess.STDOUT, start_new_session=True)

def comms(s):
    c = s.get("/api/comms").json()
    return c, sum(x["stale"] for x in c["substations"]), sum(x["state"] == "lost" for x in c["substations"])

alice, bobB = Sess("alice"), Sess("bob", B)
c, stale, lost = comms(alice)
check("healthy start: no stale points, FEP heartbeat monitored", stale == 0 and lost == 0 and c["feed"]["monitored"] and not c["feed"]["lost"], c["feed"])
snap0 = {p["t"]: p for p in alice.get("/api/snapshot").json()["pts"]}
tag = "BR0040-F.P"
before = snap0[tag]["v"]
alice.post("/api/alarms/ack", json={"all": True})

print("== outstation link lost")
subprocess.run(["pkill", "-x", "outstation-sim"])
r, dt = wait(lambda: comms(alice)[1] > 0 and comms(alice), 20)
check("link loss marks points stale within 5 s", r is not None and dt < 5, f"{dt:.1f}s")
c, stale, lost = comms(alice)
print(f"       detected after {dt:.1f} s: {stale} stale points, {lost} substations lost")
check("every substation on the dead link is reported lost", lost == len(c["substations"]), (lost, len(c["substations"])))
pts = {p["t"]: p for p in alice.get("/api/snapshot").json()["pts"]}
check("stale point keeps its last value and carries the stale quality flag", pts[tag].get("q") == "stale" and pts[tag].get("v") == before, (pts.get(tag), before))
al = alice.get("/api/alarms").json()["active"]
comms_al = [a for a in al if a["type"] == "COMMS" and a["state"].startswith("ACTIVE")]
check("one communications alarm per substation, not per point", len(comms_al) == lost and len(comms_al) < stale, (len(comms_al), stale))
check("exactly one of them is the first-out; the others are consequential", sum(1 for a in comms_al if a.get("firstOut")) == 1 and sum(1 for a in comms_al if a.get("consequential")) == lost - 1)
r = alice.command("BRK-BR0040-F.STATE", "open", reason="comms-loss interlock test")
check("operator command on stale data is refused (interlock)", r.status_code == 409, r.text[:200])
soe = [e["text"] for e in alice.get("/api/soe?limit=3000").json()["events"]]
check("loss is in the sequence of events", any("COMMUNICATIONS LOST" in t for t in soe))
time.sleep(2)
check("standby node sees the same loss from its own feed", comms(bobB)[1] > 0)

print("== outstation back")
start_outstation()
r, dt = wait(lambda: comms(alice)[1] == 0 and comms(alice), 120)
check("restored automatically after reconnect and general interrogation", r is not None, f"{dt:.0f}s")
print(f"       restored {dt:.0f} s after restart (client back-off up to 32 s)")
soe = [e["text"] for e in alice.get("/api/soe?limit=3000").json()["events"]]
check("restoration is in the sequence of events", any("COMMUNICATIONS RESTORED" in t for t in soe))
check("communications alarms returned to normal", not [a for a in alice.get("/api/alarms").json()["active"] if a["type"] == "COMMS" and a["state"].startswith("ACTIVE")])

print("== FEP lost (heartbeat timeout)")
subprocess.run(["pkill", "-x", "fep"])
t_kill = time.time()
r, dt = wait(lambda: comms(alice)[0]["feed"]["lost"], 60)
check("silent FEP detected by heartbeat timeout", r is not None, f"{dt:.0f}s")
print(f"       feed declared lost {time.time() - t_kill:.0f} s after the FEP stopped (timeout {comms(alice)[0]['feed']['timeout_ms'] / 1000:.0f} s)")
time.sleep(3)
c, stale, lost = comms(alice)
check("all points marked stale when the feed is lost", stale > 1500 and lost == len(c["substations"]), (stale, lost))
start_fep()
r, dt = wait(lambda: (lambda x: x[1] == 0 and not x[0]["feed"]["lost"])(comms(alice)), 120)
check("everything recovers when the FEP returns (its general interrogation refreshes every point)", r is not None, f"{dt:.0f}s")
print(f"       recovered {dt:.0f} s after FEP restart")
print(f"\nRESULT: {ok} passed, {fail} failed")
sys.exit(1 if fail else 0)
