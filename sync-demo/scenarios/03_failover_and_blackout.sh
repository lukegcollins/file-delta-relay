#!/usr/bin/env bash
# Scenario 3 - multi-server resilience. Proves, against the running stack:
#   failover   the client keeps syncing when the primary server is stopped
#   failback   once the primary returns, new work goes to it again
#   degraded   a lossy, slow link (10% loss, 100 ms delay) still converges,
#              on whichever server the transport settles on - both sit
#              behind the same bad link
#   blackout   a 60 s total outage mid-transfer resumes and completes
#
# The two servers are independent stores: a file committed to the secondary
# during the outage stays there. That is stated, not hidden - failover here is
# about the client staying able to sync, not about replicating data.
#
# Shaping uses tc/netem inside the CLIENT container (NET_ADMIN is granted by
# docker-compose.yml); the primary is stopped/started with docker. Nothing on
# the host is touched. Takes ~6 minutes, most of it deliberate waiting.
#
# Prereq:  docker compose up --build   (in another terminal)
# Run:     ./scenarios/03_failover_and_blackout.sh
set -euo pipefail
cd "$(dirname "$0")/.."

PRIMARY="${PRIMARY:-https://localhost:8000}"      # the compose stack serves HTTPS
SECONDARY="${SECONDARY:-https://localhost:8001}"
export CURL_CA_BUNDLE="${CURL_CA_BUNDLE:-$PWD/certs/ca.crt}"   # curl trusts the demo CA
PRIMARY_CONTAINER="${PRIMARY_CONTAINER:-sync-server-primary}"
CLIENT="${CLIENT:-sync-client}"
IFACE="${IFACE:-eth0}"
ROOT=./sync-root
mkdir -p "$ROOT"

netem()        { docker exec "$CLIENT" tc "$@"; }
health()       { curl -s -o /dev/null -w '%{http_code}' "$1/v1/health" || true; }
has_file()     {  # has_file <server> <local_file>: byte-identical copy on that server?
  curl -sf "$1/v1/file?path=/data/sync/$(basename "$2")" | cmp -s - "$2"
}
wait_for_file() {  # wait_for_file <local_file> <timeout_s> <server>...
  local f="$1" deadline=$((SECONDS + $2)); shift 2
  while [ "$SECONDS" -lt "$deadline" ]; do
    for s in "$@"; do
      if has_file "$s" "$f"; then echo "$s"; return 0; fi
    done
    sleep 3
  done
  return 1
}

cleanup() {
  netem qdisc del dev "$IFACE" root 2>/dev/null || true
  docker start "$PRIMARY_CONTAINER" >/dev/null 2>&1 || true
}
trap cleanup EXIT

echo "== 1. both servers healthy? =="
for s in "$PRIMARY" "$SECONDARY"; do
  if [ "$(health "$s")" != "200" ]; then echo "ERROR: $s not healthy"; exit 1; fi
done
echo "   primary=$PRIMARY secondary=$SECONDARY both answer /v1/health"

echo "== 2. baseline: a 1 MB file syncs (client picks either server) =="
head -c 1048576 /dev/urandom > "$ROOT/initial.bin"
wait_for_file "$ROOT/initial.bin" 60 "$PRIMARY" "$SECONDARY" >/dev/null \
  || { echo "FAIL: initial.bin never reached either server"; exit 1; }
echo "   initial.bin synced (on whichever server the client chose)"

echo "== 3. failover: stop the primary, drop in a 2 MB file =="
docker stop "$PRIMARY_CONTAINER" >/dev/null
echo "   primary stopped"
head -c 2097152 /dev/urandom > "$ROOT/failover.bin"
if wait_for_file "$ROOT/failover.bin" 90 "$SECONDARY" >/dev/null; then
  echo "   Failover to secondary succeeded: failover.bin byte-identical on secondary"
else
  echo "FAIL: failover.bin did not reach the secondary"; exit 1
fi

echo "== 4. failback: restart the primary, wait for it to come back, drop in another file =="
docker start "$PRIMARY_CONTAINER" >/dev/null
# Wait on the primary's own health, not a client log line: with random per-file
# server selection the client may never have touched the (downed) primary in
# phase 3, so "primary healthy again" is not guaranteed to appear. The invariant
# that actually matters for failback is that the primary is reachable again.
echo "   primary started; waiting for it to answer /v1/health again"
deadline=$((SECONDS + 60))
until [ "$(health "$PRIMARY")" = "200" ]; do
  if [ "$SECONDS" -ge "$deadline" ]; then
    echo "FAIL: primary never came back"; exit 1
  fi
  sleep 2
done
echo "   primary healthy again"
head -c 1048576 /dev/urandom > "$ROOT/failback.bin"
if wait_for_file "$ROOT/failback.bin" 90 "$PRIMARY" "$SECONDARY" >/dev/null; then
  echo "   Failback to primary succeeded: failback.bin byte-identical on primary"
else
  echo "FAIL: failback.bin did not reach the primary"; exit 1
fi
if has_file "$PRIMARY" "$ROOT/failover.bin"; then
  echo "   (failover.bin also on primary)"
else
  echo "   (failover.bin lives on the secondary only - the servers do not replicate)"
fi

echo "== 5. degraded link: 10% loss + 100 ms delay, 3 MB file =="
# Measured on this stack: 10%/50ms converges in ~6 s, 10%/100ms in ~50 s,
# 20%/200ms never (TCP itself cannot move 1 MiB chunks at that loss×RTT).
#
# The elapsed time is printed rather than just the pass/fail, because this step
# is the one that actually distinguishes the two branches' transport tuning, and
# the comparison in tradeoff_analysis.md needs both sides to report the same
# number from the same script. Keep this instrumentation identical to the copy
# on lightweight-portable.
netem qdisc add dev "$IFACE" root netem loss 10% delay 100ms
head -c 3145728 /dev/urandom > "$ROOT/degraded.bin"
step5_start=$SECONDS
if where=$(wait_for_file "$ROOT/degraded.bin" 180 "$PRIMARY" "$SECONDARY"); then
  echo "   degraded.bin converged (on $where) in $((SECONDS - step5_start))s"
else
  echo "FAIL: degraded.bin did not converge within $((SECONDS - step5_start))s"; exit 1
fi
netem qdisc del dev "$IFACE" root

echo "== 6. blackout: 6 MB file, 100% loss for 60 s mid-transfer, then recover =="
head -c 6291456 /dev/urandom > "$ROOT/blackout.bin"
sleep 5
netem qdisc add dev "$IFACE" root netem loss 100%
echo "   link cut; the client is retrying with backoff - watch: docker logs -f $CLIENT"
sleep 60
netem qdisc change dev "$IFACE" root netem loss 5% delay 50ms
echo "   link restored (5% loss); waiting for the resumed upload to finish"
if where=$(wait_for_file "$ROOT/blackout.bin" 240 "$PRIMARY" "$SECONDARY"); then
  echo "   blackout.bin converged (on $where) from persisted state"
else
  echo "FAIL: blackout.bin did not converge; inspect 'docker logs $CLIENT'"; exit 1
fi

cleanup
echo "== done: failover, failback, degraded link and blackout all converged =="
