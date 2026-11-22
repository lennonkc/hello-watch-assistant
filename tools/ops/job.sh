#!/bin/bash
# One-off operations, run by `tools/ops/run.sh job` with JOB=<name>[,<name>…] in tools/ops/params.env:
#
#   inspect-packs | publish-public-packs | publish-private-title | bench-show | logs | rejudge   (tools/ops/jobs.py)
#
# Several names run one after the other and stop at the first failure. run.sh has built the .venv and exported
# OPS_ROOT, OPS_VENV_PY, the machine-local paths and the discovered BUCKET / PROJECT / SERVICE_ACCOUNT. Each job's
# full output goes to .cache/ops-logs/job-<name>.log; the job log shows at most 80 lines of it (bench-show, rejudge:
# 120).
# macOS bash 3.2.
set -euo pipefail

ROOT=${OPS_ROOT:-$(cd "$(dirname "$0")/../.." && pwd)}
cd "$ROOT"
if [ -f tools/ops/params.env ]; then
  . ./tools/ops/params.env
fi
NAMES="inspect-packs | publish-public-packs | publish-private-title | bench-show | logs | rejudge"
JOBS=$(printf '%s' "${JOB:-}" | tr ',' ' ')
if [ -z "$JOBS" ]; then
  printf 'ERROR: set JOB in tools/ops/params.env (%s)\n' "$NAMES"
  exit 1
fi
for j in $JOBS; do
  case "$j" in
    inspect-packs|publish-public-packs|publish-private-title|bench-show|logs|rejudge) ;;
    *) printf 'ERROR: unknown JOB %s (%s)\n' "$j" "$NAMES"; exit 1 ;;
  esac
done
PY=${OPS_VENV_PY:-.venv/bin/python}
if [ ! -x "$PY" ]; then
  printf 'ERROR: no venv python (%s); run through tools/ops/run.sh job\n' "$PY"
  exit 1
fi
export PACKS_PREFIX="${PACKS_PREFIX:-}" PRIVATE_PREFIX="${PRIVATE_PREFIX:-}"
export PRIVATE_TITLE_SHAS="${PRIVATE_TITLE_SHAS:-}" PRIVATE_ACCESS_SHA256="${PRIVATE_ACCESS_SHA256:-}"
export PREVIEW_REBUILD="${PREVIEW_REBUILD:-}"
export BENCH_SHOW_RUN="${BENCH_SHOW_RUN:-}" BENCH_SHOW_CASES="${BENCH_SHOW_CASES:-}"
export LOGS_SINCE="${LOGS_SINCE:-}" LOGS_FILTER="${LOGS_FILTER:-}"
export BENCH_REJUDGE_RUNS="${BENCH_REJUDGE_RUNS:-}" BENCH_COMPARE="${BENCH_COMPARE:-}" BENCH_LLM="${BENCH_LLM:-1}"
export BENCH_SHOW_PRIVATE="${BENCH_SHOW_PRIVATE:-}"
export OPS_ROOT="$ROOT" PYTHONDONTWRITEBYTECODE=1

mkdir -p .cache/ops-logs
for j in $JOBS; do
  LOG=".cache/ops-logs/job-$j.log"
  MAX=78
  if [ "$j" = bench-show ] || [ "$j" = rejudge ]; then
    MAX=120
  fi
  printf '\n== job %s (full output: %s)\n' "$j" "$LOG"
  # awk reads to the end (no SIGPIPE for the job); pipefail keeps the job's exit status
  "$PY" -u tools/ops/jobs.py "$j" 2>&1 | tee "$LOG" | awk -v max="$MAX" -v lf="$LOG" '
    NR <= max { print; fflush() }
    END { if (NR > max) printf "... %d more lines in %s\n", NR - max, lf }'
done
