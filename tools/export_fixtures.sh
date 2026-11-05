#!/usr/bin/env bash
# Start cim-pds on a throwaway state dir, export bus-branch views for tests, stop it.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BIN="$ROOT/services/cim-pds/target/release/cim-pds"
export CIM_PDS_DEV=1 CIM_DATA_DIR="$ROOT/data" CIM_PDS_ADDR=127.0.0.1:8099
unset CIM_STATE_DIR
"$BIN" >/tmp/cim-fixture.log 2>&1 & PID=$!
trap 'kill $PID 2>/dev/null || true' EXIT
for i in $(seq 1 50); do curl -s localhost:8099/health >/dev/null 2>&1 && break; sleep 0.1; done
TOK=$("$BIN" mint fixture viewer '*' 600 2>/dev/null)
for id in ieee14 ieee118; do
  curl -sf -H "Authorization: Bearer $TOK" "localhost:8099/models/$id/busbranch" -o "$ROOT/data/$id.busbranch.json"
  echo "wrote data/$id.busbranch.json"
done
