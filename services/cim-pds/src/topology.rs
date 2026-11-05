//! Topology processor: node-breaker -> bus-branch.
//! Closed switches merge connectivity nodes; each merged set that contains a
//! busbar node becomes an electrical bus. Equipment on sets without a busbar
//! (e.g. behind an open breaker) is treated as out of service.
use crate::model::*;
use serde::Serialize;
use std::collections::{BTreeMap, HashMap};

#[derive(Serialize, Clone, Debug)]
pub struct Bus {
    pub idx: usize,
    pub name: String,
    pub substation: String,
    #[serde(rename = "baseKV")]
    pub base_kv: f64,
    /// 1=PQ, 2=PV, 3=REF, 4=isolated/dead (MATPOWER convention)
    #[serde(rename = "type")]
    pub bus_type: u8,
    pub pd: f64,
    pub qd: f64,
    pub gs: f64,
    pub bs: f64,
    pub island: usize,
    pub nodes: Vec<String>,
}

#[derive(Serialize, Clone, Debug)]
pub struct BrOut {
    pub mrid: String,
    pub kind: &'static str,
    pub from: usize,
    pub to: usize,
    pub r: f64,
    pub x: f64,
    pub b: f64,
    pub tap: f64,
    pub shift: f64,
    pub rate: f64,
}

#[derive(Serialize, Clone, Debug)]
pub struct GenOut {
    pub mrid: String,
    pub bus: usize,
    pub p: f64,
    pub q: f64,
    pub qmax: f64,
    pub qmin: f64,
    pub vset: f64,
    pub pmax: f64,
    pub pmin: f64,
    #[serde(rename = "costC2", skip_serializing_if = "Option::is_none")]
    pub cost_c2: Option<f64>,
    #[serde(rename = "costC1", skip_serializing_if = "Option::is_none")]
    pub cost_c1: Option<f64>,
    #[serde(rename = "costC0", skip_serializing_if = "Option::is_none")]
    pub cost_c0: Option<f64>,
}

#[derive(Serialize, Clone, Debug)]
pub struct LoadOut {
    pub mrid: String,
    pub bus: usize,
    pub p: f64,
    pub q: f64,
}

#[derive(Serialize, Clone, Debug)]
pub struct Island {
    pub id: usize,
    pub buses: Vec<usize>,
    #[serde(rename = "refBus")]
    pub ref_bus: Option<usize>,
    #[serde(rename = "hasSlack")]
    pub has_slack: bool,
    pub dead: bool,
    #[serde(rename = "loadMW")]
    pub load_mw: f64,
    #[serde(rename = "genMaxMW")]
    pub gen_max_mw: f64,
}

#[derive(Serialize, Clone, Debug)]
pub struct Dropped {
    pub mrid: String,
    pub kind: &'static str,
    pub reason: String,
}

#[derive(Serialize, Clone, Debug)]
pub struct BusBranch {
    #[serde(rename = "modelId")]
    pub model_id: String,
    pub version: u64,
    #[serde(rename = "baseMVA")]
    pub base_mva: f64,
    pub buses: Vec<Bus>,
    pub branches: Vec<BrOut>,
    pub gens: Vec<GenOut>,
    pub loads: Vec<LoadOut>,
    pub islands: Vec<Island>,
    pub dropped: Vec<Dropped>,
}

struct Uf(Vec<usize>);
impl Uf {
    fn new(n: usize) -> Self {
        Uf((0..n).collect())
    }
    fn find(&mut self, mut x: usize) -> usize {
        while self.0[x] != x {
            self.0[x] = self.0[self.0[x]];
            x = self.0[x];
        }
        x
    }
    fn union(&mut self, a: usize, b: usize) {
        let (ra, rb) = (self.find(a), self.find(b));
        if ra != rb {
            let (lo, hi) = if ra < rb { (ra, rb) } else { (rb, ra) };
            self.0[hi] = lo;
        }
    }
}

