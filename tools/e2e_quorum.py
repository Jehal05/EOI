#!/usr/bin/env python3
"""Live test of quorum leadership and fencing on a 3-node control centre (start with GS_QUORUM=1 tools/stack.sh up).
Crashes are emulated with SIGSTOP/SIGCONT (a frozen leader, the worst case for split brain). Needs the stack."""
import os, sys, time, json, signal, httpx
sys.path.insert(0, os.path.dirname(__file__))
from gs_client import Sess
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, f"{ROOT}/services/ems-analytics"); os.environ["EMS_DEV"] = "1"
from ems import auth as emsauth
ok = fail = 0
def check(n, c, d=""):
    global ok, fail
    if c: ok += 1; print("  PASS ", n)
    else: fail += 1; print("  FAIL ", n, str(d)[:300])
PORT = {"A": 8083, "B": 8183, "C": 8283}
BASE = {k: f"http://127.0.0.1:{v}" for k, v in PORT.items()}
PID = {k: int(open(f"/tmp/gs/core{k}.pid").read()) for k in PORT}
sess = {}
def S(k):
    if k not in sess: sess[k] = Sess("alice", BASE[k])
    return sess[k]
def health(k):
    try: return httpx.get(BASE[k] + "/health", timeout=1.5).json()
    except Exception: return None
def leaders(skip=()):
    return [k for k in PORT if k not in skip and (h := health(k)) and h.get("role") == "leader"]
def wait(f, t=15, step=0.2):
    end = time.time() + t
    while time.time() < end:
        v = f()
        if v: return v
        time.sleep(step)
    return None
def cluster(k): return S(k).get("/api/metrics").json()["cluster"]
frozen = set(); max_leaders = 0
def sweep(skip=()):
    global max_leaders
    n = len(leaders(skip)); max_leaders = max(max_leaders, n); return n
def freeze(k): os.kill(PID[k], signal.SIGSTOP); frozen.add(k)
def thaw(k): os.kill(PID[k], signal.SIGCONT); frozen.discard(k)
def breaker(sess):
    for p in sess.get("/api/snapshot").json()["pts"]:
        if p["t"].startswith("BRK-BR") and p.get("s") == 2: return p["t"]
def command(k, tag, action):
    s = S(k); return s.post("/api/command", json={"tag": tag, "action": action, "reason": "quorum e2e", "totp": s.code()})
try:
    L = wait(lambda: (l := leaders()) and len(l) == 1 and l[0], 20)
    check("exactly one leader among three nodes", bool(L), leaders())
    cl = cluster(L)
    check("cluster reports quorum mode with quorum 2", cl["mode"] == "quorum" and cl["quorum"] == 2, cl)
    terms = {k: cluster(k)["term"] for k in PORT}
    check("all nodes agree on the term", len(set(terms.values())) == 1, terms)
    for _ in range(20): sweep(); time.sleep(0.1)
    check("stable: no second leader appeared", max_leaders == 1 and leaders() == [L])
    tag = breaker(S(L))
    others = [k for k in PORT if k != L]
    r = command(others[0], tag, "open"); check("a standby refuses operator commands (409)", r.status_code == 409, (r.status_code, r.text[:100]))
    r = command(L, tag, "open"); check("the leader executes a command through the FEP", r.status_code == 200, r.text[:200])
    time.sleep(1.2); command(L, tag, "close")
    t0 = cluster(L)["term"]

    # --- frozen leader
    freeze(L)
    t_freeze = time.time()
    N = wait(lambda: (sweep(skip=frozen) or True) and (l := leaders(skip=frozen)) and len(l) == 1 and l[0], 20, 0.1)
    dt = time.time() - t_freeze
    check(f"a new leader takes over from the frozen one ({dt:.1f} s)", bool(N) and N != L, leaders(skip=frozen))
    t1 = cluster(N)["term"]
    check("the new leader has a higher term (fencing token)", t1 > t0, (t0, t1))
    time.sleep(1.5)
    r = command(N, tag, "open"); check("the new leader's command reaches the field (accepted by the FEP fence)", r.status_code == 200, r.text[:200])
    time.sleep(1.2); command(N, tag, "close")
    # --- the old leader comes back believing it is the leader
    thaw(L)
    for _ in range(30): sweep(); time.sleep(0.1)
    check("the returning leader does not lead (it adopts the higher term)", leaders() == [N] and max_leaders == 1, (leaders(), max_leaders))
    cl = wait(lambda: (c := cluster(L)) and c["term"] == t1 and c, 10)
    check("it is a follower in the current term", bool(cl) and cl["role"] == "standby", cl)
    r = command(L, tag, "open"); check("and refuses commands (409)", r.status_code == 409, r.text[:100])
    # --- FEP fence, directly: a stale term is refused, current is accepted, a missing term is refused once fencing is on
    tok = emsauth.mint("fence-test", "operator")
    f = lambda term: httpx.post("http://127.0.0.1:8084/command", json={"tag": tag, "action": "open"}, headers={"Authorization": "Bearer " + tok, **({"X-Fence-Term": str(term)} if term else {})}, timeout=15)
    r = f(t0); check("FEP refuses a command stamped with the deposed leader's term", r.status_code == 409 and "fenced" in r.text, (r.status_code, r.text[:120]))
    r = f(None); check("FEP refuses a command with no term once fencing is active", r.status_code == 409, (r.status_code, r.text[:120]))
    r = f(t1 + 0); check("FEP accepts the current term", r.status_code in (200, 409) and "fenced" not in r.text, (r.status_code, r.text[:120]))
    time.sleep(1.2); command(N, tag, "close")
    # --- replication fencing: a record stamped with an old term is refused by the peers
    pk = int(open("/tmp/gs/coreN.pid").read()) if os.path.exists("/tmp/gs/coreN.pid") else None
    rtok = emsauth.mint("core-A", "admin")
    rec = {"k": "ACK", "ts": int(time.time() * 1000), "seq": 1, "user": "intruder", "all": True}
    r = httpx.post(BASE[N] + "/api/replicate", json=rec, headers={"Authorization": "Bearer " + rtok, "X-Term": str(t0)}, timeout=5)
    check("a replicated record from an old term is refused (409)", r.status_code == 409, (r.status_code, r.text[:100]))
    r = httpx.post(BASE[N] + "/api/replicate", json=rec, headers={"Authorization": "Bearer " + rtok}, timeout=5)
    check("a replicated record with no term is refused", r.status_code == 409)
    # --- majority lost
    others = [k for k in PORT if k != N]
    for k in others: freeze(k)
    t_lost = time.time()
    gone = wait(lambda: not leaders(skip=frozen), 6, 0.1)
    check(f"a leader without a majority steps down within its lease ({time.time() - t_lost:.1f} s)", bool(gone))
    r = command(N, tag, "open"); check("and refuses commands while it has no quorum", r.status_code == 409, r.text[:100])
    for k in others: thaw(k)
    L2 = wait(lambda: (l := leaders()) and len(l) == 1 and l[0], 20)
    check("service resumes when the majority is back", bool(L2))
    # --- persistence of term and vote
    st = {k: json.load(open(f"/tmp/gs/core{k}/cluster.json")) for k in PORT}
    cur = cluster(L2)["term"]
    check("each node's term is persisted on disk and not behind the cluster", all(s["term"] <= cur and s["term"] >= t1 for s in st.values()), (st, cur))
    check("at no sampled instant were there two leaders", max_leaders <= 1, max_leaders)
finally:
    for k in list(frozen): thaw(k)
print(f"\n{ok} passed, {fail} failed")
sys.exit(1 if fail else 0)
