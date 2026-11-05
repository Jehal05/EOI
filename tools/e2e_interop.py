#!/usr/bin/env python3
"""Live test of model exchange and parallel run: CIMXML full/difference round trip through the gateway, MATPOWER and PSS/E import,
power-flow agreement of imported models with the packaged model AND with an independent pypower solve, hostile-input refusals,
and parallel-run mode (command lockout, replication to the standby, legacy-sample comparison). Needs a running stack."""
import os, sys, json, subprocess, time, copy
import httpx
sys.path.insert(0, os.path.dirname(__file__))
from gs_client import Sess, A, B

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
ok = fail = 0
def check(name, cond, detail=""):
    global ok, fail
    if cond: ok += 1; print(f"  PASS  {name}")
    else: fail += 1; print(f"  FAIL  {name}  {str(detail)[:300]}")

U = str(int(time.time()) % 100000)
RT, RTB, MP, RAW, M14 = f"rt{U}", f"rtb{U}", f"mp{U}", f"raw{U}", f"m14{U}"
adm, carol, north, bob, alice = Sess("admin"), Sess("carol"), Sess("north"), Sess("bob"), Sess("alice")
T = {"Content-Type": "text/plain"}
def imp(s, path, body, **q): return s.h.post("/cim/import/" + path, params=q, content=body, headers=T)

def ems(path, body):
    sys.path.insert(0, f"{ROOT}/services/ems-analytics")
    os.environ["EMS_DEV"] = "1"
    from ems import auth
    return httpx.post("http://127.0.0.1:8082" + path, json=body, headers={"Authorization": "Bearer " + auth.mint("e2e", "viewer")}, timeout=120)

def vm_by_bus(model):
    r = ems("/v1/powerflow", {"model": model}).json()
    assert r["converged"], r
    return {int("".join(c for c in b["name"] if c.isdigit())): (b["vm"], b["va_deg"]) for b in r["buses"]}, r["losses_mw"]

print("== CIMXML full model through the gateway")
x = adm.h.get("/cim/models/ieee118/export.xml")
check("export is RDF/XML with a model header", x.status_code == 200 and "rdf+xml" in x.headers["content-type"] and "<md:FullModel" in x.text and "<?xml" in x.text[:10], x.status_code)
check("export covers terminals, limits and regulating controls", all(k in x.text for k in ("cim:Terminal", "cim:CurrentLimit", "cim:RegulatingControl", "cim:PowerTransformerEnd", "cim:LinearShuntCompensator")))
check("export is deterministic", adm.h.get("/cim/models/ieee118/export.xml").text == x.text)
r = imp(carol, "cimxml", x.text, id=RT); check("engineer re-imports it as another model", r.status_code == 200 and r.json()["counts"]["substations"] == 118, r.text)
a, b = adm.h.get("/cim/models/ieee118").json(), adm.h.get(f"/cim/models/{RT}").json()
def norm(m): 
    m = copy.deepcopy(m); m.pop("id", None); m.pop("source", None)
    return {k: (sorted(v, key=lambda e: e["mrid"]) if isinstance(v, list) else v) for k, v in m.items()}
na, nb = norm(a), norm(b)
check("the re-imported model equals the original field for field", na == nb, [k for k in na if na[k] != nb[k]])