pub fn process(m: &Model, version: u64) -> BusBranch {
    let nidx: HashMap<&str, usize> = m
        .connectivity_nodes
        .iter()
        .enumerate()
        .map(|(i, n)| (n.mrid.as_str(), i))
        .collect();
    let mut uf = Uf::new(m.connectivity_nodes.len());
    for s in m.switches.iter().filter(|s| !s.open) {
        if let (Some(&a), Some(&b)) = (nidx.get(s.node1.as_str()), nidx.get(s.node2.as_str())) {
            uf.union(a, b);
        }
    }
    // busbar nodes per root
    let mut by_root: BTreeMap<usize, Vec<usize>> = BTreeMap::new();
    for (i, n) in m.connectivity_nodes.iter().enumerate() {
        if n.is_busbar() {
            let r = uf.find(i);
            by_root.entry(r).or_default().push(i);
        }
    }
    // deterministic order: by smallest busbar mrid in each set
    let mut sets: Vec<(usize, Vec<usize>)> = by_root.into_iter().collect();
    sets.sort_by(|a, b| {
        let ka = a.1.iter().map(|&i| &m.connectivity_nodes[i].mrid).min().unwrap();
        let kb = b.1.iter().map(|&i| &m.connectivity_nodes[i].mrid).min().unwrap();
        ka.cmp(kb)
    });
    let vl_of: HashMap<&str, &VoltageLevel> = m.voltage_levels.iter().map(|v| (v.mrid.as_str(), v)).collect();
    let root_bus: HashMap<usize, usize> = sets.iter().enumerate().map(|(bi, (r, _))| (*r, bi)).collect();

    let mut buses: Vec<Bus> = sets
        .iter()
        .enumerate()
        .map(|(bi, (_, members))| {
            let mut mem = members.clone();
            mem.sort_by(|&a, &b| m.connectivity_nodes[a].mrid.cmp(&m.connectivity_nodes[b].mrid));
            let first = &m.connectivity_nodes[mem[0]];
            let vl = vl_of.get(first.voltage_level.as_str());
            Bus {
                idx: bi,
                name: first.name.clone(),
                substation: vl.map(|v| v.substation.clone()).unwrap_or_default(),
                base_kv: vl.map(|v| v.nominal_kv).unwrap_or(0.0),
                bus_type: 1,
                pd: 0.0,
                qd: 0.0,
                gs: 0.0,
                bs: 0.0,
                island: 0,
                nodes: mem.iter().map(|&i| m.connectivity_nodes[i].mrid.clone()).collect(),
            }
        })
        .collect();

    let bus_of = |node: &str, uf: &mut Uf| -> Option<usize> {
        let i = *nidx.get(node)?;
        let r = uf.find(i);
        root_bus.get(&r).copied()
    };

    let mut dropped = vec![];
    let mut loads = vec![];
    for l in &m.loads {
        match bus_of(&l.node, &mut uf) {
            Some(b) => {
                buses[b].pd += l.p;
                buses[b].qd += l.q;
                loads.push(LoadOut { mrid: l.mrid.clone(), bus: b, p: l.p, q: l.q });
            }
            None => dropped.push(Dropped { mrid: l.mrid.clone(), kind: "load", reason: "not connected to a busbar".into() }),
        }
    }
    for s in &m.shunts {
        match bus_of(&s.node, &mut uf) {
            Some(b) => {
                buses[b].gs += s.g;
                buses[b].bs += s.b;
            }
            None => dropped.push(Dropped { mrid: s.mrid.clone(), kind: "shunt", reason: "not connected to a busbar".into() }),
        }
    }
    let mut gens = vec![];
    for g in &m.generators {
        if !g.in_service {
            dropped.push(Dropped { mrid: g.mrid.clone(), kind: "generator", reason: "out of service".into() });
            continue;
        }
        match bus_of(&g.node, &mut uf) {
            Some(b) => gens.push(GenOut {
                mrid: g.mrid.clone(), bus: b, p: g.p, q: g.q, qmax: g.qmax, qmin: g.qmin,
                vset: g.vset, pmax: g.pmax, pmin: g.pmin,
                cost_c2: g.cost_c2, cost_c1: g.cost_c1, cost_c0: g.cost_c0,
            }),
            None => dropped.push(Dropped { mrid: g.mrid.clone(), kind: "generator", reason: "not connected to a busbar".into() }),
        }
    }
    let mut branches = vec![];
    let mut add_branches = |list: &Vec<Branch>, kind: &'static str, uf: &mut Uf, buses: &Vec<Bus>| {
        let _ = buses;
        for br in list {
            let a = bus_of(&br.node1, uf);
            let b = bus_of(&br.node2, uf);
            match (a, b) {
                (Some(a), Some(b)) if a != b => branches.push(BrOut {
                    mrid: br.mrid.clone(), kind, from: a, to: b, r: br.r, x: br.x, b: br.bch,
                    tap: br.ratio.unwrap_or(0.0), shift: br.angle.unwrap_or(0.0), rate: br.rating,
                }),
                (Some(_), Some(_)) => dropped.push(Dropped { mrid: br.mrid.clone(), kind, reason: "both ends on same bus".into() }),
                _ => dropped.push(Dropped { mrid: br.mrid.clone(), kind, reason: "open-ended (breaker open)".into() }),
            }
        }
    };
    add_branches(&m.ac_line_segments, "line", &mut uf, &buses);
    add_branches(&m.transformers, "transformer", &mut uf, &buses);

    // islands (connected components over in-service branches)
    let n = buses.len();
    let mut adj: Vec<Vec<usize>> = vec![vec![]; n];
    for br in &branches {
        adj[br.from].push(br.to);
        adj[br.to].push(br.from);
    }
    let mut isl_of = vec![usize::MAX; n];
    let mut islands: Vec<Island> = vec![];
    for s in 0..n {
        if isl_of[s] != usize::MAX {
            continue;
        }
        let id = islands.len();
        let mut stack = vec![s];
        isl_of[s] = id;
        let mut members = vec![];
        while let Some(u) = stack.pop() {
            members.push(u);
            for &v in &adj[u] {
                if isl_of[v] == usize::MAX {
                    isl_of[v] = id;
                    stack.push(v);
                }
            }
        }
        members.sort();
        islands.push(Island { id, buses: members, ref_bus: None, has_slack: false, dead: false, load_mw: 0.0, gen_max_mw: 0.0 });
    }
    for b in buses.iter_mut() {
        b.island = isl_of[b.idx];
    }
    for isl in islands.iter_mut() {
        isl.load_mw = isl.buses.iter().map(|&b| buses[b].pd).sum();
        let mut best: Option<(usize, f64)> = None;
        for g in gens.iter().filter(|g| isl_of[g.bus] == isl.id) {
            isl.gen_max_mw += g.pmax;
            if best.map(|(_, p)| g.pmax > p).unwrap_or(true) {
                best = Some((g.bus, g.pmax));
            }
        }
        let slack_bus = isl.buses.iter().copied().find(|&b| buses[b].substation == m.slack_substation);
        if let Some(sb) = slack_bus {
            isl.ref_bus = Some(sb);
            isl.has_slack = true;
        } else if let Some((gb, _)) = best {
            isl.ref_bus = Some(gb); // island self-references to its largest generator
        } else {
            isl.dead = true;
        }
    }
    // bus types
    let gen_bus: std::collections::HashSet<usize> = gens.iter().map(|g| g.bus).collect();
    for b in buses.iter_mut() {
        let isl = &islands[b.island];
        b.bus_type = if isl.dead {
            4
        } else if isl.ref_bus == Some(b.idx) {
            3
        } else if gen_bus.contains(&b.idx) {
            2
        } else {
            1
        };
    }
    BusBranch { model_id: m.id.clone(), version, base_mva: m.base_mva, buses, branches, gens, loads, islands, dropped }
}

