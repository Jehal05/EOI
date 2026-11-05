#!/usr/bin/env python3
"""Live test of the secured field links. Restarts the stack three times:
  1. GS_TLS=1 GS_AUTH=1  mutual TLS + application-layer command authentication (IEC 104)
  2. GS_IEC101=1         IEC 101 FT1.2 link (the FEP has no other path to the outstation)
and finally restores the plain stack. Run on its own."""
import sys, os, time, ssl, socket, struct, hmac, hashlib, subprocess, httpx
sys.path.insert(0, os.path.dirname(__file__))
from gs_client import Sess

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
D = "/tmp/gs"
ok = fail = 0
def check(name, cond, detail=""):
    global ok, fail
    if cond: ok += 1; print(f"  PASS  {name}")
    else: fail += 1; print(f"  FAIL  {name}  {detail}")

def up(**opt):
    subprocess.run([f"{ROOT}/tools/stack.sh", "down"], capture_output=True)
    env = dict(os.environ, **{k: "1" for k, v in opt.items() if v})
    subprocess.Popen([f"{ROOT}/tools/stack.sh", "up"], env=env, stdout=open(f"{D}/up.out", "w"), stderr=subprocess.STDOUT, start_new_session=True)
    t0 = time.time()
    while time.time() - t0 < 180:
        try:
            if httpx.get("http://127.0.0.1:8084/health", timeout=2).status_code == 200: break
        except Exception: pass
        time.sleep(1)
    else:
        raise SystemExit("stack did not come up: " + open(f"{D}/up.out").read()[-500:])
    time.sleep(25)

def fresh(s):
    c = s.get("/api/comms").json()
    return sum(x["stale"] for x in c["substations"]) == 0 and c["substations"]

def wait(f, t):
    t0 = time.time()
    while time.time() - t0 < t:
        v = f()
        if v: return v
        time.sleep(1)

def state(s, tag):
    return {p["t"]: p for p in s.get("/api/snapshot").json()["pts"]}[tag].get("s")

def operate(s, tag):
    r = s.command(tag, "open", reason="secure link e2e")
    if r.status_code != 200: return r
    wait(lambda: state(s, tag) == 1, 10)
    return s.command(tag, "close", reason="secure link e2e restore")

TAG = "BRK-BR0040-F.STATE"

# --- raw IEC 104 probes -------------------------------------------------------------------------
def tls_ctx(cert=None, key=None, ca=None):
    c = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    c.minimum_version = ssl.TLSVersion.TLSv1_2
    if ca: c.load_verify_locations(ca)
    else: c.check_hostname = False; c.verify_mode = ssl.CERT_NONE
    if cert: c.load_cert_chain(cert, key)
    return c

def rd(sock, timeout=3):
    sock.settimeout(timeout)
    def need(n):
        b = b""
        while len(b) < n:
            x = sock.recv(n - len(b))
            if not x: raise EOFError
            b += x
        return b
    h = need(2)
    if h[0] != 0x68: raise ValueError("not IEC 104")
    return need(h[1])

def apdu_i(ssn, asdu): return bytes([0x68, 4 + len(asdu)]) + struct.pack("<HH", ssn << 1, 0) + asdu
def dc(ca, ioa, dcs, sel):
    return bytes([46, 1, 6, 0]) + struct.pack("<H", ca) + struct.pack("<I", ioa)[:3] + bytes([(0x80 if sel else 0) | dcs])

def probe(ctx, name="outstation"):
    s = socket.create_connection(("127.0.0.1", 2404), timeout=5)
    if ctx is None: return s
    return ctx.wrap_socket(s, server_hostname=name)

def refused(ctx):
    """True if no usable IEC 104 session results."""
    try:
        s = probe(ctx)
        s.sendall(bytes([0x68, 4, 0x07, 0, 0, 0]))   # STARTDT act
        rd(s)
        return False
    except Exception:
        return True

def first_type(frame):
    return frame[4] if len(frame) > 4 else None

def wait_type(sock, typ, n=30):
    for _ in range(n):
        f = rd(sock)
        if len(f) > 4 and (f[0] & 1) == 0 and f[4] == typ: return f
    return None

