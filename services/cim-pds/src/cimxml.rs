//! IEC 61970-552 style CIMXML (RDF/XML) exchange for the CIM subset of this prototype:
//! a full model with an md:FullModel header, a difference model (dm:forwardDifferences / reverseDifferences),
//! and a from-scratch parser for importing both. Standard CIM16 classes and properties are used where one exists
//! (SI units); values with no standard home, and exact per-unit originals, travel as `gs:` extension properties.
//! Not a certified CGMES profile serialisation: no equipment-profile validation against the RDFS schema.
use crate::model::*;
use sha2::{Digest, Sha256};
use std::collections::BTreeMap;

pub const NS_RDF: &str = "http://www.w3.org/1999/02/22-rdf-syntax-ns#";
pub const NS_CIM: &str = "http://iec.ch/TC57/2013/CIM-schema-cim16#";
pub const NS_MD: &str = "http://iec.ch/TC57/61970-552/ModelDescription/1#";
pub const NS_DM: &str = "http://iec.ch/TC57/61970-552/DifferenceModel/1#";
pub const NS_GS: &str = "http://gridsovereign.invalid/cim-ext#";

#[derive(Clone, Debug, PartialEq)]
pub enum Val {
    Lit(String),
    Res(String), // rdf id of the target, without '#'
}

#[derive(Clone, Debug, PartialEq)]
pub struct Obj {
    pub class: String, // "cim:Breaker", "gs:ModelParameters"
    pub props: BTreeMap<String, Val>,
}
pub type Graph = BTreeMap<String, Obj>;

/// Deterministic UUID-shaped identifier (RFC 4122 layout, version 5 style, SHA-256 based) from a class and mRID.
pub fn rid(class: &str, mrid: &str) -> String {
    let mut h = Sha256::new();
    h.update(b"gridsovereign/");
    h.update(class.as_bytes());
    h.update(b"/");
    h.update(mrid.as_bytes());
    let d = h.finalize();
    let mut b = [0u8; 16];
    b.copy_from_slice(&d[..16]);
    b[6] = (b[6] & 0x0F) | 0x50;
    b[8] = (b[8] & 0x3F) | 0x80;
    let x = hex::encode(b);
    format!("_{}-{}-{}-{}-{}", &x[0..8], &x[8..12], &x[12..16], &x[16..20], &x[20..32])
}

fn lit(x: impl ToString) -> Val { Val::Lit(x.to_string()) }
fn res(class: &str, mrid: &str) -> Val { Val::Res(rid(class, mrid)) }

struct B<'a> { g: &'a mut Graph }
impl B<'_> {
    fn add(&mut self, rkind: &str, mrid: &str, class: &str, name: &str, props: Vec<(&str, Val)>) {
        let mut p = BTreeMap::new();
        p.insert("cim:IdentifiedObject.mRID".to_string(), lit(mrid));
        p.insert("cim:IdentifiedObject.name".to_string(), lit(name));
        for (k, v) in props { p.insert(k.to_string(), v); }
        self.g.insert(rid(rkind, mrid), Obj { class: class.to_string(), props: p });
    }
}

const SWITCH_CLASSES: [&str; 8] = ["Breaker", "Disconnector", "GroundDisconnector", "LoadBreakSwitch", "Fuse", "Recloser", "Sectionaliser", "Jumper"];

