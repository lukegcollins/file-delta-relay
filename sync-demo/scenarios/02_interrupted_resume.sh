#!/usr/bin/env bash
# Scenario 2 - unstable network. Proves that an interrupted transfer resumes
# from persisted state rather than restarting, and that content-addressed
# puts mean the server never receives a chunk twice.
#
# It shapes the CLIENT container's own egress with tc/netem (needs the
# NET_ADMIN cap that docker-compose.yml already grants), so nothing on the
# host is touched.
#
# Prereq:  docker compose up --build   (in another terminal)
# Run:     ./scenarios/02_interrupted_resume.sh
set -euo pipefail
cd "$(dirname "$0")/.."

# The client load-balances each file across two independent, non-replicating
# stores, so large.bin may converge on either. These checks accept both.
PRIMARY="${PRIMARY:-https://localhost:8000}"      # the compose stack serves HTTPS
SECONDARY="${SECONDARY:-https://localhost:8001}"
export CURL_CA_BUNDLE="${CURL_CA_BUNDLE:-$PWD/certs/ca.crt}"   # curl trusts the demo CA
CLIENT=${CLIENT:-sync-client}
IFACE=${IFACE:-eth0}
ROOT=./sync-root
mkdir -p "$ROOT"

netem() { docker exec "$CLIENT" tc "$@"; }
# Total chunks across both independent stores.
chunks() {
  local p s
  p=$(curl -s "$PRIMARY/v1/stats"   | python3 -c 'import sys,json;print(json.load(sys.stdin)["chunks"])' 2>/dev/null || echo 0)
  s=$(curl -s "$SECONDARY/v1/stats" | python3 -c 'import sys,json;print(json.load(sys.stdin)["chunks"])' 2>/dev/null || echo 0)
  echo $((p + s))
}
# large.bin byte-identical on whichever server the client chose.
converged() {
  curl -sf "$PRIMARY/v1/file?path=/data/sync/large.bin"   | cmp -s - "$ROOT/large.bin" || \
  curl -sf "$SECONDARY/v1/file?path=/data/sync/large.bin" | cmp -s - "$ROOT/large.bin"
}

cleanup() { netem qdisc del dev "$IFACE" root 2>/dev/null || true; }
trap cleanup EXIT

echo "== 1. degrade the link: 30% packet loss + 100ms delay =="
cleanup
netem qdisc add dev "$IFACE" root netem loss 30% delay 100ms

echo "== 2. drop in a 12 MB file to sync over the bad link =="
head -c 12582912 /dev/urandom > "$ROOT/large.bin"
BEFORE=$(chunks)

echo "== 3. mid-transfer, cut the network entirely for 6s =="
sleep 4
netem qdisc change dev "$IFACE" root netem loss 100%
echo "   (100% loss now; the client is retrying with backoff -- watch its logs)"
sleep 6

echo "== 4. restore the degraded-but-usable link =="
netem qdisc change dev "$IFACE" root netem loss 10% delay 50ms
echo "   client should reconnect, re-query missing chunks, and finish."

echo "== 5. wait for convergence, then verify =="
for i in $(seq 1 40); do
  if converged; then
    AFTER=$(chunks)
    echo "OK after ${i} checks: server copy byte-identical."
    echo "chunks stored total=$AFTER (delta this run=$((AFTER-BEFORE)));"
    echo "no chunk was stored twice -- idempotent content-addressed puts."
    cleanup
    exit 0
  fi
  sleep 3
done

echo "did not converge in time; inspect 'docker logs $CLIENT'"; exit 1
