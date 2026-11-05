# ADR-0001 Polyglot services
Status: accepted
- Rust for cim-pds: memory safety on the model/authorization path, predictable latency, no GC pauses.
- Go for fep-gateway: strong network/concurrency primitives, memory safe, easy static binaries for edge deployment.
- Java 21 for scada-core: mature concurrency (virtual threads), strong ecosystem for long-lived operational software and utility IT teams.
- Python for ems-analytics: NumPy/SciPy sparse numerics; hot kernels can later move to Rust/C++.
- TypeScript for UI: type-safe, component-based HMI.
Cost: more build tooling. Mitigation: contract-first HTTP/JSON APIs, one Makefile.
