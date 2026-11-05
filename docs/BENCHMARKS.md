# Measured benchmarks (honest report)

Everything below was measured on ONE small sandbox host (2 vCPU, ~7 GB RAM, software rendering, all services co-located).
Numbers are not production-hardware figures. Reproduce with `tools/bench_scale.py` (raw output: `docs/bench_scale.json`).
Networks are synthetic: the public IEEE-118 case tiled and joined by synthetic tie lines. Not real grid data.

## EMS scale (tools/bench_scale.py, 1 core per case)

| Buses | Branches | PF warm-start | PF flat-start | State estimation (WLS) | Mean N-1 case | Full N-1 screen (extrapolated, 1 core) |
|---:|---:|---:|---|---:|---:|---:|
| 118 | 186 | 1.3 ms | 10 ms, converged | 37 ms (with bad-data ID) | 6.4 ms | 1.2 s |
| 1,062 | 1,698 | 10 ms | 22 ms, converged | 477 ms (with bad-data ID) | 13.8 ms | 23 s |
| 5,074 | 8,124 | 44 ms | 77 ms, converged | 306 ms (no bad-data ID, see note) | 43.9 ms | 357 s |
| 10,030 | 16,062 | 85 ms | **did NOT converge** | 654 ms (no bad-data ID, see note) | 87.3 ms | 1,403 s |

Notes and limitations
- **Flat-start Newton-Raphson failed at 10,030 buses** (also at 7,080 and 8,260). Warm start from a previous solution converges in 3 iterations, and real-time operation always warm-starts, but a robust cold-start strategy (generator-setpoint initialisation, damping, fast-decoupled fallback) is an open item.
- **Bad-data identification is skipped above 3,000 state variables** because the normalized-residual step uses a dense inverse. At 5k and 10k buses the SE time above is the WLS solve and chi-square only. A sparse selected inverse is required for full scale.
- **N-1 times at 1k+ are extrapolations**: 40 random outages were timed and multiplied by the number of branches. The 10k screen (~1,400 s single-core, roughly 700 s on this 2-core host) is far outside a 30 s cycle. Production needs distributed screening plus contingency ranking and a sparse/LODF pre-screen. This is a known gap.
- The EOI's full target (10,000 buses, 25,000 lines, 10,000 transformers, ...) has branches beyond what was tested (16k here).
- Not measured: 20 lakh point ingest at scale, 10-year historian retention, N-2, OPF, DSA. They are not implemented.

## Real-time path (live stack, single host)

- Field simulator -> IEC 104 outstation -> Go FEP -> scada-core -> UI stream: p50 about 18-136 ms and p95 about 113-220 ms across runs (the FEP batches at 200 ms, so run-to-run spread is large). Telemetry is synthetic at 2 s cycles for 118 buses (~1,100 measurands plus breaker states).
- State estimation on live data (118 buses, ~1,100 measurements): about 40-70 ms round trip; chi-square clean J about 710-900 vs threshold about 958 (dof about 860). N-1 screen on 186 branches: about 0.7 s wall on 2 workers. EMS cycle total about 0.8 s, run every 30 s.
- Failover test (tools/e2e_failover.py, 10 checks): leader killed, standby takes over and the operator records survive. Exact takeover time depends on the heartbeat setting; see test output. There is no quorum or fencing, so split-brain is possible under network partitions (documented gap).
- Deterministic replay: 19,339 records replayed, state and alarm hashes identical (scada-core test suite).

## Test inventory (all passing at time of writing)

| Component | Tests |
|---|---|
| scada-core (Java, JDK only) | 102 unit/engine checks incl. replay determinism, tamper detection, TOTP, token contract vector |
| cim-pds (Rust) | 13 |
| fep-gateway (Go) | IEC 104 codec/session + token packages |
| ems-analytics (Python) | 79 (PF, SE, N-1, N-2, short circuit, OPF/SCOPF, security enhancement, TCM, AGC, DSA) |
| historian (Python) | 17 (store, forecasting, audited API gate) |
| UI (vitest) | 23 (themes, WCAG contrast, i18n, logic) |
| tools/e2e_core.py | 44 end-to-end checks (auth, MFA, SBO control, interlocks, replication, replay, tamper) |
| tools/e2e_failover.py | 10 |
| tools/ui_e2e.py (Playwright) | 38 incl. axe-core on 11 screens, 3 themes + dim, Hindi, fps |


## N-2 screening (tools/bench_n2.py, raw: docs/bench_n2.json; 2 workers on the 2-vCPU host)