#[cfg(test)]
mod tests {
    use super::*;

    pub fn tiny() -> Model {
        let j = serde_json::json!({
          "id":"t","name":"tiny","baseMVA":100.0,"slackSubstation":"S1",
          "substations":[{"mrid":"S1","name":"S1"},{"mrid":"S2","name":"S2"}],
          "voltageLevels":[{"mrid":"V1","name":"V1","substation":"S1","nominalKV":132.0},{"mrid":"V2","name":"V2","substation":"S2","nominalKV":132.0}],
          "connectivityNodes":[
            {"mrid":"N1-BB","name":"bb1","voltageLevel":"V1"},{"mrid":"N2-BB","name":"bb2","voltageLevel":"V2"},
            {"mrid":"T1","name":"t1","voltageLevel":"V1"},{"mrid":"T2","name":"t2","voltageLevel":"V2"},
            {"mrid":"TG","name":"tg","voltageLevel":"V1"},{"mrid":"TL","name":"tl","voltageLevel":"V2"}],
          "switches":[
            {"mrid":"B1","name":"b1","node1":"N1-BB","node2":"T1","open":false},
            {"mrid":"B2","name":"b2","node1":"N2-BB","node2":"T2","open":false},
            {"mrid":"BG","name":"bg","node1":"N1-BB","node2":"TG","open":false},
            {"mrid":"BL","name":"bl","node1":"N2-BB","node2":"TL","open":false}],
          "acLineSegments":[{"mrid":"L1","name":"l1","r":0.01,"x":0.1,"node1":"T1","node2":"T2"}],
          "generators":[{"mrid":"G1","name":"g1","node":"TG","p":50.0,"q":0.0,"qmax":50.0,"qmin":-50.0,"vset":1.0,"pmax":100.0,"pmin":0.0}],
          "loads":[{"mrid":"LD1","name":"ld1","node":"TL","p":40.0,"q":10.0}]
        });
        serde_json::from_value(j).unwrap()
    }

    #[test]
    fn closed_network_is_one_island() {
        let bb = process(&tiny(), 1);
        assert_eq!(bb.buses.len(), 2);
        assert_eq!(bb.branches.len(), 1);
        assert_eq!(bb.islands.len(), 1);
        assert_eq!(bb.buses[0].bus_type, 3); // slack at S1
        assert_eq!(bb.buses[1].pd, 40.0);
    }

