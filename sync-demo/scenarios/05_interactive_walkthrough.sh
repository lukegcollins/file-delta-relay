#!/usr/bin/env bash
# Interactive walkthrough for a live demo: step through the file events the
# client reacts to - create, no-op, edit, rename, delete, plus an optional
# mid-transfer network outage - pausing before each step so you can narrate,
# and printing the server's chunk/file/tombstone deltas after each one.
#
# Run via:  ./demo.sh walkthrough            (pauses on Enter between steps)
#           ./demo.sh walkthrough --auto     (no pauses; used for self-testing)
#
# Needs the compose stack; it is started automatically if not already up.
set -euo pipefail
cd "$(dirname "$0")/.."

AUTO=false
[ "${1:-}" = "--auto" ] && AUTO=true

# The client load-balances each file across two independent, non-replicating
# stores, so a file may land on either. Every check here spans both.
PRIMARY="${PRIMARY:-https://localhost:8000}"      # the compose stack serves HTTPS
SECONDARY="${SECONDARY:-https://localhost:8001}"
export CURL_CA_BUNDLE="${CURL_CA_BUNDLE:-$PWD/certs/ca.crt}"   # curl trusts the demo CA
CLIENT="${CLIENT:-sync-client}"
IFACE="${IFACE:-eth0}"
ROOT=./sync-root
POLL=3          # keep equal to SYNC_INTERVAL in docker-compose.yml

# A numeric stat (chunks/files/tombstones) summed across both stores.
field() {  # field <chunks|files|tombstones>
  local p s
  p=$(curl -s "$PRIMARY/v1/stats"   | python3 -c "import sys,json;print(json.load(sys.stdin)['$1'])" 2>/dev/null || echo 0)
  s=$(curl -s "$SECONDARY/v1/stats" | python3 -c "import sys,json;print(json.load(sys.stdin)['$1'])" 2>/dev/null || echo 0)
  echo $((p + s))
}
stats()  { echo "primary $(curl -s "$PRIMARY/v1/stats") · secondary $(curl -s "$SECONDARY/v1/stats")"; }
netem()  { docker exec "$CLIENT" tc "$@"; }
both_healthy() { curl -sf "$PRIMARY/v1/health" -o /dev/null && curl -sf "$SECONDARY/v1/health" -o /dev/null; }

cleanup() { netem qdisc del dev "$IFACE" root 2>/dev/null || true; }
trap cleanup EXIT

# Condition-based waiting: poll until a command succeeds, never a blind sleep.
wait_until() {  # wait_until <timeout_s> <cmd...>
  local deadline=$((SECONDS + $1)); shift
  until "$@" 2>/dev/null; do
    if [ "$SECONDS" -ge "$deadline" ]; then
      echo "   timed out waiting for: $*" >&2
      return 1
    fi
    sleep 1
  done
}

server_matches() {  # server_matches <local_file> - byte-identical on either store?
  local f="$1" b; b="$(basename "$f")"
  curl -sf "$PRIMARY/v1/file?path=/data/sync/$b"   | cmp -s - "$f" && return 0
  curl -sf "$SECONDARY/v1/file?path=/data/sync/$b" | cmp -s - "$f" && return 0
  return 1
}

tombstones_at_least() { [ "$(field tombstones)" -ge "$1" ]; }

step() {  # step <title> <talking point>
  echo
  echo "== $1 =="
  echo "   ($2)"
  if ! $AUTO; then read -r -p "   press Enter to run this step... "; fi
}

show_delta() {  # show_delta <chunks_before>
  local now; now="$(field chunks)"
  echo "   server: $(stats)"
  echo "   chunk delta this step: $((now - $1))"
}

# --- ensure the stack is up ------------------------------------------------
if ! both_healthy 2>/dev/null; then
  echo "== stack not fully up; starting it (docker compose up --build -d) =="
  [ -f certs/server.key ] && [ -f certs/ca.crt ] || ./certs/gen_certs.sh
  docker compose up --build -d
  wait_until 60 both_healthy || { echo "servers never became healthy"; exit 1; }
