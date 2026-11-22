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
. ./deploy/web/env.sh                # service_env, env_names, live_env, revision_q
hr() { printf '\n== %s\n' "$*"; }
die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
have() { command -v "$1" >/dev/null 2>&1; }

# Job logs are read with limited context: listings and tool output are capped, full output goes to .cache/ops-logs/.
LOGS="$ROOT/.cache/ops-logs"
mkdir -p "$LOGS"
show_head() {                    # FILE [N]: first N lines, then how many were left out
  local n=${2:-15} total
  total=$(wc -l < "$1" | tr -d ' ')
  head -n "$n" "$1"
  if [ "$total" -gt "$n" ]; then say "  ... $((total - n)) more lines in ${1#$ROOT/}"; fi
}
show_tail() {                    # FILE [N]: last N lines, after saying how many were left out
  local n=${2:-15} total
  total=$(wc -l < "$1" | tr -d ' ')
  if [ "$total" -gt "$n" ]; then say "  ... $((total - n)) earlier lines in ${1#$ROOT/}"; fi
  tail -n "$n" "$1"
}

# ── python and .venv ─────────────────────────────────────────────────

PYV_CODE='import sys; print(sys.version_info[0] * 100 + sys.version_info[1])'

PYFULL_CODE='import sys; print("%d.%d.%d" % sys.version_info[:3])'

# Interpreter for the .venv: 3.13 (what the image runs), then 3.12, then 3.14. Never 3.15+: binary wheels are missing
# there and the runner cannot compile native code. Every package must come as a wheel.
py_rank() {
  case "$1" in
    313) echo 1 ;;
    312) echo 2 ;;
    314) echo 3 ;;
    *) echo 99 ;;
  esac
}

# candidate paths, one per line (duplicates and missing ones are fine); "all" also lists versions that are never used
py_candidates() {
  local m p minors="13 12 14"
  if [ "${1:-}" = all ]; then minors="13 12 14 15 16 11"; fi
  for m in $minors; do
    for p in "python3.$m" "/opt/homebrew/bin/python3.$m" "/opt/homebrew/opt/python@3.$m/bin/python3.$m" \
             "/usr/local/bin/python3.$m" "/usr/local/opt/python@3.$m/bin/python3.$m" \
             "/Library/Frameworks/Python.framework/Versions/3.$m/bin/python3" "$HOME"/.pyenv/versions/3.$m*/bin/python3; do
      printf '%s\n' "$p"
    done
    if have uv; then
      uv python find "3.$m" 2>/dev/null || true
    fi
  done
  printf '%s\n' python3
}

# resolved, existing, de-duplicated interpreters as "path version-code" lines
py_found() {
  local p v seen="|"
  py_candidates "${1:-}" > "$LOGS/py-candidates.txt"
  while IFS= read -r p; do
    [ -n "$p" ] || continue
    if [ "${p#/}" = "$p" ]; then
      p=$(command -v "$p" 2>/dev/null) || continue
    fi
    [ -x "$p" ] || continue
    case "$seen" in *"|$p|"*) continue ;; esac
    seen="$seen$p|"
    v=$("$p" -c "$PYV_CODE" 2>/dev/null) || continue
    printf '%s %s\n' "$p" "$v"
  done < "$LOGS/py-candidates.txt"
}

