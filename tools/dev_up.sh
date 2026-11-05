#!/usr/bin/env bash
# Start cim-pds + ems-analytics for local development (dev secret; NOT for production).
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
export CIM_PDS_DEV=1 EMS_DEV=1 CIM_DATA_DIR="$ROOT/data"
mkdir -p /tmp/gs && rm -rf /tmp/gs/state
CIM_STATE_DIR=/tmp/gs/state "$ROOT/services/cim-pds/target/release/cim-pds" > /tmp/gs/cim.log 2>&1 &
echo $! > /tmp/gs/cim.pid
(cd "$ROOT/services/ems-analytics" && /home/claude/venv/bin/python -m uvicorn ems.api:app --host 127.0.0.1 --port 8082 > /tmp/gs/ems.log 2>&1 &
 echo $! > /tmp/gs/ems.pid)
for i in $(seq 1 60); do curl -s localhost:8081/health >/dev/null && curl -s localhost:8082/health >/dev/null && break; sleep 0.2; done
echo "cim-pds :8081, ems-analytics :8082"
