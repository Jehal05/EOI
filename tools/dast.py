#!/usr/bin/env python3
"""First-party dynamic security test of the running stack (not a replacement for an independent penetration test).
Checks, per service: unauthenticated access to every route, wrong-verb handling, token tampering and forgery,
mutated-body fuzzing of every POST route with valid tokens (no 5xx, no hang), path traversal, header hygiene, CORS,
oversize bodies, header-injection strings. Routes of the core are read from Api.java, those of EMS and the historian
from their OpenAPI documents, cim-pds from a fixed list. Run on a stack started by tools/stack.sh; destructive on the
test stack only (the fuzzing posts garbage to operator endpoints)."""
import base64, json, os, random, re, sys, time
import httpx
sys.path.insert(0, os.path.dirname(__file__))
from gs_client import Sess

ROOT = os.path.join(os.path.dirname(__file__), "..")
CORE, CIM, EMS, HIST, FEP = "http://127.0.0.1:8083", "http://127.0.0.1:8081", "http://127.0.0.1:8082", "http://127.0.0.1:8085", "http://127.0.0.1:8084"
findings = []; passed = 0
def ok(name, cond, detail=""):
    global passed
    if cond: passed += 1
    else:
        findings.append((name, str(detail)[:300])); print("  FINDING  " + name + "  " + str(detail)[:200])
H = httpx.Client(timeout=15)
def call(method, url, tok=None, **kw):
    h = kw.pop("headers", {})
    if tok: h["Authorization"] = "Bearer " + tok
    try: return H.request(method, url, headers=h, **kw)
    except Exception as e: return e

alice, admin, bob = Sess("alice"), Sess("admin"), Sess("bob")

# ---- route inventory
core_routes = sorted(set(re.findall(r'case "(GET|POST|PUT|DELETE) (/api/[^"]*)"', open(f"{ROOT}/services/scada-core/src/gs/core/Api.java").read())))
public = {("POST", "/api/login")}
def openapi(base):
    d = httpx.get(base + "/openapi.json", timeout=10).json()
    return [(m.upper(), p) for p, ms in d["paths"].items() for m in ms if m in ("get", "post", "put", "delete")]
ems_routes, hist_routes = openapi(EMS), openapi(HIST)
print(f"routes: core {len(core_routes)}, ems {len(ems_routes)}, historian {len(hist_routes)}")

print("== 1. no token, garbage token, tampered token")
def fill(p): return re.sub(r"\{[^}]*\}", "x", p)
for name, base, routes, pub in (("core", CORE, core_routes, public), ("ems", EMS, ems_routes, {("GET", "/health")}), ("historian", HIST, hist_routes, {("GET", "/health")})):
    for m, p in routes:
        if (m, p) in pub or p in ("/openapi.json", "/docs", "/redoc", "/api/stream"): continue
        u = base + fill(p)
        for label, tok in (("no token", None), ("garbage token", "x.y"), ("empty bearer", "")):
            r = call(m, u, tok, content=b"{}" if m == "POST" else None, headers={"Content-Type": "application/json"})
            ok(f"{name} {m} {p}: {label} -> 401", not isinstance(r, Exception) and r.status_code == 401, getattr(r, "status_code", r))
payload_b64 = alice.tok.split(".")[0] if not alice.tok.startswith("v2.") else alice.tok.split(".")[1]
def b64d(s): return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))
def b64e(b): return base64.urlsafe_b64encode(b).rstrip(b"=").decode()
claims = json.loads(b64d(payload_b64)); claims["role"] = "admin"
forged = alice.tok.replace(payload_b64, b64e(json.dumps(claims, separators=(",", ":")).encode()))
for name, u in (("core", CORE + "/api/oplog"), ("ems", EMS + "/v1/dts/scenarios?model=ieee14"), ("historian", HIST + "/v1/stats"), ("cim-pds", CIM + "/models")):
    r = call("GET", u, forged); ok(f"{name}: payload edited to role admin is rejected", not isinstance(r, Exception) and r.status_code == 401, getattr(r, "status_code", r))
    r = call("GET", u, "none." + alice.tok); ok(f"{name}: prefixed/odd token is rejected", not isinstance(r, Exception) and r.status_code == 401, getattr(r, "status_code", r))

print("== 2. role boundaries (viewer must not reach operator/admin routes)")
for m, p, body in (("POST", "/api/command", {"tag": "x", "action": "open", "reason": "t", "totp": "000000"}), ("POST", "/api/ai/switch", {"on": False}),
                   ("POST", "/api/parallel/switch", {"on": True}), ("POST", "/api/alarms/ack", {"id": 1}), ("GET", "/api/oplog", None), ("GET", "/api/verify-replay", None)):
    r = call(m, CORE + p, bob.tok, json=body)
    ok(f"viewer {m} {p} -> 403", not isinstance(r, Exception) and r.status_code == 403, getattr(r, "status_code", r))