find_python() {
  local best="" best_r=99 p v r
  py_found > "$LOGS/py-found.txt"
  while read -r p v; do
    r=$(py_rank "$v")
    if [ "$r" -lt "$best_r" ]; then
      best=$p
      best_r=$r
    fi
  done < "$LOGS/py-found.txt"
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

# pip with a short, live log (one line per 25 packages collected, results, errors). Wheels only: a missing wheel
# fails at once with the package name instead of a long source build that cannot link on the runner.
pip_install() {
  "$VENV_PY" -m pip install --only-binary=:all: --progress-bar off --disable-pip-version-check "$@" 2>&1 | awk '
    /^Collecting / { n++; if (n % 25 == 0) { print "  collected " n " packages so far"; fflush() } next }
    /Successfully|ERROR|^error|No matching|Installing collected/ { print "  " substr($0, 1, 200); fflush() }
  '
}

# .venv in the checkout (git-ignored, survives between jobs). Rebuilt when its Python minor version differs from the
# chosen interpreter; requirements re-installed only when the requirements files change.
ensure_venv() {
  local base="" want_v want have_hash v
  if base=$(find_python); then
    want_v=$("$base" -c "$PYV_CODE")
  elif have uv; then
    want_v=313
  else
    die "no Python 3.13 / 3.12 / 3.14 found (3.15+ is never used): install it with: brew install python@3.13"
  fi
  if [ -e .venv ]; then
    v=$("$VENV_PY" -c "$PYV_CODE" 2>/dev/null) || v=none
    if [ "$v" != "$want_v" ]; then
      say "removing .venv (python $v; want $want_v)"
      rm -rf .venv
    fi
  fi
  if [ ! -x "$VENV_PY" ]; then
    if [ -n "$base" ]; then
      say "creating .venv with $base ($("$base" --version 2>&1))"
      "$base" -m venv .venv || die "python -m venv failed"
    else
      say "no Python 3.12-3.14 installed: creating .venv with a uv-managed Python 3.13"
      if ! uv venv --seed --python 3.13 .venv >"$LOGS/uv-venv.log" 2>&1; then
        show_tail "$LOGS/uv-venv.log" 10
        die "uv venv failed"
      fi
    fi
  fi
  if [ "$want_v" != 313 ]; then
    say "WARN: .venv uses python $want_v; the image runs 3.13 (brew install python@3.13)"
  fi
  want=$(file_hash requirements-ingest.txt requirements-web.txt)
  have_hash=$(cat .venv/.requirements.sha256 2>/dev/null || true)
  if [ "$want" != "$have_hash" ]; then
    say "installing requirements into .venv, wheels only ($(date -u '+%H:%M:%S') UTC)"
    pip_install --upgrade pip || die "pip upgrade failed"
    pip_install -r requirements-ingest.txt -r requirements-web.txt || die "pip install failed (see the lines above)"
    printf '%s\n' "$want" > .venv/.requirements.sha256
    say "requirements installed ($(date -u '+%H:%M:%S') UTC)"
  fi
}

# ── cloud discovery ──────────────────────────────────────────────────

# reads the service JSON on stdin; argv: env NAME | url | traffic | sa | tag-url TAG | tag-revision TAG
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
    for t in svc.get("status", {}).get("traffic", [])[:15]:
        rev = t.get("revisionName") or ("LATEST" if t.get("latestRevision") else "?")
        print("  %-40s %4s%%  tag=%-8s %s" % (rev, t.get("percent", 0), t.get("tag", "-"), t.get("url", "")))
elif what == "sa":
    print(svc.get("spec", {}).get("template", {}).get("spec", {}).get("serviceAccountName", ""))
elif what == "tag-url":
    for t in svc.get("status", {}).get("traffic", []):
        if t.get("tag") == sys.argv[2]:
            print(t.get("url", ""))
elif what == "tag-revision":
    for t in svc.get("status", {}).get("traffic", []):
        if t.get("tag") == sys.argv[2]:
            print(t.get("revisionName", ""))
'

# asset sha of every film in catalog/catalog.json (from ingest/titles/<id>.json)
PY_SHAS='
import json, pathlib
cat = json.loads(pathlib.Path("catalog/catalog.json").read_text(encoding="utf-8"))
for it in cat["items"]:
    spec = json.loads(pathlib.Path("ingest/titles/%s.json" % it["id"]).read_text(encoding="utf-8"))
    print(spec["expected_asset_sha256"][0])
'
PY_TOKEN='import secrets; print(secrets.token_hex(24))'

# summarizes `gcloud storage ls -l` output on stdin; argv: packs|files PREFIX. packs: one line per film and
# distribution (pack.db and MANIFEST.json size / date, thumbnail count); files: newest first. At most 15 lines.
PY_LS='
import re, sys
mode, prefix = sys.argv[1], sys.argv[2].rstrip("/")
rows = []
for line in sys.stdin:
    m = re.match(r"\s*(\d+)\s+(\S+)\s+(gs://\S+)\s*$", line)
    if m:
        rows.append((int(m.group(1)), m.group(2)[:10], m.group(3)))
def h(n):
    for u in ("B", "KB", "MB", "GB"):
        if n < 1024 or u == "GB":
            return "%d %s" % (n, u) if u == "B" else "%.1f %s" % (n, u)
        n /= 1024.0
out = []
if mode == "packs":
    groups = {}
    for size, date, url in rows:
        parts = url[len(prefix) + 1:].split("/")
        if len(parts) < 3:
            continue
        g = groups.setdefault((parts[0], parts[1]), {"thumbs": 0, "other": 0, "bytes": 0})
        g["bytes"] += size
        name = "/".join(parts[2:])
        if name in ("pack.db", "MANIFEST.json"):
            g[name] = "%s %s" % (h(size), date)
        elif name.startswith("thumbs/"):
            g["thumbs"] += 1
        else:
            g["other"] += 1
    for (sha, dist), g in sorted(groups.items()):
        out.append("  %s  %-11s pack.db %-20s MANIFEST %-17s thumbs %5d  other %d  (%s)" % (
            sha[:12], dist, g.get("pack.db", "-"), g.get("MANIFEST.json", "-"), g["thumbs"], g["other"], h(g["bytes"])))
else:
    for size, date, url in sorted(rows, key=lambda r: r[1], reverse=True):
        out.append("  %-76s %10s  %s" % (url[len(prefix) + 1:], h(size), date))
for line in out[:15]:
    print(line)
if len(out) > 15:
    print("  ... %d more" % (len(out) - 15))
print("  total: %d objects, %s" % (len(rows), h(sum(r[0] for r in rows))))
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
  JSON_PY=$VENV_PY                              # any python3 can parse gcloud's JSON
  if [ ! -x "$JSON_PY" ]; then
    JSON_PY=$(find_python) || JSON_PY=$(command -v python3) || die "no python3 to parse gcloud output"
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
  local k v p chosen mark
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
  say "REGION=$REGION SERVICE=$SERVICE TAG=$TAG PACKS_PREFIX=${PACKS_PREFIX:-(from the service)} BENCH_FLOW=${BENCH_FLOW:-(server default)}"

  hr "tools"
  say "bash: $BASH_VERSION"
  for p in git gcloud ffmpeg ffprobe curl openssl; do
    if have "$p"; then say "$p: $(command -v "$p")"; else say "$p: (not found)"; fi
  done
  if have git; then say "  $(git --version)"; fi
  if have gcloud; then say "  $(gcloud version 2>/dev/null | head -1)"; fi
  if have ffmpeg; then say "  $(ffmpeg -version 2>/dev/null | head -1)"; fi
  for p in uv pyenv brew; do
    if have "$p"; then say "$p: $(command -v "$p")"; else say "$p: (not found)"; fi
  done

  hr "python interpreters (3.13 preferred, then 3.12, then 3.14; 3.15+ never used)"
  chosen=$(find_python) || chosen=""
  py_found all > "$LOGS/py-all.txt"
  while read -r p v; do
    if [ "$p" = "$chosen" ]; then
      mark="<- chosen"
    elif [ "$(py_rank "$v")" = 99 ]; then
      mark="(not used)"
    else
      mark=""
    fi
    say "  $p  $("$p" -c "$PYFULL_CODE" 2>/dev/null)  $mark"
  done < "$LOGS/py-all.txt"
  if [ -z "$chosen" ]; then
    if have uv; then
      say "  none usable: tests will create the .venv with a uv-managed 3.13"
    else
      say "  none usable: brew install python@3.13"
    fi
  fi
  if [ -e .venv ]; then
    say ".venv: $("$VENV_PY" --version 2>&1 || echo 'broken'), requirements hash $(cut -c1-12 .venv/.requirements.sha256 2>/dev/null || echo 'none (install incomplete)')"
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
    if [ -n "$v" ]; then say "latest revision env $k=$v"; fi
  done
  if [ -n "$(svc_q env WATCH_BENCH_TOKEN)" ]; then say "latest revision env WATCH_BENCH_TOKEN: set (value hidden)"; fi
  if [ -n "$(svc_q env WATCH_AGENT_PROMPTS_SECRET)" ]; then say "latest revision env WATCH_AGENT_PROMPTS_SECRET: set"; fi

  hr "traffic"
  svc_q traffic
  hr "revisions (newest 8)"
  gcloud run revisions list --service "$SERVICE" --project "$PROJECT" --region "$REGION" --limit 8 \
    --sort-by '~metadata.creationTimestamp' \
    --format 'table(metadata.name, metadata.creationTimestamp, status.conditions[0].status:label=READY, spec.containers[0].image.basename())' \
    2>&1 || say "(revisions list failed)"

  hr "packs under ${PACKS_GCS:-?} (per film and distribution)"
  if [ -n "$PACKS_GCS" ]; then
    if gcloud storage ls -l "$PACKS_GCS/**" >"$LOGS/packs-ls.txt" 2>/dev/null; then
      "$JSON_PY" -c "$PY_LS" packs "$PACKS_GCS" <"$LOGS/packs-ls.txt"
    else
      say "(listing failed)"
    fi
  fi
  if [ -n "${BUCKET:-}" ]; then
    hr "film assets under gs://$BUCKET/assets/ (newest first)"
    if gcloud storage ls -l "gs://$BUCKET/assets/**" >"$LOGS/assets-ls.txt" 2>/dev/null; then
      "$JSON_PY" -c "$PY_LS" files "gs://$BUCKET/assets" <"$LOGS/assets-ls.txt"
    else
      say "(listing failed)"
    fi
  fi
}

