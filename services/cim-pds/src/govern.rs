//! Model change governance: proposals (change requests) with diff, review, two-person approval,
//! three-way merge against the moving head, and hierarchical merge/split of regional models.
use crate::auth::{now_ms, Claims};
use crate::diff::{self, Change};
use crate::model::Model;
use crate::store::{model_sha, Entry};
use crate::{authz, err, persist, ApiErr, AppState, S};
use axum::{
    extract::{Path, Query, State},
    http::{HeaderMap, StatusCode},
    Json,
};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::collections::{BTreeSet, HashMap};

#[derive(Serialize, Deserialize, Clone, Debug)]
pub struct Review {
    pub user: String,
    pub ts_ms: u64,
    pub comment: String,
}

#[derive(Serialize, Deserialize, Clone, Debug)]
pub struct Ev {
    pub ts_ms: u64,
    pub user: String,
    pub action: String,
    pub detail: String,
}

#[derive(Serialize, Deserialize, Clone, Debug)]
pub struct Proposal {
    pub id: u64,
    pub title: String,
    pub author: String,
    pub created_ms: u64,
    pub base_version: u64,
    pub base_sha: String,
    pub proposed: Model,
    pub status: String, // open | approved | rejected | merged | withdrawn
    pub approvals: Vec<Review>,
    pub rejections: Vec<Review>,
    pub regions: Vec<String>,
    pub changes: usize,
    pub merged_version: Option<u64>,
    pub events: Vec<Ev>,
}

pub type Props = HashMap<String, Vec<Proposal>>;

pub fn required_approvals() -> usize {
    std::env::var("CIM_APPROVALS").ok().and_then(|v| v.parse().ok()).filter(|n| *n >= 1).unwrap_or(1)
}

pub fn persist_props(s: &AppState, id: &str, p: &[Proposal]) {
    if let Some(dir) = &s.state_dir {
        let path = dir.join(format!("{id}.proposals.json"));
        let tmp = dir.join(format!("{id}.proposals.json.tmp"));
        if std::fs::write(&tmp, serde_json::to_vec(p).unwrap()).is_ok() {
            let _ = std::fs::rename(tmp, path);
        }
    }
}

fn covers_all(c: &Claims, regions: &[String]) -> Result<(), ApiErr> {
    for r in regions {
        if !c.covers(r) {
            return Err(err(StatusCode::FORBIDDEN, format!("user '{}' has no jurisdiction over region '{}' touched by this proposal", c.sub, r)));
        }
    }
    Ok(())
}

fn ev(p: &mut Proposal, user: &str, action: &str, detail: String) {
    p.events.push(Ev { ts_ms: now_ms(), user: user.into(), action: action.into(), detail });
}

fn summary(p: &Proposal) -> Value {
    json!({ "id": p.id, "title": p.title, "author": p.author, "created_ms": p.created_ms, "baseVersion": p.base_version,
            "status": p.status, "approvals": p.approvals.iter().map(|a| &a.user).collect::<Vec<_>>(),
            "regions": p.regions, "changes": p.changes, "mergedVersion": p.merged_version })
}

fn issues_json(m: &Model) -> (bool, Value) {
    let iss = crate::validate::validate(m);
    (crate::validate::has_errors(&iss), serde_json::to_value(&iss).unwrap())
}

#[derive(Deserialize)]
pub struct NewProposal {
    title: String,
    #[serde(rename = "baseVersion")]
    base_version: Option<u64>,
    model: Option<Model>,
    ops: Option<Vec<Value>>,
}

