#!/usr/bin/env bash
# Build and deploy the public web demo to Cloud Run.
#   PROJECT=<project> BUCKET=<bucket> PROMPTS_DIR=<dir with agent prompts> [REGION=us-west1] deploy/web/deploy.sh
#
# What it sets up (idempotent):
#   - service account hello-watch-web: Gemini on Agent Platform, read-only on the bucket (packs + films),
#     signs its own playback URLs, reads the agent-prompt secret;
#   - secret hello-watch-agent-prompts: a new version from PROMPTS_DIR (skipped when PROMPTS_DIR is unset);
#   - image <region>-docker.pkg.dev/<project>/cedar-watch/web:<commit> via Cloud Build (committed code only);
#   - Cloud Run service hello-watch: public, at most MAX_INSTANCES instances, per-IP and daily question limits.
set -euo pipefail
: "${PROJECT:?set PROJECT}" "${BUCKET:?set BUCKET}"
REGION=${REGION:-us-west1}
SERVICE=${SERVICE:-hello-watch}
SA_NAME=${SA_NAME:-hello-watch-web}
SECRET=${SECRET:-hello-watch-agent-prompts}
MAX_INSTANCES=${MAX_INSTANCES:-2}
MIN_INSTANCES=${MIN_INSTANCES:-1}
SA=$SA_NAME@$PROJECT.iam.gserviceaccount.com
ROOT=$(git -C "$(dirname "$0")" rev-parse --show-toplevel)
cd "$ROOT"
PY=${PY:-$ROOT/.venv/bin/python}

# 1. service account and its permissions
gcloud iam service-accounts describe "$SA" --project "$PROJECT" >/dev/null 2>&1 \
  || gcloud iam service-accounts create "$SA_NAME" --project "$PROJECT" --display-name "hello-watch web demo"
gcloud projects add-iam-policy-binding "$PROJECT" --member "serviceAccount:$SA" --role roles/aiplatform.user \
  --condition None >/dev/null
gcloud storage buckets add-iam-policy-binding "gs://$BUCKET" --member "serviceAccount:$SA" \
  --role roles/storage.objectViewer >/dev/null
gcloud iam service-accounts add-iam-policy-binding "$SA" --project "$PROJECT" --member "serviceAccount:$SA" \
  --role roles/iam.serviceAccountTokenCreator >/dev/null                      # V4 signed URLs via IAM signBlob

# 2. agent prompts → Secret Manager
if [[ -n "${PROMPTS_DIR:-}" ]]; then
  TMP=$(mktemp); trap 'rm -f "$TMP"' EXIT
  "$PY" -m server.prompts "$PROMPTS_DIR" > "$TMP"
  gcloud secrets describe "$SECRET" --project "$PROJECT" >/dev/null 2>&1 \
    || gcloud secrets create "$SECRET" --project "$PROJECT" --replication-policy automatic --labels app=hello-watch
  gcloud secrets versions add "$SECRET" --project "$PROJECT" --data-file "$TMP"
fi
gcloud secrets add-iam-policy-binding "$SECRET" --project "$PROJECT" --member "serviceAccount:$SA" \
  --role roles/secretmanager.secretAccessor >/dev/null

# 3. image (committed code only)
PATHS=(pyproject.toml requirements-web.txt catalog schema ingest prompts server web deploy/web)
git diff --quiet HEAD -- "${PATHS[@]}" || echo "WARN: uncommitted changes are NOT in the image (git archive HEAD)" >&2
COMMIT=$(git rev-parse --short=12 HEAD)
SRC=$(mktemp -d); trap 'rm -rf "$SRC"; rm -f "${TMP:-}"' EXIT
git archive --format=tar.gz -o "$SRC/src.tgz" HEAD "${PATHS[@]}"
gcloud builds submit "$SRC/src.tgz" --project "$PROJECT" --region "$REGION" --config deploy/web/cloudbuild.yaml \
  --gcs-source-staging-dir "gs://$BUCKET/cloudbuild/source" --substitutions "_REGION=$REGION,_TAG=$COMMIT" \
  --service-account "projects/$PROJECT/serviceAccounts/cedar-watch-build@$PROJECT.iam.gserviceaccount.com"
IMAGE=$REGION-docker.pkg.dev/$PROJECT/cedar-watch/web:$COMMIT

# 4. service
gcloud run deploy "$SERVICE" --project "$PROJECT" --region "$REGION" --image "$IMAGE" --service-account "$SA" \
  --allow-unauthenticated --cpu 2 --memory 2Gi --concurrency 20 --timeout 300 --cpu-boost \
  --min-instances "$MIN_INSTANCES" --max-instances "$MAX_INSTANCES" \
  --set-env-vars "CEDAR_GCP_PROJECT=$PROJECT,CEDAR_GEMINI_BACKEND=enterprise,CEDAR_INGEST_BUCKET=$BUCKET,\
WATCH_PACKS_GCS=gs://$BUCKET/demo-packs,WATCH_AGENT_PROMPTS_SECRET=projects/$PROJECT/secrets/$SECRET/versions/latest"
gcloud run services describe "$SERVICE" --project "$PROJECT" --region "$REGION" --format 'value(status.url)'