mode_tests() {
  local rc=0 lint=0
  ensure_venv
  hr "pytest"
  "$VENV_PY" -m pytest -q -p no:cacheprovider ingest/tests ingest/cloud/tests server/tests bench/tests tools/ops/tests \
    >"$LOGS/pytest.log" 2>&1 || rc=$?
  show_tail "$LOGS/pytest.log" 40
  hr "benchmark case lint"
  "$VENV_PY" bench/judge.py --lint >"$LOGS/lint.log" 2>&1 || lint=$?
  show_tail "$LOGS/lint.log" 25
  if [ "$rc" != 0 ] || [ "$lint" != 0 ]; then die "tests failed (pytest exit $rc, lint exit $lint)"; fi
}

# gs://<bucket>/<path> for a path parameter (PACKS_PREFIX, PRIVATE_PREFIX): a path inside the service's bucket
# (bucket names are never committed). Only A-Z a-z 0-9 . _ - / : no comma or space can reach --set-env-vars.
bucket_url() {
  local name=$1 prefix=${2#/}
  case "$prefix" in
    gs://*|*..*|"") die "$name must be a path inside the bucket, e.g. demo-packs-next (got '$2')" ;;
    *[!A-Za-z0-9._/-]*) die "$name may contain only A-Z a-z 0-9 . _ - / (got '$2')" ;;
  esac
  if [ -z "${BUCKET:-}" ]; then die "$name needs the service's CEDAR_INGEST_BUCKET"; fi
  printf 'gs://%s/%s\n' "$BUCKET" "${prefix%/}"
}