print("== power flow agreement (EMS)")
ref, ref_loss = vm_by_bus("ieee118")
vr, lr = vm_by_bus(RT)
check("CIMXML round trip: identical power flow (max |dVm| < 1e-9)", max(abs(ref[k][0] - vr[k][0]) for k in ref) < 1e-9 and abs(ref_loss - lr) < 1e-6)
legacy = os.path.join(ROOT, "data", "legacy")
r = imp(carol, "matpower", open(f"{legacy}/case118.m").read(), id=MP); check("MATPOWER case118 imports", r.status_code == 200 and r.json()["counts"]["generators"] == 54, r.text)
r2 = imp(carol, "psse", open(f"{legacy}/case118.raw").read(), id=RAW); check("PSS/E RAW case118 imports", r2.status_code == 200 and r2.json()["counts"]["lines"] + r2.json()["counts"]["transformers"] == 186, r2.text)
vm_, lm = vm_by_bus(MP); vw_, lw = vm_by_bus(RAW)
check("MATPOWER import: power flow equals the packaged model", max(abs(ref[k][0] - vm_[k][0]) for k in ref) < 1e-4 and abs(ref_loss - lm) < 1e-2, (max(abs(ref[k][0] - vm_[k][0]) for k in ref), ref_loss, lm))
check("PSS/E import: power flow equals the MATPOWER import", max(abs(vw_[k][0] - vm_[k][0]) for k in ref) < 1e-9)
py = subprocess.run(["/home/claude/venv/bin/python", "-c", """
import json, numpy as np
from pypower.api import runpf, ppoption
from pypower import case118
r, ok = runpf(case118.case118(), ppoption(VERBOSE=0, OUT_ALL=0))
print(json.dumps({'ok': bool(ok), 'vm': r['bus'][:,7].tolist(), 'va': r['bus'][:,8].tolist(), 'loss': float(np.sum(r['branch'][:,13]+r['branch'][:,15]).real) }))
"""], capture_output=True, text=True)
ref_py = json.loads(py.stdout.strip().splitlines()[-1])
d = max(abs(vm_[i + 1][0] - ref_py["vm"][i]) for i in range(118)); sl = 68; da = max(abs((vm_[i + 1][1] - vm_[sl + 1][1]) - (ref_py["va"][i] - ref_py["va"][sl])) for i in range(118))
check(f"independent check against pypower runpf on IEEE-118 (max |dVm| = {d:.2e} pu, |dVa| relative to the slack = {da:.2e} deg)", ref_py["ok"] and d < 1e-4 and da < 1e-2, (d, da))
r = imp(carol, "matpower", open(f"{legacy}/case14.m").read(), id=M14); check("MATPOWER case14 imports", r.status_code == 200, r.text)
check("case14 power flow converges", len(vm_by_bus(M14)[0]) == 14)

print("== difference model")
src = adm.h.get(f"/cim/models/{RT}").json()
src["loads"][0]["p"] += 3.25; src["acLineSegments"][5]["x"] *= 1.1
rp = carol.h.put(f"/cim/models/{RT}", json=src); check("engineer edits the model (version 2)", rp.status_code == 200 and rp.json()["version"] == 2, rp.text)
dx = adm.h.get(f"/cim/models/{RT}/difference.xml", params={"from": 1, "to": 2})
check("difference is a dm:DifferenceModel with forward and reverse parts", dx.status_code == 200 and "dm:forwardDifferences" in dx.text and "dm:reverseDifferences" in dx.text and len(dx.text) < 6000, len(dx.text))
base = adm.h.get(f"/cim/models/{RT}/export.xml", params={"version": 1}).text
r = imp(carol, "cimxml", base, id=RTB); check("old version exported and loaded as rt118b", r.status_code == 200, r.text)
r = carol.h.post(f"/cim/models/{RTB}/apply-difference", content=dx.text, headers=T); check("difference applies to the old version", r.status_code == 200, r.text)
check("result equals version 2 of the source", norm(adm.h.get(f"/cim/models/{RTB}").json()) == norm(adm.h.get(f"/cim/models/{RT}").json()))
r = carol.h.post(f"/cim/models/{RTB}/apply-difference", content=dx.text, headers=T); check("applying it twice is refused (wrong base), nothing stored", r.status_code == 409, (r.status_code, r.text[:150]))
check("history is intact", adm.h.get(f"/cim/models/{RTB}/verify").json().get("ok") is True)