| Buses | Branches | All pairs | Candidates screened | AC-verified | DC build | DC screen | Total wall |
|---:|---:|---:|---:|---:|---:|---:|---:|
| 118 | 186 | 17,205 | 1,319 | 81 | 12 ms | 8 ms | 1.15 s |
| 1,062 | 1,698 | 1,440,753 | 6,233 | 120 | 28 ms | 0.67 s | 2.18 s |

Accuracy against exhaustive AC (tests/test_n2.py, `pytest -s` prints the figures):
- IEEE-14, all 190 pairs solved in AC: Spearman rank correlation 0.976; 94% of the AC-worst 10% fall inside the DC-worst 20%; mean absolute loading error 9.7 percentage points; islanding detection exact (27/27).
- IEEE-118, the 868 candidate pairs of one run (top-25 + adjacent) solved in AC: Spearman 0.995; recall 1.00 on the AC-worst 10% (n=78) within the DC-worst 20%; mean loading error 9.3 pp; islanding exact.
- The closed-form two-outage update equals an independent DC re-solve on 163 IEEE-14 pairs (tolerance 1e-8 relative).

Limitations
- Search-space reduction is a heuristic: pairs are all combinations among the 40 most critical branches plus all pairs sharing a bus. Pairs that are neither critical nor adjacent are NOT screened. The recall figures above are measured only inside the candidate set.
- A full exhaustive AC N-2 would take roughly 110 s at 118 buses and 5-6 hours at 1,062 buses (EXTRAPOLATED from mean N-1 case times of 6.4 and 13.8 ms); it was not run.
- The DC step is a ranking device; it ignores reactive power and losses, hence the ~9-10 pp loading error. Ranked rows are labelled "DC est." in the UI until AC-verified. Voltage violations are only seen in AC-verified rows.
- N-2 runs on the energised main island when open switches split the network; other islands are not screened.
- Not tested above ~1k buses; the DC screen cost grows with pairs x branches.
- A fork-after-threads deadlock was found while integrating (BLAS/SuperLU state inherited by forked workers). Fixed by running small AC batches in-process and large ones in a forkserver pool. The existing N-1 path still uses fork and logs harmless "_after_fork" warnings; it should get the same treatment.


## Short-circuit analysis (tools/bench_sc.py, raw: docs/bench_sc.json; single process)

All-bus fault levels (3-phase, LG, LL, LLG, peak current, duty) including three sparse-LU factorisations (positive, negative, zero sequence):

| Buses | Branches | Time for every bus |
|---:|---:|---:|
| 118 | 186 | 7 ms |
| 1,062 | 1,698 | 0.16 s |
| 5,074 | 8,124 | 4.4 s |
| 10,030 | 16,062 | 17.2 s |

Validation actually performed (tests/test_shortcircuit.py, 10 tests):
- Two-bus network with a hand-derived answer (Z1=Z2=j0.28, Z0=j0.39 pu): all four fault types match to 1e-9 relative.
- Symmetrical-component formulas for 3ph/LG/LL/LLG equal an INDEPENDENT phase-domain solution (Zabc = T Zseq T^-1 with the boundary conditions imposed directly) on 100 random impedance sets.
- Sparse-LU Thevenin impedances equal a dense matrix inverse on IEEE-14 for all three sequences (1e-10).
- Identities and monotonicity on IEEE-14/118: LL = (sqrt3/2) x 3ph when Z1=Z2; removing a generator never raises any fault level; duplicating branches never lowers one; dead islands excluded.

Limitations (not hidden)
- NOT validated against a published IEC 60909 reference network (for example the IEC TR 60909-4 examples): that data was not available offline. Results are therefore "IEC 60909-style", not certified IEC 60909.
- No KG/KT impedance correction factors, no motor contribution, no partial-load operation, no Ik (steady-state) or Ib (breaking) currents, no thermal equivalent current. Peak current uses the Thevenin R/X (method B-like).
- Generator ratings, x''d, all zero-sequence data (lines 3x, transformers through-connected YNyn, generators 0.5 x''d solidly grounded) and breaker interrupting ratings are SYNTHETIC because the IEEE cases carry none. Hence the headline kA values and the 3 "over rating" buses in the demo are illustrative only.
- Faults are bolted (Zf = 0). Mutually coupled lines, series compensation, HVDC and inverter-based resources (current-limited, not x''d sources) are not modelled; solar/wind/BESS contribution is a known gap.


