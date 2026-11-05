mod auth;
mod cimxml;
mod interop;
mod legacy;
mod diff;
mod govern;
mod model;
mod pointlist;
mod store;
mod topology;
mod validate;
#[cfg(test)]
mod fuzz;

use auth::{role_rank, Claims, RateLimiter};
use axum::{
    extract::{Path, Query, State},
    http::{header, HeaderMap, HeaderValue, StatusCode},
    routing::{get, post},
    Json, Router,
};
use model::Model;
use serde::Deserialize;
use serde_json::{json, Value};
use std::{collections::HashMap, path::PathBuf, sync::{Arc, RwLock}};
use store::Entry;
use tower_http::cors::{AllowOrigin, CorsLayer};

type ApiErr = (StatusCode, Json<Value>);
fn err(c: StatusCode, m: impl Into<String>) -> ApiErr {
    (c, Json(json!({ "error": m.into() })))
}

struct AppState {
    models: RwLock<HashMap<String, Entry>>,
    secret: Vec<u8>,
    limiter: RateLimiter,
    state_dir: Option<PathBuf>,
    props: RwLock<govern::Props>,
}
type S = Arc<AppState>;

fn authz(s: &AppState, h: &HeaderMap, min: &str) -> Result<Claims, ApiErr> {
    let tok = h
        .get(header::AUTHORIZATION)
        .and_then(|v| v.to_str().ok())
        .and_then(|v| v.strip_prefix("Bearer "))
        .ok_or_else(|| err(StatusCode::UNAUTHORIZED, "missing bearer token"))?;
    let c = auth::verify(&s.secret, tok).map_err(|e| err(StatusCode::UNAUTHORIZED, e))?;
    if !s.limiter.allow(&c.sub) {
        return Err(err(StatusCode::TOO_MANY_REQUESTS, "rate limit exceeded"));
    }
    if role_rank(&c.role) < role_rank(min) {
        return Err(err(StatusCode::FORBIDDEN, format!("role '{}' cannot perform this action (needs {})", c.role, min)));
    }
    Ok(c)
}

fn persist(s: &AppState, id: &str, e: &Entry) {
    if let Some(dir) = &s.state_dir {
        let p = dir.join(format!("{id}.state.json"));
        let tmp = dir.join(format!("{id}.state.json.tmp"));
        if std::fs::write(&tmp, serde_json::to_vec(e).unwrap()).is_ok() {
            let _ = std::fs::rename(tmp, p);
        }
    }
}

fn region_of_node(m: &Model, node: &str) -> Option<String> {
    let vl = m.connectivity_nodes.iter().find(|n| n.mrid == node)?.voltage_level.clone();
    let ss = &m.voltage_levels.iter().find(|v| v.mrid == vl)?.substation;
    Some(m.substations.iter().find(|s| &s.mrid == ss)?.region.clone())
}

fn pick<'a>(e: &'a Entry, q: &HashMap<String, String>) -> Result<(u64, &'a Model), ApiErr> {
    match q.get("version") {
        None => Ok((e.version(), e.current())),
        Some(v) => {
            let n: u64 = v.parse().map_err(|_| err(StatusCode::BAD_REQUEST, "version must be an integer"))?;
            e.at(n).map(|m| (n, m)).ok_or_else(|| err(StatusCode::NOT_FOUND, format!("version {n} not retained")))
        }
    }
}

macro_rules! entry {
    ($s:expr, $id:expr) => {{
        let g = $s.models.read().unwrap_or_else(|e| e.into_inner());
        match g.get(&$id) {
            Some(_) => g,
            None => return Err(err(StatusCode::NOT_FOUND, format!("unknown model {}", $id))),
        }
    }};
}

async fn health(State(s): State<S>) -> Json<Value> {
    Json(json!({ "status": "ok", "service": "cim-pds", "models": s.models.read().unwrap_or_else(|e| e.into_inner()).len() }))
}

async fn list_models(State(s): State<S>, h: HeaderMap) -> Result<Json<Value>, ApiErr> {
    authz(&s, &h, "viewer")?;
    let g = s.models.read().unwrap_or_else(|e| e.into_inner());
    let mut v: Vec<Value> = g
        .iter()
        .map(|(id, e)| {
            let m = e.current();
            json!({ "id": id, "name": m.name, "version": e.version(), "hash": e.history.last().unwrap().hash,
                    "substations": m.substations.len(), "switches": m.switches.len(),
                    "lines": m.ac_line_segments.len(), "transformers": m.transformers.len(),
                    "generators": m.generators.len(), "loads": m.loads.len() })
        })
        .collect();
    v.sort_by(|a, b| a["id"].as_str().cmp(&b["id"].as_str()));
    Ok(Json(Value::Array(v)))
}

