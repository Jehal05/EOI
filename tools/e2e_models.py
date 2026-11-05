#!/usr/bin/env python3
"""Live test of model governance on cim-pds (port 8081): proposals, two-person approval, jurisdiction, three-way
merge, conflicts, hierarchical split/merge, hash chain, restart persistence. Needs a running stack; restarts cim-pds once."""
import os, sys, json, time, subprocess, httpx

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
CIM = "http://127.0.0.1:8081"
env = dict(os.environ, GS_DEV="1", CIM_PDS_DEV="1", CIM_DATA_DIR=f"{ROOT}/data")
ok = fail = 0
def check(name, cond, detail=""):
    global ok, fail
    if cond: ok += 1; print(f"  PASS  {name}")
    else: fail += 1; print(f"  FAIL  {name}  {str(detail)[:300]}")

def tok(sub, role, jur="*"):
    return subprocess.run([f"{ROOT}/services/cim-pds/target/release/cim-pds", "mint", sub, role, jur, "3600"], env=env, capture_output=True, text=True).stdout.strip().splitlines()[-1]

class U:
    def __init__(self, sub, role, jur="*"):
        self.sub = sub; self.h = {"Authorization": "Bearer " + tok(sub, role, jur)}
    def get(self, p, **kw): return httpx.get(CIM + p, headers=self.h, timeout=60, **kw)
    def post(self, p, body=None): return httpx.post(CIM + p, headers=self.h, json=body if body is not None else {}, timeout=60)

e1, e2, e3 = U("eng1", "engineer"), U("eng2", "engineer"), U("eng3", "engineer")
op, vw = U("op1", "operator"), U("view1", "viewer")
M = "ieee118"
model = e1.get(f"/models/{M}").json()
loads = model["loads"]
regions = {s["mrid"]: s["region"] for s in model["substations"]}
vl_sub = {v["mrid"]: v["substation"] for v in model["voltageLevels"]}
node_reg = {n["mrid"]: regions[vl_sub[n["voltageLevel"]]] for n in model["connectivityNodes"]}
def ops_set(mrid, **f): return [{"op": "set", "kind": "load", "mrid": mrid, "fields": f}]
def load_p(u, mrid): return next(l for l in u.get(f"/models/{M}").json()["loads"] if l["mrid"] == mrid)["p"]
v0 = e1.get(f"/models/{M}/history").json()[-1]["version"]