/// Model -> RDF graph.
pub fn to_graph(m: &Model) -> Graph {
    let mut g = Graph::new();
    let mut b = B { g: &mut g };
    let vl: std::collections::HashMap<&str, &VoltageLevel> = m.voltage_levels.iter().map(|v| (v.mrid.as_str(), v)).collect();
    let node_vl: std::collections::HashMap<&str, &str> = m.connectivity_nodes.iter().map(|n| (n.mrid.as_str(), n.voltage_level.as_str())).collect();
    let kv = |node: &str| node_vl.get(node).and_then(|v| vl.get(v)).map(|v| v.nominal_kv).unwrap_or(1.0);
    let zb = |node: &str| kv(node) * kv(node) / m.base_mva;
    let vl_of = |node: &str| node_vl.get(node).copied().unwrap_or("");

    b.add("model", &m.id, "gs:ModelParameters", &m.name, vec![
        ("gs:ModelParameters.baseMVA", lit(m.base_mva)), ("gs:ModelParameters.slackSubstation", lit(&m.slack_substation)),
        ("gs:ModelParameters.source", lit(&m.source)), ("gs:ModelParameters.modelId", lit(&m.id))]);
    let mut kvs: Vec<f64> = m.voltage_levels.iter().map(|v| v.nominal_kv).collect();
    kvs.sort_by(|a, c| a.partial_cmp(c).unwrap());
    kvs.dedup();
    for k in &kvs {
        b.add("bv", &k.to_string(), "cim:BaseVoltage", &format!("{k} kV"), vec![("cim:BaseVoltage.nominalVoltage", lit(k))]);
    }
    let mut regions: Vec<&str> = m.substations.iter().map(|s| s.region.as_str()).collect();
    regions.sort();
    regions.dedup();
    for r in regions {
        b.add("region", r, "cim:SubGeographicalRegion", r, vec![]);
    }
    for s in &m.substations {
        let mut p = vec![("cim:Substation.Region", res("region", &s.region)), ("gs:Substation.lat", lit(s.lat)), ("gs:Substation.lon", lit(s.lon))];
        if let Some(v) = s.vm0 { p.push(("gs:Substation.vm0", lit(v))); }
        if let Some(v) = s.va0 { p.push(("gs:Substation.va0", lit(v))); }
        b.add("sub", &s.mrid, "cim:Substation", &s.name, p);
    }
    for v in &m.voltage_levels {
        let mut p = vec![("cim:VoltageLevel.Substation", res("sub", &v.substation)), ("cim:VoltageLevel.BaseVoltage", res("bv", &v.nominal_kv.to_string()))];
        if let Some(x) = v.v_min { p.push(("cim:VoltageLevel.lowVoltageLimit", lit(x * v.nominal_kv))); p.push(("gs:VoltageLevel.vMin", lit(x))); }
        if let Some(x) = v.v_max { p.push(("cim:VoltageLevel.highVoltageLimit", lit(x * v.nominal_kv))); p.push(("gs:VoltageLevel.vMax", lit(x))); }
        b.add("vl", &v.mrid, "cim:VoltageLevel", &v.name, p);
    }
    for n in &m.connectivity_nodes {
        let mut p = vec![("cim:ConnectivityNode.ConnectivityNodeContainer", res("vl", &n.voltage_level))];
        if let Some(k) = &n.kind { p.push(("gs:ConnectivityNode.kind", lit(k))); }
        b.add("cn", &n.mrid, "cim:ConnectivityNode", &n.name, p);
    }
    // terminals
    fn term(b: &mut B, eq_kind: &str, eq: &str, seq: u32, node: &str) {
        let tm = format!("{eq}#T{seq}");
        b.add("term", &tm, "cim:Terminal", &format!("T{seq}"), vec![
            ("cim:Terminal.ConductingEquipment", res(eq_kind, eq)), ("cim:Terminal.ConnectivityNode", res("cn", node)),
            ("cim:ACDCTerminal.sequenceNumber", lit(seq))]);
    }
    for s in &m.switches {
        let class = if SWITCH_CLASSES.contains(&s.kind.as_str()) { format!("cim:{}", s.kind) } else { "cim:Switch".to_string() };
        let mut p = vec![("cim:Switch.open", lit(s.open)), ("cim:Equipment.EquipmentContainer", res("vl", vl_of(&s.node1)))];
        if class == "cim:Switch" { p.push(("gs:Switch.type", lit(&s.kind))); }
        b.add("sw", &s.mrid, &class, &s.name, p);
        term(&mut b, "sw", &s.mrid, 1, &s.node1);
        term(&mut b, "sw", &s.mrid, 2, &s.node2);
    }
    let limit = |b: &mut B, kind: &str, mrid: &str, rating: f64, kvv: f64| {
        if rating > 0.0 {
            let ols = format!("{mrid}#OLS");
            b.add("ols", &ols, "cim:OperationalLimitSet", "Normal", vec![("cim:OperationalLimitSet.Equipment", res(kind, mrid))]);
            b.add("cl", &format!("{mrid}#CL"), "cim:CurrentLimit", "Normal", vec![
                ("cim:OperationalLimit.OperationalLimitSet", res("ols", &ols)),
                ("cim:CurrentLimit.value", lit(rating * 1000.0 / (3f64.sqrt() * kvv)))]);
        }
    };
    for l in &m.ac_line_segments {
        let z = zb(&l.node1);
        b.add("line", &l.mrid, "cim:ACLineSegment", &l.name, vec![
            ("cim:Equipment.EquipmentContainer", res("vl", vl_of(&l.node1))), ("cim:ACLineSegment.r", lit(l.r * z)), ("cim:ACLineSegment.x", lit(l.x * z)),
            ("cim:ACLineSegment.bch", lit(l.bch / z)), ("cim:ACLineSegment.gch", lit(0)),
            ("gs:Branch.rPu", lit(l.r)), ("gs:Branch.xPu", lit(l.x)), ("gs:Branch.bchPu", lit(l.bch)), ("gs:Branch.ratingMVA", lit(l.rating))]);
        term(&mut b, "line", &l.mrid, 1, &l.node1);
        term(&mut b, "line", &l.mrid, 2, &l.node2);
        limit(&mut b, "line", &l.mrid, l.rating, kv(&l.node1));
    }
    for t in &m.transformers {
        let (z1, k1, k2) = (zb(&t.node1), kv(&t.node1), kv(&t.node2));
        let mut p = vec![("cim:Equipment.EquipmentContainer", res("vl", vl_of(&t.node1))), ("gs:Branch.rPu", lit(t.r)), ("gs:Branch.xPu", lit(t.x)), ("gs:Branch.bchPu", lit(t.bch)), ("gs:Branch.ratingMVA", lit(t.rating))];
        if let Some(x) = t.ratio { p.push(("gs:PowerTransformer.ratio", lit(x))); }
        if let Some(x) = t.angle { p.push(("gs:PowerTransformer.angle", lit(x))); }
        b.add("xf", &t.mrid, "cim:PowerTransformer", &t.name, p);
        term(&mut b, "xf", &t.mrid, 1, &t.node1);
        term(&mut b, "xf", &t.mrid, 2, &t.node2);
        for (n, node, k) in [(1u32, &t.node1, k1), (2, &t.node2, k2)] {
            let (r, x, bb) = if n == 1 { (t.r * z1, t.x * z1, t.bch / z1) } else { (0.0, 0.0, 0.0) };
            b.add("xfe", &format!("{}#E{n}", t.mrid), "cim:PowerTransformerEnd", &format!("End{n}"), vec![
                ("cim:PowerTransformerEnd.PowerTransformer", res("xf", &t.mrid)), ("cim:TransformerEnd.endNumber", lit(n)),
                ("cim:TransformerEnd.Terminal", res("term", &format!("{}#T{n}", t.mrid))), ("cim:PowerTransformerEnd.ratedU", lit(k)),
                ("cim:PowerTransformerEnd.r", lit(r)), ("cim:PowerTransformerEnd.x", lit(x)), ("cim:PowerTransformerEnd.b", lit(bb)), ("cim:PowerTransformerEnd.g", lit(0))]);
            let _ = node;
        }
        limit(&mut b, "xf", &t.mrid, t.rating, k1);
    }
    for x in &m.generators {
        let k = kv(&x.node);
        let gu = format!("{}#GU", x.mrid);
        let mut gp = vec![("cim:GeneratingUnit.maxOperatingP", lit(x.pmax)), ("cim:GeneratingUnit.minOperatingP", lit(x.pmin)), ("cim:GeneratingUnit.initialP", lit(x.p)), ("gs:GeneratingUnit.fuel", lit(&x.fuel))];
        for (n, v) in [("costC2", x.cost_c2), ("costC1", x.cost_c1), ("costC0", x.cost_c0)] {
            if let Some(v) = v { gp.push((if n == "costC2" { "gs:GeneratingUnit.costC2" } else if n == "costC1" { "gs:GeneratingUnit.costC1" } else { "gs:GeneratingUnit.costC0" }, lit(v))); }
        }
        b.add("gu", &gu, "cim:GeneratingUnit", &x.name, gp);
        let rc = format!("{}#RC", x.mrid);
        b.add("rc", &rc, "cim:RegulatingControl", "Voltage", vec![("cim:RegulatingControl.mode", lit("voltage")), ("cim:RegulatingControl.targetValue", lit(x.vset * k)),
            ("cim:RegulatingControl.Terminal", res("term", &format!("{}#T1", x.mrid))), ("gs:RegulatingControl.vsetPu", lit(x.vset))]);
        b.add("gen", &x.mrid, "cim:SynchronousMachine", &x.name, vec![
            ("cim:Equipment.EquipmentContainer", res("vl", vl_of(&x.node))), ("cim:RotatingMachine.p", lit(x.p)), ("cim:RotatingMachine.q", lit(x.q)),
            ("cim:SynchronousMachine.maxQ", lit(x.qmax)), ("cim:SynchronousMachine.minQ", lit(x.qmin)),
            ("cim:RotatingMachine.GeneratingUnit", res("gu", &gu)), ("cim:RegulatingCondEq.RegulatingControl", res("rc", &rc)),
            ("cim:Equipment.normallyInService", lit(x.in_service))]);
        term(&mut b, "gen", &x.mrid, 1, &x.node);
    }
    for l in &m.loads {
        b.add("load", &l.mrid, "cim:EnergyConsumer", &l.name, vec![("cim:Equipment.EquipmentContainer", res("vl", vl_of(&l.node))), ("cim:EnergyConsumer.p", lit(l.p)), ("cim:EnergyConsumer.q", lit(l.q))]);
        term(&mut b, "load", &l.mrid, 1, &l.node);
    }
    for s in &m.shunts {
        let k = kv(&s.node);
        b.add("shunt", &s.mrid, "cim:LinearShuntCompensator", &s.name, vec![("cim:Equipment.EquipmentContainer", res("vl", vl_of(&s.node))),
            ("cim:LinearShuntCompensator.bPerSection", lit(s.b / (k * k))), ("cim:LinearShuntCompensator.gPerSection", lit(s.g / (k * k))),
            ("cim:ShuntCompensator.nomU", lit(k)), ("gs:Shunt.gMW", lit(s.g)), ("gs:Shunt.bMvar", lit(s.b))]);
        term(&mut b, "shunt", &s.mrid, 1, &s.node);
    }
    g
}

