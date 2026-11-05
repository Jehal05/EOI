//! Automated model validation. Errors block import/commit; warnings are advisory.
use crate::model::*;
use crate::topology;
use serde::Serialize;
use std::collections::{HashMap, HashSet};

#[derive(Serialize, Clone, Debug)]
pub struct Issue {
    pub severity: &'static str, // "error" | "warning" | "info"
    pub code: &'static str,
    pub element: String,
    pub message: String,
}

fn issue(sev: &'static str, code: &'static str, el: &str, msg: String) -> Issue {
    Issue { severity: sev, code, element: el.to_string(), message: msg }
}

pub fn validate(m: &Model) -> Vec<Issue> {
    let mut out = vec![];
    // duplicate mRIDs across all classes
    let mut seen: HashSet<&str> = HashSet::new();
    let ids = m
        .substations.iter().map(|x| x.mrid.as_str())
        .chain(m.voltage_levels.iter().map(|x| x.mrid.as_str()))
        .chain(m.connectivity_nodes.iter().map(|x| x.mrid.as_str()))
        .chain(m.switches.iter().map(|x| x.mrid.as_str()))
        .chain(m.ac_line_segments.iter().map(|x| x.mrid.as_str()))
        .chain(m.transformers.iter().map(|x| x.mrid.as_str()))
        .chain(m.generators.iter().map(|x| x.mrid.as_str()))
        .chain(m.loads.iter().map(|x| x.mrid.as_str()))
        .chain(m.shunts.iter().map(|x| x.mrid.as_str()));
    for id in ids {
        if !seen.insert(id) {
            out.push(issue("error", "DUP_MRID", id, format!("duplicate mRID {id}")));
        }
    }
    if m.base_mva <= 0.0 {
        out.push(issue("error", "BASE_MVA", &m.id, "baseMVA must be > 0".into()));
    }
    let ss: HashSet<&str> = m.substations.iter().map(|s| s.mrid.as_str()).collect();
    let vl: HashMap<&str, &VoltageLevel> = m.voltage_levels.iter().map(|v| (v.mrid.as_str(), v)).collect();
    let nodes: HashSet<&str> = m.connectivity_nodes.iter().map(|n| n.mrid.as_str()).collect();
    for v in &m.voltage_levels {
        if !ss.contains(v.substation.as_str()) {
            out.push(issue("error", "DANGLING_REF", &v.mrid, format!("voltage level references unknown substation {}", v.substation)));
        }
        if v.nominal_kv <= 0.0 {
            out.push(issue("error", "NOMINAL_KV", &v.mrid, "nominalKV must be > 0".into()));
        }
    }
    for n in &m.connectivity_nodes {
        if !vl.contains_key(n.voltage_level.as_str()) {
            out.push(issue("error", "DANGLING_REF", &n.mrid, format!("node references unknown voltage level {}", n.voltage_level)));
        }
    }
    let check_node = |owner: &str, node: &str, out: &mut Vec<Issue>| {
        if !nodes.contains(node) {
            out.push(issue("error", "DANGLING_REF", owner, format!("references unknown node {node}")));
        }
    };
    for s in &m.switches {
        check_node(&s.mrid, &s.node1, &mut out);
        check_node(&s.mrid, &s.node2, &mut out);
    }
    for b in m.ac_line_segments.iter().chain(m.transformers.iter()) {
        check_node(&b.mrid, &b.node1, &mut out);
        check_node(&b.mrid, &b.node2, &mut out);
        if b.r == 0.0 && b.x == 0.0 {
            out.push(issue("error", "ZERO_IMPEDANCE", &b.mrid, "branch has r = x = 0".into()));
        } else if b.x < 0.0 {
            out.push(issue("warning", "NEGATIVE_X", &b.mrid, "negative reactance (series capacitor/3-winding equivalent?)".into()));
        }
        if let Some(t) = b.ratio {
            if t <= 0.0 {
                out.push(issue("error", "BAD_TAP", &b.mrid, "tap ratio must be > 0".into()));
            }
        }
    }
    for g in &m.generators {
        check_node(&g.mrid, &g.node, &mut out);
        if g.pmin > g.pmax {
            out.push(issue("error", "GEN_LIMITS", &g.mrid, "pmin > pmax".into()));
        }
        if g.qmin > g.qmax {
            out.push(issue("error", "GEN_LIMITS", &g.mrid, "qmin > qmax".into()));
        }
        if g.in_service && (g.p > g.pmax + 1e-6 || g.p < g.pmin - 1e-6) && g.pmax > 0.0 {
            out.push(issue("warning", "GEN_DISPATCH", &g.mrid, format!("dispatch {:.1} MW outside [{:.1}, {:.1}]", g.p, g.pmin, g.pmax)));
        }
        if g.vset < 0.8 || g.vset > 1.2 {
            out.push(issue("warning", "GEN_VSET", &g.mrid, format!("voltage setpoint {} pu is unusual", g.vset)));
        }
    }
    for l in &m.loads {
        check_node(&l.mrid, &l.node, &mut out);
    }
    for s in &m.shunts {
        check_node(&s.mrid, &s.node, &mut out);
    }
    if !ss.contains(m.slack_substation.as_str()) {
        out.push(issue("error", "NO_SLACK", &m.id, format!("slackSubstation '{}' not found", m.slack_substation)));
    }
    // electrical checks (only if references are sound, to avoid noise)
    if !out.iter().any(|i| i.severity == "error") {
        let bb = topology::process(m, 0);
        if bb.islands.len() > 1 {
            out.push(issue("warning", "MULTI_ISLAND", &m.id, format!("model is split into {} electrical islands", bb.islands.len())));
        }
        for isl in &bb.islands {
            if isl.dead && isl.load_mw > 0.0 {
                out.push(issue("warning", "DEAD_ISLAND_LOAD", &m.id, format!("island {} has {:.1} MW load and no generation (de-energised)", isl.id, isl.load_mw)));
            }
            if !isl.dead && !isl.has_slack {
                out.push(issue("info", "ISLAND_AUTO_REF", &m.id, format!("island {} has no slack substation; largest generator used as reference", isl.id)));
            }
            if !isl.dead && isl.gen_max_mw < isl.load_mw {
                out.push(issue("warning", "ADEQUACY", &m.id, format!("island {}: generation capacity {:.0} MW < load {:.0} MW", isl.id, isl.gen_max_mw, isl.load_mw)));
            }
        }
        let open = m.switches.iter().filter(|s| s.open).count();
        if open > 0 {
            out.push(issue("info", "OPEN_SWITCHES", &m.id, format!("{open} switches open")));
        }
    }
    out
}

pub fn has_errors(issues: &[Issue]) -> bool {
    issues.iter().any(|i| i.severity == "error")
}
