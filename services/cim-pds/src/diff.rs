//! Entity-level diff, three-way merge, patch application, hierarchical (parent/child) merge and region split
//! for the CIM-subset model. Identity is (class, mRID); order inside the model never matters and merged
//! models are written in canonical (sorted) order so their hash is deterministic.
use crate::model::Model;
use serde::Serialize;
use serde_json::{json, Map, Value};
use std::collections::{BTreeMap, BTreeSet, HashMap};

pub const KINDS: [(&str, &str); 9] = [
    ("substation", "substations"),
    ("voltageLevel", "voltageLevels"),
    ("connectivityNode", "connectivityNodes"),
    ("switch", "switches"),
    ("acLineSegment", "acLineSegments"),
    ("transformer", "transformers"),
    ("generator", "generators"),
    ("load", "loads"),
    ("shunt", "shunts"),
];
const SCALARS: [&str; 4] = ["name", "baseMVA", "source", "slackSubstation"];

/// key "kind/mrid" -> entity as a JSON object
pub type Ents = BTreeMap<String, Value>;

pub fn entities(m: &Model) -> Ents {
    let v = serde_json::to_value(m).unwrap();
    let mut out = Ents::new();
    for (kind, arr) in KINDS {
        for e in v[arr].as_array().cloned().unwrap_or_default() {
            let id = e["mrid"].as_str().unwrap_or("").to_string();
            out.insert(format!("{kind}/{id}"), e);
        }
    }
    out
}

pub fn scalars(m: &Model) -> BTreeMap<String, Value> {
    let v = serde_json::to_value(m).unwrap();
    SCALARS.iter().map(|k| (k.to_string(), v.get(*k).cloned().unwrap_or(Value::Null))).collect()
}

/// Rebuild a typed model (canonical order) from entities and scalars.
pub fn build(id: &str, sc: &BTreeMap<String, Value>, ents: &Ents) -> Result<Model, String> {
    let mut o = Map::new();
    o.insert("id".into(), json!(id));
    for (k, v) in sc {
        o.insert(k.clone(), v.clone());
    }
    for (kind, arr) in KINDS {
        let pre = format!("{kind}/");
        let list: Vec<Value> = ents.iter().filter(|(k, _)| k.starts_with(&pre)).map(|(_, v)| v.clone()).collect();
        o.insert(arr.into(), Value::Array(list));
    }
    serde_json::from_value(Value::Object(o)).map_err(|e| format!("merged model does not parse: {e}"))
}

#[derive(Serialize, Clone, Debug, PartialEq)]
pub struct FieldChange {
    pub field: String,
    pub old: Value,
    pub new: Value,
}

#[derive(Serialize, Clone, Debug)]
pub struct Change {
    pub kind: String,
    pub mrid: String,
    pub op: &'static str, // add | remove | modify
    pub fields: Vec<FieldChange>,
}

fn field_diff(a: &Value, b: &Value) -> Vec<FieldChange> {
    let (ao, bo) = (a.as_object().cloned().unwrap_or_default(), b.as_object().cloned().unwrap_or_default());
    let keys: BTreeSet<&String> = ao.keys().chain(bo.keys()).collect();
    keys.into_iter()
        .filter(|k| ao.get(*k) != bo.get(*k))
        .map(|k| FieldChange { field: k.clone(), old: ao.get(k).cloned().unwrap_or(Value::Null), new: bo.get(k).cloned().unwrap_or(Value::Null) })
        .collect()
}

fn split_key(k: &str) -> (String, String) {
    let (a, b) = k.split_once('/').unwrap_or((k, ""));
    (a.into(), b.into())
}

/// Differences taking `a` to `b`.
pub fn diff(a: &Model, b: &Model) -> Vec<Change> {
    let (ea, eb) = (entities(a), entities(b));
    let mut out = vec![];
    let (sa, sb) = (scalars(a), scalars(b));
    let f: Vec<FieldChange> = sa.iter().filter(|(k, v)| sb.get(*k) != Some(*v)).map(|(k, v)| FieldChange { field: k.clone(), old: v.clone(), new: sb[k].clone() }).collect();
    if !f.is_empty() {
        out.push(Change { kind: "model".into(), mrid: a.id.clone(), op: "modify", fields: f });
    }
    let keys: BTreeSet<&String> = ea.keys().chain(eb.keys()).collect();
    for k in keys {
        let (kind, mrid) = split_key(k);
        match (ea.get(k), eb.get(k)) {
            (None, Some(n)) => out.push(Change { kind, mrid, op: "add", fields: field_diff(&Value::Null, n) }),
            (Some(o), None) => out.push(Change { kind, mrid, op: "remove", fields: field_diff(o, &Value::Null) }),
            (Some(o), Some(n)) if o != n => out.push(Change { kind, mrid, op: "modify", fields: field_diff(o, n) }),
            _ => {}
        }
    }
    out
}