pub async fn create(State(s): State<S>, Path(id): Path<String>, h: HeaderMap, Json(req): Json<NewProposal>) -> Result<Json<Value>, ApiErr> {
    let c = authz(&s, &h, "engineer")?;
    if req.title.trim().is_empty() || req.title.len() > 200 {
        return Err(err(StatusCode::BAD_REQUEST, "title is required (max 200 characters)"));
    }
    let g = s.models.read().unwrap_or_else(|e| e.into_inner());
    let e = g.get(&id).ok_or_else(|| err(StatusCode::NOT_FOUND, format!("unknown model {id}")))?;
    let bv = req.base_version.unwrap_or_else(|| e.version());
    let base = e.at(bv).ok_or_else(|| err(StatusCode::NOT_FOUND, format!("base version {bv} not retained")))?;
    let proposed = match (req.model, req.ops) {
        (Some(m), None) => {
            if m.id != id { return Err(err(StatusCode::BAD_REQUEST, "model id does not match path")); }
            diff::canonical(&m)
        }
        (None, Some(ops)) => diff::apply_ops(base, &ops).map_err(|m| err(StatusCode::BAD_REQUEST, m))?,
        _ => return Err(err(StatusCode::BAD_REQUEST, "give exactly one of 'model' or 'ops'")),
    };
    let changes = diff::diff(base, &proposed);
    if changes.is_empty() {
        return Err(err(StatusCode::BAD_REQUEST, "the proposal changes nothing"));
    }
    let (bad, issues) = issues_json(&proposed);
    if bad {
        return Err((StatusCode::UNPROCESSABLE_ENTITY, Json(json!({ "error": "proposed model is invalid", "issues": issues }))));
    }
    let regions: Vec<String> = diff::touched_regions(base, &proposed, &changes).into_iter().collect();
    covers_all(&c, &regions)?;
    let base_sha = e.history.iter().find(|x| x.version == bv).map(|x| x.model_sha.clone()).unwrap_or_default();
    drop(g);
    let mut pg = s.props.write().unwrap_or_else(|e| e.into_inner());
    let list = pg.entry(id.clone()).or_default();
    let pid = list.iter().map(|p| p.id).max().unwrap_or(0) + 1;
    let mut p = Proposal { id: pid, title: req.title.trim().into(), author: c.sub.clone(), created_ms: now_ms(), base_version: bv, base_sha,
        proposed, status: "open".into(), approvals: vec![], rejections: vec![], regions, changes: changes.len(), merged_version: None, events: vec![] };
    ev(&mut p, &c.sub, "create", format!("{} changes against v{}", changes.len(), bv));
    let out = summary(&p);
    list.push(p);
    persist_props(&s, &id, list);
    Ok(Json(json!({ "proposal": out, "changes": changes, "warnings": issues })))
}

pub async fn list(State(s): State<S>, Path(id): Path<String>, h: HeaderMap) -> Result<Json<Value>, ApiErr> {
    authz(&s, &h, "viewer")?;
    if !s.models.read().unwrap_or_else(|e| e.into_inner()).contains_key(&id) { return Err(err(StatusCode::NOT_FOUND, format!("unknown model {id}"))); }
    let pg = s.props.read().unwrap_or_else(|e| e.into_inner());
    let v: Vec<Value> = pg.get(&id).map(|l| l.iter().rev().map(summary).collect()).unwrap_or_default();
    Ok(Json(json!({ "required": required_approvals(), "proposals": v })))
}

/// How the proposal relates to the current head: fast-forward, clean three-way merge, or conflicts.
fn merge_status(e: &Entry, p: &Proposal) -> (Value, Vec<diff::Conflict>) {
    let head = e.current();
    let Some(base) = e.at(p.base_version) else {
        return (json!({ "state": "base-lost", "note": "the base version is no longer retained; re-create the proposal" }), vec![]);
    };
    if e.version() == p.base_version {
        return (json!({ "state": "fast-forward" }), vec![]);
    }
    let (_, _, conflicts) = diff::merge3(base, head, &p.proposed, &HashMap::new());
    if conflicts.is_empty() {
        (json!({ "state": "clean-merge", "headVersion": e.version() }), vec![])
    } else {
        (json!({ "state": "conflicts", "headVersion": e.version(), "count": conflicts.len() }), conflicts)
    }
}

