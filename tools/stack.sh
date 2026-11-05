#!/usr/bin/env bash
# Start/stop the local development stack (dev secrets; NOT for production).
#   tools/stack.sh up|down|status
# Options (environment): GS_TLS=1 mutual TLS between FEP and outstation (dev PKI in /tmp/gs/pki);
#   GS_AUTH=1 application-layer command authentication (HMAC challenge/reply, random key in /tmp/gs/auth.key);
#   GS_DNP3=1 the FEP reaches common address 1 over DNP3 (port 20000, TLS if GS_TLS=1) and all others over IEC 104;
#   GS_ASYM=1 Ed25519 service tokens: one key pair per service (/tmp/gs/tokkeys), every verifier trusts only the public keys, no shared HMAC secret;
#   GS_MTLS=1 (with GS_QUORUM=1) the core nodes' replication/election traffic runs over mutual TLS through bin/mtlsproxy (dev PKI in /tmp/gs/mpki);
#   GS_PG=1 the historian stores in PostgreSQL (scratch server on 127.0.0.1:5433, database histlive);
#   GS_BUS=1 the cores publish telemetry to a Redis Streams bus (127.0.0.1:6390) and the historian consumes from it instead of following a core;
#   GS_PMU=1 adds a synthetic C37.118 PMU (port 4712) on substation SS-0026: its points come from the point-list overlay CIM_PMU_FILE (/tmp/gs/pmu.json);
#   GS_OPCUA=1 starts the FEP's read-only OPC UA server on 127.0.0.1:4840 (SecurityPolicy None, anonymous);
#   GS_IEC101=1 the FEP talks IEC 101 (FT1.2 over TCP, port 2405) instead of IEC 104.
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PY=/home/claude/venv/bin/python
export GS_DEV=1 CIM_PDS_DEV=1 EMS_DEV=1 CIM_DATA_DIR="$ROOT/data"
D=/tmp/gs; mkdir -p $D
down() {
  for n in cim ems out pmu field fep sink coreA coreB coreC hist mtlsA mtlsB mtlsC; do
    [ -f $D/$n.pid ] && kill "$(cat $D/$n.pid)" 2>/dev/null; rm -f $D/$n.pid
  done
  # also reap strays from earlier runs (stale processes silently keep ports and old state)
  pkill -f "[g]s\.core\.Main" 2>/dev/null; pkill -f "[h]ist\.app:app" 2>/dev/null; pkill -x cim-pds 2>/dev/null; pkill -x outstation-sim 2>/dev/null; pkill -x pmu-sim 2>/dev/null; pkill -x fep 2>/dev/null; pkill -x mtlsproxy 2>/dev/null
  # bracket trick: the pattern must not match this shell's own command line
  pkill -f "[e]ms\.api:app" 2>/dev/null; pkill -f "tools/[f]ield_sim\.py" 2>/dev/null; pkill -f "tools/[i]ngest_sink\.py" 2>/dev/null
  sleep 1
  # anything that ignored SIGTERM (e.g. worker processes holding the listening socket) is killed outright
  pkill -9 -x cim-pds 2>/dev/null; pkill -9 -x outstation-sim 2>/dev/null; pkill -9 -x fep 2>/dev/null
  pkill -9 -f "[e]ms\.api:app" 2>/dev/null; pkill -9 -f "tools/[f]ield_sim\.py" 2>/dev/null; pkill -9 -f "[g]s\.core\.Main" 2>/dev/null
  sleep 0.5
}
# asymmetric token mode (the 'e2e' key is the test harness's admin identity: tools run with GS_TOKEN_TRUST=$D/tokkeys/trust.json GS_TOKEN_KEY=$D/tokkeys/e2e.tokkey): every service gets its own signing key, capped to the role it needs
AE_cim=env; AE_ems=env; AE_hist=env; AE_fep=env; AE_out=env; AE_A=env; AE_B=env; AE_C=env; AE_field=env
asym_setup() {
  local K=$D/tokkeys PKI="$ROOT/services/fep-gateway/bin/pki"; rm -rf $K
  $PKI tokenkey -dir $K -cn core-A -max-role admin >/dev/null && $PKI tokenkey -dir $K -cn core-B -max-role admin >/dev/null && $PKI tokenkey -dir $K -cn core-C -max-role admin >/dev/null \
   && $PKI tokenkey -dir $K -cn fep -max-role operator -sub-prefix fep-gateway >/dev/null \
   && $PKI tokenkey -dir $K -cn historian -max-role viewer -sub-prefix historian >/dev/null \
   && $PKI tokenkey -dir $K -cn ems -max-role viewer -sub-prefix ems-analytics >/dev/null \
   && $PKI tokenkey -dir $K -cn field -max-role viewer -sub-prefix field-sim >/dev/null \
   && $PKI tokenkey -dir $K -cn e2e -max-role admin >/dev/null \
   && $PKI tokenkey -dir $K -cn outstation -max-role viewer -sub-prefix outstation >/dev/null || return 1
  chmod 600 $K/*.tokkey
  for n in cim:outstation ems:ems hist:historian fep:fep out:outstation A:core-A B:core-B C:core-C field:field; do
    eval "AE_${n%%:*}=\"env GS_TOKEN_TRUST=$K/trust.json GS_TOKEN_KEY=$K/${n##*:}.tokkey\""
  done
}
wait_http() { for i in $(seq 1 100); do curl -sf -m 2 "$1" >/dev/null && return 0; sleep 0.2; done; echo "TIMEOUT waiting for $1"; return 1; }
case "$1" in
 down) down;;
 status) for p in 8081 8082 8083 8183 8283 8084 8085 8090 8091; do printf "%s " $p; curl -s -m2 localhost:$p/health | head -c 90; echo; done;;
 up)
  down; rm -rf $D/state
  if [ -n "$GS_ASYM" ]; then asym_setup || exit 1; fi
  [ -n "$GS_PMU" ] && { echo '[{"substation":"SS-0026","prefix":"PMU26","phasors":["V1"],"analogs":0}]' > $D/pmu.json; CIMPMU="CIM_PMU_FILE=$D/pmu.json"; }
  $AE_cim $CIMPMU CIM_STATE_DIR=$D/state "$ROOT/services/cim-pds/target/release/cim-pds" > $D/cim.log 2>&1 & echo $! > $D/cim.pid
  (cd "$ROOT/services/ems-analytics" && $AE_ems $PY -m uvicorn ems.api:app --host 127.0.0.1 --port 8082 > $D/ems.log 2>&1 & echo $! > $D/ems.pid)
  wait_http localhost:8081/health && wait_http localhost:8082/health || exit 1
  TOK=$($AE_out "$ROOT/services/cim-pds/target/release/cim-pds" mint outstation viewer '*' 86400 2>/dev/null | tail -1)
  OSEC=""; FSEC=""; RTU=""
  PKI="$ROOT/services/fep-gateway/bin/pki"
  if [ -n "$GS_TLS" ]; then
    rm -rf $D/pki; $PKI ca -dir $D/pki >/dev/null && $PKI issue -dir $D/pki -cn outstation -dns outstation -server >/dev/null && $PKI issue -dir $D/pki -cn fep -dns fep >/dev/null || exit 1
    OSEC="$OSEC -cert $D/pki/outstation.pem -key $D/pki/outstation.key -ca $D/pki/ca.pem -allow fep"
    FSEC="$FSEC -tls-ca $D/pki/ca.pem -tls-cert $D/pki/fep.pem -tls-key $D/pki/fep.key -tls-name outstation"
  fi
  if [ -n "$GS_AUTH" ]; then
    head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n' > $D/auth.key; chmod 600 $D/auth.key
    OSEC="$OSEC -auth-key-file $D/auth.key"; FSEC="$FSEC -auth-key-file $D/auth.key"
  fi
  if [ -n "$GS_IEC101" ]; then OSEC="$OSEC -listen101 127.0.0.1:2405 -link101 1"; RTU="-rtu iec101+tcp://127.0.0.1:2405#1"; fi
  if [ -n "$GS_DNP3" ]; then OSEC="$OSEC -listen-dnp3 127.0.0.1:20000 -dnp3-addr 10 -dnp3-ca 1"; RTU="-rtu 127.0.0.1:2404@2-100000 -rtu dnp3://127.0.0.1:20000#10@1"; fi
  if [ -n "$GS_PMU" ]; then
    (cd "$ROOT/services/fep-gateway" && bin/pmu-sim -listen 127.0.0.1:4712 -id 7 -station PMU26 > $D/pmu.log 2>&1 & echo $! > $D/pmu.pid)
    [ -z "$RTU" ] && RTU="-rtu 127.0.0.1:2404"
    RTU="$(echo "$RTU" | sed 's#-rtu 127.0.0.1:2404\( \|$\)#-rtu 127.0.0.1:2404@1-9000 #') -rtu c37118://127.0.0.1:4712#7/PMU26@9001"
  fi
  (cd "$ROOT/services/fep-gateway" && $AE_out CIM_TOKEN=$TOK bin/outstation-sim -model ${GS_MODEL:-ieee118} $OSEC > $D/out.log 2>&1 & echo $! > $D/out.pid)
  wait_http localhost:8090/health || exit 1
  $AE_field GS_MODEL=${GS_MODEL:-ieee118} $PY "$ROOT/tools/field_sim.py" > $D/field.log 2>&1 & echo $! > $D/field.pid
  wait_http localhost:8091/health || exit 1
  CORE="java -XX:+UseZGC -Xmx1g -cp $ROOT/services/scada-core/out gs.core.Main"
  if [ -n "$GS_BUS" ]; then
    redis-cli -p 6390 ping >/dev/null 2>&1 || redis-server --port 6390 --save "" --appendonly no --daemonize yes --bind 127.0.0.1 >/dev/null
    redis-cli -p 6390 del gs:telemetry >/dev/null; CORE="$CORE -bus redis://127.0.0.1:6390"
  fi
  UI=""; [ -d "$ROOT/ui/dist" ] && UI="-ui $ROOT/ui/dist"
  rm -rf $D/coreA $D/coreB $D/coreC
  PA="-peer B=http://127.0.0.1:8183"; PB="-peer A=http://127.0.0.1:8083"
  if [ -n "$GS_MTLS" ]; then
    MP=$D/mpki; PKI="$ROOT/services/fep-gateway/bin/pki"; rm -rf $MP; $PKI ca -dir $MP >/dev/null || exit 1
    for n in A B C; do $PKI issue -dir $MP -cn core-$n -dns core-$n,localhost -dual >/dev/null || exit 1; done
    $PKI revoke -dir $MP -serial 1 >/dev/null
    PROXY="$ROOT/services/fep-gateway/bin/mtlsproxy"
    # node ports: plain 8083/8183/8283, mTLS ingress 9083/9183/9283, egress to peer Y from node X at 21000+10*x+y
    pa() { case $1 in A) echo 8083;; B) echo 8183;; C) echo 8283;; esac; }
    pm() { case $1 in A) echo 9083;; B) echo 9183;; C) echo 9283;; esac; }
    ei() { case $1 in A) echo 0;; B) echo 1;; C) echo 2;; esac; }
    for X in A B C; do
      EG=""; PEERS=""
      for Y in A B C; do [ $X = $Y ] && continue
        port=$((21000 + 10*$(ei $X) + $(ei $Y))); EG="$EG -egress 127.0.0.1:$port=https://127.0.0.1:$(pm $Y)@core-$Y"; PEERS="$PEERS -peer $Y=http://127.0.0.1:$port"
      done
      eval "MPEERS_$X=\"$PEERS\""
      $PROXY -cert $MP/core-$X.pem -key $MP/core-$X.key -ca $MP/ca.pem -crl $MP/crl.pem \
        -ingress 127.0.0.1:$(pm $X)=http://127.0.0.1:$(pa $X) -internal /api/replicate,/api/cluster/:core-A,core-B,core-C $EG > $D/mtls$X.log 2>&1 & echo $! > $D/mtls$X.pid
    done
    PA="$MPEERS_A"; PB="$MPEERS_B"; PC="$MPEERS_C"
  fi
  [ -n "$GS_QUORUM" ] && [ -z "$GS_MTLS" ] && { PA="$PA -peer C=http://127.0.0.1:8283"; PB="$PB -peer C=http://127.0.0.1:8283"; PC="-peer A=http://127.0.0.1:8083 -peer B=http://127.0.0.1:8183"; }
  $AE_A JAVA_TOOL_OPTIONS= $CORE -id A -listen 127.0.0.1:8083 -data $D/coreA -model ${GS_MODEL:-ieee118} $PA $UI > $D/coreA.log 2>&1 & echo $! > $D/coreA.pid
  wait_http localhost:8083/health || { cat $D/coreA.log; exit 1; }
  mkdir -p $D/coreB && cp $D/coreA/users.json $D/coreA/dev-credentials.txt $D/coreB/
  if [ -z "$SINGLE" ]; then
    $AE_B JAVA_TOOL_OPTIONS= $CORE -id B -listen 127.0.0.1:8183 -data $D/coreB -model ${GS_MODEL:-ieee118} $PB $UI > $D/coreB.log 2>&1 & echo $! > $D/coreB.pid
    wait_http localhost:8183/health || { cat $D/coreB.log; exit 1; }
    ING="-ingest http://127.0.0.1:8083/api/ingest -ingest http://127.0.0.1:8183/api/ingest"
    if [ -n "$GS_QUORUM" ]; then
      mkdir -p $D/coreC && cp $D/coreA/users.json $D/coreA/dev-credentials.txt $D/coreC/
      $AE_C JAVA_TOOL_OPTIONS= $CORE -id C -listen 127.0.0.1:8283 -data $D/coreC -model ${GS_MODEL:-ieee118} $PC $UI > $D/coreC.log 2>&1 & echo $! > $D/coreC.pid
      wait_http localhost:8283/health || { cat $D/coreC.log; exit 1; }
      ING="$ING -ingest http://127.0.0.1:8283/api/ingest"
    fi
  else ING="-ingest http://127.0.0.1:8083/api/ingest"; fi
  rm -f $D/historian.db*
  HDB=$D/historian.db; HENV=""
  if [ -n "$GS_PG" ]; then
    PGB=/usr/lib/postgresql/16/bin
    pg_isready -h 127.0.0.1 -p 5433 >/dev/null 2>&1 || { mkdir -p /tmp/gs/pg; chown postgres /tmp/gs/pg; [ -d /tmp/gs/pg/data ] || su postgres -c "$PGB/initdb -D /tmp/gs/pg/data -A trust >/dev/null"; su postgres -c "$PGB/pg_ctl -D /tmp/gs/pg/data -o '-p 5433 -k /tmp/gs/pg -c listen_addresses=127.0.0.1' -l /tmp/gs/pg/log start" >/dev/null; sleep 2; }
    psql -h 127.0.0.1 -p 5433 -U postgres -qc "DROP DATABASE IF EXISTS histlive" -c "CREATE DATABASE histlive" || exit 1
    HDB="postgresql://postgres@127.0.0.1:5433/histlive"; HENV="HIST_DSN=$HDB"
  fi
  [ -n "$GS_BUS" ] && HENV="$HENV HIST_BUS=redis://127.0.0.1:6390"
  (cd "$ROOT/services/historian" && $AE_hist HIST_DB=$D/historian.db $HENV $PY -m uvicorn hist.app:app --host 127.0.0.1 --port 8085 > $D/hist.log 2>&1 & echo $! > $D/hist.pid)
  wait_http localhost:8085/health >/dev/null 2>&1; $PY "$ROOT/tools/seed_history.py" $HDB >> $D/hist.log 2>&1 || true
  (cd "$ROOT/services/fep-gateway" && $AE_fep bin/fep -model ${GS_MODEL:-ieee118} $ING $FSEC $RTU ${GS_OPCUA:+-opcua 127.0.0.1:4840} > $D/fep.log 2>&1 & echo $! > $D/fep.pid)
  wait_http localhost:8084/health || exit 1
  echo "stack up"; $0 status;;
esac
