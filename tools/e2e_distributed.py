#!/usr/bin/env python3
"""Live test of distributed contingency analysis over HTTP: starts a coordinator and two peer EMS processes (own ports),
checks the merged N-1 / N-2 equals the single-node answer, then kills a peer mid-way and checks the answer is unchanged.
Needs cim-pds on 8081 (stack up). Timing is printed, not asserted (host dependent)."""
import os, sys, time, subprocess, signal, json
import httpx
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, f"{ROOT}/services/ems-analytics")
os.environ["EMS_DEV"] = "1"
from ems import auth
PY = "/home/claude/venv/bin/python"
ok = fail = 0
def check(n, c, d=""):
    global ok, fail
    if c: ok += 1; print("  PASS ", n)
    else: fail += 1; print("  FAIL ", n, str(d)[:300])
procs = []
def start(port, peers=""):
    env = dict(os.environ, EMS_DEV="1", EMS_PEERS=peers, EMS_PEER_TIMEOUT="60")
    p = subprocess.Popen([PY, "-m", "uvicorn", "ems.api:app", "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning"],
                         cwd=f"{ROOT}/services/ems-analytics", env=env, stdout=open(f"/tmp/gs/ems{port}.log", "w"), stderr=subprocess.STDOUT)
    procs.append(p)
    for _ in range(60):
        try:
            if httpx.get(f"http://127.0.0.1:{port}/health", timeout=1).status_code == 200: return p
        except Exception: time.sleep(0.5)
    raise SystemExit(f"EMS on {port} did not start")
H = {"Authorization": "Bearer " + auth.mint("e2e", "viewer")}
def post(port, path, body, timeout=300): return httpx.post(f"http://127.0.0.1:{port}{path}", json=body, headers=H, timeout=timeout)
key = lambda r: [(x["outage"], round(x["severity"], 6)) for x in r["results"]]
try:
    p1 = start(8095); p2 = start(8096)
    start(8094, "http://127.0.0.1:8095,http://127.0.0.1:8096")
    start(8097)                                           # reference: no peers
    M = {"model": "ieee118", "top": 500}
    t = time.time(); ref = post(8097, "/v1/contingency/distributed", {**M, "kind": "n1"}).json(); t_ref = time.time() - t
    t = time.time(); r = post(8094, "/v1/contingency/distributed", {**M, "kind": "n1"}); t_d = time.time() - t
    check("distributed N-1 over 3 processes answers", r.status_code == 200, r.text[:200])
    r = r.json()
    check("merged N-1 equals the single-node result (all 186 cases)", key(r) == key(ref) and r["cases"] == 186, (len(key(r)), len(key(ref))))
    check("every peer took part", len(r["distributed"]["peers"]) == 3 and all(p["ok"] and p["cases"] > 0 for p in r["distributed"]["peers"]), r["distributed"])
    print(f"       N-1 wall: single {t_ref:.1f} s, 3 processes {t_d:.1f} s")
    single = post(8097, "/v1/contingency", {**M}).json()
    check("same as the ordinary N-1 endpoint", [x["outage"] for x in single["results"]] == [x["outage"] for x in r["results"]])
    n2ref = post(8097, "/v1/contingency/distributed", {**M, "kind": "n2", "k_top": 25, "verify": 80}).json()
    n2d = post(8094, "/v1/contingency/distributed", {**M, "kind": "n2", "k_top": 25, "verify": 80}).json()
    fx = lambda q: [(x["outage"], x["ac_verified"], round(x.get("severity", -1), 6)) for x in q["results"]]
    check("merged N-2 equals the single-node result", fx(n2d) == fx(n2ref) and n2d["n_ac_verified"] > 0 and len(n2d["distributed"]["peers"]) == 3)
    # fault tolerance: kill a peer, ask again
    p1.send_signal(signal.SIGKILL); p1.wait()
    r2 = post(8094, "/v1/contingency/distributed", {**M, "kind": "n1"}).json()
    dead = [p for p in r2["distributed"]["peers"] if p["peer"].endswith(":8095")][0]
    check("with a peer killed the answer is unchanged", key(r2) == key(ref))
    check("the lost shard was reassigned and reported", dead["ok"] is False and dead["reassigned_to"] in ("http://127.0.0.1:8096", "local"), dead)
    # authorisation and SSRF
    check("no token -> 401", httpx.post("http://127.0.0.1:8094/v1/contingency/distributed", json=M).status_code == 401)
    check("the request cannot name peers (unknown field ignored, configured peers used)", post(8094, "/v1/contingency/distributed", {**M, "peers": ["http://169.254.169.254"]}).status_code == 200)
    check("shard endpoint refuses a branch index outside the network", post(8096, "/v1/contingency/shard", {"model": "ieee118", "version": 1, "jobs": [[9999]]}).status_code == 422)
    check("use_peers=false runs everything locally", post(8094, "/v1/contingency/distributed", {**M, "kind": "n1", "use_peers": False}).json()["distributed"]["shards"] == 1)
finally:
    for p in procs:
        try: p.kill()
        except Exception: pass
print(f"\n{ok} passed, {fail} failed")
sys.exit(1 if fail else 0)