pub async fn get(State(s): State<S>, Path((id, pid)): Path<(String, u64)>, h: HeaderMap) -> Result<Json<Value>, ApiErr> {
    authz(&s, &h, "viewer")?;
    let g = s.models.read().unwrap_or_else(|e| e.into_inner());
    let e = g.get(&id).ok_or_else(|| err(StatusCode::NOT_FOUND, format!("unknown model {id}")))?;
    let pg = s.props.read().unwrap_or_else(|e| e.into_inner());
    let p = pg.get(&id).and_then(|l| l.iter().find(|p| p.id == pid)).ok_or_else(|| err(StatusCode::NOT_FOUND, "unknown proposal"))?;
    let changes = e.at(p.base_version).map(|b| diff::diff(b, &p.proposed)).unwrap_or_default();
    let (ms, conflicts) = merge_status(e, p);
    let ckeys: Vec<Value> = conflicts.iter().map(|c| json!({ "key": c.rkey(), "reason": c.reason, "base": c.base, "ours": c.ours, "theirs": c.theirs })).collect();
    Ok(Json(json!({ "proposal": summary(p), "required": required_approvals(), "changes": changes, "mergeStatus": ms, "conflicts": ckeys,
                    "approvals": p.approvals, "rejections": p.rejections, "events": p.events })))
}

#[derive(Deserialize, Default)]
pub struct Review1 {
    #[serde(default)]
    comment: String,
}

pub async fn review(s: S, id: String, pid: u64, h: HeaderMap, approve: bool, body: Review1) -> Result<Json<Value>, ApiErr> {
    let c = authz(&s, &h, "engineer")?;
    if body.comment.len() > 2000 { return Err(err(StatusCode::BAD_REQUEST, "comment too long")); }
    let mut pg = s.props.write().unwrap_or_else(|e| e.into_inner());
    let list = pg.get_mut(&id).ok_or_else(|| err(StatusCode::NOT_FOUND, "no proposals for this model"))?;
    let p = list.iter_mut().find(|p| p.id == pid).ok_or_else(|| err(StatusCode::NOT_FOUND, "unknown proposal"))?;
    if p.status != "open" && p.status != "approved" {
        return Err(err(StatusCode::CONFLICT, format!("proposal is {}", p.status)));
    }
    covers_all(&c, &p.regions)?;
    let r = Review { user: c.sub.clone(), ts_ms: now_ms(), comment: body.comment };
    if approve {
        if p.author == c.sub {
            return Err(err(StatusCode::FORBIDDEN, "two-person rule: the author cannot approve their own proposal"));
        }
        if p.approvals.iter().any(|a| a.user == c.sub) {
            return Err(err(StatusCode::CONFLICT, "already approved by this user"));
        }
        p.rejections.retain(|x| x.user != c.sub);
        p.approvals.push(r);
        if p.approvals.len() >= required_approvals() { p.status = "approved".into(); }
        ev(p, &c.sub, "approve", String::new());
    } else {
        p.approvals.retain(|a| a.user != c.sub);
        p.rejections.push(r);
        p.status = "rejected".into();
        ev(p, &c.sub, "reject", String::new());
    }
    let out = summary(p);
    persist_props(&s, &id, list);
    Ok(Json(out))
}

pub async fn approve(State(s): State<S>, Path((id, pid)): Path<(String, u64)>, h: HeaderMap, body: Option<Json<Review1>>) -> Result<Json<Value>, ApiErr> {
    review(s, id, pid, h, true, body.map(|b| b.0).unwrap_or_default()).await
}
pub async fn reject(State(s): State<S>, Path((id, pid)): Path<(String, u64)>, h: HeaderMap, body: Option<Json<Review1>>) -> Result<Json<Value>, ApiErr> {
    review(s, id, pid, h, false, body.map(|b| b.0).unwrap_or_default()).await
}