fn esc(s: &str) -> String {
    let mut o = String::with_capacity(s.len());
    for c in s.chars() {
        match c {
            '&' => o.push_str("&amp;"),
            '<' => o.push_str("&lt;"),
            '>' => o.push_str("&gt;"),
            '"' => o.push_str("&quot;"),
            c if (c as u32) < 0x20 && c != '\t' && c != '\n' && c != '\r' => {} // not allowed in XML 1.0
            c => o.push(c),
        }
    }
    o
}

const ROOT: &str = "<rdf:RDF xmlns:rdf=\"http://www.w3.org/1999/02/22-rdf-syntax-ns#\" xmlns:cim=\"http://iec.ch/TC57/2013/CIM-schema-cim16#\" xmlns:md=\"http://iec.ch/TC57/61970-552/ModelDescription/1#\" xmlns:dm=\"http://iec.ch/TC57/61970-552/DifferenceModel/1#\" xmlns:gs=\"http://gridsovereign.invalid/cim-ext#\">\n";

fn write_obj(o: &mut String, indent: &str, id: &str, ob: &Obj) {
    o.push_str(&format!("{indent}<{} rdf:ID=\"{}\">\n", ob.class, id));
    write_props(o, indent, &ob.props);
    o.push_str(&format!("{indent}</{}>\n", ob.class));
}
fn write_props(o: &mut String, indent: &str, props: &BTreeMap<String, Val>) {
    for (k, v) in props {
        match v {
            Val::Lit(s) => o.push_str(&format!("{indent}  <{k}>{}</{k}>\n", esc(s))),
            Val::Res(r) => o.push_str(&format!("{indent}  <{k} rdf:resource=\"#{r}\"/>\n")),
        }
    }
}

#[derive(Clone, Debug)]
pub struct Header {
    pub uuid_seed: String, // makes the header id deterministic per (model, version)
    pub created: String,   // ISO 8601 UTC
    pub version: u64,
    pub description: String,
    pub authority_set: String,
}

pub fn iso(ms: u64) -> String {
    let secs = (ms / 1000) as i64;
    let (days, rem) = (secs.div_euclid(86400), secs.rem_euclid(86400));
    // civil-from-days (Howard Hinnant)
    let z = days + 719468;
    let era = z.div_euclid(146097);
    let doe = z.rem_euclid(146097);
    let yoe = (doe - doe / 1460 + doe / 36524 - doe / 146096) / 365;
    let y = yoe + era * 400;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let d = doy - (153 * mp + 2) / 5 + 1;
    let mo = if mp < 10 { mp + 3 } else { mp - 9 };
    let y = if mo <= 2 { y + 1 } else { y };
    format!("{y:04}-{mo:02}-{d:02}T{:02}:{:02}:{:02}Z", rem / 3600, rem % 3600 / 60, rem % 60)
}

fn header_xml(h: &Header, about: &str, tag: &str) -> String {
    format!("  <{tag} rdf:about=\"urn:uuid:{}\">\n    <md:Model.created>{}</md:Model.created>\n    <md:Model.scenarioTime>{}</md:Model.scenarioTime>\n    <md:Model.version>{}</md:Model.version>\n    <md:Model.description>{}</md:Model.description>\n    <md:Model.modelingAuthoritySet>{}</md:Model.modelingAuthoritySet>\n    <md:Model.profile>http://iec.ch/TC57/2013/CIM-schema-cim16</md:Model.profile>\n  </{tag}>\n",
        &rid("header", about)[1..], h.created, h.created, h.version, esc(&h.description), esc(&h.authority_set))
}

pub fn write_full(g: &Graph, h: &Header) -> String {
    let mut o = String::with_capacity(g.len() * 400);
    o.push_str("<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n");
    o.push_str(ROOT);
    o.push_str(&header_xml(h, &h.uuid_seed, "md:FullModel"));
    for (id, ob) in g {
        write_obj(&mut o, "  ", id, ob);
    }
    o.push_str("</rdf:RDF>\n");
    o
}

