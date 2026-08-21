#!/usr/bin/env bash
# Entry point for the demo. Two things sit side by side here: the automated
# demo (run_auto_demo) and the interactive walkthrough (run_walkthrough).
#
# run_auto_demo — hands-off, writes a report:
#   ./demo.sh quick        no Docker: run the unit tests and the end-to-end
#                          integration test, write a requirement→evidence report
#   ./demo.sh full         Docker: bring up two servers + client, run the three
#                          automated scenarios (normal sync, interrupted resume,
#                          failover + blackout), collect stats/log evidence,
#                          write a report, tear everything down
#
# run_walkthrough — interactive, for narrating live:
#   ./demo.sh walkthrough  Docker: step through file create/edit/rename/delete
#                          (and an optional network outage) while watching the
#                          server's chunk counts. Add --auto to run unattended
#                          (used for self-testing).
#
# Reports land in ./reports/ as timestamped markdown.
set -euo pipefail
cd "$(dirname "$0")"

MODE="${1:-help}"
STAMP="$(date +%Y%m%d-%H%M%S)"
REPORTS=./reports
# The compose stack serves HTTPS with the demo CA; curl (here and in the
# scenarios) trusts it via CURL_CA_BUNDLE. Override SRV for a plain-http stack.
SRV="${SRV:-https://localhost:8000}"
export CURL_CA_BUNDLE="${CURL_CA_BUNDLE:-$PWD/certs/ca.crt}"

ensure_certs() {
  # The servers need certs/server.{crt,key} and the client certs/ca.crt.
  if [ ! -f certs/server.key ] || [ ! -f certs/ca.crt ]; then
    echo "== generating the demo TLS certificates (first run only) =="
    ./certs/gen_certs.sh
  fi
}

usage() {
  # Print the header comment above (everything between the shebang and the
  # first non-comment line), minus the leading "# ".
  awk 'NR == 1 { next } !/^#/ { exit } { sub(/^# ?/, ""); print }' "$0"
}

ensure_venv() {
  # uv owns Python on this machine; fall back to stdlib venv elsewhere so an
  # assessor without uv can still run `./demo.sh quick`.
  if [ ! -x .venv/bin/python ]; then
    echo "== creating virtualenv (first run only) =="
    if command -v uv >/dev/null 2>&1; then
      uv venv --python 3.12 .venv
      uv pip install --python .venv -q -r client/requirements.txt -r server/requirements.txt -r tests/requirements.txt
    else
      python3 -m venv .venv
      .venv/bin/pip install -q -r client/requirements.txt -r server/requirements.txt -r tests/requirements.txt
    fi
  fi
}

# count_ok <log> <regex-of-check-numbers>: how many "N. ... ok" lines matched
count_ok() { grep -c -E "^($2)\..*ok" "$1" || true; }

quick() {
  ensure_venv
  ensure_certs            # the integration test runs its server over TLS when certs exist
  mkdir -p "$REPORTS"
  local out="$REPORTS/quick-$STAMP.md" log rc=0
  log="$(mktemp)"
  echo "== unit tests: change-detection kernel =="
  .venv/bin/python -m unittest tests/test_change_detection.py -v 2>&1 | tee "$log" || rc=$?
  echo "== unit tests: classify() properties (hypothesis) =="
  .venv/bin/python -m unittest tests/test_classify_properties.py -v 2>&1 | tee -a "$log" || rc=$?
  echo "== running the no-Docker integration test =="
  .venv/bin/python tests/test_integration_http.py 2>&1 | tee -a "$log" || rc=$?
  echo "== SYNC_API_KEY opt-in auth =="
  .venv/bin/python tests/test_api_key_auth.py 2>&1 | tee -a "$log" || rc=$?

  local verdict="PASS"
  [ "$rc" -eq 0 ] || verdict="FAIL"
  {
    echo "# Sync demo — quick report ($verdict)"
    echo
    echo "Generated: $(date -u '+%Y-%m-%d %H:%M UTC') · mode: no Docker —"
    echo "real Flask server as a subprocess, real HTTP client driving it"
    echo "($(grep -m1 -oE 'over (https|http)' "$log" || echo 'over http'), see test output)."
    echo
    echo "## Requirement → evidence"
    echo
    echo "| Core requirement | Proven by | Observed |"
    echo "|---|---|---|"
    echo "| Change detection | unit tests + checks 2, 6 | $(grep -c ' \.\.\. ok$' "$log" || true) unit, $(count_ok "$log" '2|6')/2 passed |"
    echo "| Bandwidth | checks 3, 4 | $(count_ok "$log" '3|4')/2 passed |"
    echo "| Reliability | checks 5, 6, 8, 9, 11 | $(count_ok "$log" '5|6|8|9|11')/5 passed |"
    echo "| Integrity | checks 1, 3, 10 | $(count_ok "$log" '1|3|10')/3 passed |"
    echo "| Deletion propagation | check 7 | $(count_ok "$log" '7')/1 passed |"
    echo "| Server failover | check 8 | $(count_ok "$log" '8')/1 passed |"
    echo
    echo "## Full test output"
    echo
    echo '```'
    cat "$log"
    echo '```'
  } > "$out"
  rm -f "$log"
  echo
  echo "report written: $out ($verdict)"
  return "$rc"
}

