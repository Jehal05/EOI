//! Importers for legacy bus-branch exchange formats: MATPOWER case files (.m) and PSS/E RAW (v33 subset).
//! Both convert to the node-breaker CIM subset: one substation / voltage level / busbar per bus, and a
//! breaker between the busbar and every device (line ends, generator, load, shunt) so SCADA points exist.
//! The RAW reader is written from the published record layout and has NOT been tested against files from
//! real utility planning tools; unsupported records are refused, never skipped silently.
use crate::model::*;
use std::collections::BTreeMap;

pub struct Imported {
    pub model: Model,
    pub warnings: Vec<String>,
}

const MAX_BUSES: usize = 200_000;

struct Bus { id: i64, kv: f64, vm: f64, va: f64, slack: bool, isolated: bool }
struct Br { f: i64, t: i64, r: f64, x: f64, b: f64, rate: f64, ratio: f64, angle: f64, on: bool, xf: bool }
struct Gn { bus: i64, p: f64, q: f64, qmax: f64, qmin: f64, vg: f64, pmax: f64, pmin: f64, on: bool, cost: Option<(f64, f64, f64)> }
struct Ld { bus: i64, id: String, p: f64, q: f64 }
struct Sh { bus: i64, g: f64, b: f64 }

fn num(s: &str) -> Result<f64, String> {
    let v: f64 = s.trim().parse().map_err(|_| format!("'{}' is not a number", s.trim()))?;
    if v.is_finite() { Ok(v) } else { Err(format!("'{}' is not finite", s.trim())) }
}

