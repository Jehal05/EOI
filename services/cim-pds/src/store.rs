//! Versioned model store with a SHA-256 hash-chained audit log.
//! Every committed change creates a new version; each record's hash covers the
//! previous record's hash, so history cannot be altered undetected.
use crate::auth::now_ms;
use crate::model::Model;
use serde::Serialize;
use sha2::{Digest, Sha256};

#[derive(Serialize, serde::Deserialize, Clone, Debug)]
pub struct VersionRecord {
    pub version: u64,
    pub ts_ms: u64,
    pub user: String,
    pub action: String,
    pub detail: String,
    #[serde(rename = "modelSha")]
    pub model_sha: String,
    #[serde(rename = "prevHash")]
    pub prev_hash: String,
    pub hash: String,
}

#[derive(Serialize, serde::Deserialize)]
pub struct Entry {
    pub history: Vec<VersionRecord>,
    pub snapshots: Vec<Model>, // snapshots[i] is the model at history[i]
}

pub fn model_sha(m: &Model) -> String {
    hex::encode(Sha256::digest(serde_json::to_vec(m).unwrap()))
}

fn record_hash(prev: &str, version: u64, ts: u64, user: &str, action: &str, detail: &str, msha: &str) -> String {
    let mut h = Sha256::new();
    for part in [prev, &version.to_string(), &ts.to_string(), user, action, detail, msha] {
        h.update((part.len() as u64).to_be_bytes()); // length-prefix: no ambiguity between fields
        h.update(part.as_bytes());
    }
    hex::encode(h.finalize())
}

impl Entry {
    pub fn new(m: Model, user: &str, action: &str, detail: &str) -> Self {
        let mut e = Entry { history: vec![], snapshots: vec![] };
        e.commit(m, user, action, detail);
        e
    }
    pub fn current(&self) -> &Model {
        self.snapshots.last().unwrap()
    }
    pub fn version(&self) -> u64 {
        self.history.last().unwrap().version
    }
    pub fn at(&self, version: u64) -> Option<&Model> {
        self.history.iter().position(|h| h.version == version).map(|i| &self.snapshots[i])
    }
    pub fn commit(&mut self, m: Model, user: &str, action: &str, detail: &str) -> &VersionRecord {
        let prev = self.history.last().map(|h| h.hash.clone()).unwrap_or_else(|| "GENESIS".into());
        let version = self.history.last().map(|h| h.version + 1).unwrap_or(1);
        let ts = now_ms();
        let msha = model_sha(&m);
        let hash = record_hash(&prev, version, ts, user, action, detail, &msha);
        self.history.push(VersionRecord {
            version, ts_ms: ts, user: user.into(), action: action.into(), detail: detail.into(),
            model_sha: msha, prev_hash: prev, hash,
        });
        self.snapshots.push(m);
        // bound memory: keep the last 500 snapshots (history records are always kept)
        if self.snapshots.len() > 500 {
            // snapshots align with history tail only after trimming, so keep both aligned
            let drop = self.snapshots.len() - 500;
            self.snapshots.drain(0..drop);
            self.history.drain(0..drop);
        }
        self.history.last().unwrap()
    }
    /// Verify chain integrity and that each snapshot still matches its recorded hash.
    pub fn verify(&self) -> Result<usize, String> {
        for (i, h) in self.history.iter().enumerate() {
            if i > 0 && h.prev_hash != self.history[i - 1].hash {
                return Err(format!("chain broken at version {}", h.version));
            }
            let calc = record_hash(&h.prev_hash, h.version, h.ts_ms, &h.user, &h.action, &h.detail, &h.model_sha);
            if calc != h.hash {
                return Err(format!("record hash mismatch at version {}", h.version));
            }
            if model_sha(&self.snapshots[i]) != h.model_sha {
                return Err(format!("model content mismatch at version {}", h.version));
            }
        }
        Ok(self.history.len())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    fn m() -> Model {
        serde_json::from_value(serde_json::json!({"id":"x","name":"x","baseMVA":100.0})).unwrap()
    }
    #[test]
    fn chain_verifies_and_detects_tamper() {
        let mut e = Entry::new(m(), "sys", "import", "seed");
        let mut m2 = m();
        m2.name = "renamed".into();
        e.commit(m2, "eng", "edit", "rename");
        assert_eq!(e.verify().unwrap(), 2);
        e.history[0].detail = "forged".into();
        assert!(e.verify().is_err());
    }
    #[test]
    fn snapshot_tamper_detected() {
        let mut e = Entry::new(m(), "sys", "import", "seed");
        e.snapshots[0].name = "evil".into();
        assert!(e.verify().unwrap_err().contains("model content"));
    }
}