packs_url() {
  bucket_url PACKS_PREFIX "${PACKS_PREFIX:-}"
}

refresh_service() {
  SVC_JSON=$(gcloud run services describe "$SERVICE" --project "$PROJECT" --region "$REGION" --format=json) \
    || die "cannot describe Cloud Run service $SERVICE in $REGION"
}

# public films in the catalog (what /api/health must report)
PY_NFILMS='import json; print(len(json.load(open("catalog/catalog.json", encoding="utf-8"))["items"]))'
# reads /api/health on stdin; argv: expected public titles → "ok <titles> <revision>" or exit 1 with the reason
PY_HEALTH='
import json, sys
try:
    h = json.load(sys.stdin)
except ValueError:
    print("not JSON"); sys.exit(1)
want = int(sys.argv[1])
if not (isinstance(h, dict) and h.get("ok") is True):
    print("ok is not true"); sys.exit(1)
if h.get("titles") != want:
    print("%s public titles, expected %d" % (h.get("titles"), want)); sys.exit(1)
print("ok %s %s" % (h.get("titles"), h.get("revision") or "?"))
'

# health URL: "ok …" or dies (no answer, not ok, or not every public film loaded)
check_health() {
  local url=$1 body verdict
  have curl || die "curl not found"
  if ! body=$(curl -fsS -m 90 "$url/api/health" 2>&1); then die "health: FAILED ($url/api/health: ${body:0:200})"; fi
  if ! verdict=$(printf '%s' "$body" | "$JSON_PY" -c "$PY_HEALTH" "$("$JSON_PY" -c "$PY_NFILMS")"); then
    die "health: FAILED ($verdict)"
  fi
  say "health: $verdict"
}

