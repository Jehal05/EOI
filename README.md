# GridSovereign: Indigenous SCADA/EMS Platform (prototype)

A from-scratch, CIM-first SCADA/EMS platform for Indian Load Despatch Centres,
built against the GRID-INDIA EOI on Indigenous SCADA/EMS development.

> Status: working prototype, built bottom-up. See `docs/STATUS.md` for what is
> implemented, measured and still missing. No benchmark number in this repo is
> estimated: all are produced by scripts in `tools/` and `tests/`.

## Services (polyglot by design, see docs/adr)
| Service | Language | Role |
|---|---|---|
| cim-pds | Rust | CIM model store, node-breaker topology processor, validation, versioned audit chain |
| fep-gateway | Go | IEC 60870-5-104 front-end processor + outstation simulator |
| scada-core | Java 21 | Real-time DB, alarm engine, SOE, secured control, active/standby |
| ems-analytics | Python | Power flow, state estimator (bad-data detection), N-1 contingency |
| ui | TypeScript | ISA-101-style operator HMI, 3 themes, Hindi + English |

## Hard rules
All code written from scratch. No proprietary/foreign SCADA/EMS code. Test
networks are public IEEE cases used only as validation data.
