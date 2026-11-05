//! Model-driven SCADA point list: derived from the CIM model so engineering
//! effort is never repeated. One IEC 104 Common Address (CA) per substation.
use crate::model::*;
use serde::Serialize;
use std::collections::HashMap;

#[derive(Serialize, Clone, Debug)]
pub struct Point {
    pub ca: u16,
    pub ioa: u32,
    pub tag: String,
    pub element: String,
    pub substation: String,
    pub kind: &'static str, // "status" | "analog"
    pub quantity: &'static str,
    pub unit: &'static str,
    pub low: Option<f64>,
    pub high: Option<f64>,
}

/// Phasor measurement units attached to a substation. NOT part of the CIM model (it has no PMU class): an engineering overlay read
/// once from the JSON file named by CIM_PMU_FILE: `[{"substation":"SS-0026","prefix":"PMU26","phasors":["V1","I1"],"analogs":0}]`.
#[derive(serde::Deserialize, Clone, Debug)]
pub struct PmuCfg {
    pub substation: String,
    pub prefix: String,
    pub phasors: Vec<String>,
    #[serde(default)]
    pub analogs: usize,
}

/// First common address given to PMU streams (substations use 1..N).
pub const PMU_CA_BASE: u16 = 9001;

fn pmu_cfg() -> &'static Vec<PmuCfg> {
    static C: std::sync::OnceLock<Vec<PmuCfg>> = std::sync::OnceLock::new();
    C.get_or_init(|| match std::env::var("CIM_PMU_FILE") {
        Ok(f) if !f.is_empty() => match std::fs::read_to_string(&f).map_err(|e| e.to_string()).and_then(|t| serde_json::from_str::<Vec<PmuCfg>>(&t).map_err(|e| e.to_string())) {
            Ok(v) => v,
            Err(e) => {
                eprintln!("CIM_PMU_FILE {f}: {e}; PMU points NOT added");
                vec![]
            }
        },
        _ => vec![],
    })
}

pub fn generate(m: &Model) -> Vec<Point> {
    generate_with(m, pmu_cfg())
}

pub fn generate_with(m: &Model, pmus: &[PmuCfg]) -> Vec<Point> {
    let ca_of: HashMap<&str, u16> = m.substations.iter().enumerate().map(|(i, s)| (s.mrid.as_str(), (i + 1) as u16)).collect();
    let vl: HashMap<&str, &VoltageLevel> = m.voltage_levels.iter().map(|v| (v.mrid.as_str(), v)).collect();
    let node_ss: HashMap<&str, &str> = m
        .connectivity_nodes
        .iter()
        .filter_map(|n| vl.get(n.voltage_level.as_str()).map(|v| (n.mrid.as_str(), v.substation.as_str())))
        .collect();
    let mut next_status: HashMap<u16, u32> = HashMap::new();
    let mut next_analog: HashMap<u16, u32> = HashMap::new();
    let mut pts = vec![];
    let mut add = |pts: &mut Vec<Point>, ss: &str, tag: String, el: &str, kind: &'static str, q: &'static str, unit: &'static str, low: Option<f64>, high: Option<f64>| {
        let Some(&ca) = ca_of.get(ss) else { return };
        let ctr = if kind == "status" { next_status.entry(ca).or_insert(1) } else { next_analog.entry(ca).or_insert(1001) };
        let ioa = *ctr;
        *ctr += 1;
        pts.push(Point { ca, ioa, tag, element: el.to_string(), substation: ss.to_string(), kind, quantity: q, unit, low, high });
    };
    for s in &m.switches {
        if let Some(ss) = node_ss.get(s.node1.as_str()) {
            add(&mut pts, ss, format!("{}.STATE", s.mrid), &s.mrid, "status", "STATE", "", None, None);
        }
    }
    for n in m.connectivity_nodes.iter().filter(|n| n.is_busbar()) {
        let lvl = vl.get(n.voltage_level.as_str());
        let kv = lvl.map(|v| v.nominal_kv).unwrap_or(0.0);
        let (lo, hi) = (lvl.and_then(|v| v.v_min).unwrap_or(0.95), lvl.and_then(|v| v.v_max).unwrap_or(1.05));
        if let Some(ss) = node_ss.get(n.mrid.as_str()) {
            add(&mut pts, ss, format!("{}.V", n.mrid), &n.mrid, "analog", "V", "kV", Some(lo * kv), Some(hi * kv));
        }
    }
    for b in m.ac_line_segments.iter().chain(m.transformers.iter()) {
        for (end, node) in [("F", &b.node1), ("T", &b.node2)] {
            if let Some(ss) = node_ss.get(node.as_str()) {
                let hi = if b.rating > 0.0 { Some(b.rating) } else { None };
                add(&mut pts, ss, format!("{}-{}.P", b.mrid, end), &b.mrid, "analog", "P", "MW", hi.map(|h| -h), hi);
                add(&mut pts, ss, format!("{}-{}.Q", b.mrid, end), &b.mrid, "analog", "Q", "MVAr", None, None);
            }
        }
    }
    for g in &m.generators {
        if let Some(ss) = node_ss.get(g.node.as_str()) {
            add(&mut pts, ss, format!("{}.P", g.mrid), &g.mrid, "analog", "P", "MW", Some(g.pmin), Some(g.pmax));
            add(&mut pts, ss, format!("{}.Q", g.mrid), &g.mrid, "analog", "Q", "MVAr", Some(g.qmin), Some(g.qmax));
        }
    }
    for l in &m.loads {
        if let Some(ss) = node_ss.get(l.node.as_str()) {
            add(&mut pts, ss, format!("{}.P", l.mrid), &l.mrid, "analog", "P", "MW", None, None);
            add(&mut pts, ss, format!("{}.Q", l.mrid), &l.mrid, "analog", "Q", "MVAr", None, None);
        }
    }
    for (i, u) in pmus.iter().enumerate() {
        // Each PMU gets its own common address (PMU_CA_BASE + index) so its points never collide with the substation's IEC 104 data;
        // tag names: <prefix>.F (Hz), .DF (Hz/s), .<phasor>.MAG, .<phasor>.ANG (degrees), .A<n>. Unknown substations are skipped.
        let ss = u.substation.as_str();
        if !ca_of.contains_key(ss) {
            continue;
        }
        let ca = PMU_CA_BASE + i as u16;
        let mut ioa = 1u32;
        let mut put = |tag: String, q: &'static str, unit: &'static str| {
            pts.push(Point { ca, ioa, tag, element: u.prefix.clone(), substation: ss.to_string(), kind: "analog", quantity: q, unit, low: None, high: None });
            ioa += 1;
        };
        put(format!("{}.F", u.prefix), "F", "Hz");
        put(format!("{}.DF", u.prefix), "DF", "Hz/s");
        for ph in &u.phasors {
            put(format!("{}.{}.MAG", u.prefix, ph), "PHMAG", "");
            put(format!("{}.{}.ANG", u.prefix, ph), "PHANG", "deg");
        }
        for j in 0..u.analogs {
            put(format!("{}.A{}", u.prefix, j + 1), "AN", "");
        }
    }
    pts
}