#[derive(Serialize, Clone, Debug)]
pub struct Conflict {
    pub key: String, // "kind/mrid" or "model/<scalar>"
    pub field: Option<String>,
    pub reason: &'static str,
    pub base: Value,
    pub ours: Value,
    pub theirs: Value,
}

impl Conflict {
    /// Resolution key as the API expects it.
    pub fn rkey(&self) -> String {
        match &self.field {
            Some(f) => format!("{}#{}", self.key, f),
            None => self.key.clone(),
        }
    }
}

fn pick_side(res: &HashMap<String, String>, rkey: &str) -> Option<bool> {
    match res.get(rkey).map(|s| s.as_str()) {
        Some("ours") => Some(true),
        Some("theirs") => Some(false),
        _ => None,
    }
}

/// Three-way merge of `ours` (current head) and `theirs` (proposal) against their common `base`.
/// Per entity and per field: a change on one side wins; identical changes agree; different changes on both
/// sides are conflicts, resolved only by an explicit "ours"/"theirs" in `res` (never silently).
pub fn merge3(base: &Model, ours: &Model, theirs: &Model, res: &HashMap<String, String>) -> (BTreeMap<String, Value>, Ents, Vec<Conflict>) {
    let (eb, eo, et) = (entities(base), entities(ours), entities(theirs));
    let (sb, so, st) = (scalars(base), scalars(ours), scalars(theirs));
    let mut conflicts = vec![];
    let mut sc = BTreeMap::new();
    for k in SCALARS {
        let (b, o, t) = (&sb[k], &so[k], &st[k]);
        let v = if o == t || t == b { o.clone() } else if o == b { t.clone() } else {
            let c = Conflict { key: format!("model/{k}"), field: None, reason: "both sides changed", base: b.clone(), ours: o.clone(), theirs: t.clone() };
            match pick_side(res, &c.rkey()) {
                Some(true) => o.clone(),
                Some(false) => t.clone(),
                None => { conflicts.push(c); o.clone() }
            }
        };
        sc.insert(k.to_string(), v);
    }
    let mut out = Ents::new();
    let keys: BTreeSet<&String> = eb.keys().chain(eo.keys()).chain(et.keys()).collect();
    for k in keys {
        let (b, o, t) = (eb.get(k), eo.get(k), et.get(k));
        if o == t {
            if let Some(v) = o { out.insert(k.clone(), v.clone()); }
            continue;
        }
        if o == b {
            if let Some(v) = t { out.insert(k.clone(), v.clone()); }
            continue;
        }
        if t == b {
            if let Some(v) = o { out.insert(k.clone(), v.clone()); }
            continue;
        }
        // both sides changed it differently
        match (o, t) {
            (Some(ov), Some(tv)) => {
                let bo = b.and_then(|x| x.as_object().cloned()).unwrap_or_default();
                let (ob, ov_m) = (tv.as_object().cloned().unwrap_or_default(), ov.as_object().cloned().unwrap_or_default());
                let fields: BTreeSet<&String> = bo.keys().chain(ov_m.keys()).chain(ob.keys()).collect();
                let mut merged = Map::new();
                for f in fields {
                    let (fb, fo, ft) = (bo.get(f), ov_m.get(f), ob.get(f));
                    let val = if fo == ft || ft == fb { fo.cloned() } else if fo == fb { ft.cloned() } else {
                        let c = Conflict { key: k.clone(), field: Some(f.clone()), reason: "both sides changed the field", base: fb.cloned().unwrap_or(Value::Null), ours: fo.cloned().unwrap_or(Value::Null), theirs: ft.cloned().unwrap_or(Value::Null) };
                        match pick_side(res, &c.rkey()) {
                            Some(true) => fo.cloned(),
                            Some(false) => ft.cloned(),
                            None => { conflicts.push(c); fo.cloned() }
                        }
                    };
                    if let Some(v) = val { merged.insert(f.clone(), v); }
                }
                out.insert(k.clone(), Value::Object(merged));
            }
            _ => {
                // removed on one side, modified on the other
                let c = Conflict { key: k.clone(), field: None, reason: "removed on one side, modified on the other", base: b.cloned().unwrap_or(Value::Null), ours: o.cloned().unwrap_or(Value::Null), theirs: t.cloned().unwrap_or(Value::Null) };
                match pick_side(res, &c.rkey()) {
                    Some(true) => { if let Some(v) = o { out.insert(k.clone(), v.clone()); } }
                    Some(false) => { if let Some(v) = t { out.insert(k.clone(), v.clone()); } }
                    None => { conflicts.push(c); if let Some(v) = o { out.insert(k.clone(), v.clone()); } }
                }
            }
        }
    }
    (sc, out, conflicts)
}