/// Difference model taking `a` to `b`: additions and new values in forwardDifferences, deletions and old values in reverseDifferences.
pub fn write_diff(a: &Graph, b: &Graph, h: &Header) -> String {
    let mut fwd = String::new();
    let mut rev = String::new();
    let ids: std::collections::BTreeSet<&String> = a.keys().chain(b.keys()).collect();
    let (mut nadd, mut nrem, mut nmod) = (0, 0, 0);
    for id in ids {
        match (a.get(id), b.get(id)) {
            (None, Some(n)) => { write_obj(&mut fwd, "      ", id, n); nadd += 1; }
            (Some(o), None) => { write_obj(&mut rev, "      ", id, o); nrem += 1; }
            (Some(o), Some(n)) if o != n => {
                let keys: std::collections::BTreeSet<&String> = o.props.keys().chain(n.props.keys()).collect();
                let (mut fp, mut rp) = (BTreeMap::new(), BTreeMap::new());
                for k in keys {
                    if o.props.get(k) != n.props.get(k) {
                        if let Some(v) = n.props.get(k) { fp.insert(k.clone(), v.clone()); }
                        if let Some(v) = o.props.get(k) { rp.insert(k.clone(), v.clone()); }
                    }
                }
                for (buf, props, cls) in [(&mut fwd, &fp, &n.class), (&mut rev, &rp, &o.class)] {
                    buf.push_str(&format!("      <rdf:Description rdf:about=\"#{id}\">\n"));
                    write_props(buf, "      ", props);
                    buf.push_str("      </rdf:Description>\n");
                    let _ = cls;
                }
                nmod += 1;
            }
            _ => {}
        }
    }
    let mut o = String::new();
    o.push_str("<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n");
    o.push_str(ROOT);
    o.push_str(&format!("  <!-- {nadd} additions, {nrem} deletions, {nmod} modified objects -->\n"));
    o.push_str(&format!("  <dm:DifferenceModel rdf:about=\"urn:uuid:{}\">\n    <md:Model.created>{}</md:Model.created>\n    <md:Model.version>{}</md:Model.version>\n    <md:Model.description>{}</md:Model.description>\n    <md:Model.modelingAuthoritySet>{}</md:Model.modelingAuthoritySet>\n    <dm:forwardDifferences rdf:parseType=\"Statements\">\n{fwd}    </dm:forwardDifferences>\n    <dm:reverseDifferences rdf:parseType=\"Statements\">\n{rev}    </dm:reverseDifferences>\n  </dm:DifferenceModel>\n", &rid("diff", &h.uuid_seed)[1..], h.created, h.version, esc(&h.description), esc(&h.authority_set)));
    o.push_str("</rdf:RDF>\n");
    o
}

// ------------------------------------------------------------------------------------------------ XML parsing

#[derive(Debug)]
struct El {
    name: String,
    attrs: Vec<(String, String)>,
    text: String,
    kids: Vec<El>,
}

const MAX_DEPTH: usize = 8;
const MAX_NODES: usize = 4_000_000;

struct P<'a> { s: &'a [u8], i: usize, nodes: usize }

fn unescape(s: &str) -> Result<String, String> {
    if !s.contains('&') { return Ok(s.to_string()); }
    let mut o = String::with_capacity(s.len());
    let mut rest = s;
    while let Some(p) = rest.find('&') {
        o.push_str(&rest[..p]);
        let tail = &rest[p + 1..];
        let end = tail.find(';').ok_or("unterminated entity")?;
        let ent = &tail[..end];
        match ent {
            "amp" => o.push('&'), "lt" => o.push('<'), "gt" => o.push('>'), "quot" => o.push('"'), "apos" => o.push('\''),
            _ if ent.starts_with("#x") => o.push(char::from_u32(u32::from_str_radix(&ent[2..], 16).map_err(|_| "bad char reference")?).ok_or("bad char reference")?),
            _ if ent.starts_with('#') => o.push(char::from_u32(ent[1..].parse::<u32>().map_err(|_| "bad char reference")?).ok_or("bad char reference")?),
            _ => return Err(format!("entity &{ent}; is not allowed")), // no custom entities: no XXE, no entity expansion
        }
        rest = &tail[end + 1..];
    }
    o.push_str(rest);
    Ok(o)
}

impl P<'_> {
    fn skip_ws(&mut self) { while self.i < self.s.len() && self.s[self.i].is_ascii_whitespace() { self.i += 1; } }
    fn starts(&self, t: &str) -> bool { self.s[self.i..].starts_with(t.as_bytes()) }
    fn skip_until(&mut self, t: &str) -> Result<(), String> {
        let hay = &self.s[self.i..];
        let pos = hay.windows(t.len()).position(|w| w == t.as_bytes()).ok_or(format!("missing {t}"))?;
        self.i += pos + t.len();
        Ok(())
    }
    fn name(&mut self) -> Result<String, String> {
        let st = self.i;
        while self.i < self.s.len() && (self.s[self.i].is_ascii_alphanumeric() || b":_-.".contains(&self.s[self.i])) { self.i += 1; }
        if st == self.i { return Err(format!("name expected at byte {}", st)); }
        Ok(String::from_utf8_lossy(&self.s[st..self.i]).into_owned())
    }
    fn element(&mut self, depth: usize) -> Result<El, String> {
        if depth > MAX_DEPTH { return Err("XML nested too deeply".into()); }
        self.nodes += 1;
        if self.nodes > MAX_NODES { return Err("XML has too many elements".into()); }
        if self.s.get(self.i) != Some(&b'<') { return Err(format!("element expected at byte {}", self.i)); }
        self.i += 1;
        let name = self.name()?;
        let mut attrs = vec![];
        loop {
            self.skip_ws();
            match self.s.get(self.i) {
                Some(b'/') => {
                    if self.s.get(self.i + 1) != Some(&b'>') { return Err("bad empty-element tag".into()); }
                    self.i += 2;
                    return Ok(El { name, attrs, text: String::new(), kids: vec![] });
                }
                Some(b'>') => { self.i += 1; break; }
                Some(_) => {
                    let an = self.name()?;
                    self.skip_ws();
                    if self.s.get(self.i) != Some(&b'=') { return Err("attribute without value".into()); }
                    self.i += 1;
                    self.skip_ws();
                    let q = *self.s.get(self.i).ok_or("truncated attribute")?;
                    if q != b'"' && q != b'\'' { return Err("attribute value must be quoted".into()); }
                    self.i += 1;
                    let st = self.i;
                    while self.i < self.s.len() && self.s[self.i] != q { self.i += 1; }
                    if self.i >= self.s.len() { return Err("unterminated attribute".into()); }
                    let v = unescape(std::str::from_utf8(&self.s[st..self.i]).map_err(|_| "invalid UTF-8")?)?;
                    self.i += 1;
                    attrs.push((an, v));
                }
                None => return Err("truncated tag".into()),
            }
        }
        let mut text = String::new();
        let mut kids = vec![];
        loop {
            let st = self.i;
            while self.i < self.s.len() && self.s[self.i] != b'<' { self.i += 1; }
            if self.i >= self.s.len() { return Err(format!("element <{name}> is not closed")); }
            text.push_str(&unescape(std::str::from_utf8(&self.s[st..self.i]).map_err(|_| "invalid UTF-8")?)?);
            if self.starts("<!--") { self.i += 4; self.skip_until("-->")?; continue; }
            if self.starts("<![CDATA[") { return Err("CDATA is not supported".into()); }
            if self.starts("<?") { self.i += 2; self.skip_until("?>")?; continue; }
            if self.starts("<!") { return Err("DTD/entity declarations are not allowed".into()); }
            if self.starts("</") {
                self.i += 2;
                let close = self.name()?;
                self.skip_ws();
                if self.s.get(self.i) != Some(&b'>') || close != name { return Err(format!("mismatched closing tag </{close}> for <{name}>")); }
                self.i += 1;
                return Ok(El { name, attrs, text: text.trim().to_string(), kids });
            }
            kids.push(self.element(depth + 1)?);
        }
    }
}