pub async fn withdraw(State(s): State<S>, Path((id, pid)): Path<(String, u64)>, h: HeaderMap) -> Result<Json<Value>, ApiErr> {
    let c = authz(&s, &h, "engineer")?;
    let mut pg = s.props.write().unwrap_or_else(|e| e.into_inner());
    let list = pg.get_mut(&id).ok_or_else(|| err(StatusCode::NOT_FOUND, "no proposals for this model"))?;
    let p = list.iter_mut().find(|p| p.id == pid).ok_or_else(|| err(StatusCode::NOT_FOUND, "unknown proposal"))?;
    if p.author != c.sub && crate::auth::role_rank(&c.role) < crate::auth::role_rank("admin") {
        return Err(err(StatusCode::FORBIDDEN, "only the author or an admin can withdraw a proposal"));
    }
    if p.status == "merged" || p.status == "withdrawn" { return Err(err(StatusCode::CONFLICT, format!("proposal is {}", p.status))); }
    p.status = "withdrawn".into();
    ev(p, &c.sub, "withdraw", String::new());
    let out = summary(p);
    persist_props(&s, &id, list);
    Ok(Json(out))
}

#[derive(Deserialize, Default)]
pub struct MergeReq {
    #[serde(default)]
    resolutions: HashMap<String, String>,
    #[serde(default)]
    note: String,
}

pub async fn merge(State(s): State<S>, Path((id, pid)): Path<(String, u64)>, h: HeaderMap, body: Option<Json<MergeReq>>) -> Result<Json<Value>, ApiErr> {
    let c = authz(&s, &h, "engineer")?;
    let req = body.map(|b| b.0).unwrap_or_default();
    for v in req.resolutions.values() {
        if v != "ours" && v != "theirs" { return Err(err(StatusCode::BAD_REQUEST, "a resolution must be \"ours\" or \"theirs\"")); }
    }
    let mut g = s.models.write().unwrap_or_else(|e| e.into_inner());
    let e = g.get_mut(&id).ok_or_else(|| err(StatusCode::NOT_FOUND, format!("unknown model {id}")))?;
    let mut pg = s.props.write().unwrap_or_else(|e| e.into_inner());
    let list = pg.get_mut(&id).ok_or_else(|| err(StatusCode::NOT_FOUND, "no proposals for this model"))?;
    let p = list.iter_mut().find(|p| p.id == pid).ok_or_else(|| err(StatusCode::NOT_FOUND, "unknown proposal"))?;
    if p.status != "approved" {
        return Err(err(StatusCode::CONFLICT, format!("proposal is '{}'; it needs {} approval(s) from people other than the author", p.status, required_approvals())));
    }
    covers_all(&c, &p.regions)?;
    let base = e.at(p.base_version).ok_or_else(|| err(StatusCode::CONFLICT, "the base version is no longer retained; re-create the proposal"))?.clone();
    if model_sha(&base) != p.base_sha {
        return Err(err(StatusCode::CONFLICT, "base version content does not match the recorded hash"));
    }
    let head = e.current().clone();
    let mut resolved = 0usize;
    let merged = if e.version() == p.base_version {
        p.proposed.clone()
    } else {
        let (sc, ents, conflicts) = diff::merge3(&base, &head, &p.proposed, &req.resolutions);
        if !conflicts.is_empty() {
            let cs: Vec<Value> = conflicts.iter().map(|c| json!({ "key": c.rkey(), "reason": c.reason, "base": c.base, "ours": c.ours, "theirs": c.theirs })).collect();
            return Err((StatusCode::CONFLICT, Json(json!({ "error": "merge conflicts: resolve each with \"ours\" or \"theirs\"", "conflicts": cs }))));
        }
        resolved = req.resolutions.len();
        diff::build(&id, &sc, &ents).map_err(|m| err(StatusCode::UNPROCESSABLE_ENTITY, m))?
    };
    let (bad, issues) = issues_json(&merged);
    if bad {
        return Err((StatusCode::UNPROCESSABLE_ENTITY, Json(json!({ "error": "merge result is invalid", "issues": issues }))));
    }
    let approvers: Vec<&str> = p.approvals.iter().map(|a| a.user.as_str()).collect();
    let detail = format!("proposal #{} \"{}\" by {}; approved by {}; base v{}{}{}", p.id, p.title, p.author, approvers.join(","), p.base_version,
        if resolved > 0 { format!("; {resolved} conflict resolution(s)") } else { String::new() },
        if req.note.is_empty() { String::new() } else { format!("; note: {}", req.note) });
    let rec = e.commit(merged, &c.sub, "merge", &detail).clone();
    p.status = "merged".into();
    p.merged_version = Some(rec.version);
    ev(p, &c.sub, "merge", format!("version {}", rec.version));
    persist(&s, &id, e);
    persist_props(&s, &id, list);
    Ok(Json(json!({ "version": rec.version, "hash": rec.hash, "detail": detail })))
}

