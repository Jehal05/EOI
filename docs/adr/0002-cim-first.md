# ADR-0002 CIM-first model
Status: accepted
Authoritative model is node-breaker, using CIM class/attribute names for the
subset we implement (Substation, VoltageLevel, ConnectivityNode, Breaker/Switch,
ACLineSegment, PowerTransformer, SynchronousMachine, EnergyConsumer,
LinearShuntCompensator). Bus-branch views for EMS are derived by the topology
processor. Exchange: JSON (internal) and RDF/XML in the style of IEC 61970-552.
Limitation: this is a subset, not a certified full-profile CIM implementation.