/// Patch operations: {"op":"set","kind","mrid","fields":{..}} | {"op":"add","kind","entity":{..}} | {"op":"remove","kind","mrid"}.
pub fn apply_ops(base: &Model, ops: &[Value]) -> Result<Model, String> {
    if ops.is_empty() || ops.len() > 5000 {
        return Err("ops must contain 1..5000 items".into());
    }
    let mut ents = entities(base);
    let mut sc = scalars(base);
    for (i, op) in ops.iter().enumerate() {
        let name = op["op"].as_str().unwrap_or("");
        let kind = op["kind"].as_str().unwrap_or("");
        let mrid = op["mrid"].as_str().unwrap_or("");
        if kind == "model" {
            if name != "set" { return Err(format!("op {i}: only 'set' applies to the model")); }
            for (f, v) in op["fields"].as_object().ok_or(format!("op {i}: fields required"))? {
                if !SCALARS.contains(&f.as_str()) { return Err(format!("op {i}: unknown model field {f}")); }
                sc.insert(f.clone(), v.clone());
            }
            continue;
        }
        if !KINDS.iter().any(|(k, _)| *k == kind) {
            return Err(format!("op {i}: unknown kind '{kind}'"));
        }
        match name {
            "set" => {
                let key = format!("{kind}/{mrid}");
                let e = ents.get_mut(&key).ok_or(format!("op {i}: {key} does not exist"))?;
                let o = e.as_object_mut().unwrap();
                for (f, v) in op["fields"].as_object().ok_or(format!("op {i}: fields required"))? {
                    if f == "mrid" { return Err(format!("op {i}: mrid cannot change")); }
                    if v.is_null() { o.remove(f); } else { o.insert(f.clone(), v.clone()); }
                }
            }
            "add" => {
                let e = op["entity"].clone();
                let id = e["mrid"].as_str().ok_or(format!("op {i}: entity.mrid required"))?.to_string();
                let key = format!("{kind}/{id}");
                if ents.contains_key(&key) { return Err(format!("op {i}: {key} already exists")); }
                ents.insert(key, e);
            }
            "remove" => {
                if ents.remove(&format!("{kind}/{mrid}")).is_none() { return Err(format!("op {i}: {kind}/{mrid} does not exist")); }
            }
            _ => return Err(format!("op {i}: unknown op '{name}'")),
        }
    }
    build(&base.id, &sc, &ents)
}

/// Region (set) an entity belongs to, for jurisdiction checks. Branches and switches report both ends.
pub fn regions_of(m: &Model, kind: &str, e: &Value) -> BTreeSet<String> {
    let sub_region = |s: &str| m.substations.iter().find(|x| x.mrid == s).map(|x| x.region.clone());
    let vl_region = |v: &str| m.voltage_levels.iter().find(|x| x.mrid == v).and_then(|x| sub_region(&x.substation));
    let node_region = |n: &str| m.connectivity_nodes.iter().find(|x| x.mrid == n).and_then(|x| vl_region(&x.voltage_level));
    let mut out = BTreeSet::new();
    let s = |f: &str| e[f].as_str().unwrap_or("").to_string();
    let mut add = |r: Option<String>| out.insert(r.unwrap_or_else(|| "*".into()));
    match kind {
        "substation" => { add(e["region"].as_str().map(String::from)); }
        "voltageLevel" => { add(sub_region(&s("substation"))); }
        "connectivityNode" => { add(vl_region(&s("voltageLevel"))); }
        "switch" | "acLineSegment" | "transformer" => { add(node_region(&s("node1"))); add(node_region(&s("node2"))); }
        "generator" | "load" | "shunt" => { add(node_region(&s("node"))); }
        _ => { add(None); }
    }
    out
}

/// Regions touched by a set of changes between `base` and `new` (looking in whichever model has the entity).
pub fn touched_regions(base: &Model, new: &Model, changes: &[Change]) -> BTreeSet<String> {
    let (eb, en) = (entities(base), entities(new));
    let mut out = BTreeSet::new();
    for c in changes {
        if c.kind == "model" { out.insert("*".to_string()); continue; }
        let k = format!("{}/{}", c.kind, c.mrid);
        if let Some(e) = en.get(&k) { out.extend(regions_of(new, &c.kind, e)); }
        if let Some(e) = eb.get(&k) { out.extend(regions_of(base, &c.kind, e)); }
    }
    out
}

// ------------------------------------------------------------------------------------------------
// hierarchical merge and split

#[derive(Serialize, Clone, Debug)]
pub struct HierConflict {
    pub key: String,
    pub sources: Vec<String>,
    pub fields: Vec<String>,
}

