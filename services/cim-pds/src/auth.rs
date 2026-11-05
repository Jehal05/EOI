//! First-party authorization envelope: HMAC-SHA256 signed bearer tokens with
//! role + jurisdiction, plus a per-user sliding-window rate limiter.
//! Token = base64url(payload_json) "." hex(hmac_sha256(secret, base64url(payload_json)))
use base64::{engine::general_purpose::URL_SAFE_NO_PAD as B64, Engine};
use hmac::{Hmac, Mac};
use serde::{Deserialize, Serialize};
use sha2::Sha256;
use std::collections::HashMap;
use std::sync::{Mutex, OnceLock};
use ed25519_dalek::{Signature, Signer, SigningKey, Verifier, VerifyingKey};
use std::time::{SystemTime, UNIX_EPOCH};

type HmacSha256 = Hmac<Sha256>;

#[derive(Serialize, Deserialize, Clone, Debug)]
pub struct Claims {
    pub sub: String,
    pub role: String,
    /// regions the user may act in; ["*"] = all (NLDC)
    pub jur: Vec<String>,
    pub exp: u64,
}

pub fn role_rank(r: &str) -> u8 {
    match r {
        "viewer" => 1,
        "operator" => 2,
        "engineer" => 3,
        "admin" => 4,
        _ => 0,
    }
}

pub fn now_ms() -> u64 {
    SystemTime::now().duration_since(UNIX_EPOCH).unwrap().as_millis() as u64
}


// ---------------------------------------------------------------------------------------------------------------- Ed25519 "v2"
// v2 token = "v2." b64url(payload) "." b64url(sig); signed message = "gridsovereign-token-v2\n" + b64url(payload); payload has "kid".
// Once a trust list is loaded this process accepts v2 tokens only (no shared-secret fallback).
const DOMAIN: &str = "gridsovereign-token-v2\n";

struct TrustKey {
    key: VerifyingKey,
    max_rank: u8,
    sub_prefix: String,
}
struct Asym {
    trust: HashMap<String, TrustKey>,
    signer: Option<(String, SigningKey)>,
}
static ASYM: OnceLock<Asym> = OnceLock::new();

pub fn asymmetric() -> bool {
    ASYM.get().is_some()
}

fn b64_std(s: &str) -> Result<Vec<u8>, String> {
    base64::engine::general_purpose::STANDARD.decode(s).map_err(|e| e.to_string())
}

/// Install trust list (GS_TOKEN_TRUST) and signing key (GS_TOKEN_KEY) from the environment, when set.
pub fn init_from_env() -> Result<(), String> {
    let Ok(tp) = std::env::var("GS_TOKEN_TRUST") else { return Ok(()) };
    let v: serde_json::Value = serde_json::from_slice(&std::fs::read(&tp).map_err(|e| format!("{tp}: {e}"))?).map_err(|e| e.to_string())?;
    let mut trust = HashMap::new();
    for k in v["keys"].as_array().ok_or("trust list has no keys")? {
        let kid = k["kid"].as_str().filter(|s| !s.is_empty()).ok_or("trust entry without kid")?.to_string();
        let raw: [u8; 32] = b64_std(k["pub"].as_str().ok_or("trust entry without pub")?)?.try_into().map_err(|_| "public key is not 32 bytes")?;
        let max_rank = role_rank(k["maxRole"].as_str().unwrap_or(""));
        if max_rank == 0 {
            return Err(format!("trust entry {kid}: unknown maxRole"));
        }
        trust.insert(kid, TrustKey { key: VerifyingKey::from_bytes(&raw).map_err(|e| e.to_string())?, max_rank, sub_prefix: k["subPrefix"].as_str().unwrap_or("").to_string() });
    }
    if trust.is_empty() {
        return Err("trust list is empty".into());
    }
    let signer = match std::env::var("GS_TOKEN_KEY") {
        Ok(kp) => {
            let kv: serde_json::Value = serde_json::from_slice(&std::fs::read(&kp).map_err(|e| format!("{kp}: {e}"))?).map_err(|e| e.to_string())?;
            let seed: [u8; 32] = b64_std(kv["seed"].as_str().ok_or("key file without seed")?)?.try_into().map_err(|_| "seed is not 32 bytes")?;
            Some((kv["kid"].as_str().ok_or("key file without kid")?.to_string(), SigningKey::from_bytes(&seed)))
        }
        Err(_) => None,
    };
    ASYM.set(Asym { trust, signer }).map_err(|_| "already initialised".to_string())
}