## Optimal power flow and security-constrained OPF (tools/bench_opf.py, raw: docs/bench_opf.json; 2-vCPU host)

| Case | Buses / branches / units | OPF incl. AC verification | SCOPF incl. AC verification | SCOPF cuts | Cost of security | Unserved energy (synthetic ratings) |
|---|---|---:|---:|---:|---:|---:|
| IEEE-118 | 118 / 186 / 54 | 0.09 s | 4.3 s (includes two AC N-1 sweeps) | 107 | 15,571 /h on 131,292 /h (+11.9 %) | 41 MW |
| tiled 1,062 | 1,062 / 1,698 / 486 | 9.4 s | 145 s (AC N-1 not run at this size) | 428 | 142,423 /h on 1,198,212 /h | 369 MW |

(1,062 buses: one non-lazy LP with every line limit takes 34.7 s; lazy line-limit generation brought OPF to 9.4 s. SCOPF at this size is a study function, far from a 30 s cycle.)

Validation actually performed (tests/test_opf.py, 12 tests, `pytest -s` prints the figures)
- DC-OPF objective vs pypower's `rundcopf` (exact quadratic costs): IEEE-14 8.4e-6 relative difference, IEEE-118 7.9e-5 (40-segment piecewise-linear cost), and a congested IEEE-118 case with 8 binding lines 1.6e-5.
- Branch flows for the dispatch pypower found equal ours to 1e-4 MW (PTDF check, branches mapped by id).
- LMP = marginal cost of load: finite-difference check on 14 random buses, median error about 0 $/MWh (largest 0.5 allowed, typical <0.01).
- Lazy line-limit generation gives the same objective as the full LP (1e-9) with fewer active lines.
- SCOPF: for the secured dispatch (including shed), all 177 non-islanding outages re-solved with an INDEPENDENT DC network solve (branch physically removed, no LODF) show a worst limit excess of 0.005 MW. Cost of the secured dispatch >= unsecured; with an unbinding emergency factor the SCOPF reduces exactly to the OPF.
- AC check: every dispatch is run through the AC power flow (Q limits enforced); losses are fed back for 2 iterations.

Measured effect of DC security on the AC network (IEEE-118, emergency factor 1.15, synthetic ratings): AC N-1 cases above the emergency rating fall from 61/186 (unsecured dispatch, worst 541 %) to 28/186 (secured, worst 204 %). **DC-secured does not mean AC-secured**: the remaining 28 cases come from the 9 islanding outages (not secured by design), reactive/voltage effects and the load shed.

Limitations (not hidden)
- NOT a full AC-OPF. The model is a DC-network LP: no reactive power, voltage or tap/shunt control, no unit commitment, ramp or reserve constraints, no corrective (post-contingency) actions, no HVDC/BESS/renewable curtailment modelling. Losses enter as a lump demand from the AC solution, not as nodal loss factors.
- Costs are the PUBLIC IEEE-case quadratic polynomials (model-driven through the CIM generator fields costC2/C1/C0, passed through cim-pds); thermal ratings are synthetic (1.3x base flow), so shed energy, congestion and the cost of security in these results are illustrative. When load is shed, the unserved-energy price (5,000 /MWh) sets the nodal prices at the affected buses.
- Islanding outages are listed but not secured. Voltage-limit security is not part of the SCOPF.
- Results are ADVISORY: nothing is sent to any set-point or control path; the route needs the operator role.
- Cost function is approximated by 16 secant segments in service (40-60 in the validation tests); the approximation error was below 1e-4 relative in those tests.
- Data-handling finding: cim-pds emits lines before transformers, so bus-branch branch order differs from the IEEE case row order. Anything comparing against an external case must map by id (tests do).

## Security enhancement (tools/bench_sec.py, raw: docs/bench_sec.json; 2-vCPU host, emergency rating 115 %)

| Case | Buses / branches | Critical outages before to after | Total overload % | Worst loading % | Redispatch | Shed | Cost change | Wall time |
|---|---|---|---|---|---|---|---|---|
| IEEE-118 (synthetic ratings) | 118 / 186 | 56 to 25 | 8251 to 424 | 470 to 203 | 1,514 MW | 16.6 MW | +12,030 /h | 20 s |
| 9x tiled IEEE-118 | 1,062 / 1,698 | 513 to 513 | 57167 to 57167 | 426 to 426 | 0 | 0 | 0 | 139 s |

Validation: DC sensitivities vs AC response of the same action, median relative error 1.2 % (p90 7.3 %); every accepted plan is re-checked with a full AC N-1 sweep.

