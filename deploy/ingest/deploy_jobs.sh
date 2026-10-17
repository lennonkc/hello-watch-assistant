#!/usr/bin/env bash
# Create / update the two Cloud Run Jobs (ingest + renew). The image defaults to the tag for HEAD (run build.sh first).
#   PROJECT=<project> BUCKET=<bucket> [REGION=us-west1] [IMAGE=…] [PARALLELISM=2] [GEMINI_BACKEND=developer|enterprise] \
#     deploy/ingest/deploy_jobs.sh
# Before using GEMINI_BACKEND=enterprise, grant roles/aiplatform.user to the cedar-watch-ingest service account (see docs/cloud-deploy.md §2).
set -euo pipefail
: "${PROJECT:?set PROJECT}" "${BUCKET:?set BUCKET}"
REGION=${REGION:-us-west1}
PARALLELISM=${PARALLELISM:-2}
GEMINI_BACKEND=${GEMINI_BACKEND:-developer}
DIR=$(cd "$(dirname "$0")" && pwd)
IMAGE=${IMAGE:-$REGION-docker.pkg.dev/$PROJECT/cedar-watch/ingest:$(git -C "$DIR" rev-parse --short=12 HEAD)}
TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT
for f in job.yaml renew-job.yaml; do
  sed -e "s|\${PROJECT}|$PROJECT|g" -e "s|\${BUCKET}|$BUCKET|g" -e "s|\${IMAGE}|$IMAGE|g" \
      -e "s|\${PARALLELISM}|$PARALLELISM|g" -e "s|\${GEMINI_BACKEND}|$GEMINI_BACKEND|g" "$DIR/$f" > "$TMP/$f"
  gcloud run jobs replace "$TMP/$f" --project "$PROJECT" --region "$REGION"
done
