#!/bin/bash
# Job entry point for the operations runner (a Mac with gcloud, a Gemini key file, ffmpeg and network).
#
#   tools/ops/run.sh info | tests | deploy-staging | bench | promote | job
#
# Runs from a checkout of an exact commit with an almost empty environment (PATH and LANG; HOME may be unset).
# Written for macOS bash 3.2: no associative arrays, no mapfile, no heredocs inside $( ), BSD sed / date.
#
# Settings, later ones win:
#   .cache/ops.env        machine-local, git-ignored (HOME, HELLO_WATCH_GEMINI_KEY_FILE, HELLO_WATCH_MOVIES_DIR,
#                         HELLO_WATCH_DEVICE_PACKS_DIR, HELLO_WATCH_INGEST_CACHE)
#   tools/ops/params.env  committed job parameters (PROJECT, REGION, SERVICE, TAG, …; see that file)
# Everything cloud-specific (project, bucket, packs prefix, URLs) is discovered at run time from gcloud and from the
# deployed service's own environment; nothing is hard-coded here. Secrets are never printed.
set -euo pipefail

MODE=${1:-}
ROOT=$(cd "$(dirname "$0")/../.." && pwd)
cd "$ROOT"

# ── settings ─────────────────────────────────────────────────────────

# Both files are sourced WITHOUT exporting: only what a tool needs is exported below (HOME, the Gemini key path), so
# job parameters such as limiter values never leak into the test run's environment.
if [ -f .cache/ops.env ]; then
  . ./.cache/ops.env
fi
if [ -z "${HOME:-}" ]; then
  _user=$(id -un)
  HOME=$(dscl . -read "/Users/$_user" NFSHomeDirectory 2>/dev/null | sed 's/^NFSHomeDirectory: *//') || HOME=
  if [ -z "$HOME" ]; then
    HOME=$(eval echo "~$_user")
  fi
fi
export HOME
if [ -f tools/ops/params.env ]; then
  . ./tools/ops/params.env
fi

REGION=${REGION:-us-west1}
SERVICE=${SERVICE:-hello-watch}
TAG=${TAG:-next}
export CLOUDSDK_CORE_DISABLE_PROMPTS=1
if [ -n "${HELLO_WATCH_GEMINI_KEY_FILE:-}" ]; then
  export CEDAR_GEMINI_KEY_FILE="$HELLO_WATCH_GEMINI_KEY_FILE"
fi

for d in "$HOME/google-cloud-sdk/bin" \
         /usr/local/Caskroom/google-cloud-sdk/latest/google-cloud-sdk/bin \
         /opt/homebrew/Caskroom/google-cloud-sdk/latest/google-cloud-sdk/bin \
         /usr/local/share/google-cloud-sdk/bin /opt/homebrew/share/google-cloud-sdk/bin \
         /usr/local/bin /usr/local/sbin /opt/homebrew/bin /opt/homebrew/sbin; do
  if [ -d "$d" ]; then
    PATH="$d:$PATH"
  fi
done
export PATH

say() { printf '%s\n' "$*"; }
hr() { printf '\n== %s\n' "$*"; }
die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
have() { command -v "$1" >/dev/null 2>&1; }

# ── python and .venv ─────────────────────────────────────────────────

PYV_CODE='import sys; print(sys.version_info[0] * 100 + sys.version_info[1])'

# newest python3 >= 3.12 on PATH or in the usual Homebrew / python.org places; prints its path
find_python() {
  local best="" best_v=0 p v
  for p in python3.14 python3.13 python3.12 python3 \
           /opt/homebrew/bin/python3.14 /opt/homebrew/bin/python3.13 /opt/homebrew/bin/python3.12 \
           /usr/local/bin/python3.14 /usr/local/bin/python3.13 /usr/local/bin/python3.12 \
           /Library/Frameworks/Python.framework/Versions/3.14/bin/python3 \
           /Library/Frameworks/Python.framework/Versions/3.13/bin/python3 \
           /Library/Frameworks/Python.framework/Versions/3.12/bin/python3; do
    if [ "${p#/}" = "$p" ]; then
      p=$(command -v "$p" 2>/dev/null) || continue
    fi
    [ -x "$p" ] || continue
    v=$("$p" -c "$PYV_CODE" 2>/dev/null) || continue
    if [ "$v" -ge 312 ] && [ "$v" -gt "$best_v" ]; then
      best=$p
      best_v=$v
    fi
  done
  [ -n "$best" ] || return 1
  printf '%s\n' "$best"
}

file_hash() {
  if have shasum; then
    cat "$@" | shasum -a 256 | cut -d' ' -f1
  else
    cat "$@" | sha256sum | cut -d' ' -f1
  fi
}

VENV_PY="$ROOT/.venv/bin/python"