print("== mode 1: mutual TLS + application-layer authentication")
up(GS_TLS=1, GS_AUTH=1)
alice = Sess("alice")
check("FEP link is up over TLS: all substations have fresh data", bool(wait(lambda: fresh(alice), 60)))
check("FEP log shows TLS and authentication enabled", "TLS enabled" in open(f"{D}/fep.log").read() and "authentication enabled" in open(f"{D}/fep.log").read())
r = operate(alice, TAG)
check("authenticated operator command (open, then close) works through TLS + challenge/reply", r.status_code == 200, getattr(r, "text", ""))
P = f"{D}/pki"
check("plaintext client gets no session", refused(None))
check("TLS client without a certificate gets no session", refused(tls_ctx(ca=f"{P}/ca.pem")))
subprocess.run([f"{ROOT}/services/fep-gateway/bin/pki", "ca", "-dir", f"{D}/pki-rogue"], capture_output=True)
subprocess.run([f"{ROOT}/services/fep-gateway/bin/pki", "issue", "-dir", f"{D}/pki-rogue", "-cn", "fep", "-dns", "fep"], capture_output=True)
check("client certificate from a foreign CA gets no session", refused(tls_ctx(f"{D}/pki-rogue/fep.pem", f"{D}/pki-rogue/fep.key", f"{P}/ca.pem")))
subprocess.run([f"{ROOT}/services/fep-gateway/bin/pki", "issue", "-dir", P, "-cn", "intruder", "-dns", "intruder"], capture_output=True)
check("valid-CA certificate with a name outside the allow-list gets no session", refused(tls_ctx(f"{P}/intruder.pem", f"{P}/intruder.key", f"{P}/ca.pem")))
ctx = tls_ctx(f"{P}/fep.pem", f"{P}/fep.key", f"{P}/ca.pem")
s = probe(ctx); s.sendall(bytes([0x68, 4, 0x07, 0, 0, 0])); rd(s)
s.sendall(apdu_i(0, dc(1, 1, 1, True)))
ch = None
try: ch = wait_type(s, 81)
except Exception: pass
check("trusted peer without the key: command is answered with a challenge, not executed", ch is not None)
s.close()
s = probe(ctx); s.sendall(bytes([0x68, 4, 0x07, 0, 0, 0])); rd(s)
s.sendall(apdu_i(0, dc(1, 1, 1, True)))
ok_er = False
try:
    ch = wait_type(s, 81)
    # forge a reply with a wrong MAC: S_RP_NA_1 = type 82, csq/user/mac 22 bytes element
    el = ch[4 + 6 + 3: 4 + 6 + 3 + 16]
    rep = bytes([82, 1, 7, 0, 1, 0, 0, 0, 0]) + el[:6] + b"\x00" * 16
    s.sendall(apdu_i(1, rep))
    ok_er = wait_type(s, 87) is not None
except Exception as e:
    print("       probe:", e)
check("forged reply with a wrong MAC is rejected with an authentication error", ok_er)
s.close()
check("the system still operates after the attacks", Sess("alice").get("/api/comms").status_code == 200 and bool(fresh(alice)))

print("== mode 2: IEC 101 FT1.2 link")
up(GS_IEC101=1)
alice = Sess("alice")
check("data flows over IEC 101 only (FEP has no IEC 104 path)", bool(wait(lambda: fresh(alice), 90)))
log = open(f"{D}/out.log").read()
check("outstation reports IEC 101 slave enabled", "IEC 101 slave enabled" in log)
check("no IEC 104 session was used by the FEP", "iec104 session ended" not in log)
r = operate(alice, TAG)
check("operator command (select, execute) works over IEC 101", r.status_code == 200, getattr(r, "text", ""))

print("== mode 3: DNP3 (over TLS) for common address 1, IEC 104 for the rest")
up(GS_DNP3=1, GS_TLS=1)
alice = Sess("alice")
check("all substations fresh (CA 1 can only come over DNP3)", bool(wait(lambda: fresh(alice), 90)))
log = open(f"{D}/out.log").read()
check("outstation reports the DNP3 listener with TLS", "DNP3 outstation enabled" in log and "tls=true" in log)
env = dict(os.environ, GS_DEV="1", CIM_PDS_DEV="1", CIM_DATA_DIR=os.path.join(ROOT, "data"))
tok = subprocess.run([f"{ROOT}/services/cim-pds/target/release/cim-pds", "mint", "outstation", "viewer", "*", "86400"], env=env, capture_output=True, text=True).stdout.strip().splitlines()[-1]
pl = httpx.get("http://127.0.0.1:8081/models/ieee118/pointlist", headers={"Authorization": "Bearer " + tok}, timeout=20).json()["points"]
t1 = next(p["tag"] for p in pl if p["ca"] == 1 and p["kind"] == "status")
check("found a breaker of CA 1 in the point list", bool(t1), t1)
r = operate(alice, t1)
check("breaker of CA 1 operated through DNP3 select-before-operate over TLS", r.status_code == 200, getattr(r, "text", ""))
t2 = next(p["tag"] for p in pl if p["ca"] != 1 and p["kind"] == "status")
r = operate(alice, t2)
check("breaker of another CA still operates through IEC 104", r.status_code == 200, getattr(r, "text", ""))
check("plaintext DNP3 client gets no session", (lambda: (lambda s: (s.sendall(bytes.fromhex("056405C00A000100")), s.settimeout(2), s.recv(10) == b"")[-1])(socket.create_connection(("127.0.0.1", 20000), timeout=3)))() if True else True)

print("== restoring the plain stack")
up()
alice = Sess("alice")
check("plain stack healthy again", bool(wait(lambda: fresh(alice), 60)))
print(f"\n{ok} passed, {fail} failed")
sys.exit(1 if fail else 0)