/// Union of child models. An entity present in several children must be identical in all of them (boundary
/// copies); otherwise it is a conflict, settled only by `authority` (key -> child id). Scalars come from
/// `scalars_from`; baseMVA must agree.
pub fn hier_merge(target_id: &str, children: &[(String, &Model)], scalars_from: &str, authority: &HashMap<String, String>) -> Result<(Model, Vec<HierConflict>, Vec<String>), String> {
    if children.is_empty() { return Err("no children".into()); }
    let base_mva = children[0].1.base_mva;
    for (id, m) in children {
        if (m.base_mva - base_mva).abs() > 1e-9 {
            return Err(format!("baseMVA differs: {} has {}, {} has {}", children[0].0, base_mva, id, m.base_mva));
        }
    }
    let sf = children.iter().find(|(id, _)| id == scalars_from).ok_or_else(|| format!("scalarsFrom '{scalars_from}' is not one of the children"))?;
    let mut by_key: BTreeMap<String, Vec<(String, Value)>> = BTreeMap::new();
    for (id, m) in children {
        for (k, v) in entities(m) { by_key.entry(k).or_default().push((id.clone(), v)); }
    }
    let mut ents = Ents::new();
    let mut conflicts = vec![];
    let mut notes = vec![];
    for (k, mut list) in by_key {
        let first = list[0].1.clone();
        if list.iter().all(|(_, v)| *v == first) {
            ents.insert(k, first);
            continue;
        }
        if let Some(a) = authority.get(&k) {
            match list.iter().position(|(c, _)| c == a) {
                Some(i) => { notes.push(format!("{k}: taken from {a} by authority")); ents.insert(k, list.swap_remove(i).1); continue; }
                None => return Err(format!("authority for {k} names '{a}', which does not contain it")),
            }
        }
        let mut fields = BTreeSet::new();
        for (_, v) in &list { for fc in field_diff(&first, v) { fields.insert(fc.field); } }
        conflicts.push(HierConflict { key: k, sources: list.iter().map(|(c, _)| c.clone()).collect(), fields: fields.into_iter().collect() });
    }
    let mut sc = scalars(sf.1);
    sc.insert("baseMVA".into(), json!(base_mva));
    let m = build(target_id, &sc, &ents)?;
    Ok((m, conflicts, notes))
}

/// The part of `m` owned by `region`: every entity whose (first) node lies in the region, plus boundary
/// copies of the foreign end of tie lines. The slack substation is kept if present, else the first owned one.
pub fn split_region(m: &Model, region: &str, child_id: &str) -> Result<Model, String> {
    let ents = entities(m);
    let sub_region: HashMap<&str, &str> = m.substations.iter().map(|s| (s.mrid.as_str(), s.region.as_str())).collect();
    let vl_sub: HashMap<&str, &str> = m.voltage_levels.iter().map(|v| (v.mrid.as_str(), v.substation.as_str())).collect();
    let node_vl: HashMap<&str, &str> = m.connectivity_nodes.iter().map(|n| (n.mrid.as_str(), n.voltage_level.as_str())).collect();
    let node_reg = |n: &str| node_vl.get(n).and_then(|v| vl_sub.get(v)).and_then(|s| sub_region.get(s)).copied().unwrap_or("");
    let mut keep = BTreeSet::new();
    let mut boundary_nodes = BTreeSet::new();
    for (k, e) in &ents {
        let (kind, _) = split_key(k);
        let owner = match kind.as_str() {
            "substation" => e["region"].as_str().unwrap_or("").to_string(),
            "voltageLevel" => sub_region.get(e["substation"].as_str().unwrap_or("")).copied().unwrap_or("").to_string(),
            "connectivityNode" => vl_sub.get(e["voltageLevel"].as_str().unwrap_or("")).and_then(|s| sub_region.get(s)).copied().unwrap_or("").to_string(),
            "switch" | "acLineSegment" | "transformer" => node_reg(e["node1"].as_str().unwrap_or("")).to_string(),
            _ => node_reg(e["node"].as_str().unwrap_or("")).to_string(),
        };
        if owner == region {
            keep.insert(k.clone());
            if matches!(kind.as_str(), "acLineSegment" | "transformer" | "switch") {
                let n2 = e["node2"].as_str().unwrap_or("");
                if node_reg(n2) != region { boundary_nodes.insert(n2.to_string()); }
            }
        }
    }
    for n in boundary_nodes {
        keep.insert(format!("connectivityNode/{n}"));
        if let Some(vl) = node_vl.get(n.as_str()) {
            keep.insert(format!("voltageLevel/{vl}"));
            if let Some(s) = vl_sub.get(vl) { keep.insert(format!("substation/{s}")); }
        }
    }
    let sub: Ents = ents.into_iter().filter(|(k, _)| keep.contains(k)).collect();
    if !sub.keys().any(|k| k.starts_with("substation/")) { return Err(format!("region '{region}' has no substations")); }
    let mut sc = scalars(m);
    let slack = sc["slackSubstation"].as_str().unwrap_or("").to_string();
    let owned_first = sub.iter().find(|(k, v)| k.starts_with("substation/") && v["region"] == region).map(|(_, v)| v["mrid"].clone());
    if !sub.contains_key(&format!("substation/{slack}")) {
        sc.insert("slackSubstation".into(), owned_first.unwrap());
    }
    build(child_id, &sc, &sub)
}

pub fn canonical(m: &Model) -> Model {
    build(&m.id, &scalars(m), &entities(m)).expect("canonical rebuild")
}

#[cfg(test)]
mod tests {
    use super::*;

