#!/usr/bin/env bash
# Bundle prompts/ into one JSON document, write it to Secret Manager (as a new version), and grant the service account read access.
#   PROJECT=<project> [SECRET=cedar-watch-prompts] [SA=cedar-watch-ingest] [DIR=prompts] deploy/prompts/push_prompts.sh
# Consumers set CEDAR_PROMPTS_SECRET=projects/<project>/secrets/<secret>/versions/latest to read from here (ingest/promptbook.py).
set -euo pipefail
: "${PROJECT:?set PROJECT}"
SECRET=${SECRET:-cedar-watch-prompts}
SA=${SA:-cedar-watch-ingest}
ROOT=$(cd "$(dirname "$0")/../.." && pwd)
DIR=${DIR:-$ROOT/prompts}
PY=${PY:-$ROOT/.venv/bin/python}
TMP=$(mktemp); trap 'rm -f "$TMP"' EXIT
(cd "$ROOT" && "$PY" -m ingest.promptbook "$DIR") > "$TMP"
gcloud secrets describe "$SECRET" --project "$PROJECT" >/dev/null 2>&1 \
  || gcloud secrets create "$SECRET" --project "$PROJECT" --replication-policy automatic --labels app=cedar-watch
gcloud secrets versions add "$SECRET" --project "$PROJECT" --data-file "$TMP"
gcloud secrets add-iam-policy-binding "$SECRET" --project "$PROJECT" \
  --member "serviceAccount:$SA@$PROJECT.iam.gserviceaccount.com" --role roles/secretmanager.secretAccessor >/dev/null
echo "CEDAR_PROMPTS_SECRET=projects/$PROJECT/secrets/$SECRET/versions/latest"