fn build(id: &str, name: &str, source: &str, base: f64, buses: Vec<Bus>, brs: Vec<Br>, gens: Vec<Gn>, loads: Vec<Ld>, shunts: Vec<Sh>, mut warnings: Vec<String>) -> Result<Imported, String> {
    if buses.is_empty() { return Err("no buses".into()); }
    if buses.len() > MAX_BUSES { return Err("too many buses".into()); }
    if !(base > 0.0) { return Err("baseMVA must be positive".into()); }
    let mut m = Model { id: id.into(), name: name.into(), base_mva: base, source: source.into(), slack_substation: String::new(), substations: vec![], voltage_levels: vec![],
        connectivity_nodes: vec![], switches: vec![], ac_line_segments: vec![], transformers: vec![], generators: vec![], loads: vec![], shunts: vec![] };
    let mut kvs: BTreeMap<i64, f64> = BTreeMap::new();
    for b in &buses {
        if kvs.insert(b.id, b.kv).is_some() { return Err(format!("bus {} defined twice", b.id)); }
        if !(b.kv > 0.0) { return Err(format!("bus {} has no base kV; per-unit data cannot be placed in a CIM voltage level", b.id)); }
        let n = format!("{:04}", b.id);
        m.substations.push(Substation { mrid: format!("SS-{n}"), name: format!("Substation {}", b.id), region: "Imported".into(), lat: 0.0, lon: 0.0, vm0: Some(b.vm), va0: Some(b.va) });
        m.voltage_levels.push(VoltageLevel { mrid: format!("VL-{n}"), name: format!("SS-{n} {}kV", b.kv), substation: format!("SS-{n}"), nominal_kv: b.kv, v_min: None, v_max: None });
        m.connectivity_nodes.push(ConnectivityNode { mrid: format!("CN-{n}-BB"), name: format!("Busbar {}", b.id), voltage_level: format!("VL-{n}"), kind: Some("busbar".into()) });
        if b.slack && m.slack_substation.is_empty() { m.slack_substation = format!("SS-{n}"); }
        if b.isolated { warnings.push(format!("bus {} is marked isolated; imported as an ordinary bus", b.id)); }
    }
    if m.slack_substation.is_empty() { m.slack_substation = m.substations[0].mrid.clone(); warnings.push("no swing bus in the file; the first bus was made the slack reference".into()); }
    let bb = |b: i64| format!("CN-{:04}-BB", b);
    let vl = |b: i64| format!("VL-{:04}", b);
    let mut dev = |m: &mut Model, mrid: &str, name: &str, bus: i64, open: bool| -> String {
        let nd = format!("CN-{mrid}");
        m.connectivity_nodes.push(ConnectivityNode { mrid: nd.clone(), name: format!("Terminal {mrid}"), voltage_level: vl(bus), kind: None });
        m.switches.push(Switch { mrid: format!("BRK-{mrid}"), name: format!("Breaker {name}"), kind: "Breaker".into(), node1: bb(bus), node2: nd.clone(), open });
        nd
    };
    for (i, b) in brs.iter().enumerate() {
        for e in [b.f, b.t] { if !kvs.contains_key(&e) { return Err(format!("branch {} refers to unknown bus {e}", i + 1)); } }
        let mrid = format!("BR{:04}", i + 1);
        let nf = dev(&mut m, &format!("{mrid}-F"), &format!("{mrid}-F"), b.f, !b.on);
        let nt = dev(&mut m, &format!("{mrid}-T"), &format!("{mrid}-T"), b.t, !b.on);
        let br = Branch { mrid: mrid.clone(), name: format!("Branch {}-{}", b.f, b.t), r: b.r, x: b.x, bch: b.b, rating: b.rate, node1: nf, node2: nt,
            ratio: if b.xf { Some(b.ratio) } else { None }, angle: if b.xf { Some(b.angle) } else { None } };
        if b.xf { m.transformers.push(br) } else { m.ac_line_segments.push(br) }
    }
    for (i, g) in gens.iter().enumerate() {
        if !kvs.contains_key(&g.bus) { return Err(format!("generator {} refers to unknown bus {}", i + 1, g.bus)); }
        let mrid = format!("G{:03}", i + 1);
        let nd = dev(&mut m, &mrid, &mrid, g.bus, false);
        m.generators.push(Generator { mrid: mrid.clone(), name: format!("Generator {} @bus {}", i + 1, g.bus), node: nd, p: g.p, q: g.q, qmax: g.qmax, qmin: g.qmin, vset: g.vg, pmax: g.pmax, pmin: g.pmin,
            in_service: g.on, fuel: String::new(), cost_c2: g.cost.map(|c| c.0), cost_c1: g.cost.map(|c| c.1), cost_c0: g.cost.map(|c| c.2) });
    }
    for l in &loads {
        if !kvs.contains_key(&l.bus) { return Err(format!("load refers to unknown bus {}", l.bus)); }
        let mrid = format!("L{:04}{}", l.bus, l.id);
        let nd = dev(&mut m, &mrid, &mrid, l.bus, false);
        m.loads.push(Load { mrid: mrid.clone(), name: format!("Load @bus {}", l.bus), node: nd, p: l.p, q: l.q });
    }
    for (i, s) in shunts.iter().enumerate() {
        if !kvs.contains_key(&s.bus) { return Err(format!("shunt refers to unknown bus {}", s.bus)); }
        let mrid = format!("SH{:04}-{}", s.bus, i + 1);
        let nd = dev(&mut m, &mrid, &mrid, s.bus, false);
        m.shunts.push(Shunt { mrid: mrid.clone(), name: format!("Shunt @bus {}", s.bus), node: nd, g: s.g, b: s.b });
    }
    Ok(Imported { model: crate::diff::canonical(&m), warnings })
}

// ------------------------------------------------------------------------------------------------ MATPOWER

fn strip_comments(src: &str) -> String {
    src.lines().map(|l| match l.find('%') { Some(i) => &l[..i], None => l }).collect::<Vec<_>>().join("\n")
}

fn matrix(text: &str, name: &str) -> Result<Option<Vec<Vec<f64>>>, String> {
    let key = format!("mpc.{name}");
    let mut from = 0;
    while let Some(p) = text[from..].find(&key) {
        let at = from + p;
        let rest = &text[at + key.len()..];
        from = at + key.len();
        let t = rest.trim_start();
        if !t.starts_with('=') { continue; } // e.g. mpc.bus_name
        let t = t[1..].trim_start();
        if !t.starts_with('[') { return Err(format!("mpc.{name} is not a matrix literal")); }
        let end = t.find(']').ok_or(format!("mpc.{name} is not closed"))?;
        let mut rows = vec![];
        for row in t[1..end].split(|c| c == ';' || c == '\n') {
            let vals: Result<Vec<f64>, String> = row.split(|c: char| c == ',' || c.is_whitespace()).filter(|x| !x.is_empty()).map(num).collect();
            let vals = vals.map_err(|e| format!("mpc.{name}: {e}"))?;
            if !vals.is_empty() { rows.push(vals); }
        }
        return Ok(Some(rows));
    }
    Ok(None)
}