full() {
  mkdir -p "$REPORTS"
  local out="$REPORTS/full-$STAMP.md"
  local happy_log unstable_log resilience_log
  local rc_happy=0 rc_unstable=0 rc_resilience=0
  happy_log="$(mktemp)"; unstable_log="$(mktemp)"; resilience_log="$(mktemp)"

  cleanup() {
    echo "== tearing down the stack =="
    docker compose down -v >/dev/null 2>&1 || true
    rm -rf sync-root
  }
  trap cleanup EXIT

  ensure_certs
  echo "== bringing up the Docker stack (fresh volumes, TLS) =="
  docker compose down -v >/dev/null 2>&1 || true
  rm -rf sync-root
  docker compose up --build -d

  echo "== scenario 1: normal sync =="
  ./scenarios/01_normal_sync.sh 2>&1 | tee "$happy_log" || rc_happy=$?

  echo "== scenario 2: interrupted resume =="
  ./scenarios/02_interrupted_resume.sh 2>&1 | tee "$unstable_log" || rc_unstable=$?

  echo "== scenario 3: failover + blackout =="
  ./scenarios/03_failover_and_blackout.sh 2>&1 | tee "$resilience_log" || rc_resilience=$?

  local stats stats2 client_log
  stats="$(curl -s "$SRV/v1/stats" || echo '{}')"
  stats2="$(curl -s "${SRV%:*}:8001/v1/stats" || echo '{}')"
  client_log="$(docker logs sync-client 2>&1 | grep -E '^\[client\]' || true)"

  local verdict="PASS"
  if [ "$rc_happy" -ne 0 ] || [ "$rc_unstable" -ne 0 ] || [ "$rc_resilience" -ne 0 ]; then
    verdict="FAIL"
  fi
  {
    echo "# Sync demo — full report ($verdict)"
    echo
    echo "Generated: $(date -u '+%Y-%m-%d %H:%M UTC') · mode: Docker compose,"
    echo "two independent HTTPS servers + one client (verifying the demo CA) on a"
    echo "bridge network, the client's egress shaped with tc/netem inside its own"
    echo "namespace."
    echo
    echo "## Requirement → observed evidence"
    echo
    echo "| Core requirement | Mechanism | Evidence from this run |"
    echo "|---|---|---|"
    echo "| Change detection | stat vs. state DB, guard window | $(grep -m1 'chunks before=' "$happy_log" || echo 'see appendix A') |"
    echo "| Bandwidth | CDC chunks + dedup + zstd | $(grep -m1 'dedup:' "$happy_log" || echo 'see appendix A') |"
    echo "| Reliability | resume from persisted manifest | $(grep -m1 'OK after' "$unstable_log" || echo 'see appendix B') |"
    echo "| Reliability | failover between servers | $(grep -m1 -i 'failover.*succeeded' "$resilience_log" || echo 'see appendix C') |"
    echo "| Integrity | BLAKE3 verify-on-write, byte compare | $(grep -m1 'byte-identical' "$happy_log" || echo 'see appendix A') |"
    echo
    echo "Final server state: primary \`$stats\` · secondary \`$stats2\`"
    echo
    echo "Scenario exit codes: normal_sync=$rc_happy · interrupted_resume=$rc_unstable · failover_and_blackout=$rc_resilience"
    echo
    echo "## Client log (sync passes, outage handling, failover)"
    echo
    echo '```'
    echo "${client_log:-<no client log lines captured>}"
    echo '```'
    echo
    echo "## Appendix A — 01_normal_sync.sh output"
    echo
    echo '```'
    cat "$happy_log"
    echo '```'
    echo
    echo "## Appendix B — 02_interrupted_resume.sh output"
    echo
    echo '```'
    cat "$unstable_log"
    echo '```'
    echo
    echo "## Appendix C — 03_failover_and_blackout.sh output"
    echo
    echo '```'
    cat "$resilience_log"
    echo '```'
  } > "$out"
  rm -f "$happy_log" "$unstable_log" "$resilience_log"
  echo
  echo "report written: $out ($verdict)"
  [ "$verdict" = "PASS" ]
}

# The two things you can run, side by side.

# run_auto_demo <quick|full>: the hands-off demo that writes a report.
run_auto_demo() {
  case "${1:-}" in
    quick) quick ;;
    full)  full ;;
    *)     usage; exit 1 ;;
  esac
}

# run_walkthrough [--auto]: the interactive step-through.
run_walkthrough() {
  ensure_certs
  exec ./scenarios/05_interactive_walkthrough.sh "$@"
}

case "$MODE" in
  quick)       run_auto_demo quick ;;
  full)        run_auto_demo full ;;
  walkthrough) shift; run_walkthrough "$@" ;;
  *)           usage; exit 1 ;;
esac