print("== hostile and unauthorised input")
check("viewer cannot import", imp(bob, "cimxml", x.text, id="zz").status_code == 403)
check("regional engineer cannot import", imp(north, "matpower", open(f"{legacy}/case14.m").read(), id="zz").status_code == 403)
bomb = '<?xml version="1.0"?><!DOCTYPE r [<!ENTITY a "aaaaaaaaaa"><!ENTITY b "&a;&a;&a;&a;&a;&a;&a;&a;">]><rdf:RDF>&b;</rdf:RDF>'
r = imp(carol, "cimxml", bomb, id="zz"); check("entity-expansion document refused (400)", r.status_code == 400, r.text)
check("truncated XML refused", imp(carol, "cimxml", x.text[:5000], id="zz").status_code == 400)
check("garbage MATPOWER refused (422)", imp(carol, "matpower", "not a case", id="zz").status_code == 422)
check("garbage RAW refused (422)", imp(carol, "psse", "x\ny\nz\n", id="zz").status_code == 422)
check("path-like model id refused", imp(carol, "matpower", open(f"{legacy}/case14.m").read(), id="../x").status_code == 400)
check("unknown format is 404", imp(carol, "foo", "x", id="zz").status_code == 404)
check("no half-imported model was left behind", "zz" not in [m["id"] for m in adm.h.get("/cim/models").json()])
nx = x.text.replace('<cim:EnergyConsumer.p>', '<cim:EnergyConsumer.p>nan', 1)
r = imp(carol, "cimxml", nx, id="zz"); check("non-numeric value refused", r.status_code in (400, 422), r.status_code)

print("== parallel run")
check("parallel-run is off by default", adm.get("/api/parallel").json()["enabled"] is False)
check("operator cannot switch it", alice.post("/api/parallel/switch", json={"enabled": True, "reason": "try"}).status_code == 403)
check("a reason is mandatory", adm.post("/api/parallel/switch", json={"enabled": True, "reason": ""}).status_code == 400)
snap = alice.get("/api/snapshot").json()["pts"]
an = [p for p in snap if "v" in p][:6]; st = [p for p in snap if "s" in p][:3]
adm.post("/api/parallel/reset", json={})
samples = [{"tag": p["t"], "v": p["v"]} for p in an[:5]] + [{"tag": an[5]["t"], "v": an[5]["v"] + 50.0}] + [{"tag": p["t"], "st": p["s"]} for p in st[:2]] + [{"tag": st[2]["t"], "st": 3 - st[2]["s"]}] + [{"tag": "NOT.A.TAG", "v": 1.0}]
r = carol.post("/api/parallel/samples", json={"samples": samples, "tolAbs": 5.0, "tolRel": 0.05, "source": "e2e"}); check("engineer sends legacy samples", r.status_code == 200 and r.json()["unknownTags"] == 1, r.text)
check("viewer cannot send samples", bob.post("/api/parallel/samples", json={"samples": samples}).status_code == 403)
check("malformed sample refused", carol.post("/api/parallel/samples", json={"samples": [{"tag": "x"}]}).status_code == 400)
rep = adm.get("/api/parallel", params={"minPct": 99.5}).json()
bad = {r_["tag"]: r_ for r_ in rep["worst"]}
check("report: the deliberately wrong analog and status are the worst rows", an[5]["t"] in bad and bad[an[5]["t"]]["withinPct"] < 100 and bad[st[2]["t"]]["statusMismatches"] == 1, rep["worst"][:3])
check("report: unknown legacy tag and never-seen tags are counted", rep["legacyTagsUnknownHere"] == 1 and rep["ourTagsNeverSeenInLegacy"] > 1000)
check("report: the stated criterion is not met", rep["criterion"]["met"] is False)
r = adm.post("/api/parallel/switch", json={"enabled": True, "reason": "e2e shadow run"}); check("admin switches parallel-run mode on", r.status_code == 200 and r.json()["enabled"] is True, r.text)
time.sleep(1.5)
check("the standby replicated the mode", httpx.get(B + "/api/parallel", headers={"Authorization": "Bearer " + adm.tok}).json()["enabled"] is True)
r = alice.post("/api/command", json={"tag": "BRK-BR0010-F.STATE", "action": "open", "reason": "must be refused", "totp": alice.code()})
check("remote control is refused while on (409, no field action)", r.status_code == 409 and "parallel-run" in r.text, (r.status_code, r.text[:120]))
check("the switch is in the event log", any("PARALLEL-RUN MODE ON" in e["text"] for e in adm.get("/api/soe?limit=3000").json()["events"]))
r = adm.post("/api/parallel/switch", json={"enabled": False, "reason": "e2e done"}); check("and off again", r.status_code == 200 and r.json()["enabled"] is False)
adm.post("/api/parallel/reset", json={})
print(f"\n{ok} passed, {fail} failed")
sys.exit(1 if fail else 0)