Limits: the IEEE-118 plan improves but does not secure the system (25 outages remain critical; ratings are synthetic and very tight). On the 1,062-bus tiled case the planner finds no accepted improvement, so the result is reported as not improved: the 513 critical outages exceed what thermal-only, move-limited remedies can fix under these ratings, and the LP row cap (900) restricts what it sees. The cost is O(branches) AC power flows for the outage matrix. Thermal only; no voltage or stability constraints.

## Corridor capability (tools/bench_tcm.py, raw: docs/bench_tcm.json; 3 areas, TRM 5 %)

| Case | Buses | N-1 limit | Corridors (directions) | Wall time | Available MW (ATC) per corridor | Pre-existing constraints excluded |
|---|---|---|---|---|---|---|
| IEEE-118 (synthetic ratings) | 118 | 100 % | 4 | 0.7 s | 0, 0, 0, 0 | 229 |
| IEEE-118 | 118 | 115 % | 4 | 2.0 s | 0, 0, 0, 14.6 | 124 |
| 9x tiled IEEE-118 | 1,062 | 100 % | 6 | 5.1 s | 0, 0, 3.7, 4.1, 5.6, 7.3 | 1,857 |
| 9x tiled IEEE-118 | 1,062 | 115 % | 6 | 9.2 s | 0, 0, 2.1, 2.5, 2.9, 3.0 | 949 |

Validation: DC limits match an independent dense DC re-solve with the outaged branch physically removed (flows below the limit just before t*, above just after); the AC value is re-checked on the full AC model. The DC estimate is often larger than the AC-verified value (for example 45 MW vs 6 MW on one tiled corridor), which is why the AC step exists.

Limits: ratings are synthetic and tight, so margins are small and many constraints are already violated at the base point (excluded and counted, not hidden). Thermal and voltage-band only; AC N-1 on the 12 most critical outages per corridor; areas are auto-partitioned, not utility-defined interfaces. No stability limits, so values are not a certified TTC.

## Forecasting (tools/bench_forecast.py, raw: docs/bench_forecast.json; SYNTHETIC series, 5-minute step, 8 independent realisations per row)

| Series | History | Horizon | Skill vs persistence (mean / min) | 80 % interval coverage (mean / min) | 95 % interval coverage (mean / min) | Model chosen | Time per forecast |
|---|---|---|---|---|---|---|---|
| Load | 14 d | 60 min | 0.57 / 0.54 | 0.81 / 0.77 | 0.96 / 0.94 | ridge 8/8 | 11 ms |
| Load | 14 d | 360 min | 0.79 / 0.76 | 0.79 / 0.73 | 0.95 / 0.89 | ridge 8/8 | 36 ms |
| Load | 60 d | 360 min | 0.82 / 0.81 | 0.82 / 0.78 | 0.96 / 0.95 | ridge 8/8 | 194 ms |
| Solar | 14 d | 60 min | 0.38 / 0.30 | 0.81 / 0.80 | 0.94 / 0.94 | ridge 8/8 | 12 ms |
| Solar | 60 d | 360 min | 0.72 / 0.67 | 0.80 / 0.79 | 0.95 / 0.93 | ridge 8/8 | 185 ms |
| Wind | 14 d | 360 min | -0.01 / -0.07 | 0.78 / 0.64 | 0.92 / 0.81 | persistence 7/8, ridge 1/8 | 42 ms |
| Wind | 60 d | 360 min | 0.00 / 0.00 | 0.80 / 0.75 | 0.96 / 0.95 | persistence 8/8 | 182 ms |

Scale: one year of 5-minute load (105,120 samples) forecasts in 1.3 s; 1,000 series of 14 days is about 18 s serially (extrapolated from 100 measured runs).

Limits: synthetic data only, so skill figures show the method works on a known process, not what it will do on real load. No weather inputs. Wind has no predictable structure beyond persistence in this process and the screen says so. Coverage is measured on a held-out block and is close to nominal on average but not guaranteed per run (minimum 0.64 for the 80 % interval on 14-day wind). The seeded demo series are synthetic and named SYN.*.

## AGC simulation (tools/bench_agc.py, raw: docs/bench_agc.json; 600 s runs, dt 0.05 s, synthetic dynamic parameters)

