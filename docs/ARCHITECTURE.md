# Architecture

```mermaid
flowchart LR
  subgraph Field[Field - simulated]
    OS[IEC 104 outstations]
  end
  subgraph Edge[Network edge - memory safe]
    FEP[fep-gateway Go<br/>IEC 104, quality flags, ms timestamps]
  end
  subgraph Core[Control centre]
    BUS[(Event bus<br/>append-only log)]
    SC[scada-core Java<br/>RTDB, alarms, SOE, control, failover]
    CIM[cim-pds Rust<br/>CIM store, topology, validation, audit chain]
    EMS[ems-analytics Python<br/>PF, State Estimator, N-1]
    HIST[(Historian)]
  end
  UI[Operator UI TypeScript<br/>3 themes, EN/HI]
  OS --> FEP --> BUS --> SC
  CIM -->|bus-branch model| EMS
  CIM -->|point list| FEP
  SC --> HIST
  SC -->|snapshots| EMS
  EMS -->|results| SC
  SC --> UI
  EMS --> UI
  CIM --> UI
```

## Principles
1. CIM-first: no application owns a private network model. Every application
   reads the model from cim-pds (node-breaker) or its derived bus-branch view.
2. Independent or integrated: every service exposes an OpenAPI-described HTTP
   API and runs alone against file/JSON fixtures.
3. Event-sourced real-time state: telemetry and operator actions are appended
   to a log so any interval can be replayed deterministically.
4. Security envelope is ours: authN/Z, command authorization, hash-chained
   audit are first-party code.
