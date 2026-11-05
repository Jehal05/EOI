# Demo script (about 35 minutes; every step can be cut)

Everything below runs on synthetic or public IEEE data. Nothing is connected to real equipment. Say that first.

**Prerequisites.** `tools/stack.sh up` starts cim-pds, ems-analytics, the outstation and field simulators, the FEP, the historian and scada-core A/B. UI: http://127.0.0.1:8083. Dev users are in `/tmp/gs/coreA/dev-credentials.txt` (alice operator, bob viewer, carol engineer, admin, north; password `demo-password-<user>`; TOTP secret in the file). Other stack modes: `GS_QUORUM=1 GS_ASYM=1 GS_MTLS=1` (three cores, signed tokens, mTLS between cores), `GS_PG=1 GS_BUS=1` (PostgreSQL historian and Redis bus), `GS_PMU=1` (synthetic synchrophasor stream), `GS_OPCUA=1` (read-only OPC UA on 127.0.0.1:4840: browse it with any OPC UA client), `GS_TLS=1 GS_DNP3=1` (secure field links).

## A. Operations (12 min)
1. **Login with MFA** (alice). Wrong password; lockout after 5 failures.
2. **Grid map**: 118 buses, 186 branches, loading colours, leader/standby and link state in the header. Switch the three themes and EN/Hindi: severity is always shape plus colour.
3. **Incident**: `curl -XPOST :8091/event -d '{"trip":["BRK-BR0030-F","BRK-BR0030-T"]}'`. Alarm banner, ISA-18.2 first-out versus consequential grouping, acknowledge (A / Shift+A), shelve with a reason.
4. **Single-line diagram** (SS-0010): select-before-operate with MFA and reason. Show the interlock refusal, a viewer's 403 and a jurisdiction refusal (`north` on a Western substation).
5. **Communications loss**: stop the outstation; points go stale at once, the substation shows lost, the command is blocked; restart and watch recovery (Log and handover, Alarms).
6. **Log and handover**: write a log entry, amend it, create a shift handover with its alarm snapshot.
7. **Time travel**: `/api/verify-replay` (System health, Verify) replays the whole HMAC-chained log; flip one byte in the log file and show the tamper detection.

## B. Analysis applications (15 min, each is one screen, results are advisory)
8. **State estimator and contingency**: chi-square J against its threshold; inject a gross error and watch it removed. N-1 board for 186 cases; N-2 tab (17,205 pairs, 81 AC-verified).
9. **Short circuit**: all fault types at a bus, breaker-duty screen against synthetic ratings.
10. **Optimal dispatch**: DC OPF with LMPs and congestion prices; switch on N-1 security constraints.
11. **Security enhancement**: redispatch / line-opening / load-shed plan for an N-1 overload; the plan is re-checked with AC power flow before it is shown.
12. **Corridor capability**: transfer limit with TRM and ATC and the limiting element.
13. **Forecast**: load, solar, wind with 80/95 % bands whose coverage is measured on held-out data. Show the AI/ML switch (admin) and the audit record each use leaves.
14. **AGC**, **Dynamic security** (voltage margin, critical clearing time, small-signal modes) and **Load shedding** (UFLS, rotating roster): synthetic dynamic parameters are echoed in every result.
15. **Outages**: request, automatic security study, two-person approval, switching plan.
16. **Training simulator**: run a built-in scenario as a trainee and read the score.

## C. Models, interchange and resilience (8 min)
17. **Model manager / governance**: change a limit in the CIM model; the point list and alarm limits follow. Propose, review with a second user, merge.
18. **Migration and exchange**: export CIMXML, import the MATPOWER and PSS/E IEEE-118 files, show the power flow agrees with the packaged model; run the parallel-run comparison.
19. **Resilience**: with `GS_QUORUM=1`, kill the leader; a new one is elected, the term (fencing token) increases and the FEP refuses commands carrying the old term. Restart the old node and watch it catch up.
20. **Field protocols**: System health lists what is implemented (IEC 104, 101, DNP3, C37.118 read-only) and, in the same card, what is NOT (ICCP, IEC 61850 MMS). With `GS_PMU=1` show `PMU26.F`, `.V1.MAG`, `.V1.ANG`, then stop `pmu-sim` and show the points go not-good.
21. **Evidence**: open `docs/TRACEABILITY.md` and `docs/CONFORMANCE.md`; run `tools/trace.py` (it fails on a dangling reference) and `tools/eoi_score.py` (it scores nothing until the real EOI weights are filled in).

## Say these things out loud
- It is a working prototype, not a certifiable product. `docs/STATUS.md` lists, in the gap list, what stands between it and certification.
- Protocol stacks and security functions are our own code tested against our own simulators; none has been tested against vendor equipment or conformance suites.
- No formal usability study has been run; the harness is in `tools/usability.py`.
