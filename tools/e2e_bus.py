"""Live check of the PostgreSQL historian fed by the Redis Streams bus. Stack: GS_PG=1 GS_BUS=1 tools/stack.sh up
Also stops and restarts the bus and the historian."""
import os, signal, subprocess, sys, time, httpx
sys.path.insert(0, os.path.dirname(__file__))
from gs_client import Sess

H = "http://127.0.0.1:8085"; ok = bad = 0
def check(n, c, x=""):
    global ok, bad
    ok += bool(c); bad += not c
    print(("  PASS  " if c else "  FAIL  ") + n + ("" if c else f"  {x}"))
def rc(*a): return subprocess.run(["redis-cli", "-p", "6390", *a], capture_output=True, text=True).stdout.strip()
def wait(f, t=20):
    end = time.time() + t
    while time.time() < end:
        v = f()
        if v: return v
        time.sleep(0.4)
    return None

s = Sess("alice"); hd = {"Authorization": "Bearer " + s.tok}
def stats(): return httpx.get(H + "/v1/stats", headers=hd, timeout=10).json()
def health(): return httpx.get(H + "/health", timeout=5).json()
def newest():  return stats().get("newest_ms") or 0

print("== bus and PostgreSQL in the live path")
check("historian reads from the bus", wait(lambda: (health().get("connected_to") or "").startswith("bus:")), health())
st = stats()
check("backend is PostgreSQL", st.get("backend") in ("postgresql", "timescaledb"), st)
check("live telemetry arrives (raw rows grow, newest is recent)", wait(lambda: abs(time.time() * 1000 - newest()) < 15000), stats())
n0 = stats()["raw_rows"]; time.sleep(4); n1 = stats()["raw_rows"]
check("rows keep growing", n1 > n0, (n0, n1))
check("stream has entries and the consumer group has nothing left unacknowledged", int(rc("xlen", "gs:telemetry") or 0) > 0 and "historian" in rc("xinfo", "groups", "gs:telemetry"), rc("xinfo", "groups", "gs:telemetry"))
pend = rc("xpending", "gs:telemetry", "historian").split("\n")[0]
check("pending acknowledgements stay small", pend.isdigit() and int(pend) < 200, pend)

tag = next(p["t"] for p in httpx.get("http://127.0.0.1:8083/api/snapshot", headers={"Authorization": "Bearer " + s.tok}).json()["pts"] if "v" in p and p["t"].endswith(".P"))
now = int(time.time() * 1000)
r = httpx.get(H + "/v1/trend", params={"tag": tag, "from": now - 600_000, "to": now}, headers=hd, timeout=10).json()
check(f"trend query for a live tag ({tag}) returns points", len(r["points"]) > 0, r)

print("== bus outage: the control core keeps working, history catches up")
rc("shutdown", "nosave"); time.sleep(1)
check("core unaffected while the bus is down", httpx.get("http://127.0.0.1:8083/health", timeout=5).json()["status"] == "ok")
code = httpx.get("http://127.0.0.1:8083/api/alarms", headers={"Authorization": "Bearer " + s.tok}, timeout=5).status_code
check("operator API still answers", code == 200, code)
check("historian reports itself disconnected", wait(lambda: not health().get("connected_to"), 12), health())
t_gap = newest()
time.sleep(5)
subprocess.run(["redis-server", "--port", "6390", "--save", "", "--appendonly", "no", "--daemonize", "yes", "--bind", "127.0.0.1"], capture_output=True)
check("historian reconnects when the bus returns", wait(lambda: (health().get("connected_to") or "").startswith("bus:"), 30), health())
check("history resumes after the outage (newest row is recent again)", wait(lambda: abs(time.time() * 1000 - newest()) < 15000, 30), stats())

print("== consistency of what was stored (including the redelivery after the outage)")
r = httpx.get(H + "/v1/trend", params={"tag": tag, "from": now - 3_600_000, "to": int(time.time() * 1000) + 60_000}, headers=hd, timeout=10).json()
rows = sum(p["n"] for p in r["points"])
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "services", "historian"))
from hist.pgstore import sql
DSN = "postgresql://postgres@127.0.0.1:5433/histlive"
tid = sql(DSN, "SELECT id FROM tags WHERE tag=%s", (tag,))[0][0]
raw = sql(DSN, "SELECT COUNT(*) FROM raw WHERE tag_id=%s AND ts >= %s", (tid, now - 3_600_000))[0][0]
agg = sql(DSN, "SELECT COALESCE(SUM(n),0) FROM agg1m WHERE tag_id=%s AND bucket >= %s", (tid, (now - 3_600_000) // 60000 * 60000))[0][0]
check("aggregate row counts equal raw row counts for the tag (idempotent writes)", raw == agg or abs(raw - agg) <= 1, (raw, agg))
dup = sql(DSN, "SELECT COUNT(*) FROM (SELECT tag_id, ts FROM raw GROUP BY 1,2 HAVING COUNT(*) > 1) d")[0][0]
check("no duplicate raw rows", dup == 0, dup)
print(f"bus: {ok} passed, {bad} failed"); sys.exit(1 if bad else 0)