pub async fn diff_route(State(s): State<S>, Path(id): Path<String>, Query(q): Query<HashMap<String, String>>, h: HeaderMap) -> Result<Json<Value>, ApiErr> {
    authz(&s, &h, "viewer")?;
    let g = s.models.read().unwrap_or_else(|e| e.into_inner());
    let e = g.get(&id).ok_or_else(|| err(StatusCode::NOT_FOUND, format!("unknown model {id}")))?;
    let get = |k: &str, d: u64| -> Result<&Model, ApiErr> {
        let v = q.get(k).map(|x| x.parse::<u64>()).transpose().map_err(|_| err(StatusCode::BAD_REQUEST, format!("{k} must be an integer")))?.unwrap_or(d);
        e.at(v).ok_or_else(|| err(StatusCode::NOT_FOUND, format!("version {v} not retained")))
    };
    let (a, b) = (get("from", e.version().saturating_sub(1).max(1))?, get("to", e.version())?);
    let ch: Vec<Change> = diff::diff(a, b);
    Ok(Json(json!({ "count": ch.len(), "changes": ch })))
}

// ---------------------------------------------------------------------------- hierarchical merge / split

#[derive(Deserialize)]
pub struct ChildRef {
    model: String,
    version: Option<u64>,
}
#[derive(Deserialize)]
pub struct HierReq {
    target: String,
    children: Vec<ChildRef>,
    #[serde(rename = "scalarsFrom")]
    scalars_from: Option<String>,
    #[serde(default)]
    authority: HashMap<String, String>,
    #[serde(default)]
    commit: bool,
    #[serde(default)]
    note: String,
}