    fn load(name: &str) -> Model {
        let p = format!("{}/../../data/{name}.cim.json", env!("CARGO_MANIFEST_DIR"));
        serde_json::from_slice(&std::fs::read(p).unwrap()).unwrap()
    }
    fn set(m: &Model, kind: &str, mrid: &str, f: Value) -> Model {
        apply_ops(m, &[json!({"op":"set","kind":kind,"mrid":mrid,"fields":f})]).unwrap()
    }
    fn merged(base: &Model, a: &Model, b: &Model, res: &HashMap<String, String>) -> (Model, Vec<Conflict>) {
        let (sc, e, c) = merge3(base, a, b, res);
        (build(&base.id, &sc, &e).unwrap(), c)
    }
    fn same(a: &Model, b: &Model) -> bool { entities(a) == entities(b) && scalars(a) == scalars(b) }

    #[test]
    fn diff_is_empty_for_equal_and_order_independent() {
        let m = load("ieee14");
        assert!(diff(&m, &m).is_empty());
        let mut r = m.clone();
        r.loads.reverse();
        r.switches.reverse();
        assert!(diff(&m, &r).is_empty(), "element order must not matter");
        assert_eq!(model_sha_of(&canonical(&m)), model_sha_of(&canonical(&r)), "canonical form is deterministic");
    }
    fn model_sha_of(m: &Model) -> String { crate::store::model_sha(m) }

    #[test]
    fn diff_reports_exact_field_changes() {
        let m = load("ieee14");
        let id = m.loads[0].mrid.clone();
        let n = set(&m, "load", &id, json!({"p": 123.5}));
        let d = diff(&m, &n);
        assert_eq!(d.len(), 1);
        assert_eq!((d[0].kind.as_str(), d[0].mrid.as_str(), d[0].op), ("load", id.as_str(), "modify"));
        assert_eq!(d[0].fields.len(), 1);
        assert_eq!(d[0].fields[0].field, "p");
        assert_eq!(d[0].fields[0].new, json!(123.5));
        let rm = apply_ops(&m, &[json!({"op":"remove","kind":"load","mrid":id})]).unwrap();
        assert_eq!(diff(&m, &rm)[0].op, "remove");
        assert_eq!(diff(&rm, &m)[0].op, "add");
    }

    #[test]
    fn disjoint_edits_merge_cleanly_and_commute() {
        let base = load("ieee118");
        let (l0, l1, g0) = (base.loads[0].mrid.clone(), base.loads[1].mrid.clone(), base.generators[0].mrid.clone());
        let a = set(&set(&base, "load", &l0, json!({"p": 77.0})), "generator", &g0, json!({"p": 12.0}));
        let b = set(&base, "load", &l1, json!({"p": 33.0, "q": 4.0}));
        let (m1, c1) = merged(&base, &a, &b, &HashMap::new());
        let (m2, c2) = merged(&base, &b, &a, &HashMap::new());
        assert!(c1.is_empty() && c2.is_empty());
        assert!(same(&m1, &m2), "merge must not depend on which side is 'ours'");
        let e = entities(&m1);
        assert_eq!(e[&format!("load/{l0}")]["p"], json!(77.0));
        assert_eq!(e[&format!("load/{l1}")]["p"], json!(33.0));
        assert_eq!(e[&format!("generator/{g0}")]["p"], json!(12.0));
    }

    #[test]
    fn different_fields_of_one_entity_merge_but_same_field_conflicts() {
        let base = load("ieee118");
        let l = base.loads[0].mrid.clone();
        let a = set(&base, "load", &l, json!({"p": 10.0}));
        let b = set(&base, "load", &l, json!({"q": 5.0}));
        let (m, c) = merged(&base, &a, &b, &HashMap::new());
        assert!(c.is_empty());
        let e = &entities(&m)[&format!("load/{l}")];
        assert_eq!((e["p"].clone(), e["q"].clone()), (json!(10.0), json!(5.0)));

        let b2 = set(&base, "load", &l, json!({"p": 20.0}));
        let (_, c) = merged(&base, &a, &b2, &HashMap::new());
        assert_eq!(c.len(), 1);
        assert_eq!(c[0].rkey(), format!("load/{l}#p"));
        let mut r = HashMap::new();
        r.insert(c[0].rkey(), "theirs".to_string());
        let (m, c) = merged(&base, &a, &b2, &r);
        assert!(c.is_empty());
        assert_eq!(entities(&m)[&format!("load/{l}")]["p"], json!(20.0));
        r.insert(format!("load/{l}#p"), "ours".to_string());
        let (m, _) = merged(&base, &a, &b2, &r);
        assert_eq!(entities(&m)[&format!("load/{l}")]["p"], json!(10.0));
    }

    #[test]
    fn identical_change_on_both_sides_is_not_a_conflict() {
        let base = load("ieee14");
        let l = base.loads[0].mrid.clone();
        let a = set(&base, "load", &l, json!({"p": 9.0}));
        let (_, c) = merged(&base, &a, &a.clone(), &HashMap::new());
        assert!(c.is_empty());
    }

