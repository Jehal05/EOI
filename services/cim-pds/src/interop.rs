//! HTTP routes for model exchange: IEC 61970-552 CIMXML full/difference export, import and difference apply,
//! and legacy MATPOWER / PSS-E RAW import. All imports need the engineer role with all-region jurisdiction,
//! are validated before commit, and enter the hash-chained history like any other change.
use crate::cimxml::{self, Header};
use crate::model::Model;
use crate::store::Entry;
use crate::{authz, err, persist, validate, ApiErr, S};
use axum::{
    extract::{Path, Query, State},
    http::{header, HeaderMap, StatusCode},
    Json,
};
use serde_json::{json, Value};
use std::collections::HashMap;

type XmlResp = ([(header::HeaderName, &'static str); 1], String);

fn header_for(e: &Entry, version: u64, model: &Model) -> Header {
    let rec = e.history.iter().find(|h| h.version == version);
    Header {
        uuid_seed: format!("{}/{}", model.id, version),
        created: cimxml::iso(rec.map(|r| r.ts_ms).unwrap_or(0)),
        version,
        description: format!("{} (exported from GridSovereign cim-pds)", model.name),
        authority_set: format!("urn:gridsovereign:mas:{}", model.id),
    }
}

pub async fn export_xml(State(s): State<S>, Path(id): Path<String>, Query(q): Query<HashMap<String, String>>, h: HeaderMap) -> Result<XmlResp, ApiErr> {
    authz(&s, &h, "viewer")?;
    let g = s.models.read().unwrap_or_else(|e| e.into_inner());
    let e = g.get(&id).ok_or_else(|| err(StatusCode::NOT_FOUND, format!("unknown model {id}")))?;
    let v = match q.get("version") { Some(v) => v.parse::<u64>().map_err(|_| err(StatusCode::BAD_REQUEST, "version must be an integer"))?, None => e.version() };
    let m = e.at(v).ok_or_else(|| err(StatusCode::NOT_FOUND, format!("version {v} not retained")))?;
    Ok(([(header::CONTENT_TYPE, "application/rdf+xml")], cimxml::write_full(&cimxml::to_graph(m), &header_for(e, v, m))))
}

pub async fn difference_xml(State(s): State<S>, Path(id): Path<String>, Query(q): Query<HashMap<String, String>>, h: HeaderMap) -> Result<XmlResp, ApiErr> {
    authz(&s, &h, "viewer")?;
    let g = s.models.read().unwrap_or_else(|e| e.into_inner());
    let e = g.get(&id).ok_or_else(|| err(StatusCode::NOT_FOUND, format!("unknown model {id}")))?;
    let pv = |k: &str| -> Result<u64, ApiErr> { q.get(k).ok_or_else(|| err(StatusCode::BAD_REQUEST, format!("query parameter {k} is required")))?.parse().map_err(|_| err(StatusCode::BAD_REQUEST, format!("{k} must be an integer"))) };
    let (a, b) = (pv("from")?, pv("to")?);
    let (ma, mb) = (
        e.at(a).ok_or_else(|| err(StatusCode::NOT_FOUND, format!("version {a} not retained")))?,
        e.at(b).ok_or_else(|| err(StatusCode::NOT_FOUND, format!("version {b} not retained")))?,
    );
    let mut hd = header_for(e, b, mb);
    hd.uuid_seed = format!("{}/{}-{}", mb.id, a, b);
    hd.description = format!("difference from version {a} to version {b} of {}", mb.name);
    Ok(([(header::CONTENT_TYPE, "application/rdf+xml")], cimxml::write_diff(&cimxml::to_graph(ma), &cimxml::to_graph(mb), &hd)))
}

fn commit(s: &S, claims: &crate::auth::Claims, m: Model, action: &str, detail: &str, warnings: Vec<String>) -> Result<Json<Value>, ApiErr> {
    let issues = validate::validate(&m);
    if validate::has_errors(&issues) {
        return Err((StatusCode::UNPROCESSABLE_ENTITY, Json(json!({ "error": "imported model fails validation; nothing was stored", "issues": issues, "warnings": warnings }))));
    }
    let id = m.id.clone();
    let counts = json!({ "substations": m.substations.len(), "switches": m.switches.len(), "lines": m.ac_line_segments.len(), "transformers": m.transformers.len(),
        "generators": m.generators.len(), "loads": m.loads.len(), "shunts": m.shunts.len() });
    let mut g = s.models.write().unwrap_or_else(|e| e.into_inner());
    let rec = match g.get_mut(&id) {
        Some(e) => e.commit(m, &claims.sub, action, detail).clone(),
        None => {
            let e = Entry::new(m, &claims.sub, action, detail);
            let r = e.history[0].clone();
            g.insert(id.clone(), e);
            r
        }
    };
    persist(s, &id, g.get(&id).unwrap());
    Ok(Json(json!({ "id": id, "version": rec.version, "hash": rec.hash, "counts": counts, "validationWarnings": issues.len(), "warnings": warnings })))
}

fn engineer(s: &S, h: &HeaderMap) -> Result<crate::auth::Claims, ApiErr> {
    let c = authz(s, h, "engineer")?;
    if !c.covers("*") { return Err(err(StatusCode::FORBIDDEN, "model import requires all-region jurisdiction")); }
    Ok(c)
}
fn safe_id(id: &str) -> Result<(), ApiErr> {
    if id.is_empty() || id.len() > 64 || !id.chars().all(|c| c.is_ascii_alphanumeric() || c == '-' || c == '_') {
        return Err(err(StatusCode::BAD_REQUEST, "model id must be 1-64 characters of letters, digits, '-' or '_'"));
    }
    Ok(())
}

pub async fn import_cimxml(State(s): State<S>, Query(q): Query<HashMap<String, String>>, h: HeaderMap, body: String) -> Result<Json<Value>, ApiErr> {
    let c = engineer(&s, &h)?;
    let (g, ver) = cimxml::parse_full(&body).map_err(|e| err(StatusCode::BAD_REQUEST, format!("CIMXML: {e}")))?;
    let mut m = cimxml::from_graph(&g).map_err(|e| err(StatusCode::UNPROCESSABLE_ENTITY, format!("CIMXML: {e}")))?;
    if let Some(id) = q.get("id") { m.id = id.clone(); }
    safe_id(&m.id)?;
    commit(&s, &c, m, "import-cimxml", &format!("IEC 61970-552 full model import (source version {})", ver.map(|v| v.to_string()).unwrap_or("?".into())), vec![])
}

pub async fn apply_difference(State(s): State<S>, Path(id): Path<String>, h: HeaderMap, body: String) -> Result<Json<Value>, ApiErr> {
    let c = engineer(&s, &h)?;
    let base = {
        let g = s.models.read().unwrap_or_else(|e| e.into_inner());
        cimxml::to_graph(g.get(&id).ok_or_else(|| err(StatusCode::NOT_FOUND, format!("unknown model {id}")))?.current())
    };
    let ng = cimxml::apply_diff(&base, &body).map_err(|e| err(StatusCode::CONFLICT, format!("difference model: {e}")))?;
    let mut m = cimxml::from_graph(&ng).map_err(|e| err(StatusCode::UNPROCESSABLE_ENTITY, format!("difference model result: {e}")))?;
    m.id = id.clone();
    commit(&s, &c, m, "apply-difference", "IEC 61970-552 difference model applied", vec![])
}

pub async fn import_legacy(State(s): State<S>, Path(kind): Path<String>, Query(q): Query<HashMap<String, String>>, h: HeaderMap, body: String) -> Result<Json<Value>, ApiErr> {
    let c = engineer(&s, &h)?;
    let id = q.get("id").ok_or_else(|| err(StatusCode::BAD_REQUEST, "query parameter id is required"))?;
    safe_id(id)?;
    let name = q.get("name").cloned().unwrap_or_else(|| id.clone());
    let r = match kind.as_str() {
        "matpower" => crate::legacy::matpower(id, &name, &body),
        "psse" => crate::legacy::psse_raw(id, &name, &body),
        _ => return Err(err(StatusCode::NOT_FOUND, "unknown import format (matpower, psse)")),
    }
    .map_err(|e| err(StatusCode::UNPROCESSABLE_ENTITY, format!("{kind}: {e}")))?;
    commit(&s, &c, r.model, &format!("import-{kind}"), &format!("legacy {kind} import converted bus-branch to node-breaker"), r.warnings)
}
