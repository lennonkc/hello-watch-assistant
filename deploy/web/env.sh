# The web service's WHOLE environment, computed from parameters every time (deployed with --set-env-vars, so nothing
# is inherited from an earlier revision). Sourced by deploy/web/deploy.sh and tools/ops/run.sh; macOS bash 3.2.
#
#   service_env        KEY=VALUE lines. Needs PROJECT, BUCKET, PACKS_GCS (gs://…); optional PRIVATE_TITLES_GCS,
#                      CEDAR_GEMINI_BACKEND (enterprise), WATCH_ASK_PER_MINUTE / _PER_DAY_PER_IP / _PER_DAY_TOTAL
#                      (6 / 80 / 1500), BENCH_TOKEN_VALUE (sets WATCH_BENCH_TOKEN), EXTRA_ENV (KEY=VALUE,…).
#   env_names LINES    the names only, space-separated (what logs print; values may be secret)
#   live_env NAME      NAME in the environment of the revision serving production traffic now (empty: none)
#
# Every value is checked: non-empty, only A-Z a-z 0-9 _ . / : @ + - (no comma, space or quote can smuggle another
# variable into --set-env-vars), and EXTRA_ENV may not set a computed name (WATCH_BENCH_TOKEN, the packs, …).

SERVICE_ENV_COMPUTED="CEDAR_GCP_PROJECT CEDAR_GEMINI_BACKEND CEDAR_INGEST_BUCKET WATCH_PACKS_GCS WATCH_PRIVATE_TITLES
WATCH_ASK_PER_MINUTE WATCH_ASK_PER_DAY_PER_IP WATCH_ASK_PER_DAY_TOTAL WATCH_BENCH_TOKEN"

_env_fail() { printf 'ERROR: %s\n' "$*" >&2; return 1; }

env_line() {                     # NAME VALUE → NAME=VALUE (validated)
  case "$1" in
    ""|*[!A-Z0-9_]*|[0-9]*) _env_fail "bad env name '$1'"; return 1 ;;
  esac
  case "$2" in
    ""|*[!A-Za-z0-9_./:@+-]*) _env_fail "env $1: the value must be non-empty and use only A-Z a-z 0-9 _ . / : @ + -"; return 1 ;;
  esac
  printf '%s=%s\n' "$1" "$2"
}

service_env() {
  local pair k v seen=" "
  if [ -z "${PROJECT:-}" ] || [ -z "${BUCKET:-}" ] || [ -z "${PACKS_GCS:-}" ]; then
    _env_fail "service_env needs PROJECT, BUCKET and PACKS_GCS"; return 1
  fi
  case "$PACKS_GCS" in gs://*) ;; *) _env_fail "PACKS_GCS must be gs://… (got '$PACKS_GCS')"; return 1 ;; esac
  env_line CEDAR_GCP_PROJECT "$PROJECT" || return 1
  env_line CEDAR_GEMINI_BACKEND "${CEDAR_GEMINI_BACKEND:-enterprise}" || return 1
  env_line CEDAR_INGEST_BUCKET "$BUCKET" || return 1
  env_line WATCH_PACKS_GCS "$PACKS_GCS" || return 1
  if [ -n "${PRIVATE_TITLES_GCS:-}" ]; then
    env_line WATCH_PRIVATE_TITLES "$PRIVATE_TITLES_GCS" || return 1
  fi
  env_line WATCH_ASK_PER_MINUTE "${WATCH_ASK_PER_MINUTE:-6}" || return 1
  env_line WATCH_ASK_PER_DAY_PER_IP "${WATCH_ASK_PER_DAY_PER_IP:-80}" || return 1
  env_line WATCH_ASK_PER_DAY_TOTAL "${WATCH_ASK_PER_DAY_TOTAL:-1500}" || return 1
  if [ -n "${BENCH_TOKEN_VALUE:-}" ]; then
    env_line WATCH_BENCH_TOKEN "$BENCH_TOKEN_VALUE" || return 1
  fi
  if [ -n "${EXTRA_ENV:-}" ]; then
    # split on commas without word splitting or globbing
    while IFS= read -r pair; do
      [ -n "$pair" ] || continue
      k=${pair%%=*}
      v=${pair#*=}
      if [ "$k" = "$pair" ]; then _env_fail "EXTRA_ENV entry '$pair' is not KEY=VALUE"; return 1; fi
      case " $(printf '%s' "$SERVICE_ENV_COMPUTED" | tr '\n' ' ') " in
        *" $k "*) _env_fail "EXTRA_ENV must not set $k (computed from the job parameters)"; return 1 ;;
      esac
      case "$seen" in *" $k "*) _env_fail "EXTRA_ENV sets $k twice"; return 1 ;; esac
      seen="$seen$k "
      env_line "$k" "$v" || return 1
    done <<EOF_EXTRA
$(printf '%s' "$EXTRA_ENV" | tr ',' '\n')
EOF_EXTRA
  fi
}

env_names() {
  printf '%s\n' "$1" | sed 's/=.*//' | tr '\n' ' ' | sed 's/ $//'
}

# reads `gcloud run services describe --format=json` on stdin: the revision with the largest traffic share
_PY_LIVE_REV='
import json, sys
svc = json.load(sys.stdin)
best = None
for t in svc.get("status", {}).get("traffic", []):
    if t.get("percent", 0) > 0:
        rev = t.get("revisionName") or (svc.get("status", {}).get("latestReadyRevisionName") if t.get("latestRevision") else None)
        if rev and (best is None or t["percent"] > best[0]):
            best = (t["percent"], rev)
print(best[1] if best else "")
'
# reads `gcloud run revisions describe --format=json` on stdin; argv: env NAME | env-names | image | ready
_PY_REV='
import json, sys
rev = json.load(sys.stdin)
c = (rev.get("spec", {}).get("containers") or [{}])[0]
what = sys.argv[1]
if what == "env":
    print(next((e.get("value", "") for e in c.get("env", []) if e.get("name") == sys.argv[2]), ""))
elif what == "env-names":
    print(" ".join(sorted(e.get("name", "") for e in c.get("env", []))))
elif what == "image":
    print(c.get("image", ""))
elif what == "ready":
    conds = rev.get("status", {}).get("conditions", [])
    print("True" if any(x.get("type") == "Ready" and x.get("status") == "True" for x in conds) else "False")
'

_env_py() {
  if [ -n "${JSON_PY:-}" ] && [ -x "${JSON_PY:-}" ]; then printf '%s' "$JSON_PY"; else command -v python3; fi
}

revision_q() {                   # REVISION what [NAME]: see _PY_REV
  local rev=$1
  shift
  gcloud run revisions describe "$rev" --project "$PROJECT" --region "$REGION" --format=json 2>/dev/null \
    | "$(_env_py)" -c "$_PY_REV" "$@"
}

live_revision() {
  gcloud run services describe "$SERVICE" --project "$PROJECT" --region "$REGION" --format=json 2>/dev/null \
    | "$(_env_py)" -c "$_PY_LIVE_REV" 2>/dev/null || true
}

live_env() {
  local rev
  rev=$(live_revision)
  if [ -z "$rev" ]; then
    return 0
  fi
  revision_q "$rev" env "$1"
}