    #[test]
    fn removed_versus_modified_conflicts() {
        let base = load("ieee118");
        let l = base.loads[0].mrid.clone();
        let a = apply_ops(&base, &[json!({"op":"remove","kind":"load","mrid":l})]).unwrap();
        let b = set(&base, "load", &l, json!({"p": 1.0}));
        let (m, c) = merged(&base, &a, &b, &HashMap::new());
        assert_eq!(c.len(), 1);
        assert_eq!(c[0].rkey(), format!("load/{l}"));
        assert!(!entities(&m).contains_key(&format!("load/{l}")), "unresolved conflict keeps ours");
        let mut r = HashMap::new();
        r.insert(format!("load/{l}"), "theirs".to_string());
        let (m, c) = merged(&base, &a, &b, &r);
        assert!(c.is_empty() && entities(&m)[&format!("load/{l}")]["p"] == json!(1.0));
    }

    #[test]
    fn scalar_conflicts_are_reported() {
        let base = load("ieee14");
        let mut a = base.clone();
        a.name = "A".into();
        let mut b = base.clone();
        b.name = "B".into();
        let (_, c) = merged(&base, &a, &b, &HashMap::new());
        assert_eq!(c.len(), 1);
        assert_eq!(c[0].rkey(), "model/name");
    }

    #[test]
    fn ops_reject_bad_input() {
        let m = load("ieee14");
        for bad in [
            json!({"op":"set","kind":"load","mrid":"NOPE","fields":{"p":1}}),
            json!({"op":"add","kind":"load","entity":{"mrid": m.loads[0].mrid}}),
            json!({"op":"remove","kind":"load","mrid":"NOPE"}),
            json!({"op":"set","kind":"banana","mrid":"x","fields":{}}),
            json!({"op":"set","kind":"load","mrid":m.loads[0].mrid,"fields":{"mrid":"other"}}),
            json!({"op":"explode","kind":"load","mrid":"x"}),
            json!({"op":"set","kind":"model","fields":{"nope":1}}),
        ] {
            assert!(apply_ops(&m, &[bad.clone()]).is_err(), "accepted {bad}");
        }
        assert!(apply_ops(&m, &[]).is_err());
        // type confusion surfaces as a parse error, not a panic
        assert!(apply_ops(&m, &[json!({"op":"set","kind":"load","mrid":m.loads[0].mrid,"fields":{"p":"fifty"}})]).is_err());
    }

    #[test]
    fn random_disjoint_edits_always_merge_and_commute() {
        let base = load("ieee118");
        let mut seed = 12345u64;
        let mut rnd = |n: usize| { seed = seed.wrapping_mul(6364136223846793005).wrapping_add(1442695040888963407); ((seed >> 33) as usize) % n };
        for _ in 0..150 {
            let k = base.loads.len();
            let (i, j) = (rnd(k), rnd(k));
            let a = set(&base, "load", &base.loads[i].mrid, json!({"p": rnd(500) as f64}));
            let b = if i == j { set(&base, "generator", &base.generators[rnd(base.generators.len())].mrid, json!({"vset": 1.0 + rnd(10) as f64 / 100.0})) }
                    else { set(&base, "load", &base.loads[j].mrid, json!({"p": rnd(500) as f64})) };
            let (m1, c1) = merged(&base, &a, &b, &HashMap::new());
            let (m2, c2) = merged(&base, &b, &a, &HashMap::new());
            assert!(c1.is_empty() && c2.is_empty() && same(&m1, &m2));
            // merging with an unchanged side returns the changed side (identity)
            let (m3, _) = merged(&base, &base, &a, &HashMap::new());
            assert!(same(&m3, &a));
        }
    }

    #[test]
    fn split_then_hierarchical_merge_reproduces_the_original() {
        for name in ["ieee14", "ieee118"] {
            let m = load(name);
            let regions: BTreeSet<String> = m.substations.iter().map(|s| s.region.clone()).collect();
            let children: Vec<Model> = regions.iter().map(|r| split_region(&m, r, &format!("{}.{r}", m.id)).unwrap()).collect();
            if regions.len() > 1 {
                assert!(children.iter().all(|c| c.substations.len() < m.substations.len()), "{name}: a child must be smaller than the whole");
            }
            let owner = children.iter().find(|c| c.substations.iter().any(|s| s.mrid == m.slack_substation)).unwrap().id.clone();
            let refs: Vec<(String, &Model)> = children.iter().map(|c| (c.id.clone(), c)).collect();
            let (back, conflicts, _) = hier_merge(&m.id, &refs, &owner, &HashMap::new()).unwrap();
            assert!(conflicts.is_empty(), "{name}: {conflicts:?}");
            assert!(same(&back, &m), "{name}: merged children differ from the original");
            assert_eq!(model_sha_of(&back), model_sha_of(&canonical(&m)), "{name}: hash of the rebuilt model");
            for c in &children {
                let iss = crate::validate::validate(c);
                assert!(!crate::validate::has_errors(&iss), "{name}: child {} invalid: {:?}", c.id, iss);
            }
            // children are disjoint on everything they own: sum of owned substations = whole
            let owned: usize = children.iter().map(|c| c.substations.iter().filter(|s| c.id.ends_with(&format!(".{}", s.region))).count()).sum();
            assert_eq!(owned, m.substations.len());
        }
    }