pub async fn hier_merge(State(s): State<S>, h: HeaderMap, Json(req): Json<HierReq>) -> Result<Json<Value>, ApiErr> {
    let c = authz(&s, &h, "engineer")?;
    if !c.covers("*") { return Err(err(StatusCode::FORBIDDEN, "hierarchical merge requires all-region jurisdiction")); }
    if req.children.len() < 2 || req.children.len() > 32 { return Err(err(StatusCode::BAD_REQUEST, "give 2..32 children")); }
    if req.children.iter().any(|x| x.model == req.target) { return Err(err(StatusCode::BAD_REQUEST, "the target cannot be one of its own children")); }
    let mut g = s.models.write().unwrap_or_else(|e| e.into_inner());
    let mut snaps: Vec<(String, Model, u64, String)> = vec![];
    for ch in &req.children {
        let e = g.get(&ch.model).ok_or_else(|| err(StatusCode::NOT_FOUND, format!("unknown child model {}", ch.model)))?;
        let v = ch.version.unwrap_or_else(|| e.version());
        let m = e.at(v).ok_or_else(|| err(StatusCode::NOT_FOUND, format!("child {} version {v} not retained", ch.model)))?;
        let sha = e.history.iter().find(|x| x.version == v).map(|x| x.model_sha.clone()).unwrap_or_default();
        snaps.push((ch.model.clone(), m.clone(), v, sha));
    }
    let refs: Vec<(String, &Model)> = snaps.iter().map(|(i, m, _, _)| (i.clone(), m)).collect();
    let sf = req.scalars_from.clone().unwrap_or_else(|| snaps[0].0.clone());
    let (merged, conflicts, notes) = diff::hier_merge(&req.target, &refs, &sf, &req.authority).map_err(|m| err(StatusCode::UNPROCESSABLE_ENTITY, m))?;
    if !conflicts.is_empty() {
        return Err((StatusCode::CONFLICT, Json(json!({ "error": "children disagree on shared elements; name an authority per element", "conflicts": conflicts }))));
    }
    let (bad, issues) = issues_json(&merged);
    if bad {
        return Err((StatusCode::UNPROCESSABLE_ENTITY, Json(json!({ "error": "merged model is invalid", "issues": issues }))));
    }
    let src: Vec<String> = snaps.iter().map(|(i, _, v, sha)| format!("{i}@v{v}:{}", &sha[..12.min(sha.len())])).collect();
    let stats = json!({ "substations": merged.substations.len(), "switches": merged.switches.len(), "lines": merged.ac_line_segments.len(),
                        "transformers": merged.transformers.len(), "generators": merged.generators.len(), "loads": merged.loads.len() });
    if !req.commit {
        return Ok(Json(json!({ "committed": false, "sources": src, "notes": notes, "stats": stats, "warnings": issues })));
    }
    let detail = format!("hierarchical merge of {}{}", src.join(" + "), if req.note.is_empty() { String::new() } else { format!("; note: {}", req.note) });
    let rec = match g.get_mut(&req.target) {
        Some(e) => e.commit(merged, &c.sub, "hier-merge", &detail).clone(),
        None => {
            let e = Entry::new(merged, &c.sub, "hier-merge", &detail);
            let r = e.history[0].clone();
            g.insert(req.target.clone(), e);
            r
        }
    };
    persist(&s, &req.target, g.get(&req.target).unwrap());
    Ok(Json(json!({ "committed": true, "version": rec.version, "hash": rec.hash, "sources": src, "notes": notes, "stats": stats, "warnings": issues })))
}

pub async fn split(State(s): State<S>, Path(id): Path<String>, Query(q): Query<HashMap<String, String>>, h: HeaderMap) -> Result<Json<Value>, ApiErr> {
    let c = authz(&s, &h, "engineer")?;
    if !c.covers("*") { return Err(err(StatusCode::FORBIDDEN, "splitting requires all-region jurisdiction")); }
    let commit = q.get("commit").map(|v| v == "1" || v == "true").unwrap_or(false);
    let mut g = s.models.write().unwrap_or_else(|e| e.into_inner());
    let m = g.get(&id).ok_or_else(|| err(StatusCode::NOT_FOUND, format!("unknown model {id}")))?.current().clone();
    let regions: BTreeSet<String> = match q.get("region") {
        Some(r) => [r.clone()].into_iter().collect(),
        None => m.substations.iter().map(|x| x.region.clone()).collect(),
    };
    let mut out = vec![];
    for r in regions {
        let slug: String = r.chars().map(|ch| if ch.is_ascii_alphanumeric() { ch.to_ascii_lowercase() } else { '-' }).collect();
        let cid = format!("{id}.{slug}");
        let child = diff::split_region(&m, &r, &cid).map_err(|e| err(StatusCode::UNPROCESSABLE_ENTITY, e))?;
        let (bad, issues) = issues_json(&child);
        if bad { return Err((StatusCode::UNPROCESSABLE_ENTITY, Json(json!({ "error": format!("child for region {r} is invalid"), "issues": issues })))); }
        let info = json!({ "id": cid, "region": r, "substations": child.substations.len(), "lines": child.ac_line_segments.len(), "committed": commit });
        if commit {
            let detail = format!("split of {id} v{} for region {r}", g.get(&id).unwrap().version());
            match g.get_mut(&cid) {
                Some(e) => { e.commit(child, &c.sub, "split", &detail); }
                None => { g.insert(cid.clone(), Entry::new(child, &c.sub, "split", &detail)); }
            }
            persist(&s, &cid, g.get(&cid).unwrap());
        }
        out.push(info);
    }
    Ok(Json(json!({ "children": out })))
}