# .venv in the checkout (git-ignored, survives between jobs); deps re-installed only when requirements change
ensure_venv() {
  local base want have_hash v
  if [ -x "$VENV_PY" ]; then
    v=$("$VENV_PY" -c "$PYV_CODE" 2>/dev/null) || v=0
    if [ "$v" -lt 312 ]; then
      say "venv python too old or broken ($v); recreating"
      rm -rf .venv
    fi
  fi
  if [ ! -x "$VENV_PY" ]; then
    base=$(find_python) || die "no python3 >= 3.12 found (PATH=$PATH)"
    say "creating .venv with $base ($("$base" --version 2>&1))"
    "$base" -m venv .venv
  fi
  want=$(file_hash requirements-ingest.txt requirements-web.txt)
  have_hash=$(cat .venv/.requirements.sha256 2>/dev/null || true)
  if [ "$want" != "$have_hash" ]; then
    say "installing requirements into .venv"
    "$VENV_PY" -m pip install -q --upgrade pip
    "$VENV_PY" -m pip install -q -r requirements-ingest.txt -r requirements-web.txt
    printf '%s\n' "$want" > .venv/.requirements.sha256
  fi
}

# ── cloud discovery ──────────────────────────────────────────────────

# reads the service JSON on stdin; argv: env NAME | url | traffic
PY_SVC='
import json, sys
svc = json.load(sys.stdin)
what = sys.argv[1]
if what == "env":
    for c in svc.get("spec", {}).get("template", {}).get("spec", {}).get("containers", []):
        for e in c.get("env", []):
            if e.get("name") == sys.argv[2] and "value" in e:
                print(e["value"])
elif what == "url":
    print(svc.get("status", {}).get("url", ""))
elif what == "traffic":
    for t in svc.get("status", {}).get("traffic", []):
        rev = t.get("revisionName") or ("LATEST" if t.get("latestRevision") else "?")
        print("  %-40s %4s%%  tag=%-8s %s" % (rev, t.get("percent", 0), t.get("tag", "-"), t.get("url", "")))
elif what == "tag-url":
    for t in svc.get("status", {}).get("traffic", []):
        if t.get("tag") == sys.argv[2]:
            print(t.get("url", ""))
'

SVC_JSON=""
JSON_PY=""

svc_q() {
  [ -n "$SVC_JSON" ] || return 0
  printf '%s' "$SVC_JSON" | "$JSON_PY" -c "$PY_SVC" "$@"
}

# PROJECT, SVC_JSON, SERVICE_URL, BUCKET, PACKS_GCS; soft=1 only warns (info)
cloud_discover() {
  local soft=${1:-0}
  if ! have gcloud; then
    if [ "$soft" = 1 ]; then say "gcloud: not found"; return 0; fi
    die "gcloud not found"
  fi
  JSON_PY=$VENV_PY
  if [ ! -x "$JSON_PY" ]; then
    JSON_PY=$(find_python) || die "no python3 >= 3.12 to parse gcloud output"
  fi
  if [ -z "${PROJECT:-}" ]; then
    PROJECT=$(gcloud config get-value project 2>/dev/null) || PROJECT=
  fi
  if [ -z "$PROJECT" ]; then
    if [ "$soft" = 1 ]; then say "project: (none: gcloud config set project …, or PROJECT in params.env)"; return 0; fi
    die "no gcloud project (gcloud config set project …, or PROJECT in tools/ops/params.env)"
  fi
  SVC_JSON=$(gcloud run services describe "$SERVICE" --project "$PROJECT" --region "$REGION" --format=json 2>/dev/null) \
    || SVC_JSON=""
  if [ -z "$SVC_JSON" ]; then
    if [ "$soft" = 1 ]; then say "service $SERVICE in $REGION: not found or not readable"; return 0; fi
    die "cannot describe Cloud Run service $SERVICE in $REGION"
  fi
  SERVICE_URL=$(svc_q url)
  BUCKET=$(svc_q env CEDAR_INGEST_BUCKET)
  PACKS_GCS=$(svc_q env WATCH_PACKS_GCS)
}

# ── modes ────────────────────────────────────────────────────────────

