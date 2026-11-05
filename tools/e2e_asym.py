"""Live check of Ed25519 service tokens. Needs the stack up with GS_ASYM=1 (add GS_QUORUM=1 for the cluster check).
Run: GS_DEV=1 EMS_DEV=1 GS_TOKEN_TRUST=/tmp/gs/tokkeys/trust.json GS_TOKEN_KEY=/tmp/gs/tokkeys/e2e.tokkey python tools/e2e_asym.py"""
import base64, hashlib, hmac, json, os, sys, time, httpx
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "services", "ems-analytics"))
from gs_client import Sess
from ems import asym_token as at

ok = bad = 0
def chk(name, cond, extra=""):
    global ok, bad
    if cond: ok += 1
    else: bad += 1
    print(("PASS " if cond else "FAIL ") + name + (" " + str(extra) if extra and not cond else ""))

def b64(b): return base64.urlsafe_b64encode(b).rstrip(b"=").decode()
def hmac_tok(sub, role, secret=b"dev-secret-do-not-use-in-prod"):
    p = b64(json.dumps({"sub": sub, "role": role, "jur": ["*"], "exp": int(time.time()) + 600}, separators=(",", ":")).encode())
    return p + "." + hmac.new(secret, p.encode(), hashlib.sha256).hexdigest()

def own_key(kid):  # a signer that uses another service's private key (as a stolen key would)
    kf = json.load(open(f"/tmp/gs/tokkeys/{kid}.tokkey"))
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    k = Ed25519PrivateKey.from_private_bytes(base64.b64decode(kf["seed"]))
    def mint(sub, role, ttl=600):
        p = b64(json.dumps({"sub": sub, "role": role, "jur": ["*"], "exp": int(time.time()) + ttl, "kid": kid}, separators=(",", ":")).encode())
        return f"v2.{p}." + b64(k.sign(at.DOMAIN + p.encode()))
    return mint

# endpoints that need only a valid token: (name, url, min role)
SVC = [("core", "http://127.0.0.1:8083/api/alarms", None), ("cim-pds", "http://127.0.0.1:8081/models", None),
       ("ems", "http://127.0.0.1:8082/v1/dts/scenarios?model=ieee14", None), ("historian", "http://127.0.0.1:8085/v1/stats", None)]
def code(url, tok):
    try: return httpx.get(url, headers={"Authorization": ("Bearer " + tok) if tok else "Bearer"}, timeout=10).status_code
    except Exception as e: return str(e)

alice = Sess("alice")
chk("login issues Ed25519 v2 token", alice.tok.startswith("v2."))
for n, u, _ in SVC:
    c = code(u, alice.tok)
    chk(f"{n} accepts a v2 token", c == 200, c)
for n, u, _ in SVC:
    chk(f"{n} refuses HMAC token (no downgrade)", code(u, hmac_tok("alice", "admin")) == 401)
    chk(f"{n} refuses tampered signature", code(u, alice.tok[:-3] + ("AAA" if not alice.tok.endswith("AAA") else "BBB")) == 401)
    chk(f"{n} refuses garbage / empty", code(u, "v2.x.y") == 401 and code(u, "") == 401)

ems_mint = own_key("ems")
for n, u, _ in SVC:
    chk(f"{n}: key capped to viewer cannot mint admin", code(u, ems_mint("ems-analytics", "admin")) == 401)
    chk(f"{n}: key bound to subject prefix cannot impersonate a user", code(u, ems_mint("alice", "viewer")) == 401)
    chk(f"{n}: unknown key id refused", code(u, own_key("ems")("ems-analytics", "viewer").replace("v2.", "v2.", 1) [:3] + b64(json.dumps({"sub": "ems-analytics", "role": "viewer", "jur": ["*"], "exp": int(time.time()) + 60, "kid": "nobody"}).encode()) + "." + b64(b"x" * 64)) == 401)
    chk(f"{n}: expired token refused", code(u, ems_mint("ems-analytics", "viewer", ttl=-5)) == 401)
chk("service key within its cap is accepted by core", code("http://127.0.0.1:8083/api/alarms", ems_mint("ems-analytics", "viewer")) == 200)

# a stolen low-privilege key cannot drive a privileged action on the core
fk = own_key("historian")
r = httpx.post("http://127.0.0.1:8083/api/command", headers={"Authorization": "Bearer " + fk("historian", "operator")}, json={"tag": "x", "action": "open", "reason": "t", "totp": "000000"})
chk("viewer-capped key claiming operator is refused on /api/command", r.status_code == 401, r.status_code)

# the system still works end-to-end under asymmetric tokens
h = httpx.get("http://127.0.0.1:8084/health").json()
chk("FEP connected and up", h.get("outstations_up") == 1, h)
hh = httpx.get("http://127.0.0.1:8085/health").json()
chk("historian follows the core with its own key", hh.get("connected_to") is not None, hh)
for p, port in (("A", 8083), ("B", 8183), ("C", 8283)):
    try:
        j = httpx.get(f"http://127.0.0.1:{port}/health", timeout=3).json()
        chk(f"core {p} up", j.get("status") == "ok", j)
    except Exception:
        pass
print(f"asym: {ok} passed, {bad} failed")
sys.exit(1 if bad else 0)