print("== proposal lifecycle and roles")
r = op.post(f"/models/{M}/proposals", {"title": "x", "ops": ops_set(loads[0]["mrid"], p=1.0)}); check("operator cannot propose -> 403", r.status_code == 403, r.text)
r = vw.get(f"/models/{M}/proposals"); check("viewer can list proposals", r.status_code == 200)
r = e1.post(f"/models/{M}/proposals", {"title": "bad", "ops": [{"op": "set", "kind": "load", "mrid": "NOPE", "fields": {"p": 1}}]}); check("unknown element -> 400", r.status_code == 400, r.text)
r = e1.post(f"/models/{M}/proposals", {"title": "bad node", "ops": ops_set(loads[0]["mrid"], node="NO-SUCH-NODE")}); check("dangling reference -> 422 with issues", r.status_code == 422 and "DANGLING_REF" in r.text, r.text)
r = e1.post(f"/models/{M}/proposals", {"title": "empty", "ops": ops_set(loads[0]["mrid"], p=loads[0]["p"])}); check("no-op proposal -> 400", r.status_code == 400, r.text)
r = e1.post(f"/models/{M}/proposals", {"title": "both", "ops": ops_set(loads[0]["mrid"], p=2.0), "model": model}); check("model and ops together -> 400", r.status_code == 400)
L0, L1, L2 = loads[0]["mrid"], loads[1]["mrid"], loads[2]["mrid"]
old0 = loads[0]["p"]
r = e1.post(f"/models/{M}/proposals", {"title": "Raise load 0", "ops": ops_set(L0, p=old0 + 10.0)}); check("engineer creates proposal", r.status_code == 200, r.text)
p1 = r.json()["proposal"]["id"]; check("response lists exactly one field change", len(r.json()["changes"]) == 1 and r.json()["changes"][0]["fields"][0]["new"] == old0 + 10.0)
r = e1.post(f"/models/{M}/proposals/{p1}/approve"); check("two-person rule: author cannot approve -> 403", r.status_code == 403 and "two-person" in r.text, r.text)
r = e2.post(f"/models/{M}/proposals/{p1}/merge"); check("merge before approval -> 409", r.status_code == 409, r.text)
r = op.post(f"/models/{M}/proposals/{p1}/approve"); check("operator cannot approve -> 403", r.status_code == 403)
r = e2.post(f"/models/{M}/proposals/{p1}/approve", {"comment": "checked against the load letter"}); check("second engineer approves", r.status_code == 200 and r.json()["status"] == "approved", r.text)
r = e2.post(f"/models/{M}/proposals/{p1}/approve"); check("same approver twice -> 409", r.status_code == 409)
d = e2.get(f"/models/{M}/proposals/{p1}").json(); check("detail shows fast-forward and the approver", d["mergeStatus"]["state"] == "fast-forward" and d["approvals"][0]["user"] == "eng2", d)
check("model unchanged before merge", load_p(e1, L0) == old0)
r = e2.post(f"/models/{M}/proposals/{p1}/merge", {"note": "e2e"}); check("merge commits a new version", r.status_code == 200 and r.json()["version"] == v0 + 1, r.text)
check("merged value is live", load_p(e1, L0) == old0 + 10.0)
h = e1.get(f"/models/{M}/history").json()[-1]
check("history records merge with proposal, author, approver", h["action"] == "merge" and "eng1" in h["detail"] and "eng2" in h["detail"] and f"#{p1}" in h["detail"], h)
check("hash chain verifies", e1.get(f"/models/{M}/verify").json().get("ok") is True)
r = e3.post(f"/models/{M}/proposals/{p1}/approve"); check("merged proposal cannot be approved again -> 409", r.status_code == 409)
r = e2.post(f"/models/{M}/proposals/{p1}/merge"); check("merging twice -> 409", r.status_code == 409)

print("== stale proposals: clean three-way merge")
v1 = v0 + 1
pa = e1.post(f"/models/{M}/proposals", {"title": "A", "ops": ops_set(L1, p=111.0)}).json()["proposal"]["id"]
pb = e1.post(f"/models/{M}/proposals", {"title": "B", "ops": ops_set(L2, p=222.0)}).json()["proposal"]["id"]
for p in (pa, pb): e2.post(f"/models/{M}/proposals/{p}/approve")
check("merge A (fast-forward)", e2.post(f"/models/{M}/proposals/{pa}/merge").status_code == 200)
d = e2.get(f"/models/{M}/proposals/{pb}").json(); check("B is now stale but merges cleanly", d["mergeStatus"]["state"] == "clean-merge", d["mergeStatus"])
r = e2.post(f"/models/{M}/proposals/{pb}/merge"); check("merge B (three-way)", r.status_code == 200, r.text)
check("both A and B are in the model, and the earlier change survived", load_p(e1, L1) == 111.0 and load_p(e1, L2) == 222.0 and load_p(e1, L0) == old0 + 10.0)