| Case | Areas | Event | Lowest f, no AGC / AGC (Hz) | Final dev, no AGC (Hz) | Analytic primary (Hz) | Final dev with AGC (Hz) | Final tie dev (MW) | Sim time |
|---|---|---|---|---|---|---|---|---|
| IEEE-118 | 2 | 3 % load step, area A | 49.950 / 49.950 | 0.0466 | -0.0466 | 0.0000 | 0.16 | 0.5 s |
| IEEE-118 | 2 | trip largest unit, area A | 49.756 / 49.756 | 0.1801 | -0.1801 | 0.0000 | 0.14 | 0.5 s |
| IEEE-118 | 4 | 3 % load step | 49.955 / 49.955 | 0.0371 | -0.0371 | 0.0000 | 0.04 | 0.3 s |
| IEEE-118 | 4 | trip largest unit | 49.682 / 49.682 | 0.1801 | -0.1801 | 0.0000 | 2.87 | 0.4 s |
| 9x tiled (1,062 buses, 486 units) | 2 | 3 % load step | 49.948 / 49.948 | 0.0412 | -0.0412 | 0.0001 | 3.04 | 0.3 s |
| 9x tiled | 2 | trip largest unit | 49.939 / 49.939 | 0.0439 | -0.0439 | 0.0001 | 1.95 | 0.4 s |
| 9x tiled | 4 | 3 % load step | 49.953 / 49.955 | 0.0330 | -0.0330 | 0.0000 | 0.04 | 0.4 s |
| 9x tiled | 4 | trip largest unit | 49.910 / 49.910 | 0.0398 | -0.0398 | 0.0000 | 0.12 | 0.5 s |

Validation: the measured primary-only steady-state frequency equals the analytic value (including the governor dead band) in all 8 cases; the initial rate of change of frequency equals the swing-equation value within 10 % in the unit test.

Limits: AGC does not lower the frequency nadir (AGC is slower than the primary response, as expected). ACE does not settle inside 600 s in 3 of 8 cases (3 %/min unit ramp limit); IEEE-118 trip with 4 areas leaves 3 MW of ACE at the end. All dynamic parameters are synthetic. Without machine damping the 4-area lumped model is oscillatory-unstable (flagged by the simulator); the defaults avoid that but this is a modelling choice, not a measured property of any real system.

## Dynamic security (tools/bench_dsa.py, raw: docs/bench_dsa.json; synthetic dynamic data, 3.0 s transient window, dt 10 ms)

| Case | Voltage: margin / worst N-1 (4 outages) / time | Transient: faults / unstable / min CCT / time per fault | Small-signal: modes / lowest damping / frequencies / time |
|---|---|---|---|
| IEEE-118 (54 units) | 114 % / 26 % / 4.6 s | 118 / 0 / 0.268 s / 0.03 s (clearing 0.12 s, worst angle spread 67°) | 53 / 0.9 % / 0.48 to 1.70 Hz / 0.02 s |
| 9x tiled (1,062 buses, 486 units) | 76 % / 51 % / 10.8 s | 12 of 1,062 faults / 0 / 0.278 s / 0.94 s | 485 / 0.9 % / 0.41 to 1.70 Hz / 0.9 s |

Validation: closed-form 2-bus loading margin, SMIB equal-area CCT (within 4 ms), analytic swing-mode frequency and damping, finite-difference check of the synchronising matrix (see tests/test_dsa.py).

Limits: classical model with synthetic dynamic data; no exciters, PSS or governors; the transient screen on the tiled case covered 12 faults, not all 1,062 (about 17 min if run in full, serial); the 114 % and 76 % margins depend on the uniform load-and-generation scaling direction and synthetic Q limits; no comparison with a commercial simulator.

## Outage scheduler (tools/bench_outage.py, raw: docs/bench_outage.json)

| Case | Outage study (3 runs) | Switching plan (6 steps) | Conflict scan (2 requests) | AC power flow / full AC N-1 |
|---|---|---|---|---|
| IEEE-118 (live stack, via gateway) | 3.1 to 4.6 s | 0.11 to 0.14 s | 9.4 s | 0.01 s / 1.6 s |
| 9x tiled (1,062 buses, bus-branch only) | not run end to end (estimate 64 s = 2 x N-1 + 2 x PF) | not measured | not measured | 0.03 s / 31.9 s |

The 118-bus figures are measured on the running stack. The tiled case has no node-breaker CIM, so only the two building blocks were measured there and the study time is an extrapolation, marked as such.

Validation: new criticals equal a brute-force N-1 on the outaged network; each plan step's check equals a direct power flow; a genuine conflict (BR0017 and BR0019 isolate Busbar 14, 14 MW) is restricted for each alone and unsafe together.