# Build the image of this commit and deploy it as a NEW revision that takes no traffic, tagged $TAG. Production
# traffic is never moved (--no-traffic pins any "latest" traffic to the current revision first). The environment is
# built in full every time (deploy/web/env.sh, --set-env-vars): packs PACKS_PREFIX (default: what production serves),
# private titles PRIVATE_PREFIX (default: none), limits, STAGING_ENV, and a fresh bench token unless RELEASE=1 (the
# revision meant for promote carries no token).
mode_deploy_staging() {
  local image token envs url out rc names
  cloud_discover
  if [ -z "${BUCKET:-}" ]; then die "the service has no CEDAR_INGEST_BUCKET env var"; fi
  git rev-parse HEAD >/dev/null 2>&1 || die "deploy-staging needs a git checkout (the image is built with git archive)"
  if [ -n "${PACKS_PREFIX:-}" ]; then
    PACKS_GCS=$(packs_url) || exit 1
  else
    PACKS_GCS=$(live_env WATCH_PACKS_GCS)
    if [ -z "$PACKS_GCS" ]; then die "PACKS_PREFIX unset and the production revision has no WATCH_PACKS_GCS"; fi
  fi
  PRIVATE_TITLES_GCS=""
  if [ -n "${PRIVATE_PREFIX:-}" ]; then
    PRIVATE_TITLES_GCS="$(bucket_url PRIVATE_PREFIX "$PRIVATE_PREFIX")/titles.json" || exit 1
  fi
  token=""
  if [ "${RELEASE:-0}" != 1 ]; then
    token=$("$JSON_PY" -c "$PY_TOKEN")
  fi
  envs=$(PROJECT=$PROJECT BUCKET=$BUCKET PACKS_GCS=$PACKS_GCS PRIVATE_TITLES_GCS=$PRIVATE_TITLES_GCS \
         BENCH_TOKEN_VALUE=$token EXTRA_ENV="${STAGING_ENV:-}" service_env) || die "bad environment (see above)"
  names=$(env_names "$envs")
  hr "traffic before"
  svc_q traffic
  hr "build $(git rev-parse --short=12 HEAD)"
  if image=$(PROJECT=$PROJECT BUCKET=$BUCKET REGION=$REGION /bin/bash deploy/web/build.sh 2>"$LOGS/build.log"); then
    show_tail "$LOGS/build.log" 5
  else
    show_tail "$LOGS/build.log" 30
    die "image build failed"
  fi
  say "image: $image"

  hr "deploy (no traffic, tag $TAG$(if [ "${RELEASE:-0}" = 1 ]; then printf ', RELEASE: no bench token'; fi))"
  say "packs: $PACKS_GCS; private titles: ${PRIVATE_TITLES_GCS:-(none)}; extra env: ${STAGING_ENV:-(none)}"
  say "env (complete, replaces the previous revision's): $names"
  out=$(mktemp)
  trap 'rm -f "$out"' EXIT
  if gcloud run deploy "$SERVICE" --project "$PROJECT" --region "$REGION" --image "$image" \
      --no-traffic --tag "$TAG" --session-affinity \
      --min-instances "${MIN_INSTANCES:-1}" --max-instances "${MAX_INSTANCES:-2}" \
      --set-env-vars "$(printf '%s\n' "$envs" | paste -sd, -)" --quiet >"$out" 2>&1; then
    rc=0
  else
    rc=$?
  fi
  if [ -n "$token" ]; then
    sed "s/$token/***/g" "$out" | tail -n 15  # an error message could quote the flag value: mask the token
  else
    tail -n 15 "$out"
  fi
  rm -f "$out"
  if [ "$rc" != 0 ]; then die "gcloud run deploy failed"; fi
  mkdir -p .cache
  if [ -n "$token" ]; then                      # only now: a failed deploy leaves the previous token in place
    (umask 077 && printf '%s\n' "$token" > .cache/bench_token)
    say "WATCH_BENCH_TOKEN: fresh (in .cache/bench_token)"
  else
    rm -f .cache/bench_token
    say "WATCH_BENCH_TOKEN: none (RELEASE=1); .cache/bench_token removed"
  fi
  refresh_service
  url=$(svc_q tag-url "$TAG")
  printf 'image=%s\nrelease=%s\npacks=%s\nprivate=%s\nstaging_env=%s\ndeployed=%s\n' "$image" "${RELEASE:-0}" \
    "$PACKS_GCS" "${PRIVATE_TITLES_GCS:-}" "${STAGING_ENV:-}" "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" > .cache/staging.txt
  hr "traffic after (production unchanged)"
  svc_q traffic
  hr "tagged URL"
  say "$url"
  if [ -z "$url" ]; then die "no URL for tag $TAG after the deploy"; fi
  check_health "$url"
}

