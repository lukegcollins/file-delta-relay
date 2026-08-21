#!/usr/bin/env bash
# Brings up the full Docker stack once, runs the four official scenario
# scripts unmodified (the pass/fail record for the final report), then runs
# evidence/docker_harness.py against the same still-running stack to collect
# the extra timestamped data the failover and network-emulation plots need.
# Tears the stack down on exit either way.
#
# Run:  ./evidence/run_full_evidence.sh
set -uo pipefail
cd "$(dirname "$0")/.."

export CURL_CA_BUNDLE="$PWD/certs/ca.crt"
LOGS=./evidence/logs
mkdir -p "$LOGS"

if [ ! -f certs/server.key ] || [ ! -f certs/ca.crt ]; then
  echo "== generating demo TLS certificates =="
  ./certs/gen_certs.sh
fi

cleanup() {
  echo "== tearing down the stack =="
  docker compose down -v >/dev/null 2>&1 || true
}
trap cleanup EXIT

echo "== bringing up the stack (fresh volumes) =="
docker compose down -v >/dev/null 2>&1 || true
rm -rf sync-root
docker compose up --build -d
echo "== waiting for both servers healthy =="
deadline=$((SECONDS + 90))
until [ "$(curl -s -o /dev/null -w '%{http_code}' https://localhost:8000/v1/health)" = "200" ] && \
      [ "$(curl -s -o /dev/null -w '%{http_code}' https://localhost:8001/v1/health)" = "200" ]; do
  if [ "$SECONDS" -ge "$deadline" ]; then echo "servers never became healthy"; exit 1; fi
  sleep 2
done

declare -A RC
for s in 01_normal_sync 02_interrupted_resume 03_failover_and_blackout 04_stealth_mode; do
  echo "== running scenarios/${s}.sh =="
  ./scenarios/${s}.sh > "$LOGS/${s}.log" 2>&1
  RC[$s]=$?
  echo "   exit code: ${RC[$s]}"
done

echo "== collecting plot evidence from the live stack (docker_harness.py) =="
if [ ! -x .venv/bin/python ]; then
  echo "no .venv found; run ./demo.sh quick once first (or 'uv venv --python 3.12 .venv && uv pip install ...')"
  exit 1
fi
.venv/bin/python evidence/docker_harness.py > "$LOGS/docker_harness.log" 2>&1
RC[docker_harness]=$?
echo "   exit code: ${RC[docker_harness]}"

echo
echo "== summary =="
overall=0
for k in "${!RC[@]}"; do
  echo "   $k: ${RC[$k]}"
  [ "${RC[$k]}" -eq 0 ] || overall=1
done
exit "$overall"