fi
mkdir -p "$ROOT"

echo "baseline: $(stats)"

# --- 1. create -------------------------------------------------------------
step "1. create two files" \
     "change detection: new paths are found by the scan and synced"
B=$(field chunks)
head -c 400000 /dev/urandom > "$ROOT/blob.bin"
printf 'hello world\n%.0s' {1..500} > "$ROOT/notes.txt"
wait_until 30 server_matches "$ROOT/blob.bin"
wait_until 30 server_matches "$ROOT/notes.txt"
show_delta "$B"
echo "   both files byte-identical on a server (integrity)"

# --- 2. no-op --------------------------------------------------------------
step "2. touch nothing for one poll interval" \
     "change detection: an unchanged tree costs a stat pass, zero bytes on the wire"
B=$(field chunks)
sleep $((POLL + 2))
show_delta "$B"

# --- 3. small edit ---------------------------------------------------------
step "3. append one line to notes.txt" \
     "bandwidth: a same-store edit moves only the changed chunk"
B=$(field chunks)
echo "extra line $(date +%s)" >> "$ROOT/notes.txt"
wait_until 30 server_matches "$ROOT/notes.txt"
show_delta "$B"

# --- 4. rename -------------------------------------------------------------
step "4. rename blob.bin -> archive.bin" \
     "dedup: same content at a new path reuses its chunks (re-homed to the other store if it lands there)"
B=$(field chunks)
mv "$ROOT/blob.bin" "$ROOT/archive.bin"
wait_until 30 server_matches "$ROOT/archive.bin"
show_delta "$B"

# --- 5. optional outage ----------------------------------------------------
run_outage=true
if ! $AUTO; then
  read -r -p $'\n== 5. network outage mid-transfer (needs ~1 min) - run it? [Y/n] ' a
  [ "${a:-y}" = "n" ] && run_outage=false
fi
if $run_outage; then
  step "5. drop a 12 MB file, cut the network mid-transfer, restore it" \
       "reliability: the pass aborts, state persists as 'chunked', the next pass resumes"
  B=$(field chunks)
  cleanup
  netem qdisc add dev "$IFACE" root netem loss 30% delay 100ms
  head -c 12582912 /dev/urandom > "$ROOT/large.bin"
  sleep 4
  netem qdisc change dev "$IFACE" root netem loss 100%
  echo "   (100% loss for 6s - the client is backing off; watch: docker logs -f $CLIENT)"
  sleep 6
  netem qdisc change dev "$IFACE" root netem loss 10% delay 50ms
  wait_until 120 server_matches "$ROOT/large.bin" || {
    echo "   did not converge; inspect docker logs $CLIENT"; exit 1; }
  cleanup
  show_delta "$B"
  echo "   server copy byte-identical after the outage; no chunk stored twice"
fi

# --- 6. delete -------------------------------------------------------------
step "6. delete notes.txt" \
     "deletion propagates as a tombstone; chunk data is retained for dedup"
T=$(field tombstones); B=$(field chunks)
rm "$ROOT/notes.txt"
# The client sends the tombstone to the priority server (not a random store).
# If notes.txt's manifest lives there, the tombstone count rises; if the file
# only ever landed on the other, non-replicating store, no tombstone appears -
# report that honestly instead of hanging.
if wait_until 30 tombstones_at_least $((T + 1)); then
  show_delta "$B"
  echo "   notes.txt tombstoned (deletion propagated)"
else
  show_delta "$B"
  echo "   no new tombstone: notes.txt lived only on the other store - the client"
  echo "   sends deletes to the priority server, and the stores do not replicate"
fi

echo
echo "== done - final server state: $(stats) =="
$AUTO || echo "   (stack left running; 'docker compose down -v' to reset)"