print("== conflicts")
pc = e1.post(f"/models/{M}/proposals", {"title": "C", "ops": ops_set(L1, p=500.0)}).json()["proposal"]["id"]
pd = e3.post(f"/models/{M}/proposals", {"title": "D", "ops": ops_set(L1, p=600.0)}).json()["proposal"]["id"]
e2.post(f"/models/{M}/proposals/{pc}/approve"); e2.post(f"/models/{M}/proposals/{pd}/approve")
check("merge C", e2.post(f"/models/{M}/proposals/{pc}/merge").status_code == 200)
d = e2.get(f"/models/{M}/proposals/{pd}").json(); check("D reports one conflict on the load's p", d["mergeStatus"]["state"] == "conflicts" and d["conflicts"][0]["key"] == f"load/{L1}#p", d["mergeStatus"])
r = e2.post(f"/models/{M}/proposals/{pd}/merge"); check("merging D without resolution -> 409 and nothing changes", r.status_code == 409 and "conflicts" in r.json() and load_p(e1, L1) == 500.0, r.text)
r = e2.post(f"/models/{M}/proposals/{pd}/merge", {"resolutions": {f"load/{L1}#p": "maybe"}}); check("invalid resolution word -> 400", r.status_code == 400)
r = e2.post(f"/models/{M}/proposals/{pd}/merge", {"resolutions": {f"load/{L1}#p": "ours"}}); check("resolve with 'ours' keeps the head value", r.status_code == 200 and load_p(e1, L1) == 500.0, r.text)
h = e1.get(f"/models/{M}/history").json()[-1]; check("history notes the conflict resolution", "1 conflict resolution" in h["detail"], h["detail"])
pe = e1.post(f"/models/{M}/proposals", {"title": "E", "ops": ops_set(L2, p=1.0)}).json()["proposal"]["id"]
pf = e3.post(f"/models/{M}/proposals", {"title": "F", "ops": ops_set(L2, p=2.0)}).json()["proposal"]["id"]
e2.post(f"/models/{M}/proposals/{pe}/approve"); e2.post(f"/models/{M}/proposals/{pf}/approve"); e2.post(f"/models/{M}/proposals/{pe}/merge")
r = e2.post(f"/models/{M}/proposals/{pf}/merge", {"resolutions": {f"load/{L2}#p": "theirs"}}); check("resolve with 'theirs' takes the proposal value", r.status_code == 200 and load_p(e1, L2) == 2.0, r.text)

print("== reject, withdraw, jurisdiction")
pg = e1.post(f"/models/{M}/proposals", {"title": "G", "ops": ops_set(L0, p=5.0)}).json()["proposal"]["id"]
r = e2.post(f"/models/{M}/proposals/{pg}/reject", {"comment": "no"}); check("reject", r.status_code == 200 and r.json()["status"] == "rejected")
check("rejected proposal cannot be merged", e2.post(f"/models/{M}/proposals/{pg}/merge").status_code == 409)
ph = e1.post(f"/models/{M}/proposals", {"title": "H", "ops": ops_set(L0, p=6.0)}).json()["proposal"]["id"]
check("another engineer cannot withdraw it", e2.post(f"/models/{M}/proposals/{ph}/withdraw").status_code == 403)
check("author withdraws", e1.post(f"/models/{M}/proposals/{ph}/withdraw").json()["status"] == "withdrawn")
north = next(l for l in loads if node_reg[l["node"]] == "Northern")["mrid"]
south = next(l for l in loads if node_reg[l["node"]] == "Southern")["mrid"]
en = U("eng-north", "engineer", "Northern")
r = en.post(f"/models/{M}/proposals", {"title": "south change", "ops": ops_set(south, p=1.0)}); check("Northern engineer cannot propose a Southern change -> 403", r.status_code == 403 and "Southern" in r.text, r.text)
r = en.post(f"/models/{M}/proposals", {"title": "north change", "ops": ops_set(north, p=1.0)}); check("Northern engineer can propose a Northern change", r.status_code == 200, r.text)
pn = r.json()["proposal"]["id"]
check("Southern-only engineer cannot approve it -> 403", U("eng-south", "engineer", "Southern").post(f"/models/{M}/proposals/{pn}/approve").status_code == 403)
check("all-region engineer approves it", e2.post(f"/models/{M}/proposals/{pn}/approve").status_code == 200)
r = e1.post(f"/models/{M}/proposals", {"title": "rename", "ops": [{"op": "set", "kind": "model", "fields": {"name": "IEEE 118 renamed"}}]})
check("model-level change touches all regions: Northern engineer cannot propose it", en.post(f"/models/{M}/proposals", {"title": "rename", "ops": [{"op": "set", "kind": "model", "fields": {"name": "x"}}]}).status_code == 403)
d = e1.get(f"/models/{M}/diff?from={v1}&to={v1+1}").json(); check("diff endpoint between versions", d["count"] == 1, d)