r = call("POST", CORE + "/api/ai/switch", alice.tok, json={"on": False}); ok("operator cannot flip the AI switch (admin only) -> 403", not isinstance(r, Exception) and r.status_code == 403, getattr(r, "status_code", r))

print("== 3. wrong verbs and unknown routes")
for meth in ("TRACE", "PATCH", "OPTIONS", "DELETE", "PUT"):
    r = call(meth, CORE + "/api/alarms", alice.tok)
    ok(f"core {meth} /api/alarms is not a 5xx", not isinstance(r, Exception) and r.status_code < 500, getattr(r, "status_code", r))
r = call("GET", CORE + "/api/does-not-exist", alice.tok); ok("unknown /api route -> JSON 404 (not the app shell)", not isinstance(r, Exception) and r.status_code == 404 and "<html" not in r.text.lower(), getattr(r, "status_code", r))

print("== 4. mutated-body fuzzing of POST routes (valid tokens)")
rnd = random.Random(7)
def mutate_json(s):
    b = bytearray(s.encode())
    for _ in range(rnd.randint(1, 5)):
        k = rnd.randrange(7)
        if not b: b = bytearray(b"{")
        i = rnd.randrange(len(b))
        if k == 0: b[i] ^= 1 << rnd.randrange(7)
        elif k == 1: del b[i:i + rnd.randint(1, 8)]
        elif k == 2: b[i:i] = rnd.choice([b'null', b'[]', b'{}', b'"\\u0000"', b'-1e999', b'99999999999999999999', b'"' + b"A" * 5000 + b'"', b"[" * 300])
        elif k == 3: b = b[:i]
        elif k == 4: b[i] = rnd.randrange(256)
        elif k == 5: b[i:i] = b[i:i + 40]
        else: b[i:i] = b'"__proto__":{"admin":true},'
    return bytes(b)
seeds = {"core": ['{"tag":"G001.P","v":1.5}', '{"tag":"BRK1.STATE","action":"open","reason":"x","totp":"123456"}', '{"id":1,"reason":"x","minutes":5}', '{"on":true,"reason":"x"}', '{"text":"note","category":"op"}', '{"model":"ieee118"}'],
         "ems": ['{"model":"ieee14"}', '{"model":"ieee14","open_switches":["x"],"outages":["y"]}', '{"model":"ieee118","version":1,"workers":2}', '{"measurements":[{"kind":"vm","ref":"1","value":1.0,"sigma":0.01}],"model":"ieee14"}'],
         "historian": ['{"tags":["G001.P"],"mode":"sum","horizon_min":60}']}
fiveXX = 0; total = 0; slow = 0
for name, base, routes, tok in (("core", CORE, core_routes, alice.tok), ("ems", EMS, ems_routes, alice.tok), ("historian", HIST, hist_routes, alice.tok)):
    for m, p in routes:
        if m != "POST" or p in ("/api/login", "/api/ingest", "/api/replicate", "/api/cluster/vote", "/api/cluster/prevote", "/api/cluster/heartbeat", "/api/parallel/reset", "/api/ai/switch", "/api/parallel/switch") or "shard" in p: continue
        for t in (alice.tok, admin.tok):
            for _ in range(8):
                body = mutate_json(rnd.choice(seeds[name])); total += 1
                t0 = time.time(); r = call("POST", base + fill(p), t, content=body, headers={"Content-Type": "application/json"}); dt = time.time() - t0
                if isinstance(r, Exception): ok(f"{name} POST {p}: connection survived a mutated body", False, r); continue
                if r.status_code >= 500: fiveXX += 1; ok(f"{name} POST {p} returned {r.status_code} on a mutated body", False, body[:120])
                if dt > 10: slow += 1; ok(f"{name} POST {p} took {dt:.1f}s on a mutated body", False, body[:120])
ok(f"fuzzed {total} bodies: no 5xx, none slower than 10 s", fiveXX == 0 and slow == 0, (fiveXX, slow))
for name, u in (("core", CORE), ("ems", EMS), ("historian", HIST), ("cim-pds", CIM)):
    r = call("GET", u + "/health"); ok(f"{name} still healthy after fuzzing", not isinstance(r, Exception) and r.status_code == 200, r)

print("== 5. path traversal and header hygiene")
for p in ("/../../../../etc/passwd", "/..%2f..%2f..%2fetc/passwd", "/%2e%2e/%2e%2e/etc/passwd", "/....//....//etc/passwd", "/assets/..%5c..%5cetc/passwd", "/%00/etc/passwd", "/etc/passwd%00.js"):
    r = httpx.get(CORE + p, timeout=10)  # httpx normalises "..": use raw request below too
    ok(f"core static {p} leaks nothing", r.status_code in (400, 403, 404) or "root:" not in r.text, r.status_code)