    #[test]
    fn opening_line_breaker_splits_into_dead_island() {
        let mut m = tiny();
        m.switches.iter_mut().find(|s| s.mrid == "B2").unwrap().open = true;
        let bb = process(&m, 2);
        assert_eq!(bb.branches.len(), 0);
        assert_eq!(bb.islands.len(), 2);
        let dead = bb.islands.iter().find(|i| i.dead).expect("dead island");
        assert!((dead.load_mw - 40.0).abs() < 1e-9);
        assert_eq!(bb.buses[1].bus_type, 4);
        assert!(bb.dropped.iter().any(|d| d.mrid == "L1"));
    }

    #[test]
    fn bus_coupler_merges_busbars() {
        let mut m = tiny();
        m.connectivity_nodes.push(ConnectivityNode { mrid: "N1B-BB".into(), name: "bb1b".into(), voltage_level: "V1".into(), kind: None });
        m.switches.push(Switch { mrid: "BC".into(), name: "coupler".into(), kind: "Breaker".into(), node1: "N1-BB".into(), node2: "N1B-BB".into(), open: false });
        assert_eq!(process(&m, 1).buses.len(), 2); // merged -> still 2 buses
        m.switches.last_mut().unwrap().open = true;
        assert_eq!(process(&m, 1).buses.len(), 3); // coupler open -> separate bus
    }
}

#[cfg(test)]
mod ieee_tests {
    use super::*;
    use crate::validate;

    fn load(name: &str) -> Model {
        let p = format!("{}/../../data/{name}.cim.json", env!("CARGO_MANIFEST_DIR"));
        serde_json::from_slice(&std::fs::read(p).expect("data file; run tools/make_cim_json.py")).unwrap()
    }

    #[test]
    fn ieee14_topology_matches_source_case() {
        let m = load("ieee14");
        let bb = process(&m, 1);
        assert_eq!(bb.buses.len(), 14);
        assert_eq!(bb.branches.len(), 20);
        assert_eq!(bb.islands.len(), 1);
        assert_eq!(bb.gens.len(), 5);
        let load_mw: f64 = bb.buses.iter().map(|b| b.pd).sum();
        assert!((load_mw - 259.0).abs() < 1e-6, "IEEE-14 total load is 259 MW, got {load_mw}");
        assert_eq!(bb.buses.iter().filter(|b| b.bus_type == 3).count(), 1);
        assert!(!validate::has_errors(&validate::validate(&m)));
    }

    #[test]
    fn ieee118_topology_matches_source_case() {
        let m = load("ieee118");
        let bb = process(&m, 1);
        assert_eq!(bb.buses.len(), 118);
        assert_eq!(bb.branches.len(), 186);
        assert_eq!(bb.islands.len(), 1);
        assert_eq!(bb.gens.len(), 54);
        let load_mw: f64 = bb.buses.iter().map(|b| b.pd).sum();
        assert!((load_mw - 4242.0).abs() < 1e-6, "IEEE-118 total load is 4242 MW, got {load_mw}");
        assert!(!validate::has_errors(&validate::validate(&m)));
    }

    #[test]
    fn pointlist_is_unique_per_ca() {
        let m = load("ieee118");
        let pts = crate::pointlist::generate(&m);
        let mut seen = std::collections::HashSet::new();
        for p in &pts {
            assert!(seen.insert((p.ca, p.ioa)), "duplicate (CA,IOA) {} {}", p.ca, p.ioa);
        }
        assert!(pts.len() > 1500);
    }

    #[test]
    fn pmu_points_continue_the_substation_counter() {
        use crate::pointlist::{generate_with, PmuCfg};
        let m = load("ieee118");
        let base = generate_with(&m, &[]);
        let ss = m.substations[0].mrid.clone();
        let cfg = [PmuCfg { substation: ss, prefix: "PMUX".into(), phasors: vec!["V1".into()], analogs: 1 },
                   PmuCfg { substation: "no-such".into(), prefix: "PMUY".into(), phasors: vec![], analogs: 0 }];
        let with = generate_with(&m, &cfg);
        assert_eq!(with.len(), base.len() + 5, "F, DF, V1.MAG, V1.ANG, A1 added; unknown substation skipped");
        let mut seen = std::collections::HashSet::new();
        for p in &with { assert!(seen.insert((p.ca, p.ioa)), "duplicate (CA,IOA)"); }
        assert!(with.iter().any(|p| p.tag == "PMUX.V1.ANG" && p.unit == "deg" && p.ioa == 4));
        assert!(base.iter().zip(with.iter()).all(|(a, b)| a.tag == b.tag && a.ioa == b.ioa), "existing points keep their addresses");
        assert!(with.iter().filter(|p| p.tag.starts_with("PMUX.")).all(|p| p.ca == crate::pointlist::PMU_CA_BASE), "PMU points live on their own common address");
    }
}