# Benchmark a URL (default: the $TAG revision) with bench/run_bench.py, grade with bench/judge.py, print the scorecard.
mode_bench() {
  local url run_id label compare gen_dir pvt
  cloud_discover
  ensure_venv
  url=${BENCH_URL:-}
  if [ -z "$url" ]; then
    url=$(svc_q tag-url "$TAG")
  fi
  if [ -z "$url" ]; then die "no URL: no revision is tagged $TAG (run deploy-staging) and BENCH_URL is unset"; fi
  if [ -n "${PACKS_PREFIX:-}" ]; then
    PACKS_GCS=$(packs_url) || exit 1
  fi
  if [ -z "${PACKS_GCS:-}" ]; then die "the service has no WATCH_PACKS_GCS env var and PACKS_PREFIX is unset"; fi

  run_id=${BENCH_RUN_ID:-}
  if [ -z "$run_id" ]; then
    run_id="$(date -u '+%m%d-%H%M')-$TAG"
    if [ -n "${BENCH_FLOW:-}" ]; then
      run_id="$run_id-$BENCH_FLOW"
    fi
    if [ -n "${BENCH_GENERATE:-}" ]; then
      run_id="$run_id-gen"
    fi
  fi
  gen_dir=""
  if [ -n "${BENCH_GENERATE:-}" ]; then
    gen_dir="$ROOT/.cache/bench/generated/$run_id"
  fi
  pvt=$(svc_q env WATCH_PRIVATE_TITLES)
  if [ -n "${PRIVATE_PREFIX:-}" ]; then
    pvt="$(bucket_url PRIVATE_PREFIX "$PRIVATE_PREFIX")/titles.json" || exit 1
  fi

  # the packs this URL serves, the pack lint of the hand-written cases, generated cases (bounded output)
  hr "packs, case lint$(if [ -n "$gen_dir" ]; then printf ', generated cases'; fi)"
  if ! OPS_ROOT="$ROOT" PACKS_GCS="$PACKS_GCS" PRIVATE_TITLES_GCS="$pvt" BENCH_BASE="$url" \
      BENCH_GENERATE="${BENCH_GENERATE:-}" BENCH_TITLES="${BENCH_TITLES:-}" BENCH_SEED="${BENCH_SEED:-}" \
      BENCH_GEN_DIR="$gen_dir" BENCH_SHOW_PRIVATE="${BENCH_SHOW_PRIVATE:-}" \
      "$VENV_PY" tools/ops/jobs.py bench-prep >"$LOGS/bench-$run_id-prep.log" 2>&1; then
    show_head "$LOGS/bench-$run_id-prep.log" 60
    die "bench preparation failed"
  fi
  show_head "$LOGS/bench-$run_id-prep.log" 60

  export BENCH_RUNS_DIR="$ROOT/.cache/bench/runs"
  label="${BENCH_LABEL:-}"
  if [ -z "$label" ] && [ -f .cache/staging.txt ] && [ -z "${BENCH_URL:-}" ]; then
    label="staging: $(tr '\n' ' ' < .cache/staging.txt)"
  fi
  hr "run $run_id against $url"
  set -- --base "$url" --run-id "$run_id" --workers "${BENCH_WORKERS:-1}" --label "$label" --progress 25
  if [ -s .cache/bench_token ]; then
    set -- "$@" --token-file .cache/bench_token
  fi
  if [ -n "${BENCH_ONLY:-}" ]; then
    set -- "$@" --only "$BENCH_ONLY"
  fi
  if [ -n "${BENCH_FLOW:-}" ]; then
    set -- "$@" --flow "$BENCH_FLOW"               # X-Watch-Flow header; honoured only with the bench token
  fi
  if [ -n "$gen_dir" ]; then
    set -- "$@" --cases "$gen_dir"                 # a generated set is its own run, never mixed with hand-written cases
  fi
  # progress every 25 cases and the first 10 errors; everything in the log
  if ! "$VENV_PY" bench/run_bench.py "$@" 2>&1 | tee "$LOGS/bench-$run_id-client.log" | awk '
      /ERROR/ { e++; if (e <= 10) print; else if (e == 11) print "  (more errors in the client log)"; fflush(); next }
      { print; fflush() }'; then
    die "bench client failed (.cache/ops-logs/bench-$run_id-client.log)"
  fi

  hr "scorecard summary"
  compare=""
  if [ -n "${BENCH_COMPARE:-}" ]; then
    compare=$BENCH_COMPARE
  fi
  set -- --packs "$ROOT/.cache/packs"
  if [ -n "$gen_dir" ]; then
    set -- "$@" --cases "$gen_dir"
  fi
  if [ "${BENCH_SHOW_PRIVATE:-}" = 1 ]; then
    set -- "$@" --show-private                     # otherwise a private preview title's cases show ids and numbers only
  fi
  if [ "${BENCH_LLM:-1}" = 0 ]; then
    set -- "$@" --no-llm
  elif [ -z "${CEDAR_GEMINI_KEY_FILE:-}" ] || [ ! -s "$CEDAR_GEMINI_KEY_FILE" ]; then
    say "WARN: no Gemini key file (HELLO_WATCH_GEMINI_KEY_FILE): rule gate only, no LLM judge"
    set -- "$@" --no-llm
  fi
  if [ -n "$compare" ]; then
    set -- "$compare" "$run_id" "$@"
  else
    set -- "$run_id" "$@"
  fi
  if ! "$VENV_PY" bench/judge.py "$@" --summary >"$LOGS/bench-$run_id-judge.txt" 2>&1; then
    show_tail "$LOGS/bench-$run_id-judge.txt" 30
    die "judge failed"
  fi
  show_head "$LOGS/bench-$run_id-judge.txt" 120
}