fn parse_xml(src: &str) -> Result<El, String> {
    let mut p = P { s: src.as_bytes(), i: 0, nodes: 0 };
    loop {
        p.skip_ws();
        if p.starts("<?") { p.i += 2; p.skip_until("?>")?; }
        else if p.starts("<!--") { p.i += 4; p.skip_until("-->")?; }
        else if p.starts("<!") { return Err("DTD/entity declarations are not allowed".into()); }
        else { break; }
    }
    let root = p.element(0)?;
    p.skip_ws();
    while p.starts("<!--") { p.i += 4; p.skip_until("-->")?; p.skip_ws(); }
    if p.i != p.s.len() { return Err("content after the root element".into()); }
    Ok(root)
}

fn attr<'a>(e: &'a El, n: &str) -> Option<&'a str> { e.attrs.iter().find(|(k, _)| k == n).map(|(_, v)| v.as_str()) }

fn read_props(e: &El) -> Result<BTreeMap<String, Val>, String> {
    let mut props = BTreeMap::new();
    for k in &e.kids {
        let v = match attr(k, "rdf:resource") {
            Some(r) => Val::Res(r.trim_start_matches('#').to_string()),
            None => { if !k.kids.is_empty() { return Err(format!("property {} has nested elements", k.name)); } Val::Lit(k.text.clone()) }
        };
        if props.insert(k.name.clone(), v).is_some() { return Err(format!("property {} given twice on {}", k.name, e.name)); }
    }
    Ok(props)
}

fn read_objs(parent: &El, g: &mut Graph) -> Result<(), String> {
    for e in &parent.kids {
        if e.name == "md:FullModel" || e.name == "dm:DifferenceModel" { continue; }
        let id = attr(e, "rdf:ID").map(|s| s.to_string()).or_else(|| attr(e, "rdf:about").map(|s| s.trim_start_matches('#').to_string())).ok_or(format!("object <{}> has no rdf:ID or rdf:about", e.name))?;
        let props = read_props(e)?;
        if g.insert(id.clone(), Obj { class: e.name.clone(), props }).is_some() { return Err(format!("object {id} defined twice")); }
    }
    Ok(())
}

/// Parse a full-model document into a graph.
pub fn parse_full(src: &str) -> Result<(Graph, Option<u64>), String> {
    let root = parse_xml(src)?;
    if root.name != "rdf:RDF" { return Err("root element must be rdf:RDF".into()); }
    let mut g = Graph::new();
    read_objs(&root, &mut g)?;
    let ver = root.kids.iter().find(|k| k.name == "md:FullModel").and_then(|h| h.kids.iter().find(|k| k.name == "md:Model.version")).and_then(|v| v.text.parse().ok());
    if g.is_empty() { return Err("the document contains no objects".into()); }
    Ok((g, ver))
}

/// Apply a difference model to a graph: reverse then forward, as in 61970-552 (deletions first).
pub fn apply_diff(base: &Graph, src: &str) -> Result<Graph, String> {
    let root = parse_xml(src)?;
    let dm = root.kids.iter().find(|k| k.name == "dm:DifferenceModel").ok_or("no dm:DifferenceModel in the document")?;
    let section = |n: &str| -> Result<Vec<(String, El)>, String> {
        let mut out = vec![];
        for s in dm.kids.iter().filter(|k| k.name == n) {
            for e in &s.kids {
                let (id, full) = match (attr(e, "rdf:ID"), attr(e, "rdf:about")) {
                    (Some(i), _) => (i.to_string(), true),
                    (None, Some(a)) => (a.trim_start_matches('#').to_string(), false),
                    _ => return Err("difference statement without rdf:ID or rdf:about".into()),
                };
                let _ = full;
                out.push((id, El { name: e.name.clone(), attrs: e.attrs.clone(), text: String::new(), kids: e.kids.iter().map(|k| El { name: k.name.clone(), attrs: k.attrs.clone(), text: k.text.clone(), kids: vec![] }).collect() }));
            }
        }
        Ok(out)
    };
    let (fwd, rev) = (section("dm:forwardDifferences")?, section("dm:reverseDifferences")?);
    let mut g = base.clone();
    for (id, e) in &rev {
        let is_full = attr(e, "rdf:ID").is_some();
        let cur = g.get_mut(id).ok_or(format!("reverse difference refers to unknown object {id}"))?;
        if is_full {
            if *cur != (Obj { class: e.name.clone(), props: read_props(e)? }) { return Err(format!("object {id} to delete does not match the model (the difference was made against another version)")); }
            g.remove(id);
        } else {
            for (k, v) in read_props(e)? {
                if cur.props.get(&k) != Some(&v) { return Err(format!("{id}: {k} is not {:?} in this model; the difference was made against another version", v)); }
                cur.props.remove(&k);
            }
        }
    }
    for (id, e) in &fwd {
        if attr(e, "rdf:ID").is_some() {
            if g.contains_key(id) { return Err(format!("added object {id} already exists")); }
            g.insert(id.clone(), Obj { class: e.name.clone(), props: read_props(e)? });
        } else {
            let cur = g.get_mut(id).ok_or(format!("forward difference refers to unknown object {id}"))?;
            for (k, v) in read_props(e)? { cur.props.insert(k, v); }
        }
    }
    Ok(g)
}

// ------------------------------------------------------------------------------------------------ graph -> model