async fn get_model(State(s): State<S>, Path(id): Path<String>, Query(q): Query<HashMap<String, String>>, h: HeaderMap) -> Result<Json<Value>, ApiErr> {
    authz(&s, &h, "viewer")?;
    let g = entry!(s, id);
    let (_, m) = pick(g.get(&id).unwrap(), &q)?;
    Ok(Json(serde_json::to_value(m).unwrap()))
}

async fn busbranch(State(s): State<S>, Path(id): Path<String>, Query(q): Query<HashMap<String, String>>, h: HeaderMap) -> Result<Json<Value>, ApiErr> {
    authz(&s, &h, "viewer")?;
    let g = entry!(s, id);
    let (v, m) = pick(g.get(&id).unwrap(), &q)?;
    // Operational overlay: ?open=A,B&close=C forces switch states for THIS view only (real-time
    // SCADA status), without creating a model version or touching the audit chain.
    let list = |k: &str| -> Vec<String> { q.get(k).map(|v| v.split(',').filter(|x| !x.is_empty()).map(String::from).collect()).unwrap_or_default() };
    let (open, close) = (list("open"), list("close"));
    if open.is_empty() && close.is_empty() {
        return Ok(Json(serde_json::to_value(topology::process(m, v)).unwrap()));
    }
    if open.len() + close.len() > 5000 {
        return Err(err(StatusCode::BAD_REQUEST, "too many overlay switches"));
    }
    let mut mm = m.clone();
    for (ids, want_open) in [(&open, true), (&close, false)] {
        for sid in ids {
            match mm.switches.iter_mut().find(|x| &x.mrid == sid) {
                Some(sw) => sw.open = want_open,
                None => return Err(err(StatusCode::NOT_FOUND, format!("unknown switch {sid}"))),
            }
        }
    }
    Ok(Json(serde_json::to_value(topology::process(&mm, v)).unwrap()))
}

async fn validate_route(State(s): State<S>, Path(id): Path<String>, h: HeaderMap) -> Result<Json<Value>, ApiErr> {
    authz(&s, &h, "viewer")?;
    let g = entry!(s, id);
    let issues = validate::validate(g.get(&id).unwrap().current());
    Ok(Json(json!({ "valid": !validate::has_errors(&issues), "issues": issues })))
}

async fn history(State(s): State<S>, Path(id): Path<String>, h: HeaderMap) -> Result<Json<Value>, ApiErr> {
    authz(&s, &h, "viewer")?;
    let g = entry!(s, id);
    Ok(Json(serde_json::to_value(&g.get(&id).unwrap().history).unwrap()))
}

async fn verify_route(State(s): State<S>, Path(id): Path<String>, h: HeaderMap) -> Result<Json<Value>, ApiErr> {
    authz(&s, &h, "viewer")?;
    let g = entry!(s, id);
    Ok(Json(match g.get(&id).unwrap().verify() {
        Ok(n) => json!({ "ok": true, "records": n }),
        Err(e) => json!({ "ok": false, "error": e }),
    }))
}

async fn pointlist_route(State(s): State<S>, Path(id): Path<String>, h: HeaderMap) -> Result<Json<Value>, ApiErr> {
    authz(&s, &h, "viewer")?;
    let g = entry!(s, id);
    let e = g.get(&id).unwrap();
    let pts = pointlist::generate(e.current());
    Ok(Json(json!({ "modelId": id, "version": e.version(), "count": pts.len(), "points": pts })))
}

#[derive(Deserialize)]
struct SwitchAction {
    #[serde(rename = "switch")]
    switch_id: String,
    open: bool,
}
#[derive(Deserialize)]
struct SwitchReq {
    actions: Vec<SwitchAction>,
    #[serde(default)]
    reason: String,
}

