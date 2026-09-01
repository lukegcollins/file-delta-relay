#!/usr/bin/env bash
# Stealth mode scenario: sync a file with source deletion enabled.
#
# The normal client container does NOT delete files; this scenario starts a
# temporary one-off client that syncs a single file and then removes the local
# copy (simulating counter‑forensics). It verifies the file is present on the
# server and absent locally.
#
# Prereq: docker compose up --build
# Run:    ./scenarios/04_stealth_mode.sh
set -euo pipefail
cd "$(dirname "$0")/.."

SRV_FRONT="https://localhost:8443"          # Nginx front proxy (host mapping)
CA_BUNDLE="./certs/ca.crt"
ROOT=./sync-root
CLIENT_IMAGE="sync-demo-client"              # adjust if image name differs
STATE_DIR="./test-state"                     # separate state for one-off client
mkdir -p "$ROOT" "$STATE_DIR"

echo "== 1. create a file to sync (and delete locally) =="
head -c 1048576 /dev/urandom > "$ROOT/stealth_test.bin"
LOCAL_HASH=$(sha256sum "$ROOT/stealth_test.bin" | awk '{print $1}')

echo "== 2. run one-off client with SYNC_DELETE_AFTER=true =="
docker run --rm \
  --network sync-demo_sync-network \
  -v "$(pwd)/$ROOT:/data/sync" \
  -v "$(pwd)/$STATE_DIR:/state" \
  -v "$(pwd)/certs:/certs:ro" \
  -e SYNC_SERVERS="https://front-proxy:443" \
  -e SYNC_CA_BUNDLE="/certs/ca.crt" \
  -e SYNC_ROOTS="/data/sync" \
  -e SYNC_STATE_DB="/state/client.db" \
  -e SYNC_DELETE_AFTER="true" \
  -e SYNC_ONCE="true" \
  "$CLIENT_IMAGE"

echo "== 3. verify local file was deleted =="
if [ -f "$ROOT/stealth_test.bin" ]; then
  echo "FAIL: local file still exists"; exit 1
else
  echo "OK: local file removed after sync"
fi

echo "== 4. verify server has the file =="
SERVER_HASH=$(curl -sk "$SRV_FRONT/api/v1/file?path=/data/sync/stealth_test.bin" | sha256sum | awk '{print $1}')
if [ "$SERVER_HASH" = "$LOCAL_HASH" ]; then
  echo "OK: server copy byte‑identical"
else
  echo "FAIL: server hash mismatch"; exit 1
fi

echo "== stealth mode scenario passed =="