fn mint_v2(a: &Asym, c: &Claims) -> String {
    let (kid, sk) = a.signer.as_ref().expect("asymmetric mode without a signing key (GS_TOKEN_KEY)");
    let mut v = serde_json::to_value(c).unwrap();
    v["kid"] = serde_json::Value::String(kid.clone());
    let p = B64.encode(serde_json::to_vec(&v).unwrap());
    let sig = sk.sign(format!("{DOMAIN}{p}").as_bytes());
    format!("v2.{p}.{}", B64.encode(sig.to_bytes()))
}

fn verify_v2(a: &Asym, token: &str) -> Result<Claims, &'static str> {
    if token.len() > 4096 {
        return Err("malformed token");
    }
    let rest = token.strip_prefix("v2.").ok_or("malformed token")?;
    let (p, sig) = rest.split_once('.').ok_or("malformed token")?;
    let sig: [u8; 64] = B64.decode(sig).map_err(|_| "malformed token")?.try_into().map_err(|_| "malformed token")?;
    let bytes = B64.decode(p).map_err(|_| "malformed payload")?;
    let v: serde_json::Value = serde_json::from_slice(&bytes).map_err(|_| "malformed claims")?;
    let k = a.trust.get(v["kid"].as_str().unwrap_or("")).ok_or("unknown signing key")?;
    k.key.verify(format!("{DOMAIN}{p}").as_bytes(), &Signature::from_bytes(&sig)).map_err(|_| "bad signature")?;
    let c: Claims = serde_json::from_value(v).map_err(|_| "malformed claims")?;
    if role_rank(&c.role) == 0 || role_rank(&c.role) > k.max_rank {
        return Err("role exceeds what the signing key may issue");
    }
    if !c.sub.starts_with(&k.sub_prefix) {
        return Err("subject not allowed for the signing key");
    }
    if c.exp * 1000 < now_ms() {
        return Err("token expired");
    }
    Ok(c)
}

pub fn mint(secret: &[u8], c: &Claims) -> String {
    if let Some(a) = ASYM.get() {
        return mint_v2(a, c);
    }
    let payload = B64.encode(serde_json::to_vec(c).unwrap());
    let mut mac = HmacSha256::new_from_slice(secret).unwrap();
    mac.update(payload.as_bytes());
    format!("{}.{}", payload, hex::encode(mac.finalize().into_bytes()))
}

pub fn verify(secret: &[u8], token: &str) -> Result<Claims, &'static str> {
    if let Some(a) = ASYM.get() {
        return verify_v2(a, token);
    }
    let (payload, sig) = token.split_once('.').ok_or("malformed token")?;
    let sig_bytes = hex::decode(sig).map_err(|_| "malformed signature")?;
    if hex::encode(&sig_bytes) != sig {
        return Err("malformed signature"); // one canonical spelling only: upper-case hex is the same MAC but a different string
    }
    let mut mac = HmacSha256::new_from_slice(secret).unwrap();
    mac.update(payload.as_bytes());
    mac.verify_slice(&sig_bytes).map_err(|_| "bad signature")?; // constant-time compare
    let bytes = B64.decode(payload).map_err(|_| "malformed payload")?;
    let c: Claims = serde_json::from_slice(&bytes).map_err(|_| "malformed claims")?;
    if c.exp * 1000 < now_ms() {
        return Err("token expired");
    }
    Ok(c)
}

impl Claims {
    pub fn covers(&self, region: &str) -> bool {
        self.jur.iter().any(|j| j == "*" || j == region)
    }
}

pub struct RateLimiter {
    window_ms: u64,
    max: u32,
    state: Mutex<HashMap<String, (u64, u32)>>,
}

