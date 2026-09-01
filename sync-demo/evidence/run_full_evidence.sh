#!/usr/bin/env bash
# Brings up the full Docker stack once and collects every piece of evidence the
# final report cites, in one stack lifecycle:
#
#   1. scenarios 01 and 02, run unmodified (the pass/fail record)
#   2. scenarios 03 and 04, run unmodified but *under* evidence/ab_benchmark.py,
#      which samples docker stats throughout. Same scripts, same pass/fail
#      record, and the branch-comparison data (plots 7-9) falls out of the same
#      execution rather than needing a second, differently-conditioned run --
#      which is what made the earlier A/B hard to trust.
#   3. evidence/docker_harness.py against the same still-running stack, for the
#      timestamped failover and network-emulation data (plots 4-6)
#
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

if [ ! -x .venv/bin/python ]; then
  echo "no .venv found; run ./demo.sh quick once first (or 'uv venv --python 3.12 .venv && uv pip install ...')"
  exit 1
fi

declare -A RC
for s in 01_normal_sync 02_interrupted_resume; do
  echo "== running scenarios/${s}.sh =="
  ./scenarios/${s}.sh > "$LOGS/${s}.log" 2>&1
  RC[$s]=$?
  echo "   exit code: ${RC[$s]}"
done

# Scenarios 03 and 04 under the resource sampler. ab_benchmark runs each script
# unmodified and writes its output to the same evidence/logs/<name>.log the loop
# above uses, so the pass/fail record is unchanged in form and content; the only
# addition is docker stats sampled across both.
BRANCH="$(git rev-parse --abbrev-ref HEAD)"
echo "== running scenarios/03 + scenarios/04 under evidence/ab_benchmark.py (label: $BRANCH) =="
.venv/bin/python evidence/ab_benchmark.py --label "$BRANCH" --log-dir "$LOGS" \
  -- ./scenarios/03_failover_and_blackout.sh ./scenarios/04_stealth_mode.sh
RC[ab_benchmark_03_04]=$?
echo "   exit code: ${RC[ab_benchmark_03_04]}"

echo "== collecting plot evidence from the live stack (docker_harness.py) =="
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
