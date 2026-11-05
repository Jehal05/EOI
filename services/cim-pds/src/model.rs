//! CIM-subset network model (node-breaker). Names follow IEC 61970-301 classes
//! for the subset implemented here. Impedances are per-unit on `baseMVA`.
use serde::{Deserialize, Serialize};

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Model {
    pub id: String,
    pub name: String,
    #[serde(rename = "baseMVA")]
    pub base_mva: f64,
    #[serde(default)]
    pub source: String,
    #[serde(default, rename = "slackSubstation")]
    pub slack_substation: String,
    #[serde(default)]
    pub substations: Vec<Substation>,
    #[serde(default, rename = "voltageLevels")]
    pub voltage_levels: Vec<VoltageLevel>,
    #[serde(default, rename = "connectivityNodes")]
    pub connectivity_nodes: Vec<ConnectivityNode>,
    #[serde(default)]
    pub switches: Vec<Switch>,
    #[serde(default, rename = "acLineSegments")]
    pub ac_line_segments: Vec<Branch>,
    #[serde(default)]
    pub transformers: Vec<Branch>,
    #[serde(default)]
    pub generators: Vec<Generator>,
    #[serde(default)]
    pub loads: Vec<Load>,
    #[serde(default)]
    pub shunts: Vec<Shunt>,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Substation {
    pub mrid: String,
    pub name: String,
    #[serde(default)]
    pub region: String,
    #[serde(default)]
    pub lat: f64,
    #[serde(default)]
    pub lon: f64,
    #[serde(default)]
    pub vm0: Option<f64>,
    #[serde(default)]
    pub va0: Option<f64>,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct VoltageLevel {
    pub mrid: String,
    pub name: String,
    pub substation: String,
    #[serde(rename = "nominalKV")]
    pub nominal_kv: f64,
    /// Operating voltage limits as a fraction of nominal (CIM OperationalLimitSet, simplified). Default 0.95 / 1.05.
    #[serde(default, rename = "vMin", skip_serializing_if = "Option::is_none")]
    pub v_min: Option<f64>,
    #[serde(default, rename = "vMax", skip_serializing_if = "Option::is_none")]
    pub v_max: Option<f64>,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct ConnectivityNode {
    pub mrid: String,
    pub name: String,
    #[serde(rename = "voltageLevel")]
    pub voltage_level: String,
    /// "busbar" marks a node that forms an electrical bus. Defaults by `-BB` suffix.
    #[serde(default)]
    pub kind: Option<String>,
}

impl ConnectivityNode {
    pub fn is_busbar(&self) -> bool {
        match &self.kind {
            Some(k) => k == "busbar",
            None => self.mrid.ends_with("-BB"),
        }
    }
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Switch {
    pub mrid: String,
    pub name: String,
    #[serde(rename = "type", default = "default_switch_type")]
    pub kind: String,
    pub node1: String,
    pub node2: String,
    pub open: bool,
}
fn default_switch_type() -> String {
    "Breaker".into()
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Branch {
    pub mrid: String,
    pub name: String,
    pub r: f64,
    pub x: f64,
    #[serde(default)]
    pub bch: f64,
    #[serde(default)]
    pub rating: f64,
    pub node1: String,
    pub node2: String,
    #[serde(default)]
    pub ratio: Option<f64>,
    #[serde(default)]
    pub angle: Option<f64>,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Generator {
    pub mrid: String,
    pub name: String,
    pub node: String,
    pub p: f64,
    pub q: f64,
    pub qmax: f64,
    pub qmin: f64,
    pub vset: f64,
    pub pmax: f64,
    pub pmin: f64,
    #[serde(rename = "inService", default = "yes")]
    pub in_service: bool,
    #[serde(default)]
    pub fuel: String,
    /// Quadratic production cost c2*P^2 + c1*P + c0 (currency/h, P in MW). Optional; used by OPF.
    #[serde(rename = "costC2", default, skip_serializing_if = "Option::is_none")]
    pub cost_c2: Option<f64>,
    #[serde(rename = "costC1", default, skip_serializing_if = "Option::is_none")]
    pub cost_c1: Option<f64>,
    #[serde(rename = "costC0", default, skip_serializing_if = "Option::is_none")]
    pub cost_c0: Option<f64>,
}
fn yes() -> bool {
    true
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Load {
    pub mrid: String,
    pub name: String,
    pub node: String,
    pub p: f64,
    pub q: f64,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Shunt {
    pub mrid: String,
    pub name: String,
    pub node: String,
    pub g: f64,
    pub b: f64,
}