    #[test]
    fn disagreeing_boundary_copies_conflict_until_an_authority_is_named() {
        let m = load("ieee118");
        let regions: Vec<String> = m.substations.iter().map(|s| s.region.clone()).collect::<BTreeSet<_>>().into_iter().collect();
        let mut kids: Vec<Model> = regions.iter().map(|r| split_region(&m, r, &format!("k.{r}")).unwrap()).collect();
        // find a substation that appears in two children and alter it in one
        let shared = {
            let mut seen: HashMap<String, usize> = HashMap::new();
            for k in &kids { for s in &k.substations { *seen.entry(s.mrid.clone()).or_default() += 1; } }
            seen.into_iter().find(|(_, n)| *n > 1).expect("118-bus model has boundary substations").0
        };
        let idx = kids.iter().position(|k| k.substations.iter().any(|s| s.mrid == shared)).unwrap();
        kids[idx].substations.iter_mut().find(|s| s.mrid == shared).unwrap().name = "RENAMED".into();
        let owner = kids.iter().find(|c| c.substations.iter().any(|s| s.mrid == m.slack_substation)).unwrap().id.clone();
        let refs: Vec<(String, &Model)> = kids.iter().map(|c| (c.id.clone(), c)).collect();
        let (_, conflicts, _) = hier_merge("t", &refs, &owner, &HashMap::new()).unwrap();
        assert_eq!(conflicts.len(), 1);
        assert_eq!(conflicts[0].key, format!("substation/{shared}"));
        assert_eq!(conflicts[0].fields, vec!["name".to_string()]);
        let mut auth = HashMap::new();
        auth.insert(conflicts[0].key.clone(), kids[idx].id.clone());
        let (back, c2, _) = hier_merge("t", &refs, &owner, &auth).unwrap();
        assert!(c2.is_empty());
        assert_eq!(back.substations.iter().find(|s| s.mrid == shared).unwrap().name, "RENAMED");
        // naming an authority that does not hold the element is refused
        auth.insert(conflicts[0].key.clone(), "nobody".into());
        assert!(hier_merge("t", &refs, &owner, &auth).is_err());
    }

    #[test]
    fn hier_merge_refuses_mixed_base_mva() {
        let a = load("ieee14");
        let mut b = a.clone();
        b.id = "other".into();
        b.base_mva = 50.0;
        let r = hier_merge("t", &[("a".into(), &a), ("b".into(), &b)], "a", &HashMap::new());
        assert!(r.unwrap_err().contains("baseMVA"));
    }

    #[test]
    fn touched_regions_cover_both_ends_of_a_tie_line() {
        let m = load("ieee118");
        let regions: BTreeSet<String> = m.substations.iter().map(|s| s.region.clone()).collect();
        let tie = m.ac_line_segments.iter().find(|l| {
            let r = |n: &str| regions_of(&m, "acLineSegment", &json!({"node1": n, "node2": n}));
            r(&l.node1) != r(&l.node2)
        }).expect("a line between regions");
        let n = set(&m, "acLineSegment", &tie.mrid, json!({"rating": 999.0}));
        let ch = diff(&m, &n);
        assert!(touched_regions(&m, &n, &ch).len() >= 2);
    }

    fn tiled(m: &Model, n: usize) -> Model {
        let mut v = serde_json::to_value(m).unwrap();
        let keys = ["mrid", "substation", "voltageLevel", "node", "node1", "node2"];
        fn walk(x: &mut Value, pre: &str, keys: &[&str]) {
            match x {
                Value::Object(o) => for (k, val) in o.iter_mut() {
                    if keys.contains(&k.as_str()) { if let Value::String(s) = val { *s = format!("{pre}{s}"); } } else { walk(val, pre, keys); }
                },
                Value::Array(a) => for e in a { walk(e, pre, keys); },
                _ => {}
            }
        }
        let mut all: Option<Value> = None;
        for t in 0..n {
            let mut c = v.clone();
            let pre = format!("T{t}-");
            for (_, arr) in KINDS { walk(&mut c[arr], &pre, &keys); }
            if let Some(Value::String(s)) = c.get_mut("slackSubstation") { *s = format!("{pre}{s}"); }
            match &mut all {
                None => all = Some(c),
                Some(a) => for (_, arr) in KINDS { a[arr].as_array_mut().unwrap().extend(c[arr].as_array().unwrap().clone()); },
            }
        }
        v = all.unwrap();
        v["slackSubstation"] = json!(format!("T0-{}", m.slack_substation));
        serde_json::from_value(v).unwrap()
    }

