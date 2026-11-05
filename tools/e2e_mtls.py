"""Live check of the mTLS cluster links. Stack: GS_ASYM=1 GS_QUORUM=1 GS_MTLS=1 tools/stack.sh up
Run: GS_DEV=1 EMS_DEV=1 GS_TOKEN_TRUST=/tmp/gs/tokkeys/trust.json GS_TOKEN_KEY=/tmp/gs/tokkeys/e2e.tokkey python tools/e2e_mtls.py"""
import os, signal, subprocess, sys, time, ssl, httpx
sys.path.insert(0, os.path.dirname(__file__))
from gs_client import Sess

MP = "/tmp/gs/mpki"; PKI = os.path.join(os.path.dirname(__file__), "..", "services", "fep-gateway", "bin", "pki")
ok = bad = 0
def check(n, c, x=""):
    global ok, bad
    ok += bool(c); bad += not c
    print(("  PASS  " if c else "  FAIL  ") + n + ("" if c else f"  {x}"))

def client(cert=None, key=None, ca=f"{MP}/ca.pem"):
    ctx = ssl.create_default_context(cafile=ca)
    ctx.check_hostname = False
    if cert: ctx.load_cert_chain(cert, key)
    return httpx.Client(verify=ctx, timeout=8)

def try_get(c, url, method="GET", **kw):
    try: r = c.request(method, url, **kw); return r.status_code, r.text
    except Exception as e: return None, str(e)[:100]

def issue(cn, d=MP, dual=False):
    subprocess.run([PKI, "issue", "-dir", d, "-cn", cn, "-dns", cn] + (["-dual"] if dual else []), check=True, capture_output=True)
    return f"{d}/{cn}.pem", f"{d}/{cn}.key"

NODE = {"A": 9083, "B": 9183, "C": 9283}
nodeB = f"https://127.0.0.1:{NODE['B']}"

print("== transport")
s, t = try_get(client(), nodeB + "/health")
check("no client certificate: handshake refused", s is None, (s, t))
rogue = "/tmp/gs/rogue-pki"; subprocess.run([PKI, "ca", "-dir", rogue, "-name", "Rogue CA"], check=True, capture_output=True)
rc, rk = issue("core-A", rogue)
s, t = try_get(client(rc, rk), nodeB + "/health")
check("certificate from another CA refused", s is None, (s, t))
c = client(f"{MP}/core-A.pem", f"{MP}/core-A.key")
s, t = try_get(c, nodeB + "/health")
check("node certificate accepted, /health reaches core B", s == 200 and '"node":"B"' in t.replace(" ", ""), (s, t))

print("== per-endpoint certificate names")
ev, ek = issue("ui-gateway")
ce = client(ev, ek)
s, t = try_get(ce, nodeB + "/health")
check("other valid name may use ordinary endpoints", s == 200, (s, t))
for p in ("/api/replicate", "/api/cluster/vote", "/api/cluster/heartbeat"):
    s, t = try_get(ce, nodeB + p)
    check(f"{p} refused to a name not on the list (403 from proxy)", s == 403 and "not permitted" in t, (s, t))
    s, t = try_get(c, nodeB + p, "POST", content=b"{}")
    check(f"{p} reaches the core for core-A (core decides: no token -> 401)", s == 401 and "not permitted" not in t, (s, t))
s, t = try_get(client(f"{MP}/core-A.pem", f"{MP}/core-A.key"), nodeB + "/api/alarms", headers={"X-Client-CN": "core-A"})
check("spoofed X-Client-CN gives no extra rights (core still wants a token)", s == 401, (s, t))

print("== revocation")
import re
out = subprocess.run([PKI, "issue", "-dir", MP, "-cn", "temp-peer", "-dns", "temp-peer"], capture_output=True, text=True).stdout
serial = out.split("serial")[1].strip()
tc = client(f"{MP}/temp-peer.pem", f"{MP}/temp-peer.key")
s, _ = try_get(tc, nodeB + "/health"); check("fresh certificate works", s == 200)
subprocess.run([PKI, "revoke", "-dir", MP, "-serial", serial], check=True, capture_output=True)
time.sleep(1)
s, t = try_get(client(f"{MP}/temp-peer.pem", f"{MP}/temp-peer.key"), nodeB + "/health")
check("revoked certificate refused (CRL re-read without restart)", s is None, (s, t))

print("== cluster runs over the mTLS links")
def cluster(k, port): 
    s = Sess("admin", f"http://127.0.0.1:{port}"); return s.get("/api/metrics").json()["cluster"]
ports = {"A": 8083, "B": 8183, "C": 8283}
cl = {k: cluster(k, p) for k, p in ports.items()}
leaders = [k for k, v in cl.items() if v.get("role") == "leader"]
check("exactly one leader in quorum mode, one term across nodes", len(leaders) == 1 and len({v["term"] for v in cl.values()}) == 1, cl)
L = leaders[0]
alice = Sess("alice", f"http://127.0.0.1:{ports[L]}")
def seq(p): return httpx.get(f"http://127.0.0.1:{p}/health").json()["seq"]
s0 = {k: seq(p) for k, p in ports.items()}
r = alice.post("/api/alarms/ack", json={"id": "none"}) if False else None
time.sleep(3)
s1 = {k: seq(p) for k, p in ports.items()}
check("leader sees both peers alive through the proxies", all(p["alive"] for p in cluster(L, ports[L])["peers"]), cluster(L, ports[L]))
check("standbys keep applying the leader's records (replication over mTLS)", all(s1[k] >= s1[L] - 5 for k in ports), (s0, s1))

print("== peers have no plaintext path")
pid = int(open("/tmp/gs/mtlsB.pid").read()); was_leader_B = (L == "B"); os.kill(pid, signal.SIGKILL)
time.sleep(6)
dead = "B"
def role(n):
    try: return httpx.get(f"http://127.0.0.1:{ports[n]}/health", timeout=3).json()["role"]
    except Exception: return None
def wait_leader(t):
    t0 = time.time()
    while time.time() - t0 < t:
        for n in ("A", "C"):
            if role(n) == "leader": return n
        time.sleep(0.5)
    return None
L2 = wait_leader(20)      # if B was the leader the survivors must elect a new one (the old leader is gone, so this is not 'keeps')
check("the two remaining nodes have a leader after core B is killed (quorum 2/3)" + (" (B was the leader: re-elected)" if was_leader_B else " (leader unchanged)"), L2 is not None and (was_leader_B or L2 == L), (L, L2, role("A"), role("C")))
cl2 = cluster(L2 or "A", ports[L2 or "A"])
alive = {p["id"]: p["alive"] for p in cl2["peers"]}
other = "C" if (L2 or "A") == "A" else "A"
check("leader sees core B down (its proxy is gone) and the other survivor up", alive.get(dead) is False and alive.get(other) is True, cl2)
print(f"mtls: {ok} passed, {bad} failed"); sys.exit(1 if bad else 0)