fn s<'a>(o: &'a Obj, k: &str) -> Result<&'a str, String> {
    match o.props.get(k) { Some(Val::Lit(v)) => Ok(v), _ => Err(format!("{}: property {k} missing", o.class)) }
}
fn f(o: &Obj, k: &str) -> Result<f64, String> {
    let v: f64 = s(o, k)?.trim().parse().map_err(|_| format!("{}: {k} is not a number", o.class))?;
    if v.is_finite() { Ok(v) } else { Err(format!("{}: {k} is not finite", o.class)) }
}
fn fo(o: &Obj, k: &str) -> Result<Option<f64>, String> { if o.props.contains_key(k) { f(o, k).map(Some) } else { Ok(None) } }
fn b(o: &Obj, k: &str) -> Result<bool, String> { match s(o, k)? { "true" | "1" => Ok(true), "false" | "0" => Ok(false), v => Err(format!("{}: {k}={v} is not boolean", o.class)) } }
fn r<'a>(o: &'a Obj, k: &str) -> Result<&'a str, String> { match o.props.get(k) { Some(Val::Res(v)) => Ok(v), _ => Err(format!("{}: reference {k} missing", o.class)) } }

pub fn from_graph(g: &Graph) -> Result<Model, String> {
    let mp = g.values().find(|o| o.class == "gs:ModelParameters").ok_or("the document has no gs:ModelParameters object (baseMVA and slack substation); import it as a bus-branch case instead")?;
    let base_mva = f(mp, "gs:ModelParameters.baseMVA")?;
    if base_mva <= 0.0 { return Err("baseMVA must be positive".into()); }
    let mrid = |id: &str| -> Result<String, String> { g.get(id).and_then(|o| s(o, "cim:IdentifiedObject.mRID").ok()).map(String::from).ok_or(format!("reference to unknown object {id}")) };
    let name = |o: &Obj| s(o, "cim:IdentifiedObject.name").map(String::from).unwrap_or_default();
    let mut m = Model { id: s(mp, "gs:ModelParameters.modelId")?.to_string(), name: name(mp), base_mva, source: s(mp, "gs:ModelParameters.source").unwrap_or("").to_string(),
        slack_substation: s(mp, "gs:ModelParameters.slackSubstation")?.to_string(), substations: vec![], voltage_levels: vec![], connectivity_nodes: vec![], switches: vec![],
        ac_line_segments: vec![], transformers: vec![], generators: vec![], loads: vec![], shunts: vec![] };
    // terminals by equipment and sequence number
    let mut terms: BTreeMap<(String, u32), String> = BTreeMap::new();
    for o in g.values().filter(|o| o.class == "cim:Terminal") {
        let eq = r(o, "cim:Terminal.ConductingEquipment")?.to_string();
        let seq: u32 = s(o, "cim:ACDCTerminal.sequenceNumber")?.parse().map_err(|_| "bad terminal sequenceNumber")?;
        terms.insert((eq, seq), mrid(r(o, "cim:Terminal.ConnectivityNode")?)?);
    }
    let node = |id: &str, seq: u32| -> Result<String, String> { terms.get(&(id.to_string(), seq)).cloned().ok_or(format!("equipment {id} has no terminal {seq}")) };
    let kv_of_vl: BTreeMap<String, f64> = g.iter().filter(|(_, o)| o.class == "cim:VoltageLevel").map(|(id, o)| {
        let bv = r(o, "cim:VoltageLevel.BaseVoltage").and_then(|bid| g.get(bid).ok_or("unknown BaseVoltage".to_string())).and_then(|bo| f(bo, "cim:BaseVoltage.nominalVoltage"));
        (id.clone(), bv.unwrap_or(f64::NAN))
    }).collect();
    let node_vl: BTreeMap<String, String> = g.iter().filter(|(_, o)| o.class == "cim:ConnectivityNode").filter_map(|(_, o)| Some((s(o, "cim:IdentifiedObject.mRID").ok()?.to_string(), r(o, "cim:ConnectivityNode.ConnectivityNodeContainer").ok()?.to_string()))).collect();
    let kv = |n: &str| -> Result<f64, String> {
        let v = node_vl.get(n).and_then(|v| kv_of_vl.get(v)).copied().unwrap_or(f64::NAN);
        if v.is_finite() && v > 0.0 { Ok(v) } else { Err(format!("cannot determine the base voltage at node {n}")) }
    };
    for (id, o) in g {
        match o.class.as_str() {
            "cim:Substation" => {
                let reg = g.get(r(o, "cim:Substation.Region")?).ok_or("unknown region")?;
                m.substations.push(Substation { mrid: s(o, "cim:IdentifiedObject.mRID")?.into(), name: name(o), region: name(reg), lat: fo(o, "gs:Substation.lat")?.unwrap_or(0.0), lon: fo(o, "gs:Substation.lon")?.unwrap_or(0.0), vm0: fo(o, "gs:Substation.vm0")?, va0: fo(o, "gs:Substation.va0")? });
            }
            "cim:VoltageLevel" => {
                let k = kv_of_vl[id];
                if !(k.is_finite() && k > 0.0) { return Err(format!("voltage level {id} has no valid base voltage")); }
                let vmin = match fo(o, "gs:VoltageLevel.vMin")? { Some(v) => Some(v), None => fo(o, "cim:VoltageLevel.lowVoltageLimit")?.map(|x| x / k) };
                let vmax = match fo(o, "gs:VoltageLevel.vMax")? { Some(v) => Some(v), None => fo(o, "cim:VoltageLevel.highVoltageLimit")?.map(|x| x / k) };
                m.voltage_levels.push(VoltageLevel { mrid: s(o, "cim:IdentifiedObject.mRID")?.into(), name: name(o), substation: mrid(r(o, "cim:VoltageLevel.Substation")?)?, nominal_kv: k, v_min: vmin, v_max: vmax });
            }
            "cim:ConnectivityNode" => m.connectivity_nodes.push(ConnectivityNode { mrid: s(o, "cim:IdentifiedObject.mRID")?.into(), name: name(o), voltage_level: mrid(r(o, "cim:ConnectivityNode.ConnectivityNodeContainer")?)?, kind: o.props.get("gs:ConnectivityNode.kind").and_then(|v| if let Val::Lit(x) = v { Some(x.clone()) } else { None }) }),
            c if c.starts_with("cim:") && (SWITCH_CLASSES.contains(&&c[4..]) || c == "cim:Switch") => {
                let kind = if c == "cim:Switch" { s(o, "gs:Switch.type").unwrap_or("Switch").to_string() } else { c[4..].to_string() };
                m.switches.push(Switch { mrid: s(o, "cim:IdentifiedObject.mRID")?.into(), name: name(o), kind, node1: node(id, 1)?, node2: node(id, 2)?, open: b(o, "cim:Switch.open")? });
            }
            "cim:ACLineSegment" | "cim:PowerTransformer" => {
                let (n1, n2) = (node(id, 1)?, node(id, 2)?);
                let k1 = kv(&n1)?;
                let z1 = k1 * k1 / base_mva;
                let (rr, xx, bb) = if o.props.contains_key("gs:Branch.rPu") { (f(o, "gs:Branch.rPu")?, f(o, "gs:Branch.xPu")?, f(o, "gs:Branch.bchPu")?) } else if o.class == "cim:ACLineSegment" {
                    (f(o, "cim:ACLineSegment.r")? / z1, f(o, "cim:ACLineSegment.x")? / z1, f(o, "cim:ACLineSegment.bch")? * z1)
                } else {
                    let e1 = g.iter().find(|(_, e)| e.class == "cim:PowerTransformerEnd" && r(e, "cim:PowerTransformerEnd.PowerTransformer").ok() == Some(id.as_str()) && s(e, "cim:TransformerEnd.endNumber").ok() == Some("1")).map(|(_, e)| e).ok_or("transformer without end 1")?;
                    (f(e1, "cim:PowerTransformerEnd.r")? / z1, f(e1, "cim:PowerTransformerEnd.x")? / z1, f(e1, "cim:PowerTransformerEnd.b")? * z1)
                };
                let rating = match fo(o, "gs:Branch.ratingMVA")? {
                    Some(v) => v,
                    None => g.values().find(|c| c.class == "cim:CurrentLimit" && r(c, "cim:OperationalLimit.OperationalLimitSet").ok().and_then(|ols| g.get(ols)).and_then(|l| r(l, "cim:OperationalLimitSet.Equipment").ok()) == Some(id.as_str()))
                        .map(|c| f(c, "cim:CurrentLimit.value").map(|a| a * 3f64.sqrt() * k1 / 1000.0)).transpose()?.unwrap_or(0.0),
                };
                let br = Branch { mrid: s(o, "cim:IdentifiedObject.mRID")?.into(), name: name(o), r: rr, x: xx, bch: bb, rating, node1: n1, node2: n2,
                    ratio: fo(o, "gs:PowerTransformer.ratio")?, angle: fo(o, "gs:PowerTransformer.angle")? };
                if o.class == "cim:ACLineSegment" { m.ac_line_segments.push(br) } else { m.transformers.push(br) }
            }
            "cim:SynchronousMachine" => {
                let gu = g.get(r(o, "cim:RotatingMachine.GeneratingUnit")?).ok_or("unknown GeneratingUnit")?;
                let rc = g.get(r(o, "cim:RegulatingCondEq.RegulatingControl")?).ok_or("unknown RegulatingControl")?;
                let n = node(id, 1)?;
                let vset = match fo(rc, "gs:RegulatingControl.vsetPu")? { Some(v) => v, None => f(rc, "cim:RegulatingControl.targetValue")? / kv(&n)? };
                m.generators.push(Generator { mrid: s(o, "cim:IdentifiedObject.mRID")?.into(), name: name(o), node: n, p: f(o, "cim:RotatingMachine.p")?, q: f(o, "cim:RotatingMachine.q")?,
                    qmax: f(o, "cim:SynchronousMachine.maxQ")?, qmin: f(o, "cim:SynchronousMachine.minQ")?, vset, pmax: f(gu, "cim:GeneratingUnit.maxOperatingP")?, pmin: f(gu, "cim:GeneratingUnit.minOperatingP")?,
                    in_service: b(o, "cim:Equipment.normallyInService")?, fuel: s(gu, "gs:GeneratingUnit.fuel").unwrap_or("").to_string(),
                    cost_c2: fo(gu, "gs:GeneratingUnit.costC2")?, cost_c1: fo(gu, "gs:GeneratingUnit.costC1")?, cost_c0: fo(gu, "gs:GeneratingUnit.costC0")? });
            }
            "cim:EnergyConsumer" => m.loads.push(Load { mrid: s(o, "cim:IdentifiedObject.mRID")?.into(), name: name(o), node: node(id, 1)?, p: f(o, "cim:EnergyConsumer.p")?, q: f(o, "cim:EnergyConsumer.q")? }),
            "cim:LinearShuntCompensator" => {
                let n = node(id, 1)?;
                let k = kv(&n)?;
                let (gg, bb) = match (fo(o, "gs:Shunt.gMW")?, fo(o, "gs:Shunt.bMvar")?) { (Some(a), Some(c)) => (a, c), _ => (f(o, "cim:LinearShuntCompensator.gPerSection")? * k * k, f(o, "cim:LinearShuntCompensator.bPerSection")? * k * k) };
                m.shunts.push(Shunt { mrid: s(o, "cim:IdentifiedObject.mRID")?.into(), name: name(o), node: n, g: gg, b: bb });
            }
            _ => {}
        }
    }
    Ok(crate::diff::canonical(&m))
}