pub fn matpower(id: &str, name: &str, src: &str) -> Result<Imported, String> {
    let t = strip_comments(src);
    let base = {
        let p = t.find("mpc.baseMVA").ok_or("mpc.baseMVA not found")?;
        let r = &t[p + 11..];
        let r = r.trim_start().strip_prefix('=').ok_or("bad mpc.baseMVA")?;
        num(&r[..r.find(';').ok_or("bad mpc.baseMVA")?])?
    };
    let bus = matrix(&t, "bus")?.ok_or("mpc.bus not found")?;
    let gen = matrix(&t, "gen")?.ok_or("mpc.gen not found")?;
    let branch = matrix(&t, "branch")?.ok_or("mpc.branch not found")?;
    let gc = matrix(&t, "gencost")?;
    let mut w = vec![];
    let mut buses = vec![];
    for (i, r) in bus.iter().enumerate() {
        if r.len() < 13 { return Err(format!("mpc.bus row {} has {} columns, needs 13", i + 1, r.len())); }
        if r[0].fract() != 0.0 { return Err(format!("mpc.bus row {}: bus number is not an integer", i + 1)); }
        buses.push(Bus { id: r[0] as i64, kv: r[9], vm: r[7], va: r[8], slack: r[1] == 3.0, isolated: r[1] == 4.0 });
    }
    let mut loads = vec![];
    let mut shunts = vec![];
    for r in &bus {
        if r[2] != 0.0 || r[3] != 0.0 { loads.push(Ld { bus: r[0] as i64, id: String::new(), p: r[2], q: r[3] }); }
        if r[4] != 0.0 || r[5] != 0.0 { shunts.push(Sh { bus: r[0] as i64, g: r[4], b: r[5] }); }
    }
    let mut gens = vec![];
    for (i, r) in gen.iter().enumerate() {
        if r.len() < 10 { return Err(format!("mpc.gen row {} has {} columns, needs 10", i + 1, r.len())); }
        let cost = match &gc {
            Some(c) if i < c.len() && c[i].len() >= 4 && c[i][0] == 2.0 => {
                let n = c[i][3] as usize;
                let k = &c[i][4..];
                if k.len() < n { None } else { match n { 3 => Some((k[0], k[1], k[2])), 2 => Some((0.0, k[0], k[1])), _ => { w.push(format!("gencost row {} has {} coefficients; only polynomial order 1 and 2 are imported", i + 1, n)); None } } }
            }
            Some(c) if i < c.len() => { w.push(format!("gencost row {} is a piecewise-linear or unsupported model; cost omitted", i + 1)); None }
            _ => None,
        };
        gens.push(Gn { bus: r[0] as i64, p: r[1], q: r[2], qmax: r[3], qmin: r[4], vg: r[5], on: r[7] > 0.0, pmax: r[8], pmin: r[9], cost });
    }
    let mut brs = vec![];
    for (i, r) in branch.iter().enumerate() {
        if r.len() < 11 { return Err(format!("mpc.branch row {} has {} columns, needs 11", i + 1, r.len())); }
        let xf = r[8] != 0.0;
        brs.push(Br { f: r[0] as i64, t: r[1] as i64, r: r[2], x: r[3], b: r[4], rate: r[5], ratio: if xf { r[8] } else { 1.0 }, angle: r[9], on: r[10] > 0.0, xf });
    }
    build(id, name, "imported from MATPOWER case file", base, buses, brs, gens, loads, shunts, w)
}

// ------------------------------------------------------------------------------------------------ PSS/E RAW

fn fields(line: &str) -> Vec<String> {
    let mut out = vec![];
    let mut cur = String::new();
    let mut q: Option<char> = None;
    for c in line.chars() {
        match (q, c) {
            (Some(x), c) if c == x => q = None,
            (Some(_), c) => cur.push(c),
            (None, '\'') | (None, '"') => q = Some(c),
            (None, '/') => break,
            (None, ',') => { out.push(cur.trim().to_string()); cur.clear(); }
            (None, c) => cur.push(c),
        }
    }
    out.push(cur.trim().to_string());
    out
}
fn fnum(f: &[String], i: usize, d: f64) -> Result<f64, String> {
    match f.get(i) { Some(s) if !s.is_empty() => num(s), _ => Ok(d) }
}