Limits: breaker-only isolation (no isolators or earths); full AC N-1 twice per study, so large networks need the distributed N-1 of a later item; the conflict scan studies each distinct overlapping set once; ratings are synthetic.

## Operator log, handover and communications loss (tools/bench_logbook.py, tools/e2e_comms.py, Java EngineTests; raw: docs/bench_logbook.json)

| Case | Measured |
|---|---|
| Outstation link loss to all points stale in the core (live, 1,707 points, 118 substations) | 0.6 s (e2e_comms; poll-limited, so an upper bound) |
| Recovery after outstation restart (live) | 1 to 6 s here; the client back-off is up to 32 s so the worst case is longer |
| Silent FEP to feed declared lost | 20 s (the configured timeout) |
| Recovery after FEP restart | 1 s |
| Engine cost to mark every point stale and raise the alarms, 118-size (1,652 points, 118 substations) | 7 ms, recovery 11 ms |
| Same at 9x tiled size (14,868 points, 1,062 substations, 1,062 alarms) | 13 ms, recovery 24 ms |
| Log append, 300 live calls | p50 44 ms, p95 48 ms |
| Log read (200 entries), search, comms (118 substations), handover snapshot | p50 44 ms each, p95 44.6 to 50 ms |

The flat 44 ms per API call is the same for trivial calls, so it is a floor from the test setup (probably loopback delayed ACK), not server compute; the engine rows are in-process timings with synthetic point sets (14 points per substation, not the real CIM point list at tiled size).

Limits: the tiled case has no CIM and no live outstation, so the 1,062-substation row is an engine-only measurement; detection latency on a real WAN link depends on IEC 104 keep-alive timers, which the simulator does not exercise beyond a closed socket.

## Load-shed support (tools/bench_shed.py, raw: docs/bench_shed.json; synthetic dynamics and priorities)

UFLS assessment (60 s window, default illustrative stage table, limits: lowest frequency 48.8 Hz and 49.5 Hz after 60 s):

| Case | Scenario | Lost | Lowest f without / with shedding | f after 60 s with | Shed | Within limits |
|---|---|---|---|---|---|---|
| IEEE-118 | two largest units | 1,123 MW (26 %) | 47.24 / 48.97 Hz | 49.25 Hz | 12 % (2 stages) | no |
| IEEE-118 | 35 % of generation | 1,600 MW (37 %) | 45.02 / 48.60 Hz | 49.88 Hz | 30 % (4 stages) | no |
| 9x tiled, 1,062 buses | 25 % of generation | 10,111 MW | 47.24 / 48.97 Hz | 49.25 Hz | 12 % (2 stages) | no |
| 9x tiled | 35 % of generation | 13,927 MW | 45.28 / 48.75 Hz | 48.76 Hz | 20 % (3 stages) | no |

Run time 0.06 s (118) and 0.10 s (1,062). Loss of a single unit passes at 1,062 buses (the unit is 1.5 % of generation) and fails the 49.5 Hz limit at 118 buses (14 % of generation, 5 % reserve).

Rotating roster, 12 one-hour periods, deficit 3 % of load, AC check per period:

| Case | Deficit | Eligible loads | Rotating: loads shed / fairness / worst load MWh / time | Same loads every period: loads shed / fairness / worst load MWh | Periods with new overloads (rotating / static) |
|---|---|---|---|---|---|
| IEEE-118 | 127 MW | 71 | 45 / 0.42 / 113 / 0.42 s | 2 / 0.03 / 936 | 8 of 12 / 0 of 12 |
| 9x tiled | 1,145 MW | 662 | 338 / 0.30 / 277 / 1.5 s | 7 / 0.01 / 2,208 | 12 of 12 / 12 of 12 |

Validation: swing-equation initial ROCOF, droop steady state, stage shedding final frequency (see tests/test_shed.py); AC check against an independent power flow.

Limits: synthetic dynamics, illustrative relay table, synthetic priorities; the tiled case has one load block per load bus built for the benchmark (not a CIM load list); rotation improves the fairness index 16 to 30x but leaves network overloads in many periods, so it is a fairness tool plus a check, not a security-constrained optimiser.

## Dispatcher training simulator (tools/bench_dts.py, raw: docs/bench_dts.json)

| Case | Scenario library | Session start | One action or event (AC power flow) |
|---|---|---|---|
| IEEE-118 | 0.01 s | 0.01 s | 13 ms |
| 9x tiled, 1,062 buses (one load block per load bus) | 0.03 s | 0.03 s | 28 ms |