#[cfg(test)]
mod tests {
    use super::*;
    fn load(name: &str) -> Model {
        serde_json::from_slice(&std::fs::read(format!("{}/../../data/{name}.cim.json", env!("CARGO_MANIFEST_DIR"))).unwrap()).unwrap()
    }
    fn hdr() -> Header { Header { uuid_seed: "t".into(), created: iso(1_762_300_000_000), version: 1, description: "test".into(), authority_set: "http://example.invalid/mas".into() } }
    fn same(a: &Model, b: &Model) -> bool { crate::diff::entities(a) == crate::diff::entities(b) && crate::diff::scalars(a) == crate::diff::scalars(b) }

    #[test]
    fn iso_timestamps_are_right() {
        assert_eq!(iso(0), "1970-01-01T00:00:00Z");
        assert_eq!(iso(951_782_400_000), "2000-02-29T00:00:00Z");
        assert_eq!(iso(1_762_300_000_000), "2025-11-04T23:46:40Z");
    }

    #[test]
    fn identifiers_are_uuid_shaped_deterministic_and_distinct() {
        let a = rid("load", "L1");
        assert_eq!(a, rid("load", "L1"));
        assert_ne!(a, rid("gen", "L1"));
        assert_ne!(a, rid("load", "L2"));
        let u = &a[1..];
        assert_eq!(u.len(), 36);
        assert_eq!(&u[14..15], "5");
        assert!(["8", "9", "a", "b"].contains(&&u[19..20]));
    }

