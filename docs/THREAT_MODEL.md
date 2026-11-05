# Threat model (STRIDE, initial)
| Threat | Asset | Mitigation in prototype | Gap |
|---|---|---|---|
| Spoofing | operator identity, commands | password + TOTP MFA, signed command tokens | hardware tokens, SSO |
| Tampering | CIM model, audit | SHA-256 hash-chained version/audit log | external anchoring, HSM signing |
| Repudiation | control actions | every command audited with user, time, outcome | WORM storage |
| Information disclosure | telemetry, model | local-only deployment, no external calls, TLS option | mTLS everywhere, encryption at rest |
| DoS | FEP/API | rate limiting, bounded queues | network-level controls, IDS/IPS |
| Elevation | RBAC | role + jurisdiction checks server-side | formal policy engine |
OT/IT: reference segmentation: field zone / FEP DMZ / control zone / corporate. Documented only.

## Reality pass (what is implemented vs designed)
Implemented and tested: token auth (HMAC, expiry), RBAC, jurisdiction checks, MFA TOTP with single-use steps, PBKDF2 (210k), lockout, command rate limit, interlocks, SBO with audit written before the field command, HMAC-chained tamper-evident log, strict CSP, in-memory UI token.
Designed but NOT implemented: mTLS between services, asymmetric token signing, IEC 62351, HSM/key rotation, signed updates, IDS, network segmentation reference design, replay-protection nonce on field commands. Shared-secret tokens mean any service holding the secret can mint tokens: a known weakness.