async fn switch_route(State(s): State<S>, Path(id): Path<String>, h: HeaderMap, Json(req): Json<SwitchReq>) -> Result<Json<Value>, ApiErr> {
    let claims = authz(&s, &h, "operator")?;
    if req.actions.is_empty() || req.actions.len() > 200 {
        return Err(err(StatusCode::BAD_REQUEST, "actions must contain 1..200 items"));
    }
    let mut g = s.models.write().unwrap_or_else(|e| e.into_inner());
    let e = g.get_mut(&id).ok_or_else(|| err(StatusCode::NOT_FOUND, format!("unknown model {id}")))?;
    let mut m = e.current().clone();
    let mut changed = vec![];
    for a in &req.actions {
        let (n1, cur) = match m.switches.iter().find(|x| x.mrid == a.switch_id) {
            Some(x) => (x.node1.clone(), x.open),
            None => return Err(err(StatusCode::NOT_FOUND, format!("unknown switch {}", a.switch_id))),
        };
        let region = region_of_node(&m, &n1).unwrap_or_default();
        if !claims.covers(&region) {
            return Err(err(StatusCode::FORBIDDEN, format!("user '{}' has no jurisdiction over region '{}' (switch {})", claims.sub, region, a.switch_id)));
        }
        if cur != a.open {
            m.switches.iter_mut().find(|x| x.mrid == a.switch_id).unwrap().open = a.open;
            changed.push(format!("{}:{}", a.switch_id, if a.open { "open" } else { "close" }));
        }
    }
    if changed.is_empty() {
        return Ok(Json(json!({ "version": e.version(), "changed": [], "note": "no change" })));
    }
    let issues = validate::validate(&m);
    if validate::has_errors(&issues) {
        return Err((StatusCode::UNPROCESSABLE_ENTITY, Json(json!({ "error": "resulting model invalid", "issues": issues }))));
    }
    let detail = format!("{} | reason: {}", changed.join(","), req.reason);
    let rec = e.commit(m, &claims.sub, "switch", &detail).clone();
    persist(&s, &id, e);
    Ok(Json(json!({ "version": rec.version, "hash": rec.hash, "changed": changed })))
}

async fn put_model(State(s): State<S>, Path(id): Path<String>, h: HeaderMap, Json(m): Json<Model>) -> Result<Json<Value>, ApiErr> {
    let claims = authz(&s, &h, "engineer")?;
    if !claims.covers("*") {
        return Err(err(StatusCode::FORBIDDEN, "model import requires all-region jurisdiction"));
    }
    if m.id != id {
        return Err(err(StatusCode::BAD_REQUEST, "model id does not match path"));
    }
    let issues = validate::validate(&m);
    if validate::has_errors(&issues) {
        return Err((StatusCode::UNPROCESSABLE_ENTITY, Json(json!({ "error": "validation failed", "issues": issues }))));
    }
    let mut g = s.models.write().unwrap_or_else(|e| e.into_inner());
    let rec = match g.get_mut(&id) {
        Some(e) => e.commit(m, &claims.sub, "import", "model replaced via API").clone(),
        None => {
            let e = Entry::new(m, &claims.sub, "import", "model created via API");
            let r = e.history[0].clone();
            g.insert(id.clone(), e);
            r
        }
    };
    persist(&s, &id, g.get(&id).unwrap());
    Ok(Json(json!({ "version": rec.version, "hash": rec.hash, "warnings": issues.len() })))
}

fn load_models(data_dir: &str, state_dir: &Option<PathBuf>) -> Result<HashMap<String, Entry>, String> {
    let mut out = HashMap::new();
    let rd = std::fs::read_dir(data_dir).map_err(|e| format!("cannot read {data_dir}: {e}"))?;
    let mut files: Vec<_> = rd.filter_map(|f| f.ok()).map(|f| f.path()).filter(|p| p.to_string_lossy().ends_with(".cim.json")).collect();
    files.sort();
    for p in files {
        let m: Model = serde_json::from_slice(&std::fs::read(&p).map_err(|e| e.to_string())?).map_err(|e| format!("{}: {e}", p.display()))?;
        let id = m.id.clone();
        if let Some(dir) = state_dir {
            let sp = dir.join(format!("{id}.state.json"));
            if sp.exists() {
                let e: Entry = serde_json::from_slice(&std::fs::read(&sp).map_err(|e| e.to_string())?).map_err(|e| format!("{}: {e}", sp.display()))?;
                // refuse to start on tampered history rather than silently reseeding
                e.verify().map_err(|er| format!("state file {} failed integrity check: {er}", sp.display()))?;
                println!("restored {id} at version {} (hash chain verified)", e.version());
                out.insert(id, e);
                continue;
            }
        }
        let issues = validate::validate(&m);
        if validate::has_errors(&issues) {
            return Err(format!("seed model {id} has validation errors: {}", serde_json::to_string(&issues).unwrap()));
        }
        println!("seeded {id}: {} substations, {} warnings/infos", m.substations.len(), issues.len());
        out.insert(id, Entry::new(m, "system", "import", &format!("seed from {}", p.display())));
    }
    Ok(out)
}

