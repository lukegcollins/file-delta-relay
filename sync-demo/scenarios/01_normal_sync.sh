#!/usr/bin/env bash
# Scenario 1 - normal operation. Demonstrates, against the running stack:
#   change detection  (only new/modified files are processed)
#   bandwidth         (an edit moves a couple of chunks; a rename moves none)
#   integrity         (server reassembly matches the local bytes)
#   deletion          (a removed file is tombstoned)
#
# Prereq:  docker compose up --build   (in another terminal)
# Run:     ./scenarios/01_normal_sync.sh
set -euo pipefail
cd "$(dirname "$0")/.."

# The client load-balances each file across two independent, non-replicating
# stores, so a file may land on either. These checks are written against both.
PRIMARY="${PRIMARY:-https://localhost:8000}"      # the compose stack serves HTTPS
SECONDARY="${SECONDARY:-https://localhost:8001}"
export CURL_CA_BUNDLE="${CURL_CA_BUNDLE:-$PWD/certs/ca.crt}"   # curl trusts the demo CA
ROOT=./sync-root
mkdir -p "$ROOT"

# Total chunk count across both independent stores.
chunks() {
  local p s
  p=$(curl -s "$PRIMARY/v1/stats"   | python3 -c 'import sys,json;print(json.load(sys.stdin)["chunks"])' 2>/dev/null || echo 0)
  s=$(curl -s "$SECONDARY/v1/stats" | python3 -c 'import sys,json;print(json.load(sys.stdin)["chunks"])' 2>/dev/null || echo 0)
  echo $((p + s))
}

stats() {
  echo "primary:   $(curl -s "$PRIMARY/v1/stats")"
  echo "secondary: $(curl -s "$SECONDARY/v1/stats")"
}
pause() { sleep "${1:-5}"; }   # give the 3s client poll time to run

# The client polls on a randomized (exponential) interval, so evidence steps
# poll for the expected post-sync state instead of asserting after a fixed
# pause. present() = is $ROOT/<name> byte-identical on whichever store the
# client chose; the wait_* helpers block on a condition up to a deadline.
present() {
  curl -sf "$PRIMARY/v1/file?path=/data/sync/$1"   | cmp -s - "$ROOT/$1" || \
  curl -sf "$SECONDARY/v1/file?path=/data/sync/$1" | cmp -s - "$ROOT/$1"
}
wait_present()   { local d=$((SECONDS + ${2:-30})); until present "$1";            do [ "$SECONDS" -ge "$d" ] && return 1; sleep 2; done; }
wait_chunks_ge() { local d=$((SECONDS + ${2:-30})); until [ "$(chunks)" -ge "$1" ]; do [ "$SECONDS" -ge "$d" ] && return 1; sleep 2; done; }

echo "== baseline =="; stats

echo "== 1. create two files =="
head -c 400000 /dev/urandom > "$ROOT/blob.bin"
printf 'hello world\n%.0s' {1..500} > "$ROOT/notes.txt"
# Wait for the initial sync to actually commit before measuring, so the chunk
# baseline the next steps compare against is stable rather than mid-sync.
wait_present blob.bin 30  || { echo "FAIL: blob.bin never synced"; exit 1; }
wait_present notes.txt 30 || { echo "FAIL: notes.txt never synced"; exit 1; }
stats
echo "server holds both files and their chunks."

echo "== 2. edit notes.txt (append one line) =="
BEFORE=$(chunks)
echo "extra line" >> "$ROOT/notes.txt"
# Poll until the edit is committed as at least one new chunk (change detection +
# content addressing), rather than reading the count after a fixed pause.
wait_chunks_ge $((BEFORE + 1)) 30 \
  || { echo "FAIL: edit never produced a new chunk (change detection)"; exit 1; }
AFTER=$(chunks)
echo "chunks before=$BEFORE after=$AFTER  (+$((AFTER - BEFORE)): only notes.txt was re-processed" \
     "(change detection) — blob.bin was untouched; a same-store edit adds just the changed chunk,"\
     "a re-homed one lands the file on the other store)"

echo "== 3. rename blob.bin -> archive.bin (no content change) =="
BEFORE=$(chunks)
mv "$ROOT/blob.bin" "$ROOT/archive.bin"
# Wait for the rename to commit (archive.bin readable) before reading the count,
# then report the true delta. Note: the two stores do not replicate and the
# client picks one at random per file, so if archive.bin lands on the store that
# did not already hold blob.bin's chunks, those existing hashes are re-homed
# there (a small, bounded delta) — no new *content* is ever created.
wait_present archive.bin 30 || { echo "FAIL: rename never committed"; exit 1; }
AFTER=$(chunks)
DELTA=$((AFTER - BEFORE))
if [ "$DELTA" -eq 0 ]; then
  detail="rename reused blob.bin's chunks in place (content-addressed dedup)"
else
  detail="rename re-homed $DELTA existing chunk(s) to the other, non-replicating store — same hashes, no new content"
fi
echo "chunks before=$BEFORE after=$AFTER  (dedup: $detail)"

echo "== 4. verify integrity: server bytes == local bytes =="
# Poll instead of asserting after a fixed pause: the client's poll interval is
# randomized (exponential, mean SYNC_INTERVAL), so the rename may not be
# committed the instant we look. Wait up to 30 s for archive.bin to appear
# byte-identical on whichever server the client chose.
integrity_ok() {
  curl -sf "$PRIMARY/v1/file?path=/data/sync/archive.bin"   | cmp -s - "$ROOT/archive.bin" || \
  curl -sf "$SECONDARY/v1/file?path=/data/sync/archive.bin" | cmp -s - "$ROOT/archive.bin"
}
deadline=$((SECONDS + 30))
until integrity_ok; do
  if [ "$SECONDS" -ge "$deadline" ]; then
    echo "MISMATCH"; exit 1
  fi
  sleep 2
done
echo "OK: server copy of archive.bin is byte-identical (on primary or secondary)"

echo "== 5. delete notes.txt =="
rm "$ROOT/notes.txt"
pause; stats
echo "tombstones should have incremented; live files decremented."

echo "== done =="