import socket
for p in ("/../../../../etc/passwd", "/..%2f..%2f..%2fetc/passwd", "/assets/../../../../../etc/passwd"):
    s = socket.create_connection(("127.0.0.1", 8083), 5); s.sendall(f"GET {p} HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n".encode())
    data = b""
    while True:
        c = s.recv(65536)
        if not c: break
        data += c
    ok(f"core raw request {p}: no file content", b"root:" not in data, data[:80])
for p in ("/cim/../../etc/passwd", "/ems/..%2f..%2fetc/passwd", "/hist/%2e%2e/%2e%2e/etc/passwd"):
    r = call("GET", CORE + p, alice.tok); ok(f"core proxy {p}: no file content", not isinstance(r, Exception) and "root:" not in r.text, getattr(r, "status_code", r))
r = httpx.get(CORE + "/", timeout=10)
for h, want in (("content-security-policy", "default-src 'self'"), ("x-content-type-options", "nosniff"), ("x-frame-options", "DENY"), ("referrer-policy", "no-referrer")):
    ok(f"UI response carries {h}", want.lower() in r.headers.get(h, "").lower(), r.headers.get(h))
ok("CSP forbids inline scripts and remote origins", "script-src" in r.headers.get("content-security-policy", "") or "default-src 'self'" in r.headers.get("content-security-policy", "") and "unsafe-eval" not in r.headers.get("content-security-policy", ""))
ok("no Server/X-Powered-By version banner", not any(h in r.headers for h in ("server", "x-powered-by")), dict(r.headers))
r = httpx.get(CORE + "/health", headers={"Origin": "https://evil.example"}, timeout=10)
ok("CORS: a foreign Origin is not echoed back", "access-control-allow-origin" not in {k.lower() for k in r.headers}, dict(r.headers))
r = httpx.options(CORE + "/api/command", headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "POST"}, timeout=10)
ok("CORS preflight from a foreign origin is not approved", "access-control-allow-origin" not in {k.lower() for k in r.headers})
r = call("GET", CORE + "/api/alarms", alice.tok); ok("API responses are not cacheable", "no-store" in r.headers.get("cache-control", ""))

print("== 6. oversize input and injection strings")
r = call("POST", CORE + "/api/ingest", alice.tok, content=b"x" * (40 << 20))
ok("40 MB ingest body is refused (413 or the connection is dropped)", isinstance(r, Exception) or r.status_code in (400, 413), getattr(r, "status_code", r))
r = call("GET", CORE + "/health"); ok("core healthy after the oversize upload", not isinstance(r, Exception) and r.status_code == 200 and r.json()["status"] == "ok", r)
r = call("POST", CORE + "/api/login", content=b"{" + b'"user":"' + b"A" * 2_000_000 + b'"}'); ok("2 MB login body is refused or rejected cleanly (status or dropped connection)", isinstance(r, Exception) or r.status_code in (400, 401, 413, 429), getattr(r, "status_code", r))
for evil in ("x'; DROP TABLE raw;--", "x\r\nSet-Cookie: a=b", "../../etc/passwd", "<script>alert(1)</script>", "%00", "x" * 5000):
    r = call("GET", HIST + "/v1/trend", alice.tok, params={"tag": evil, "from": 0, "to": 1000})
    ok(f"historian trend tag {evil[:18]!r}: handled (no 5xx, no injected header)", not isinstance(r, Exception) and r.status_code < 500 and "set-cookie" not in {k.lower() for k in r.headers}, getattr(r, "status_code", r))
r = call("GET", HIST + "/v1/stats", alice.tok); ok("historian intact after injection strings", not isinstance(r, Exception) and r.status_code == 200 and r.json().get("raw_rows", 0) > 0, getattr(r, "status_code", r))
r1 = call("POST", CORE + "/api/login", json={"user": "alice", "password": "wrong-password"}); r2 = call("POST", CORE + "/api/login", json={"user": "no-such-user-zz", "password": "wrong-password"})
ok("login: unknown user and wrong password are indistinguishable (status and body)", r1.status_code == r2.status_code and r1.text == r2.text, (r1.status_code, r1.text[:60], r2.status_code, r2.text[:60]))

print("== 7. FEP command port")
for hdrs in ({}, {"Authorization": "Bearer x.y"}):
    r = call("POST", FEP + "/command", headers=hdrs, json={"tag": "x", "action": "open"}); ok(f"FEP /command without a valid token -> 401 ({'garbage' if hdrs else 'none'})", not isinstance(r, Exception) and r.status_code == 401, getattr(r, "status_code", r))
r = call("POST", FEP + "/command", bob.tok, json={"tag": "x", "action": "open"}); ok("FEP /command with a viewer token -> 403 (operator needed)", not isinstance(r, Exception) and r.status_code in (401, 403), getattr(r, "status_code", r))

print(f"\ndast: {passed} checks passed, {len(findings)} findings")
sys.exit(1 if findings else 0)
