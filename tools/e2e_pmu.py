#!/usr/bin/env python3
"""C37.118 synchrophasor stream on the live stack (start it with GS_PMU=1 tools/stack.sh up): the PMU points are defined by the
point-list overlay in cim-pds, mapped by the FEP's PMU bridge, and must show up in the SCADA core like any other analog.
Destructive at the end (stops and restarts the PMU simulator). Run on its own."""
import sys, os, time, math, subprocess
sys.path.insert(0, os.path.dirname(__file__))
from gs_client import Sess

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
ok = fail = 0
def check(name, cond, detail=""):
    global ok, fail
    if cond: ok += 1; print(f"  PASS  {name}")
    else: fail += 1; print(f"  FAIL  {name}  {detail}")
def wait(f, t, step=0.4):
    t0 = time.time()
    while time.time() - t0 < t:
        v = f()
        if v: return v
        time.sleep(step)
    return None

alice, bob = Sess("alice"), Sess("bob")
snap = lambda s=alice: {p["t"]: p for p in s.get("/api/snapshot").json()["pts"]}
TAGS = ["PMU26.F", "PMU26.DF", "PMU26.V1.MAG", "PMU26.V1.ANG"]
print("== PMU points in the model-driven point list and the core")
d = wait(lambda: (lambda x: x if all(t in x for t in TAGS) else None)(snap()), 30)
check("all four PMU points reach the core", d is not None, TAGS)
d = d or snap()
check("frequency within the simulator's +-20 mHz swing of 50 Hz", 'PMU26.F' in d and abs(d["PMU26.F"]["v"] - 50) <= 0.021, d.get("PMU26.F"))
check("voltage magnitude is 1.0 pu", 'PMU26.V1.MAG' in d and abs(d["PMU26.V1.MAG"]["v"] - 1.0) < 1e-3, d.get("PMU26.V1.MAG"))
check("angle within the simulator's +-0.05 rad (2.865 deg)", 'PMU26.V1.ANG' in d and abs(d["PMU26.V1.ANG"]["v"]) <= 0.05 * 180 / math.pi + 0.01, d.get("PMU26.V1.ANG"))
check("points are good quality", all(d[t].get("q", "good") == "good" for t in TAGS if t in d), {t: d.get(t) for t in TAGS})
f1 = d["PMU26.F"]["v"]
moved = wait(lambda: abs(snap()["PMU26.F"]["v"] - f1) > 1e-4, 12)
check("values keep updating (frequency changes over time)", bool(moved))
check("readable by a viewer", bob.get("/api/snapshot").status_code == 200)

print("== read-only and isolation")
r = alice.post("/api/command", json={"tag": "PMU26.F", "action": "open", "reason": "test", "totp": alice.code()})
check("a command on a PMU point is refused", r.status_code >= 400, r.text[:200])
d2 = snap()
check("ordinary substation data unaffected (breaker states still reported)", sum(1 for t in d2 if t.startswith("BRK-")) > 100)
al = alice.get("/api/alarms").json()
al = al if isinstance(al, list) else al.get("alarms", [])
check("no alarms raised by healthy PMU data", not any("PMU26" in str(a) for a in al), [a for a in al if "PMU26" in str(a)][:2])

print("== PMU loss and recovery (destructive)")
subprocess.run(["pkill", "-x", "pmu-sim"])
def stale_now():
    x = snap()
    return all(x[t].get("q") == "stale" or x[t].get("s") == 1 or x[t].get("st") == 1 for t in TAGS) if all(t in x for t in TAGS) else None
s0 = snap()["PMU26.F"]; print("       point after loss:", s0)
lost = wait(lambda: (lambda x: x["PMU26.F"] if x["PMU26.F"].get("q") not in (None, "good") else None)(snap()), 25)
check("PMU points marked not-good after the stream stops", lost is not None, snap()["PMU26.F"])
check("breaker data stays good while only the PMU is down", snap().get("BRK-BR0010-F.STATE", {}).get("q", "good") == "good")
subprocess.Popen([f"{ROOT}/services/fep-gateway/bin/pmu-sim", "-listen", "127.0.0.1:4712", "-id", "7", "-station", "PMU26"], stdout=open("/tmp/gs/pmu.log", "a"), stderr=subprocess.STDOUT, start_new_session=True)
back = wait(lambda: (lambda x: x["PMU26.F"] if x["PMU26.F"].get("q") in (None, "good") and abs(x["PMU26.F"]["v"] - 50) < 0.05 else None)(snap()), 40)
check("points recover when the stream returns", back is not None, snap()["PMU26.F"])
print(f"\ne2e_pmu: {ok} passed, {fail} failed")
sys.exit(1 if fail else 0)
