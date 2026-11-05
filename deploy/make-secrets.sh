#!/usr/bin/env bash
# Generate the secret files for docker-compose.yml (./secrets) or, with --k8s NAMESPACE, print a kubectl command for the chart.
# Uses the first-party pki tool (services/fep-gateway/bin/pki). Safe to re-run: it refuses to overwrite an existing directory.
set -euo pipefail
cd "$(dirname "$0")"; PKI="$(pwd)/../services/fep-gateway/bin/pki"; OUT="${OUT:-secrets}"
[ -x "$PKI" ] || { echo "build the pki tool first: (cd ../services/fep-gateway && go build -o bin/pki ./cmd/pki)"; exit 1; }
[ -e "$OUT" ] && { echo "$OUT exists; remove it first"; exit 1; }
umask 077; mkdir -p "$OUT"
IDS="${CORE_IDS:-A B C}"                      # compose uses A B C; the Helm chart uses the pod ordinals 0 1 2 (CORE_IDS="0 1 2")
for n in $IDS; do "$PKI" tokenkey -dir "$OUT" -cn "core-$n" -max-role admin >/dev/null; done
"$PKI" tokenkey -dir "$OUT" -cn fep -max-role operator -sub-prefix fep-gateway >/dev/null
"$PKI" tokenkey -dir "$OUT" -cn historian -max-role viewer -sub-prefix historian >/dev/null
"$PKI" tokenkey -dir "$OUT" -cn ems -max-role viewer -sub-prefix ems-analytics >/dev/null
"$PKI" tokenkey -dir "$OUT" -cn outstation -max-role viewer -sub-prefix outstation >/dev/null
head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n' > "$OUT/audit.key"
head -c 24 /dev/urandom | od -An -tx1 | tr -d ' \n' > "$OUT/pg_password"
printf 'postgresql://postgres:%s@postgres:5432/gs' "$(cat "$OUT/pg_password")" > "$OUT/hist_dsn"
echo "secrets written to $OUT (trust.json lists only public keys; *.tokkey are private, mode 600)"
