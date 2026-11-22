#!/bin/bash
# One-off operations, run by `tools/ops/run.sh job` with JOB=<name> in tools/ops/params.env:
#
#   inspect-packs | publish-public-packs | publish-private-title | bench-show | logs     (see tools/ops/jobs.py)
#
# run.sh has built the .venv and exported OPS_ROOT, OPS_VENV_PY, the machine-local paths and the discovered BUCKET /
# PROJECT / SERVICE_ACCOUNT. The job's full output goes to .cache/ops-logs/job-<name>.log; the job log shows at most
# 80 lines of it (bench-show: 120). macOS bash 3.2.
set -euo pipefail

ROOT=${OPS_ROOT:-$(cd "$(dirname "$0")/../.." && pwd)}
cd "$ROOT"
if [ -f tools/ops/params.env ]; then
  . ./tools/ops/params.env
fi
JOB=${JOB:-}
case "$JOB" in
  inspect-packs|publish-public-packs|publish-private-title|bench-show|logs) ;;
  "") printf 'ERROR: set JOB in tools/ops/params.env (%s)\n' \
        "inspect-packs | publish-public-packs | publish-private-title | bench-show | logs"
      exit 1 ;;
  *) printf 'ERROR: unknown JOB %s\n' "$JOB"; exit 1 ;;
esac
PY=${OPS_VENV_PY:-.venv/bin/python}
if [ ! -x "$PY" ]; then
  printf 'ERROR: no venv python (%s); run through tools/ops/run.sh job\n' "$PY"
  exit 1
fi
export PACKS_PREFIX="${PACKS_PREFIX:-}" PRIVATE_PREFIX="${PRIVATE_PREFIX:-}"
export PRIVATE_TITLE_SHAS="${PRIVATE_TITLE_SHAS:-}" PRIVATE_ACCESS_SHA256="${PRIVATE_ACCESS_SHA256:-}"
export BENCH_SHOW_RUN="${BENCH_SHOW_RUN:-}" BENCH_SHOW_CASES="${BENCH_SHOW_CASES:-}"
export LOGS_SINCE="${LOGS_SINCE:-}" LOGS_FILTER="${LOGS_FILTER:-}"
export OPS_ROOT="$ROOT" PYTHONDONTWRITEBYTECODE=1

mkdir -p .cache/ops-logs
LOG=".cache/ops-logs/job-$JOB.log"
MAX=78
if [ "$JOB" = bench-show ]; then
  MAX=120
fi
printf 'job %s (full output: %s)\n' "$JOB" "$LOG"
# awk reads to the end (no SIGPIPE for the job); pipefail keeps the job's exit status
"$PY" -u tools/ops/jobs.py "$JOB" 2>&1 | tee "$LOG" | awk -v max="$MAX" -v lf="$LOG" '
  NR <= max { print; fflush() }
  END { if (NR > max) printf "... %d more lines in %s\n", NR - max, lf }'