mode_info() {
  local k v p
  hr "host"
  say "date: $(date -u '+%Y-%m-%d %H:%M:%S UTC')"
  say "uname: $(uname -srm)"
  say "checkout: $ROOT"
  say "commit: $(git rev-parse --short=12 HEAD 2>/dev/null || echo '(not a git checkout)')"
  say "disk free: $(df -h "$ROOT" | tail -1 | awk '{print $4 " of " $2 " (" $5 " used)"}')"

  hr "settings"
  if [ -f .cache/ops.env ]; then say ".cache/ops.env: present"; else say ".cache/ops.env: missing"; fi
  if [ -f tools/ops/params.env ]; then say "tools/ops/params.env: present"; else say "tools/ops/params.env: missing"; fi
  for k in HOME HELLO_WATCH_GEMINI_KEY_FILE HELLO_WATCH_MOVIES_DIR HELLO_WATCH_DEVICE_PACKS_DIR HELLO_WATCH_INGEST_CACHE; do
    v=$(eval "printf '%s' \"\${$k:-}\"")
    if [ -z "$v" ]; then
      say "$k: (unset)"
    elif [ -e "$v" ]; then
      say "$k: $v (exists)"
    else
      say "$k: $v (MISSING)"
    fi
  done
  if [ -n "${CEDAR_GEMINI_KEY_FILE:-}" ] && [ -s "$CEDAR_GEMINI_KEY_FILE" ]; then
    say "Gemini key file: present (non-empty)"
  else
    say "Gemini key file: not present"
  fi
  say "REGION=$REGION SERVICE=$SERVICE TAG=$TAG"

  hr "tools"
  say "bash: $BASH_VERSION"
  for p in git gcloud ffmpeg ffprobe curl openssl; do
    if have "$p"; then say "$p: $(command -v "$p")"; else say "$p: (not found)"; fi
  done
  if have git; then say "  $(git --version)"; fi
  if have gcloud; then say "  $(gcloud version 2>/dev/null | head -1)"; fi
  if have ffmpeg; then say "  $(ffmpeg -version 2>/dev/null | head -1)"; fi
  if p=$(find_python); then
    say "python >= 3.12: $p ($("$p" --version 2>&1))"
  else
    say "python >= 3.12: (not found)"
  fi
  if [ -x "$VENV_PY" ]; then
    say ".venv: $("$VENV_PY" --version 2>&1), requirements hash $(cut -c1-12 .venv/.requirements.sha256 2>/dev/null || echo '?')"
  else
    say ".venv: (not created yet; the tests mode creates it)"
  fi

  hr "gcloud"
  if ! have gcloud; then
    say "gcloud not found; skipping cloud checks"
    return 0
  fi
  if [ -n "$(gcloud auth list --filter=status:ACTIVE --format='value(account)' 2>/dev/null)" ]; then
    say "authenticated: yes"
  else
    say "authenticated: NO (gcloud auth login)"
  fi
  if gcloud auth print-access-token >/dev/null 2>&1; then say "access token: ok"; else say "access token: FAILED"; fi
  cloud_discover 1
  if [ -n "${PROJECT:-}" ]; then say "project: $PROJECT"; fi
  if [ -z "$SVC_JSON" ]; then return 0; fi
  say "service URL: $SERVICE_URL"
  say "bucket (CEDAR_INGEST_BUCKET): ${BUCKET:-(unset)}"
  say "packs prefix (WATCH_PACKS_GCS): ${PACKS_GCS:-(unset)}"
  for k in WATCH_MODEL WATCH_FLOW WATCH_ASK_PER_MINUTE WATCH_ASK_PER_DAY_PER_IP CEDAR_GEMINI_BACKEND; do
    v=$(svc_q env "$k")
    if [ -n "$v" ]; then say "service env $k=$v"; fi
  done
  if [ -n "$(svc_q env WATCH_BENCH_TOKEN)" ]; then say "service env WATCH_BENCH_TOKEN: set (value hidden)"; fi

  hr "traffic"
  svc_q traffic
  hr "revisions (newest 8)"
  gcloud run revisions list --service "$SERVICE" --project "$PROJECT" --region "$REGION" --limit 8 \
    --sort-by '~metadata.creationTimestamp' \
    --format 'table(metadata.name, metadata.creationTimestamp, status.conditions[0].status:label=READY, spec.containers[0].image.basename())' \
    2>&1 || say "(revisions list failed)"

  hr "packs under ${PACKS_GCS:-?}"
  if [ -n "$PACKS_GCS" ]; then
    gcloud storage ls -l "$PACKS_GCS/*/master/*" 2>&1 | grep -v '/thumbs/$' | sed "s|$PACKS_GCS/||" || say "(listing failed)"
    gcloud storage du -s "$PACKS_GCS" 2>&1 | sed "s|$PACKS_GCS|total|" || true
  fi
  if [ -n "${BUCKET:-}" ]; then
    hr "film assets under gs://$BUCKET/assets/"
    gcloud storage ls -l "gs://$BUCKET/assets/" 2>&1 | sed "s|gs://$BUCKET/assets/||" | tail -12 || say "(listing failed)"
  fi
}

mode_tests() {
  ensure_venv
  hr "pytest"
  "$VENV_PY" -m pytest -q -p no:cacheprovider ingest/tests ingest/cloud/tests server/tests bench/tests
  hr "benchmark case lint"
  "$VENV_PY" bench/judge.py --lint
}

not_yet() { die "mode '$MODE' is not implemented in this commit"; }

case "$MODE" in
  info) mode_info ;;
  tests) mode_tests ;;
  deploy-staging|bench|promote|job) not_yet ;;
  *) die "usage: tools/ops/run.sh info|tests|deploy-staging|bench|promote|job" ;;
esac