# Move 100 % of traffic to the $TAG revision, after checking it: Ready, image built from the checked-out commit,
# healthy with every public film, and no bench token (deploy it with RELEASE=1 first; PROMOTE_WITH_BENCH_TOKEN=1
# overrides). Prints the env names it carries.
mode_promote() {
  local url rev image want names
  cloud_discover
  url=$(svc_q tag-url "$TAG")
  rev=$(svc_q tag-revision "$TAG")
  if [ -z "$url" ] || [ -z "$rev" ]; then die "no revision is tagged $TAG"; fi
  hr "checks of $rev"
  if [ "$(revision_q "$rev" ready)" != True ]; then die "$rev is not Ready"; fi
  say "ready: yes"
  image=$(revision_q "$rev" image)
  want=$(git rev-parse --short=12 HEAD 2>/dev/null) || die "promote needs a git checkout of the commit to release"
  case "$image" in
    *":$want") say "image: $image (= checked-out commit)" ;;
    *) die "image $image is not the checked-out commit $want" ;;
  esac
  names=$(revision_q "$rev" env-names)
  say "env names: $names"
  case " $names " in
    *" WATCH_BENCH_TOKEN "*)
      if [ "${PROMOTE_WITH_BENCH_TOKEN:-0}" != 1 ]; then
        die "$rev carries WATCH_BENCH_TOKEN: deploy-staging with RELEASE=1 for the revision to promote"
      fi
      say "WARN: promoting a revision that carries the bench token (PROMOTE_WITH_BENCH_TOKEN=1)" ;;
  esac
  check_health "$url"
  hr "traffic before"
  svc_q traffic
  gcloud run services update-traffic "$SERVICE" --project "$PROJECT" --region "$REGION" --to-tags "$TAG=100" \
    --quiet 2>&1 | tail -5 || die "update-traffic failed"
  refresh_service
  hr "traffic after"
  svc_q traffic
}