Time stepping without events costs nothing (the state is reused), so a 60 s step is only as slow as the events inside it. Limits: AC only, no frequency dynamics; sessions are in memory.

## Secure field links (go test -bench, tools/e2e_secure.py; loopback, 2-core sandbox)

| Operation | Result |
|---|---|
| IEC 104 select + execute round trip, plain | 67 us |
| Same with challenge/reply authentication | 126 us (adds two round trips and two HMACs per control ASDU) |
| IEC 101 class-1 event drained by the master (in-memory link, one event per frame) | 18 us per event (about 55,000 events/s of protocol processing) |

**At tiled ~1k-bus scale** (`go test -bench 'GI15k|Connect|Handshake' ./internal/secure`; one outstation holding 14,868 analog points, the point count of the tiled model in the logbook benchmark; loopback; 2 invocations of 3-5 runs each, ranges shown):

| Operation | Result |
|---|---|
| General interrogation of 14,868 points, plain TCP | 11.5 to 18.4 ms |
| Same over mutual TLS 1.3 | 10.5 to 19.0 ms (ranges overlap: no difference measurable on this host, which has AES hardware support) |
| TCP connect (loopback) | 27 to 43 us |
| Mutual TLS 1.3 handshake (ECDSA certificates, both sides verified) | 0.84 to 1.10 ms |

The 118-bus case is the same code with about 8 times fewer points and is not separately timed. These are protocol-processing times only: no WAN latency, no 62351-5 style key exchange, a synthetic handler, and the receiver counts objects without storing them. Run-to-run noise on the shared 2-core host is about +-30 %.

Limits: the IEC 101 figure has no baud-rate limit; on a real 9,600 baud line a 15-byte frame takes about 16 ms, so the line, not the software, bounds throughput. Loopback hides network latency. The live run in `tools/e2e_secure.py` showed no measurable change in data freshness with TLS enabled. Numbers are from one run in a shared sandbox.

## Model governance (cargo test --release bench_governance -- --ignored, 20 repetitions; tools/e2e_models.py)

| Model | Buses / entities | Diff | Three-way merge | Apply 1 patch op | Validate | Split into 5 regions | Hierarchical merge of 5 |
|---|---|---|---|---|---|---|---|
| IEEE-118 | 118 / 1,785 | 22 ms | 40 ms | 15 ms | 2 ms | 57 ms | 15 ms |
| 9x tiled | 1,062 / 16,065 | 208 ms | 387 ms | 149 ms | 24 ms | 863 ms | 195 ms |

Both rebuild the original exactly from the split children (checked in the same run). Limits: the diff and merge convert the whole model to JSON values on every call, so cost grows linearly with model size (about 10x entities, 10x time); no incremental diff. The tiled model is nine renamed copies of IEEE-118 with no tie lines between tiles, built for the benchmark. One sandbox, one run, no network latency.


## Model exchange (CIMXML, legacy import)

Measured by `cargo test --release bench_exchange -- --ignored --nocapture` in `services/cim-pds`, 20 repetitions (5 for the tiled model), single thread, in-process (no HTTP):

| Model | Buses / objects | CIMXML size | Build graph | Write | Parse | Graph to model | Diff write | Diff apply |
|---|---|---|---|---|---|---|---|---|
| IEEE-118 | 118 / 3,909 | 1.8 MB | 13 ms | 12 ms | 29 ms | 13 ms | 2 ms | 5 ms |
| 9x tiled | 1,062 / 35,109 | 16.2 MB | 153 ms | 133 ms | 270 ms | 147 ms | 36 ms | 63 ms |

Legacy import on IEEE-118: MATPOWER (19 kB) 7.9 ms, PSS/E RAW (23 kB) 8.2 ms (parse, convert to node-breaker, canonicalise). The difference file for a one-load change is 1.2 kB on both models. Limits: the whole document is held in memory (the import route accepts 64 MB; a 16 MB file takes about 0.6 s end to end excluding validation and the commit); the legacy importers were measured on the 118-bus files only, because no larger public file is bundled; HTTP, gateway proxy and disk persistence are not included, so end-to-end times are higher (the live round trip of a 1.8 MB export and re-import runs inside `tools/e2e_interop.py` without a timing assertion).


## Scale hardening (cold-start power flow, sparse bad-data identification)