    #[test]
    fn full_model_round_trips_exactly_for_both_ieee_cases() {
        for name in ["ieee14", "ieee118"] {
            let m = load(name);
            let g = to_graph(&m);
            let xml = write_full(&g, &hdr());
            assert!(xml.contains("md:FullModel") && xml.contains("md:Model.version") && xml.contains("cim:Terminal.ConnectivityNode"));
            let (g2, ver) = parse_full(&xml).unwrap();
            assert_eq!(ver, Some(1));
            assert_eq!(g, g2, "{name}: graph changed through XML");
            let back = from_graph(&g2).unwrap();
            assert!(same(&back, &m), "{name}: model changed through CIMXML");
        }
    }

    #[test]
    fn export_is_deterministic_and_independent_of_element_order() {
        let m = load("ieee118");
        let mut r = m.clone();
        r.loads.reverse(); r.switches.reverse(); r.generators.reverse();
        assert_eq!(write_full(&to_graph(&m), &hdr()), write_full(&to_graph(&r), &hdr()));
    }

    #[test]
    fn standard_si_values_alone_recover_the_per_unit_model() {
        // strip the gs: lossless copies; the importer must rebuild from the standard CIM properties (tolerance 1e-9 relative)
        let m = load("ieee118");
        let mut g = to_graph(&m);
        for o in g.values_mut() {
            if o.class != "gs:ModelParameters" {
                o.props.retain(|k, _| !(k.starts_with("gs:Branch.") || k.starts_with("gs:Shunt.") || k == "gs:RegulatingControl.vsetPu" || k.starts_with("gs:VoltageLevel.")));
            }
        }
        let back = from_graph(&g).unwrap();
        let (a, b) = (crate::diff::entities(&m), crate::diff::entities(&back));
        assert_eq!(a.keys().collect::<Vec<_>>(), b.keys().collect::<Vec<_>>());
        let mut worst = 0f64;
        for (k, va) in &a {
            let vb = &b[k];
            for (f, x) in va.as_object().unwrap() {
                match (x.as_f64(), vb.get(f).and_then(|y| y.as_f64())) {
                    (Some(p), Some(q)) => { let e = (p - q).abs() / p.abs().max(1.0); worst = worst.max(e); assert!(e < 1e-9, "{k}.{f}: {p} vs {q}"); }
                    _ => assert_eq!(Some(x), vb.get(f), "{k}.{f}"),
                }
            }
        }
        println!("worst relative difference through SI units: {worst:e}");
    }

    #[test]
    fn difference_model_applies_to_give_the_new_model_and_refuses_the_wrong_base() {
        let a = load("ieee118");
        let mut ops = vec![];
        ops.push(serde_json::json!({"op":"set","kind":"load","mrid":a.loads[0].mrid,"fields":{"p":321.0}}));
        ops.push(serde_json::json!({"op":"set","kind":"acLineSegment","mrid":a.ac_line_segments[3].mrid,"fields":{"x":0.5}}));
        ops.push(serde_json::json!({"op":"remove","kind":"shunt","mrid":a.shunts[0].mrid}));
        ops.push(serde_json::json!({"op":"add","kind":"load","entity":{"mrid":"LD-NEW","name":"New","node":a.loads[1].node,"p":5.0,"q":1.0}}));
        let b = crate::diff::apply_ops(&a, &ops).unwrap();
        let (ga, gb) = (to_graph(&a), to_graph(&b));
        let xml = write_diff(&ga, &gb, &hdr());
        assert!(xml.contains("dm:forwardDifferences") && xml.contains("dm:reverseDifferences") && xml.contains("rdf:parseType=\"Statements\""));
        let applied = apply_diff(&ga, &xml).unwrap();
        assert_eq!(applied, gb);
        assert!(same(&from_graph(&applied).unwrap(), &b));
        // applying it to another version must fail loudly, not silently produce a hybrid
        let other = crate::diff::apply_ops(&a, &[serde_json::json!({"op":"set","kind":"load","mrid":a.loads[0].mrid,"fields":{"p":999.0}})]).unwrap();
        assert!(apply_diff(&to_graph(&other), &xml).is_err());
        // an empty difference between equal models is valid
        assert_eq!(apply_diff(&ga, &write_diff(&ga, &ga, &hdr())).unwrap(), ga);
    }

    #[test]
    fn hostile_xml_is_refused() {
        for bad in [
            "<?xml version=\"1.0\"?><!DOCTYPE x [<!ENTITY a \"aaaa\">]><rdf:RDF>&a;</rdf:RDF>",
            "<rdf:RDF><cim:Load rdf:ID=\"_a\"><cim:X>&unknown;</cim:X></cim:Load></rdf:RDF>",
            "<rdf:RDF><cim:Load rdf:ID=\"_a\"></cim:Load2></rdf:RDF>",
            "<rdf:RDF><cim:Load rdf:ID=\"_a\"",
            "<rdf:RDF><cim:Load/></rdf:RDF>",
            "<rdf:RDF><![CDATA[x]]></rdf:RDF>",
            "<rdf:RDF></rdf:RDF>",
            "<foo/>",
            "",
            "<rdf:RDF><a rdf:ID=\"x\"><p>1</p><p>2</p></a></rdf:RDF>",
        ] {
            assert!(parse_full(bad).is_err(), "accepted: {bad}");
        }
        let deep = format!("<rdf:RDF>{}{}</rdf:RDF>", "<a rdf:ID=\"x\">".repeat(20), "</a>".repeat(20));
        assert!(parse_full(&deep).is_err(), "deep nesting accepted");
    }

    #[test]
    fn parser_never_panics_on_mutated_documents() {
        let m = load("ieee14");
        let xml = write_full(&to_graph(&m), &hdr()).into_bytes();
        let mut seed = 99u64;
        for _ in 0..400 {
            let mut x = xml.clone();
            for _ in 0..3 {
                seed = seed.wrapping_mul(6364136223846793005).wrapping_add(1442695040888963407);
                let i = (seed >> 33) as usize % x.len();
                x[i] = (seed >> 20) as u8;
            }
            if let Ok(s) = String::from_utf8(x) {
                if let Ok((g, _)) = parse_full(&s) { let _ = from_graph(&g); }
            }
        }
    }

    #[test]
    fn special_characters_survive() {
        let mut m = load("ieee14");
        m.substations[0].name = "A&B <\"C\"> 'd' é 日本".into();
        let xml = write_full(&to_graph(&m), &hdr());
        let (g, _) = parse_full(&xml).unwrap();
        assert_eq!(from_graph(&g).unwrap().substations.iter().find(|s| s.mrid == m.substations[0].mrid).unwrap().name, m.substations[0].name);
    }
}