# One-off operations: run tools/ops/job.sh of this commit (with the .venv and the machine-local paths exported).
mode_job() {
  if [ ! -f tools/ops/job.sh ]; then die "this commit has no tools/ops/job.sh"; fi
  ensure_venv
  hr "cloud"
  cloud_discover 1
  export OPS_ROOT="$ROOT" OPS_VENV_PY="$VENV_PY" REGION SERVICE TAG
  export HELLO_WATCH_GEMINI_KEY_FILE="${HELLO_WATCH_GEMINI_KEY_FILE:-}" HELLO_WATCH_MOVIES_DIR="${HELLO_WATCH_MOVIES_DIR:-}"
  export HELLO_WATCH_DEVICE_PACKS_DIR="${HELLO_WATCH_DEVICE_PACKS_DIR:-}" HELLO_WATCH_INGEST_CACHE="${HELLO_WATCH_INGEST_CACHE:-}"
  export PROJECT="${PROJECT:-}" BUCKET="${BUCKET:-}" SERVICE_ACCOUNT="$(svc_q sa)"
  say "project: ${PROJECT:-?} · bucket: ${BUCKET:-?} · service account: ${SERVICE_ACCOUNT:-?}"
  hr "tools/ops/job.sh"
  /bin/bash tools/ops/job.sh
}

case "$MODE" in
  info) mode_info ;;
  tests) mode_tests ;;
  deploy-staging) mode_deploy_staging ;;
  bench) mode_bench ;;
  promote) mode_promote ;;
  job) mode_job ;;
  *) die "usage: tools/ops/run.sh info|tests|deploy-staging|bench|promote|job" ;;
esac