impl RateLimiter {
    pub fn new(window_ms: u64, max: u32) -> Self {
        Self { window_ms, max, state: Mutex::new(HashMap::new()) }
    }
    pub fn allow(&self, key: &str) -> bool {
        let now = now_ms();
        let mut s = self.state.lock().unwrap_or_else(|e| e.into_inner());
        let e = s.entry(key.to_string()).or_insert((now, 0));
        if now - e.0 >= self.window_ms {
            *e = (now, 0);
        }
        e.1 += 1;
        e.1 <= self.max
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    fn claims(exp: u64) -> Claims {
        Claims { sub: "op1".into(), role: "operator".into(), jur: vec!["Northern".into()], exp }
    }
    #[test]
    fn roundtrip_and_tamper() {
        let t = mint(b"k", &claims(u64::MAX / 4000));
        assert_eq!(verify(b"k", &t).unwrap().sub, "op1");
        assert!(verify(b"other", &t).is_err());
        let mut bad = t.clone();
        bad.replace_range(2..3, "Z");
        assert!(verify(b"k", &bad).is_err());
    }
    #[test]
    fn cross_language_vector() {
        // produced by tools/token_vector.py (independent implementation)
        let t = "eyJzdWIiOiJ2ZWN0b3IiLCJyb2xlIjoib3BlcmF0b3IiLCJqdXIiOlsiTm9ydGhlcm4iXSwiZXhwIjo0MTAyNDQ0ODAwfQ.aa49339aa8cae099180f44323c5b6130884b98651ab521cf52d3da63a8c1ba38";
        let c = verify(b"contract-test-secret-0123456789", t).expect("vector must verify");
        assert_eq!((c.sub.as_str(), c.role.as_str()), ("vector", "operator"));
        assert!(c.covers("Northern") && !c.covers("Southern"));
        assert!(verify(b"contract-test-secret-0123456780", t).is_err());
    }
    #[test]
    fn expired_rejected() {
        let t = mint(b"k", &claims(1));
        assert_eq!(verify(b"k", &t).unwrap_err(), "token expired");
    }
    #[test]
    fn jurisdiction() {
        assert!(claims(1).covers("Northern"));
        assert!(!claims(1).covers("Southern"));
    }
    #[test]
    fn rate_limit() {
        let r = RateLimiter::new(60_000, 3);
        assert!(r.allow("a") && r.allow("a") && r.allow("a"));
        assert!(!r.allow("a"));
        assert!(r.allow("b"));
    }
}

#[cfg(test)]
mod asym_tests {
    use super::*;
    // ASYM is process-global, so the whole asymmetric policy is exercised in one test.
    #[test]
    fn ed25519_tokens_policy_and_forgery() {
        let sk_core = SigningKey::from_bytes(&[7u8; 32]);
        let sk_ems = SigningKey::from_bytes(&[9u8; 32]);
        let mut trust = HashMap::new();
        trust.insert("core-A".to_string(), TrustKey { key: sk_core.verifying_key(), max_rank: 4, sub_prefix: String::new() });
        trust.insert("ems".to_string(), TrustKey { key: sk_ems.verifying_key(), max_rank: 1, sub_prefix: "ems".into() });
        let core = Asym { trust, signer: Some(("core-A".into(), sk_core.clone())) };
        let c = |sub: &str, role: &str, exp: u64| Claims { sub: sub.into(), role: role.into(), jur: vec!["North".into()], exp };
        let far = u64::MAX / 4000;
        let t = mint_v2(&core, &c("alice", "operator", far));
        let got = verify_v2(&core, &t).unwrap();
        assert_eq!((got.sub.as_str(), got.role.as_str()), ("alice", "operator"));
        assert!(got.covers("North") && !got.covers("South"));
        assert_eq!(verify_v2(&core, &mint_v2(&core, &c("a", "viewer", 1))).unwrap_err(), "token expired");
        // restricted key: cap and subject prefix
        let ems = Asym { trust: HashMap::new(), signer: Some(("ems".into(), sk_ems.clone())) };
        let ems_tok = mint_v2(&ems, &c("ems-analytics", "viewer", far));
        assert!(verify_v2(&core, &ems_tok).is_ok());
        assert_eq!(verify_v2(&core, &mint_v2(&ems, &c("ems-analytics", "admin", far))).unwrap_err(), "role exceeds what the signing key may issue");
        assert_eq!(verify_v2(&core, &mint_v2(&ems, &c("alice", "viewer", far))).unwrap_err(), "subject not allowed for the signing key");
        // forgeries
        let parts: Vec<&str> = t.split('.').collect();
        let evil = B64.encode(br#"{"sub":"alice","role":"admin","jur":["*"],"exp":99999999999,"kid":"core-A"}"#);
        assert!(verify_v2(&core, &format!("v2.{evil}.{}", parts[2])).is_err());
        let attacker = SigningKey::from_bytes(&[1u8; 32]);
        let forged = Asym { trust: HashMap::new(), signer: Some(("core-A".into(), attacker)) };
        assert_eq!(verify_v2(&core, &mint_v2(&forged, &c("alice", "admin", far))).unwrap_err(), "bad signature");
        for bad in ["", "v2.", "v2.x", "v2..", "v2.a.b.c", "payload.deadbeef"] {
            assert!(verify_v2(&core, bad).is_err(), "{bad}");
        }
        assert!(verify_v2(&core, &"x".repeat(5000)).is_err());
        // unknown kid
        let unk = Asym { trust: HashMap::new(), signer: Some(("nobody".into(), SigningKey::from_bytes(&[3u8; 32]))) };
        assert_eq!(verify_v2(&core, &mint_v2(&unk, &c("a", "viewer", far))).unwrap_err(), "unknown signing key");
    }
}
