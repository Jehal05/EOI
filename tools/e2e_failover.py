#!/usr/bin/env python3
"""Destructive resilience test: crash the leader (kill -9), measure failover, operate on the new leader,
restart the crashed node, check it recovers from its tamper-evident log, catches up and resynchronises."""
import os, sys, time, signal, subprocess, httpx
sys.path.insert(0, os.path.dirname(__file__))
from gs_client import Sess, A, B

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
ok = fail = 0
def check(name, cond, detail=""):
    global ok, fail
    if cond: ok += 1; print(f"  PASS  {name}")
    else: fail += 1; print(f"  FAIL  {name}  {detail}")

def health(base):
    try: return httpx.get(base + "/health", timeout=0.4).json()
    except Exception: return None

def snap(sess): return {p["t"]: p for p in sess.get("/api/snapshot").json()["pts"]}

def diff(a, b):
    d = 0
    for t, p in a.items():
        q = b.get(t)
        if q is None or p.get("s") != q.get("s") or abs((p.get("v") or 0) - (q.get("v") or 0)) > 1e-6: d += 1
    return d

alice_b = Sess("alice", B)
assert health(A)["role"] == "leader" and health(B)["role"] == "standby", "start from a clean stack"
pid = int(open("/tmp/gs/coreA.pid").read())

print("== crash the leader")
t0 = time.time()
os.kill(pid, signal.SIGKILL)
t_leader = None
while time.time() - t0 < 15:
    h = health(B)
    if h and h["role"] == "leader": t_leader = time.time() - t0; break
    time.sleep(0.05)
check("standby promoted itself", t_leader is not None)
print(f"       failover time (kill -9 -> B accepts leadership): {t_leader:.2f} s")
s = snap(alice_b); fresh = sum(1 for p in s.values() if time.time() * 1000 - p["m"] < 30000)
check("standby holds the complete warm RTDB", len(s) == 1707, len(s))

print("== operate on the new leader")
tag = "BRK-BR0050-F.STATE"
tc = time.time()
r = alice_b.command(tag, "open", "failover test")
check("command accepted by new leader", r.status_code == 200, r.text)
print(f"       command round trip on new leader: {(time.time() - tc) * 1000:.0f} ms")
time.sleep(4)
check("telemetry on B confirms OPEN", snap(alice_b)[tag].get("s") == 1)

print("== restart the crashed node")
env = dict(os.environ, GS_DEV="1", JAVA_TOOL_OPTIONS="")
t1 = time.time()
p = subprocess.Popen(["java", "-XX:+UseZGC", "-Xmx1g", "-cp", f"{ROOT}/services/scada-core/out", "gs.core.Main", "-id", "A", "-listen", "127.0.0.1:8083",
                      "-data", "/tmp/gs/coreA", "-model", "ieee118", "-peer", "B=http://127.0.0.1:8183"], env=env, stdout=open("/tmp/gs/coreA.log", "a"), stderr=subprocess.STDOUT)
open("/tmp/gs/coreA.pid", "w").write(str(p.pid))
while time.time() - t1 < 30 and not health(A): time.sleep(0.1)
up = time.time() - t1
check("node A restarted", health(A) is not None)
print(f"       restart to serving: {up:.2f} s (event log chain verified + state restored)")
log = open("/tmp/gs/coreA.log").read().splitlines()
print("       " + [l for l in log if "records restored" in l][-1])
alice_a = Sess("alice", A)
t2 = time.time()
while time.time() - t2 < 20 and health(A)["role"] != "leader": time.sleep(0.1)
check("A resumes leadership (lowest id)", health(A)["role"] == "leader")
time.sleep(8)    # startup catch-up + FEP general interrogation
soe_a = [e["text"] for e in alice_a.get("/api/soe?limit=2000").json()["events"]]
check("A caught up the operator command executed on B while A was down", any("OPEN commanded by alice (failover test)" in t for t in soe_a))
d = 99
for _ in range(10):
    d = diff(snap(alice_a), snap(alice_b))
    if d <= 3: break
    time.sleep(1)
check("A's RTDB resynchronised with B after general interrogation (<=3 in-flight diffs)", d <= 3, d)
print(f"       points differing between A and B: {d} / 1707")

print("== integrity after crash + recovery")
carol_a, carol_b = Sess("carol", A), Sess("carol", B)
for n, c in (("A", carol_a), ("B", carol_b)):
    v = c.get("/api/verify-replay").json()
    check(f"node {n}: chain verifies and replay is bit-identical", v["chain_ok"] and v["identical"], v)

alice_a.command(tag, "close", "restore after failover test")
print(f"\nRESULT: {ok} passed, {fail} failed")
sys.exit(1 if fail else 0)
