#!/bin/bash
# Build and deploy the public web demo to Cloud Run (moves 100 % of traffic to the new revision).
#   PROJECT=<project> BUCKET=<bucket> [REGION=us-west1] [PACKS_PREFIX=…] [PRIVATE_PREFIX=…] [EXTRA_ENV=…] deploy/web/deploy.sh
# The environment is computed in full by deploy/web/env.sh (the same as tools/ops/run.sh deploy-staging, without a
# bench token): packs PACKS_PREFIX, else what the production revision serves now, else demo-packs (first deploy);
# private titles PRIVATE_PREFIX/titles.json, else what the production revision serves now (PRIVATE_PREFIX=none: none).
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
. ./deploy/web/env.sh
for v in "${PACKS_PREFIX:-}" "${PRIVATE_PREFIX:-}"; do
  case "$v" in *..*|gs://*|*[!A-Za-z0-9._/-]*) echo "ERROR: bad prefix '$v'" >&2; exit 1 ;; esac
done
if [ -n "${PACKS_PREFIX:-}" ]; then
  PACKS_GCS="gs://$BUCKET/${PACKS_PREFIX%/}"
else
  PACKS_GCS=$(live_env WATCH_PACKS_GCS)
  PACKS_GCS=${PACKS_GCS:-gs://$BUCKET/demo-packs}
fi
case "${PRIVATE_PREFIX:-}" in
  "") PRIVATE_TITLES_GCS=$(live_env WATCH_PRIVATE_TITLES)
      PRIVATE_HOW="inherited from production"; [ -n "$PRIVATE_TITLES_GCS" ] || PRIVATE_HOW="production has none" ;;
  none) PRIVATE_TITLES_GCS=""; PRIVATE_HOW="PRIVATE_PREFIX=none" ;;
  *) PRIVATE_TITLES_GCS="gs://$BUCKET/${PRIVATE_PREFIX%/}/titles.json"; PRIVATE_HOW="from PRIVATE_PREFIX" ;;
esac
ENV_LINES=$(PACKS_GCS=$PACKS_GCS PRIVATE_TITLES_GCS=$PRIVATE_TITLES_GCS BENCH_TOKEN_VALUE= service_env)
echo "env: $(env_names "$ENV_LINES") (packs $PACKS_GCS; private titles ${PRIVATE_TITLES_GCS:-none}: $PRIVATE_HOW)" >&2

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
ENV_VARS=$(printf '%s\n' "$ENV_LINES" | paste -sd, -)
gcloud run deploy "$SERVICE" --project "$PROJECT" --region "$REGION" --image "$IMAGE" --service-account "$SA" \
  --allow-unauthenticated --cpu 2 --memory 2Gi --concurrency 20 --timeout 300 --cpu-boost --session-affinity \
  --min-instances "$MIN_INSTANCES" --max-instances "$MAX_INSTANCES" --set-env-vars "$ENV_VARS"
# a staging revision (tools/ops/run.sh deploy-staging / promote) may have pinned traffic: send it to this one
gcloud run services update-traffic "$SERVICE" --project "$PROJECT" --region "$REGION" --to-latest >/dev/null
gcloud run services describe "$SERVICE" --project "$PROJECT" --region "$REGION" --format 'value(status.url)'