print("== hierarchical split and merge")
r = en.post(f"/models/{M}/split"); check("split needs all-region jurisdiction -> 403", r.status_code == 403)
r = e1.post(f"/models/{M}/split?commit=1"); ch = r.json().get("children", []); check("split commits one child model per region", r.status_code == 200 and len(ch) == 5, r.text)
ids = [c["id"] for c in ch]
check("children are smaller than the whole and listed", all(c["substations"] < 118 for c in ch) and set(ids) <= {m["id"] for m in e1.get("/models").json()})
owner = next(i for i in ids if any(s["mrid"] == model["slackSubstation"] for s in e1.get(f"/models/{i}").json()["substations"]))
body = {"target": "ieee118-rebuilt", "children": [{"model": i} for i in ids], "scalarsFrom": owner}
r = en.post("/hierarchy/merge", body); check("hierarchical merge needs all-region jurisdiction -> 403", r.status_code == 403)
r = e1.post("/hierarchy/merge", body); check("dry run succeeds and commits nothing", r.status_code == 200 and r.json()["committed"] is False and "ieee118-rebuilt" not in {m["id"] for m in e1.get("/models").json()}, r.text)
r = e1.post("/hierarchy/merge", dict(body, commit=True)); check("commit creates the merged model", r.status_code == 200 and r.json()["committed"], r.text)
a, b = e1.get(f"/models/{M}").json(), e1.get("/models/ieee118-rebuilt").json()
def canon(m): return {k: sorted(json.dumps(x, sort_keys=True) for x in m[k]) for k in ("substations", "voltageLevels", "connectivityNodes", "switches", "acLineSegments", "transformers", "generators", "loads", "shunts")}
check("the rebuilt national model equals the current 118-bus model, element for element", canon(a) == canon(b))
check("its history names every child with version and hash", all(i in e1.get("/models/ieee118-rebuilt/history").json()[-1]["detail"] for i in ids))
# a child disagrees about a boundary substation
bsub = None
seen = {}
for i in ids:
    for s in e1.get(f"/models/{i}").json()["substations"]: seen.setdefault(s["mrid"], []).append(i)
bsub, holders = next((k, v) for k, v in seen.items() if len(v) > 1)
pp = e1.post(f"/models/{holders[0]}/proposals", {"title": "rename boundary", "ops": [{"op": "set", "kind": "substation", "mrid": bsub, "fields": {"name": "RENAMED-IN-CHILD"}}]})
check("child model governed by the same workflow (proposal)", pp.status_code == 200, pp.text)
e2.post(f"/models/{holders[0]}/proposals/{pp.json()['proposal']['id']}/approve"); e2.post(f"/models/{holders[0]}/proposals/{pp.json()['proposal']['id']}/merge")
r = e1.post("/hierarchy/merge", dict(body, target="ieee118-rebuilt2")); check("children disagreeing about a shared substation -> 409 naming it", r.status_code == 409 and bsub in r.text, r.text)
r = e1.post("/hierarchy/merge", dict(body, target="ieee118-rebuilt2", authority={f"substation/{bsub}": holders[0]}, commit=True)); check("an explicit authority settles it", r.status_code == 200, r.text)
check("authority's value is used", next(s for s in e1.get("/models/ieee118-rebuilt2").json()["substations"] if s["mrid"] == bsub)["name"] == "RENAMED-IN-CHILD")
r = e1.post("/hierarchy/merge", {"target": "t3", "children": [{"model": ids[0]}, {"model": "ieee14"}], "scalarsFrom": ids[0]}); check("mixing unrelated models is refused with a clear reason", r.status_code in (409, 422), r.text)

print("== persistence across a restart")
before = e1.get(f"/models/{M}/proposals").json()["proposals"]
subprocess.run(["pkill", "-x", "cim-pds"]); time.sleep(1)
subprocess.Popen([f"{ROOT}/services/cim-pds/target/release/cim-pds"], env=dict(env, CIM_STATE_DIR="/tmp/gs/state"), stdout=open("/tmp/gs/cim.log", "a"), stderr=subprocess.STDOUT, start_new_session=True)
for _ in range(50):
    try:
        if httpx.get(CIM + "/health", timeout=2).status_code == 200: break
    except Exception: time.sleep(0.3)
after = e1.get(f"/models/{M}/proposals").json()["proposals"]
check("proposals, approvals and statuses survive a restart", before == after and len(after) >= 8, (len(before), len(after)))
check("model history and chain survive", e1.get(f"/models/{M}/verify").json().get("ok") is True and load_p(e1, L2) == 2.0)
print(f"\n{ok} passed, {fail} failed")
sys.exit(1 if fail else 0)