    #[test]
    #[ignore]
    fn bench_governance() {
        use std::time::Instant;
        for (name, m) in [("IEEE-118", load("ieee118")), ("9x tiled", tiled(&load("ieee118"), 9))] {
            let nb = crate::topology::process(&m, 0).islands.iter().map(|i| i.buses.len()).sum::<usize>();
            let n_ent = entities(&m).len();
            let (l0, l1) = (m.loads[0].mrid.clone(), m.loads[1].mrid.clone());
            let a = set(&m, "load", &l0, json!({"p": 77.0}));
            let b = set(&m, "load", &l1, json!({"p": 33.0}));
            let t = |f: &mut dyn FnMut()| { let t0 = Instant::now(); for _ in 0..20 { f(); } t0.elapsed().as_secs_f64() * 1000.0 / 20.0 };
            let d = t(&mut || { diff(&m, &a); });
            let mg = t(&mut || { merged(&m, &a, &b, &HashMap::new()); });
            let ops = t(&mut || { apply_ops(&m, &[json!({"op":"set","kind":"load","mrid":l0,"fields":{"p":1.0}})]).unwrap(); });
            let regions: BTreeSet<String> = m.substations.iter().map(|s| s.region.clone()).collect();
            let t0 = Instant::now();
            let kids: Vec<Model> = regions.iter().map(|r| split_region(&m, r, &format!("k.{r}")).unwrap()).collect();
            let sp = t0.elapsed().as_secs_f64() * 1000.0;
            let owner = kids.iter().find(|c| c.substations.iter().any(|s| s.mrid == m.slack_substation)).unwrap().id.clone();
            let refs: Vec<(String, &Model)> = kids.iter().map(|c| (c.id.clone(), c)).collect();
            let t0 = Instant::now();
            let (back, c, _) = hier_merge("x", &refs, &owner, &HashMap::new()).unwrap();
            let hm = t0.elapsed().as_secs_f64() * 1000.0;
            let val = { let t0 = Instant::now(); crate::validate::validate(&m); t0.elapsed().as_secs_f64() * 1000.0 };
            println!("BENCH {name}: buses {nb}, entities {n_ent}: diff {d:.1} ms, 3-way merge {mg:.1} ms, apply 1 op {ops:.1} ms, validate {val:.1} ms, split into {} regions {sp:.1} ms, hierarchical merge {hm:.1} ms, conflicts {}, rebuilt equals original: {}", regions.len(), c.len(), same(&back, &m));
        }
    }

    #[test]
    #[ignore]
    fn bench_exchange() {
        use std::time::Instant;
        let t = |n: usize, f: &mut dyn FnMut()| { let t0 = Instant::now(); for _ in 0..n { f(); } t0.elapsed().as_secs_f64() * 1000.0 / n as f64 };
        let hd = crate::cimxml::Header { uuid_seed: "b".into(), created: "2026-01-01T00:00:00Z".into(), version: 1, description: "b".into(), authority_set: "b".into() };
        for (name, m) in [("IEEE-118", load("ieee118")), ("9x tiled", tiled(&load("ieee118"), 9))] {
            let buses = crate::topology::process(&m, 0).islands.iter().map(|i| i.buses.len()).sum::<usize>();
            let g = crate::cimxml::to_graph(&m);
            let xml = crate::cimxml::write_full(&g, &hd);
            let mut m2 = m.clone(); m2.loads[0].p += 1.0;
            let g2 = crate::cimxml::to_graph(&m2);
            let n = if buses > 500 { 5 } else { 20 };
            let a = t(n, &mut || { crate::cimxml::to_graph(&m); });
            let b = t(n, &mut || { crate::cimxml::write_full(&g, &hd); });
            let c = t(n, &mut || { crate::cimxml::parse_full(&xml).unwrap(); });
            let (pg, _) = crate::cimxml::parse_full(&xml).unwrap();
            let d = t(n, &mut || { crate::cimxml::from_graph(&pg).unwrap(); });
            let e = t(n, &mut || { crate::cimxml::write_diff(&g, &g2, &hd); });
            let dx = crate::cimxml::write_diff(&g, &g2, &hd);
            let f = t(n, &mut || { crate::cimxml::apply_diff(&g, &dx).unwrap(); });
            println!("BENCH {name}: {buses} buses, {} objects, {:.1} MB CIMXML: build graph {a:.1} ms, write {b:.1} ms, parse {c:.1} ms, graph->model {d:.1} ms, diff write {e:.1} ms ({} B), diff apply {f:.1} ms", g.len(), xml.len() as f64 / 1e6, dx.len());
        }
        let root = format!("{}/../../data/legacy", env!("CARGO_MANIFEST_DIR"));
        let mp = std::fs::read_to_string(format!("{root}/case118.m")).unwrap();
        let raw = std::fs::read_to_string(format!("{root}/case118.raw")).unwrap();
        let a = t(50, &mut || { crate::legacy::matpower("x", "x", &mp).unwrap(); });
        let b = t(50, &mut || { crate::legacy::psse_raw("x", "x", &raw).unwrap(); });
        println!("BENCH legacy IEEE-118: MATPOWER ({} kB) {a:.2} ms, PSS/E RAW ({} kB) {b:.2} ms", mp.len() / 1000, raw.len() / 1000);
    }
}