#[tokio::main]
async fn main() {
    let args: Vec<String> = std::env::args().collect();
    if let Err(e) = auth::init_from_env() {
        eprintln!("token keys: {e}");
        std::process::exit(2);
    }
    let secret = if auth::asymmetric() { Vec::new() } else { match std::env::var("CIM_PDS_SECRET") {
        Ok(s) if s.len() >= 16 => s.into_bytes(),
        _ if std::env::var("CIM_PDS_DEV").ok().as_deref() == Some("1") => {
            eprintln!("WARNING: using insecure development secret (CIM_PDS_DEV=1)");
            b"dev-secret-do-not-use-in-prod".to_vec()
        }
        _ => {
            eprintln!("CIM_PDS_SECRET (>=16 chars) is required. Set CIM_PDS_DEV=1 only for local development.");
            std::process::exit(2);
        }
    } };
    if args.get(1).map(|s| s.as_str()) == Some("mint") {
        // cim-pds mint <user> <role> <jur,comma,separated|*> <ttl_seconds>
        if args.len() != 6 {
            eprintln!("usage: cim-pds mint <user> <role> <jurisdictions|*> <ttl_seconds>");
            std::process::exit(2);
        }
        let ttl: u64 = args[5].parse().expect("ttl");
        let c = Claims { sub: args[2].clone(), role: args[3].clone(), jur: args[4].split(',').map(String::from).collect(), exp: auth::now_ms() / 1000 + ttl };
        println!("{}", auth::mint(&secret, &c));
        return;
    }
    let data_dir = std::env::var("CIM_DATA_DIR").unwrap_or_else(|_| "../../data".into());
    let state_dir = std::env::var("CIM_STATE_DIR").ok().map(PathBuf::from);
    if let Some(d) = &state_dir {
        std::fs::create_dir_all(d).expect("state dir");
    }
    let models = load_models(&data_dir, &state_dir).unwrap_or_else(|e| {
        eprintln!("startup failed: {e}");
        std::process::exit(1);
    });
    let mut props: govern::Props = HashMap::new();
    if let Some(d) = &state_dir {
        for id in models.keys() {
            let pp = d.join(format!("{id}.proposals.json"));
            if let Ok(b) = std::fs::read(&pp) {
                let l: Vec<govern::Proposal> = serde_json::from_slice(&b).unwrap_or_else(|e| {
                    eprintln!("proposal file {} unreadable: {e}", pp.display());
                    std::process::exit(1);
                });
                props.insert(id.clone(), l);
            }
        }
    }
    let state: S = Arc::new(AppState { models: RwLock::new(models), secret, limiter: RateLimiter::new(10_000, 600), state_dir, props: RwLock::new(props) });
    let mut app = Router::new()
        .route("/health", get(health))
        .route("/models", get(list_models))
        .route("/models/:id", get(get_model).put(put_model))
        .route("/models/:id/busbranch", get(busbranch))
        .route("/models/:id/validate", get(validate_route))
        .route("/models/:id/history", get(history))
        .route("/models/:id/verify", get(verify_route))
        .route("/models/:id/pointlist", get(pointlist_route))
        .route("/models/:id/export.xml", get(interop::export_xml))
        .route("/models/:id/difference.xml", get(interop::difference_xml))
        .route("/models/:id/apply-difference", post(interop::apply_difference).layer(axum::extract::DefaultBodyLimit::max(64 << 20)))
        .route("/import/cimxml", post(interop::import_cimxml).layer(axum::extract::DefaultBodyLimit::max(64 << 20)))
        .route("/import/:kind", post(interop::import_legacy).layer(axum::extract::DefaultBodyLimit::max(64 << 20)))
        .route("/models/:id/switch", post(switch_route))
        .route("/models/:id/diff", get(govern::diff_route))
        .route("/models/:id/proposals", get(govern::list).post(govern::create))
        .route("/models/:id/proposals/:pid", get(govern::get))
        .route("/models/:id/proposals/:pid/approve", post(govern::approve))
        .route("/models/:id/proposals/:pid/reject", post(govern::reject))
        .route("/models/:id/proposals/:pid/withdraw", post(govern::withdraw))
        .route("/models/:id/proposals/:pid/merge", post(govern::merge))
        .route("/models/:id/split", post(govern::split))
        .route("/hierarchy/merge", post(govern::hier_merge))
        .with_state(state);
    if let Ok(origin) = std::env::var("CIM_PDS_CORS_ORIGIN") {
        app = app.layer(
            CorsLayer::new()
                .allow_origin(AllowOrigin::exact(HeaderValue::from_str(&origin).expect("cors origin")))
                .allow_methods([axum::http::Method::GET, axum::http::Method::POST, axum::http::Method::PUT])
                .allow_headers([header::AUTHORIZATION, header::CONTENT_TYPE]),
        );
    }
    let addr = std::env::var("CIM_PDS_ADDR").unwrap_or_else(|_| "127.0.0.1:8081".into());
    let listener = tokio::net::TcpListener::bind(&addr).await.unwrap_or_else(|e| {
        eprintln!("cannot bind {addr}: {e}");
        std::process::exit(1);
    });
    println!("cim-pds listening on {addr}");
    axum::serve(listener, app)
        .with_graceful_shutdown(async {
            let _ = tokio::signal::ctrl_c().await;
        })
        .await
        .unwrap();
}
