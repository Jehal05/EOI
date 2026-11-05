//! Deterministic mutation fuzzing of the parsers that read untrusted input (CIMXML, difference model, MATPOWER, PSS/E RAW,
//! token verification). Not coverage-guided (no cargo-fuzz / nightly toolchain here): seeds are valid documents, each
//! iteration applies 1-8 random edits (bit flip, byte replace, delete a span, duplicate a span, truncate, splice from
//! another seed, insert a tag-like fragment) and the parser must return (Ok or Err) without panicking, and within a time
//! limit. Fixed PRNG seed, so a failure is reproducible; the failing input is written to /tmp before the panic propagates.
use crate::{cimxml, legacy};

struct Rng(u64);
impl Rng {
    fn next(&mut self) -> u64 { self.0 ^= self.0 << 13; self.0 ^= self.0 >> 7; self.0 ^= self.0 << 17; self.0 }
    fn below(&mut self, n: usize) -> usize { if n == 0 { 0 } else { (self.next() % n as u64) as usize } }
}

const FRAGMENTS: &[&str] = &["<", ">", "</", "<!DOCTYPE x [<!ENTITY a \"b\">]>", "<![CDATA[", "&amp;", "&#x0;", "&#1114112;", "<?xml", "rdf:ID=\"", "\"", "/>",
    "<cim:", "xmlns:", "-1e999", "NaN", "999999999999999999999", "\u{0}", "\n\n\n", "<a><a><a><a><a><a><a><a><a><a><a><a>"];

fn mutate(r: &mut Rng, seed: &[u8], other: &[u8]) -> Vec<u8> {
    let mut b = seed.to_vec();
    for _ in 0..=r.below(8) {
        if b.is_empty() { b.push(b'<'); }
        match r.below(7) {
            0 => { let i = r.below(b.len()); b[i] ^= 1 << r.below(8); }
            1 => { let i = r.below(b.len()); b[i] = r.next() as u8; }
            2 => { let i = r.below(b.len()); let n = 1 + r.below(64); let e = (i + n).min(b.len()); b.drain(i..e); }
            3 => { let i = r.below(b.len()); let n = 1 + r.below(256); let e = (i + n).min(b.len()); let s: Vec<u8> = b[i..e].to_vec(); let at = r.below(b.len()); for (k, x) in s.into_iter().enumerate() { b.insert(at + k, x); } }
            4 => { let n = r.below(b.len()); b.truncate(n); }
            5 => { if !other.is_empty() { let i = r.below(other.len()); let n = 1 + r.below(512); let e = (i + n).min(other.len()); let at = r.below(b.len() + 1); let s = other[i..e].to_vec(); for (k, x) in s.into_iter().enumerate() { b.insert(at + k, x); } } }
            _ => { let f = FRAGMENTS[r.below(FRAGMENTS.len())].as_bytes(); let at = r.below(b.len() + 1); for (k, x) in f.iter().enumerate() { b.insert(at + k, *x); } }
        }
        if b.len() > 2_000_000 { b.truncate(2_000_000); }
    }
    b
}

fn run(name: &str, seeds: &[Vec<u8>], iters: usize, f: impl Fn(&str) + std::panic::RefUnwindSafe) {
    let mut r = Rng(0x9E37_79B9_7F4A_7C15 ^ name.len() as u64);
    let t0 = std::time::Instant::now();
    let mut slowest = 0u128;
    for i in 0..iters {
        let s = &seeds[r.below(seeds.len())]; let o = &seeds[r.below(seeds.len())];
        let m = mutate(&mut r, s, o);
        let text = String::from_utf8_lossy(&m).into_owned();
        let t = std::time::Instant::now();
        let res = std::panic::catch_unwind(|| f(&text));
        let ms = t.elapsed().as_millis();
        slowest = slowest.max(ms);
        if res.is_err() || ms > 5_000 {
            let p = format!("/tmp/fuzz-fail-{name}-{i}.bin"); let _ = std::fs::write(&p, &text);
            panic!("{name}: {} on iteration {i} ({} bytes, {ms} ms), input saved to {p}", if res.is_err() { "panic" } else { "too slow" }, text.len());
        }
    }
    println!("fuzz {name}: {iters} inputs in {:.1} s, slowest {slowest} ms, no panic", t0.elapsed().as_secs_f64());
}

fn data(rel: &str) -> Vec<u8> { std::fs::read(format!("{}/../../data/{rel}", env!("CARGO_MANIFEST_DIR"))).unwrap() }

#[test]
fn fuzz_cimxml_and_difference_model() {
    let load = |n: &str| -> crate::model::Model { serde_json::from_slice(&data(&format!("{n}.cim.json"))).unwrap() };
    let h = cimxml::Header { uuid_seed: "f".into(), created: cimxml::iso(1_762_300_000_000), version: 1, description: "fuzz".into(), authority_set: "http://example.invalid/mas".into() };
    let g14 = cimxml::to_graph(&load("ieee14"));
    let mut m2 = load("ieee14"); if let Some(l) = m2.loads.first_mut() { l.p += 1.0; }
    let g2 = cimxml::to_graph(&m2);
    let full = cimxml::write_full(&g14, &h);
    let diff = cimxml::write_diff(&g14, &g2, &h);
    let iters: usize = std::env::var("FUZZ_ITERS").ok().and_then(|v| v.parse().ok()).unwrap_or(3000);
    run("cimxml-full", &[full.clone().into_bytes(), diff.clone().into_bytes()], iters, |s| {
        if let Ok((g, _)) = cimxml::parse_full(s) { let _ = cimxml::from_graph(&g); }
    });
    let base = g14.clone();
    run("cimxml-diff", &[diff.into_bytes(), full.into_bytes()], iters, move |s| { let _ = cimxml::apply_diff(&base, s); });
}

#[test]
fn fuzz_legacy_importers() {
    let iters: usize = std::env::var("FUZZ_ITERS").ok().and_then(|v| v.parse().ok()).unwrap_or(5000);
    run("matpower", &[data("legacy/case14.m"), data("legacy/case118.m")], iters, |s| { let _ = legacy::matpower("f", "f", s); });
    run("psse-raw", &[data("legacy/case14.raw"), data("legacy/case118.raw")], iters, |s| { let _ = legacy::psse_raw("f", "f", s); });
}

#[test]
fn fuzz_token_verification() {
    let secret = b"0123456789abcdef-fuzz-secret";
    let good = crate::auth::mint(secret, &crate::auth::Claims { sub: "a".into(), role: "admin".into(), jur: vec!["*".into()], exp: u64::MAX / 4000 });
    let iters: usize = std::env::var("FUZZ_ITERS").ok().and_then(|v| v.parse().ok()).unwrap_or(20000);
    run("token", &[good.clone().into_bytes(), b"v2.eyJzdWIiOiJhIn0.AAAA".to_vec()], iters, move |s| {
        if s != good { assert!(crate::auth::verify(secret, s).is_err(), "a mutated token verified"); }
    });
}
