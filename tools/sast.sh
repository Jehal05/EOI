#!/usr/bin/env bash
# Static analysis over all first-party code. Prints a summary; exit 1 if any rule marked BLOCK fires.
# Tools used: go vet, cargo clippy, bandit, npm audit, pip-audit (when the network allows), tsc, plus the grep rules below
# (a small substitute for semgrep: this environment has no rule registry). Not a replacement for an independent code audit.
cd "$(dirname "$0")/.."; fail=0
hdr() { printf '\n== %s\n' "$1"; }
rule() { # rule NAME BLOCK|WARN REGEX PATHS...  (comment lines and tests are excluded)
  local name=$1 sev=$2 re=$3; shift 3
  local hits; hits=$(grep -rnE "$re" "$@" 2>/dev/null | grep -vE "/(test|tests|node_modules|dist|target|out|out-test|testdata)/|_test\.go|\.test\.|fuzz" | grep -vE ":[0-9]+:\s*(//|\*|#|/\*)" | grep -v "sast:ok" || true)
  if [ -n "$hits" ]; then printf '%s [%s]\n%s\n' "$name" "$sev" "$(echo "$hits" | head -8)"; [ "$sev" = BLOCK ] && fail=1; else printf 'ok    %s\n' "$name"; fi
}
hdr "go vet"; (cd services/fep-gateway && go vet ./... && echo "ok    go vet clean")
hdr "cargo clippy (warnings counted, not blocking)"; (cd services/cim-pds && cargo clippy --release 2>&1 | grep -cE "^warning" | sed 's/^/warnings: /')
hdr "bandit (Python product code)"; /home/claude/venv/bin/bandit -q -r services/ems-analytics/ems services/historian/hist -f txt 2>&1 | grep -E "Issue:|Location" | paste - - | head -10
hdr "TypeScript"; (cd ui && npx tsc --noEmit && echo "ok    tsc strict clean")
hdr "dependency advisories"
(cd ui && timeout 60 npm audit --omit=dev 2>&1 | tail -4) || true
timeout 120 /home/claude/venv/bin/pip-audit -r services/ems-analytics/requirements.txt -r services/historian/requirements.txt 2>&1 | tail -6 || true
hdr "rules: Java"
rule "no process execution"              BLOCK 'ProcessBuilder|Runtime\.getRuntime\(\)\.exec' services/scada-core/src
rule "no Java deserialisation"           BLOCK 'ObjectInputStream|readObject\(' services/scada-core/src
rule "no MD5/SHA-1"                      BLOCK 'getInstance\("(MD5|SHA-?1)"' services/scada-core/src
rule "no trust-all TLS"                  BLOCK 'TrustManager\[\]|setHostnameVerifier|ALLOW_ALL' services/scada-core/src
rule "no stack traces to clients"        WARN  'printStackTrace' services/scada-core/src
hdr "rules: Go"
rule "no InsecureSkipVerify"             BLOCK 'InsecureSkipVerify' services/fep-gateway/cmd services/fep-gateway/internal
rule "no math/rand in security code"     BLOCK 'math/rand' services/fep-gateway/internal/token services/fep-gateway/internal/secure
rule "no shell execution"                BLOCK 'exec\.Command' services/fep-gateway/cmd services/fep-gateway/internal
rule "no unsafe"                         BLOCK '"unsafe"' services/fep-gateway/cmd services/fep-gateway/internal
hdr "rules: Rust"
rule "no unsafe blocks"                  BLOCK '\bunsafe\b' services/cim-pds/src
rule "no transmute"                      BLOCK 'transmute' services/cim-pds/src
rule "unwrap/expect on request paths"    WARN  '\.unwrap\(\)|\.expect\(' services/cim-pds/src/interop.rs services/cim-pds/src/main.rs services/cim-pds/src/auth.rs
hdr "rules: Python"
rule "no eval/exec/pickle"               BLOCK '\beval\(|\bexec\(|pickle\.|marshal\.' services/ems-analytics/ems services/historian/hist
rule "no shell=True"                     BLOCK 'shell\s*=\s*True' services/ems-analytics/ems services/historian/hist
rule "no verify=False"                   BLOCK 'verify\s*=\s*False' services/ems-analytics/ems services/historian/hist
rule "no yaml.load"                      BLOCK 'yaml\.load\(' services/ems-analytics/ems services/historian/hist
rule "no assert for control flow"        BLOCK '^\s*assert ' services/ems-analytics/ems services/historian/hist
hdr "rules: UI"
rule "no innerHTML / eval"               BLOCK 'dangerouslySetInnerHTML|\.innerHTML|\beval\(|new Function\(' ui/src
rule "no token in web storage"           BLOCK '(local|session)Storage[^;]*(token|Token|jwt|bearer)' ui/src
hdr "rules: secrets"
rule "dev secret only behind a dev flag" WARN  'dev-secret-do-not-use-in-prod' services/scada-core/src services/cim-pds/src services/fep-gateway/cmd services/fep-gateway/internal services/ems-analytics/ems services/historian/hist
rule "no private keys / tokens committed" BLOCK 'BEGIN (EC |RSA |OPENSSH )?PRIVATE KEY|AKIA[0-9A-Z]{16}' services ui/src tools deploy docs
echo; [ $fail = 0 ] && echo "SAST: no blocking findings" || echo "SAST: BLOCKING findings above"; exit $fail