Measured by the script in `tests/test_scale.py` plus a timing run on this host (synthetic tiled IEEE-118, each case with two planted 25-sigma gross errors on branch flows, full measurement set P/Q injection, V, P/Q flows both ends):

| Buses | Measurements | Cold PF | Strategy | State estimation with bad-data removal | Residual method | Errors found |
|---|---|---|---|---|---|---|
| 1,062 | 9,978 | 57 ms | newton | 2.0 s | dense | 2 of 2 |
| 5,074 | 47,718 | 101 ms | newton | 1.2 s | sparse-candidates | 2 of 2 |
| 10,030 | 94,338 | 5,613 ms | levenberg-marquardt (25 it) | 2.7 s | sparse-candidates | 2 of 2 |

Before this item, the 10,030-bus flat start did not converge in 30 iterations. Limits: one random seed per size in this table (the tests use other seeds); the estimator times include the removals (two rounds); timings are single-process on this host.


## Distributed contingency analysis

`tools/e2e_distributed.py`, IEEE-118, 186 N-1 cases, coordinator plus two peers as separate uvicorn processes on this host (the host has 2 CPU cores): single process 5.4 s, three processes 10.3 s. The merged result is identical to the single-process one (checked case by case), also after killing a peer. The distributed mode is slower here by design of the test rig: the three processes share one machine and each shard repeats the model load and the base-case solve. No multi-host or large-model timing was measured, so no speed-up is claimed.


## Control-centre resilience (item 19)

Measured by `tools/e2e_quorum.py` and unit tests on one host (3 core nodes as processes, loopback): after the leader process is killed a standby is elected and accepts commands in about 4.7 s; an isolated leader stops accepting commands when its 1.2 s lease runs out (observed step-down about 1.0 s after isolation); 5-node chaos test (random partitions and restarts, fixed seed) shows no two leaders in the same term. Token verification and the proxy were not benchmarked for throughput. Limits: single host, no real network latency or loss, one seed for the chaos run, timings come from the test logs of the earlier run and were not re-measured after the later changes in this item.


## Historian storage (tools/bench_hist.py)

204,840 synthetic points (1,707 tags x 120 scans, the IEEE-118 point list), batches of 1,707, one process, this 2-core host, PostgreSQL 16.15 on loopback, default settings (no tuning, `synchronous_commit` on):

| Store | Write rate | Trend query (raw, avg of 30) | Bytes per raw row (incl. aggregate) |
|---|---|---|---|
| SQLite (WAL) | 92,656 rows/s | 0.2 ms | 61 |
| PostgreSQL (set-based unnest inserts, pg8000 driver) | 16,186 rows/s | 13.1 ms | 180 |

The first PostgreSQL version (psycopg, row by row) reached 7,720 rows/s; the set-based rewrite with psycopg reached 31,211 rows/s. The driver was then changed to pg8000 (pure Python, BSD-3) because psycopg is LGPL-3.0 and the project allows only permissive libraries; that cost half the write rate and doubled query time. Run-to-run variation on this shared 2-core host was not measured. Limits: single run per store, synthetic regular data, no concurrent readers, no compression (a TimescaleDB compression policy would shrink the 180 bytes), and 16k rows/s is far below a 20 lakh-point telemetry target, which would need partitioned parallel writers and was not tested.

## Synchrophasor (C37.118) stream (tools/e2e_pmu.py, go test ./internal/pmubridge; SYNTHETIC PMU)

Measured on the live stack with `pmu-sim` at 30 frames/s, one phasor: the FEP forwards one value set per second (the last frame in each window), so the core sees 4 points/s per PMU instead of 120. Conversion arithmetic is checked against hand-computed values (|3+4j| = 5, angle 53.1301 degrees, polar radians to degrees). On loss of the stream the points are marked not-good (the e2e allows 25 s; the actual detection time was not recorded) and recover when the stream returns. Not measured: more than one PMU, a PDC aggregating many PMUs, real clock-sync quality, end-to-end latency in milliseconds (the 1 s thinning makes the stream unsuitable for wide-area control; it is for monitoring and trending only).

## OPC UA server, reduced (go test -bench Read1000 ./internal/opcua; tools/e2e_opcua.py)

One Read service call for 1,000 variables in an address space of 14,868 points (the tiled ~1k-bus point count): 5.5 ms per call (3 runs: 5.46 to 5.62 ms), loopback, 2-core host, including the Go test client's decoding. Not measured: concurrent sessions, Browse of the full space at that size, memory per session, behaviour with a real client at 14,868 points (the live check used the 1,707-point IEEE-118 space).
