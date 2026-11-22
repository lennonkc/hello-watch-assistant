#!/bin/bash
# Build and deploy the public web demo to Cloud Run (moves 100 % of traffic to the new revision).
#   PROJECT=<project> BUCKET=<bucket> [REGION=us-west1] deploy/web/deploy.sh
# For a revision without traffic (staging, benchmarks) use `tools/ops/run.sh deploy-staging` instead.
#
# What it sets up (idempotent):
#   - service account hello-watch-web: Gemini on Agent Platform, read-only on the bucket (packs + films),
#     signs its own playback URLs;
#   - image <region>-docker.pkg.dev/<project>/cedar-watch/web:<commit> via Cloud Build (committed code only,
#     deploy/web/build.sh);
#   - Cloud Run service hello-watch: public, session affinity (follow-up questions keep their per-instance
#     conversation), at most MAX_INSTANCES instances, per-IP and daily question limits.
# The agent prompts ship in the image (prompts/agent/); no prompt secret is read or written.
set -euo pipefail
: "${PROJECT:?set PROJECT}" "${BUCKET:?set BUCKET}"
REGION=${REGION:-us-west1}
SERVICE=${SERVICE:-hello-watch}
SA_NAME=${SA_NAME:-hello-watch-web}
MAX_INSTANCES=${MAX_INSTANCES:-2}
MIN_INSTANCES=${MIN_INSTANCES:-1}
WATCH_ASK_PER_MINUTE=${WATCH_ASK_PER_MINUTE:-6}
WATCH_ASK_PER_DAY_PER_IP=${WATCH_ASK_PER_DAY_PER_IP:-80}
WATCH_ASK_PER_DAY_TOTAL=${WATCH_ASK_PER_DAY_TOTAL:-1500}
EXTRA_ENV=${EXTRA_ENV:-}                     # more env vars, comma-separated KEY=VALUE (e.g. WATCH_FLOW=…)
SA=$SA_NAME@$PROJECT.iam.gserviceaccount.com
ROOT=$(git -C "$(dirname "$0")" rev-parse --show-toplevel)
cd "$ROOT"

# 1. service account and its permissions
gcloud iam service-accounts describe "$SA" --project "$PROJECT" >/dev/null 2>&1 \
  || gcloud iam service-accounts create "$SA_NAME" --project "$PROJECT" --display-name "hello-watch web demo"
gcloud projects add-iam-policy-binding "$PROJECT" --member "serviceAccount:$SA" --role roles/aiplatform.user \
  --condition None >/dev/null
gcloud storage buckets add-iam-policy-binding "gs://$BUCKET" --member "serviceAccount:$SA" \
  --role roles/storage.objectViewer >/dev/null
gcloud iam service-accounts add-iam-policy-binding "$SA" --project "$PROJECT" --member "serviceAccount:$SA" \
  --role roles/iam.serviceAccountTokenCreator >/dev/null                      # V4 signed URLs via IAM signBlob

# 2. image (committed code only)
IMAGE=$(PROJECT=$PROJECT BUCKET=$BUCKET REGION=$REGION /bin/bash deploy/web/build.sh)

# 3. service
ENV_VARS="CEDAR_GCP_PROJECT=$PROJECT,CEDAR_GEMINI_BACKEND=enterprise,CEDAR_INGEST_BUCKET=$BUCKET"
ENV_VARS="$ENV_VARS,WATCH_PACKS_GCS=gs://$BUCKET/demo-packs,WATCH_ASK_PER_MINUTE=$WATCH_ASK_PER_MINUTE"
ENV_VARS="$ENV_VARS,WATCH_ASK_PER_DAY_PER_IP=$WATCH_ASK_PER_DAY_PER_IP,WATCH_ASK_PER_DAY_TOTAL=$WATCH_ASK_PER_DAY_TOTAL"
if [ -n "$EXTRA_ENV" ]; then
  ENV_VARS="$ENV_VARS,$EXTRA_ENV"
fi
gcloud run deploy "$SERVICE" --project "$PROJECT" --region "$REGION" --image "$IMAGE" --service-account "$SA" \
  --allow-unauthenticated --cpu 2 --memory 2Gi --concurrency 20 --timeout 300 --cpu-boost --session-affinity \
  --min-instances "$MIN_INSTANCES" --max-instances "$MAX_INSTANCES" --set-env-vars "$ENV_VARS"
# a staging revision (tools/ops/run.sh deploy-staging / promote) may have pinned traffic: send it to this one
gcloud run services update-traffic "$SERVICE" --project "$PROJECT" --region "$REGION" --to-latest >/dev/null
gcloud run services describe "$SERVICE" --project "$PROJECT" --region "$REGION" --format 'value(status.url)'