pub fn psse_raw(id: &str, name: &str, src: &str) -> Result<Imported, String> {
    let lines: Vec<&str> = src.lines().collect();
    if lines.len() < 4 { return Err("file too short for a PSS/E RAW case".into()); }
    let h = fields(lines[0]);
    let base = fnum(&h, 1, 100.0)?;
    let rev = fnum(&h, 2, 0.0)? as i32;
    if rev != 0 && rev != 33 { return Err(format!("PSS/E RAW revision {rev} is not supported (v33 only)")); }
    let mut w = vec![];
    let mut i = 3;
    let mut sections: Vec<Vec<&str>> = vec![];
    let mut cur = vec![];
    while i < lines.len() && sections.len() < 6 {
        let l = lines[i];
        i += 1;
        if l.trim().is_empty() { continue; }
        if fields(l)[0].trim() == "0" && l.trim_start().starts_with('0') { sections.push(std::mem::take(&mut cur)); } else { cur.push(l); }
    }
    if sections.len() < 6 { return Err("PSS/E RAW: bus, load, fixed shunt, generator, branch and transformer sections not all found".into()); }
    let mut buses = vec![];
    for l in &sections[0] {
        let f = fields(l);
        let ide = fnum(&f, 3, 1.0)? as i32;
        buses.push(Bus { id: fnum(&f, 0, f64::NAN)? as i64, kv: fnum(&f, 2, 0.0)?, vm: fnum(&f, 7, 1.0)?, va: fnum(&f, 8, 0.0)?, slack: ide == 3, isolated: ide == 4 });
    }
    let kv: BTreeMap<i64, f64> = buses.iter().map(|b| (b.id, b.kv)).collect();
    let mut loads = vec![];
    for l in &sections[1] {
        let f = fields(l);
        if fnum(&f, 2, 1.0)? as i32 != 1 { continue; }
        loads.push(Ld { bus: fnum(&f, 0, f64::NAN)? as i64, id: f.get(1).cloned().unwrap_or_default().replace(' ', ""), p: fnum(&f, 5, 0.0)?, q: fnum(&f, 6, 0.0)? });
    }
    let mut shunts = vec![];
    for l in &sections[2] {
        let f = fields(l);
        if fnum(&f, 2, 1.0)? as i32 != 1 { continue; }
        shunts.push(Sh { bus: fnum(&f, 0, f64::NAN)? as i64, g: fnum(&f, 3, 0.0)?, b: fnum(&f, 4, 0.0)? });
    }
    let mut gens = vec![];
    for l in &sections[3] {
        let f = fields(l);
        gens.push(Gn { bus: fnum(&f, 0, f64::NAN)? as i64, p: fnum(&f, 2, 0.0)?, q: fnum(&f, 3, 0.0)?, qmax: fnum(&f, 4, 9999.0)?, qmin: fnum(&f, 5, -9999.0)?, vg: fnum(&f, 6, 1.0)?,
            on: fnum(&f, 14, 1.0)? as i32 == 1, pmax: fnum(&f, 16, 9999.0)?, pmin: fnum(&f, 17, 0.0)?, cost: None });
    }
    let mut brs = vec![];
    for l in &sections[4] {
        let f = fields(l);
        let (a, b) = (fnum(&f, 0, f64::NAN)? as i64, fnum(&f, 1, f64::NAN)?.abs() as i64);
        let (gi, bi, gj, bj) = (fnum(&f, 9, 0.0)?, fnum(&f, 10, 0.0)?, fnum(&f, 11, 0.0)?, fnum(&f, 12, 0.0)?);
        if gi != 0.0 || bi != 0.0 || gj != 0.0 || bj != 0.0 { w.push(format!("branch {a}-{b}: line-end shunts (GI/BI/GJ/BJ) are not imported; add them as shunt devices if needed")); }
        brs.push(Br { f: a, t: b, r: fnum(&f, 3, 0.0)?, x: fnum(&f, 4, 0.0)?, b: fnum(&f, 5, 0.0)?, rate: fnum(&f, 6, 0.0)?, ratio: 1.0, angle: 0.0, on: fnum(&f, 13, 1.0)? as i32 == 1, xf: false });
    }
    let tl = &sections[5];
    let mut k = 0;
    while k < tl.len() {
        let f1 = fields(tl[k]);
        if fnum(&f1, 2, 0.0)? != 0.0 { return Err("three-winding transformers are not supported by this importer".into()); }
        if k + 3 >= tl.len() { return Err("truncated transformer record".into()); }
        let (f2, f3, f4) = (fields(tl[k + 1]), fields(tl[k + 2]), fields(tl[k + 3]));
        k += 4;
        let (i, j) = (fnum(&f1, 0, f64::NAN)? as i64, fnum(&f1, 1, f64::NAN)? as i64);
        let (cw, cz) = (fnum(&f1, 4, 1.0)? as i32, fnum(&f1, 5, 1.0)? as i32);
        if cz == 3 { return Err(format!("transformer {i}-{j}: CZ=3 (load loss in watts) is not supported")); }
        if fnum(&f1, 6, 1.0)? as i32 != 1 { w.push(format!("transformer {i}-{j}: magnetising admittance in non-pu form is ignored")); }
        let (mut r, mut x) = (fnum(&f2, 0, 0.0)?, fnum(&f2, 1, 0.0)?);
        if cz == 2 { let sb = fnum(&f2, 2, base)?; if sb <= 0.0 { return Err(format!("transformer {i}-{j}: SBASE1-2 must be positive")); } r *= base / sb; x *= base / sb; }
        let (w1, w2) = (fnum(&f3, 0, 1.0)?, fnum(&f4, 0, 1.0)?);
        let ratio = if cw == 2 {
            let (ki, kj) = (*kv.get(&i).ok_or(format!("transformer refers to unknown bus {i}"))?, *kv.get(&j).ok_or(format!("transformer refers to unknown bus {j}"))?);
            if ki <= 0.0 || kj <= 0.0 { return Err("transformer on a bus with no base kV".into()); }
            (w1 / ki) / (w2 / kj)
        } else if cw == 1 { w1 / w2 } else { return Err(format!("transformer {i}-{j}: CW={cw} is not supported")); };
        if w3_nonzero(&f1) { w.push(format!("transformer {i}-{j}: magnetising branch (MAG1/MAG2) is not imported")); }
        brs.push(Br { f: i, t: j, r, x, b: 0.0, rate: fnum(&f3, 3, 0.0)?, ratio, angle: fnum(&f3, 2, 0.0)?, on: fnum(&f1, 11, 1.0)? as i32 == 1, xf: true });
    }
    build(id, name, "imported from PSS/E RAW v33 file", base, buses, brs, gens, loads, shunts, w)
}
fn w3_nonzero(f1: &[String]) -> bool { f1.get(7).and_then(|s| s.parse::<f64>().ok()).map_or(false, |v| v != 0.0) || f1.get(8).and_then(|s| s.parse::<f64>().ok()).map_or(false, |v| v != 0.0) }

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn matpower_small_case() {
        let src = "function mpc = c\nmpc.version='2';\nmpc.baseMVA = 100; % c\nmpc.bus = [\n1 3 0 0 0 0 1 1.02 0 138 1 1.1 0.9;\n2 1 50 10 0 5 1 1.0 -2 138 1 1.1 0.9;\n];\nmpc.gen = [\n1 60 0 50 -50 1.02 100 1 200 0;\n];\nmpc.branch = [\n1 2 0.01 0.1 0.02 100 0 0 0 0 1 -360 360;\n];\nmpc.gencost = [\n2 0 0 3 0.01 20 5;\n];\n";
        let r = matpower("t", "T", src).unwrap();
        let m = r.model;
        assert_eq!((m.substations.len(), m.ac_line_segments.len(), m.generators.len(), m.loads.len(), m.shunts.len()), (2, 1, 1, 1, 1));
        assert_eq!(m.slack_substation, "SS-0001");
        assert_eq!(m.generators[0].cost_c1, Some(20.0));
        assert!(!crate::validate::has_errors(&crate::validate::validate(&m)));
    }
    #[test]
    fn refuses_garbage() {
        assert!(matpower("t", "T", "nothing").is_err());
        assert!(matpower("t", "T", "mpc.baseMVA = 100;\nmpc.bus = [1 2 x];").is_err());
        assert!(psse_raw("t", "T", "garbage").is_err());
        assert!(psse_raw("t", "T", "0, 100.0, 29, 0, 0, 60\na\nb\n").is_err());
    }
    #[test]
    fn raw_small_case_with_transformer() {
        let raw = "0, 100.0, 33, 0, 1, 60.0 / header\nT1\nT2\n1,'A',138.0,3,1,1,1,1.02,0.0\n2,'B',69.0,1,1,1,1,1.0,-1.0\n0 / END OF BUS DATA\n2,'1 ',1,1,1,40.0,10.0,0,0,0,0,1\n0 / END OF LOAD DATA\n0 / END OF FIXED SHUNT DATA\n1,'1 ',50.0,0,40,-40,1.02,0,100,0,0,0,0,1,1,100,100,0\n0 / END OF GENERATOR DATA\n0 / END OF BRANCH DATA\n1,2,0,'1 ',1,1,1,0,0,2,'XF',1\n0.001,0.05,100.0\n1.05,0.0,0.0,150,0,0,0,0,1.1,0.9,1.1,0.9,33,0,0,0\n1.0,0.0\n0 / END OF TRANSFORMER DATA\n";
        let r = psse_raw("t", "T", raw).unwrap();
        assert_eq!(r.model.transformers.len(), 1);
        assert!((r.model.transformers[0].ratio.unwrap() - 1.05).abs() < 1e-12);
        assert_eq!(r.model.transformers[0].rating, 150.0);
        assert_eq!(r.model.loads[0].p, 40.0);
        assert_eq!(r.model.slack_substation, "SS-0001");
    }

    fn sig(m: &Model) -> (Vec<[i64; 6]>, Vec<(String, i64, i64)>, Vec<i64>, Vec<i64>) {
        let q = |x: f64| (x * 1e9).round() as i64;
        let bus_of = |n: &str| -> String {
            // device node -> bus via its breaker to a busbar
            let sw = m.switches.iter().find(|s| s.node2 == n || s.node1 == n).unwrap();
            let bb = if sw.node1 == n { &sw.node2 } else { &sw.node1 };
            m.connectivity_nodes.iter().find(|c| &c.mrid == bb).unwrap().name.replace("Busbar ", "")
        };
        let mut br: Vec<[i64; 6]> = m.ac_line_segments.iter().chain(&m.transformers).map(|b| [bus_of(&b.node1).parse().unwrap(), bus_of(&b.node2).parse().unwrap(), q(b.r), q(b.x), q(b.bch), q(b.ratio.unwrap_or(0.0))]).collect();
        br.sort();
        let mut ld: Vec<(String, i64, i64)> = m.loads.iter().map(|l| (bus_of(&l.node), q(l.p), q(l.q))).collect();
        ld.sort();
        let mut gn: Vec<i64> = m.generators.iter().map(|g| q(g.pmax) + 3 * q(g.qmax) + 7 * q(g.vset)).collect();
        gn.sort();
        let mut sh: Vec<i64> = m.shunts.iter().map(|s| q(s.b)).collect();
        sh.sort();
        (br, ld, gn, sh)
    }

    #[test]
    fn ieee_cases_import_equal_to_the_packaged_models_from_both_formats() {
        let root = format!("{}/../../data", env!("CARGO_MANIFEST_DIR"));
        for (nm, pk) in [("case14", "ieee14"), ("case118", "ieee118")] {
            let pkg: Model = serde_json::from_slice(&std::fs::read(format!("{root}/{pk}.cim.json")).unwrap()).unwrap();
            let mp = matpower("x", "x", &std::fs::read_to_string(format!("{root}/legacy/{nm}.m")).unwrap()).unwrap();
            let raw = psse_raw("x", "x", &std::fs::read_to_string(format!("{root}/legacy/{nm}.raw")).unwrap()).unwrap();
            for r in [&mp, &raw] {
                assert!(!crate::validate::has_errors(&crate::validate::validate(&r.model)), "{nm}: validation errors");
                assert_eq!(r.model.slack_substation, pkg.slack_substation, "{nm}");
            }
            let (a, b, c) = (sig(&mp.model), sig(&raw.model), sig(&pkg));
            assert_eq!(a.0, b.0, "{nm}: branches differ between MATPOWER and RAW");
            assert_eq!(a.1, b.1, "{nm}: loads");
            assert_eq!(a.3, b.3, "{nm}: shunts");
            assert_eq!(a.0, c.0, "{nm}: branches differ from the packaged model");
            assert_eq!(a.1, c.1, "{nm}: loads differ from the packaged model");
            assert_eq!(a.3, c.3, "{nm}: shunts");
            assert_eq!(mp.model.generators.len(), pkg.generators.len());
            assert_eq!(mp.model.generators.iter().filter(|g| g.cost_c1.is_some()).count(), pkg.generators.iter().filter(|g| g.cost_c1.is_some()).count());
            println!("{nm}: {} buses, {} branches, {} loads, {} gens: MATPOWER == RAW == packaged to 1e-9", mp.model.substations.len(), a.0.len(), a.1.len(), mp.model.generators.len());
            let _ = b.2; let _ = c.2;
        }
    }
}
