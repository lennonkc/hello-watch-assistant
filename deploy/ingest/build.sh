#!/usr/bin/env bash
# Build the ingest image: git archive of HEAD (committed code only) → Cloud Build (regional default pool, default machine type) → Artifact Registry.
# If the FFmpeg base image (Dockerfile.ffmpeg) tag is not in the registry yet, build it first (about 40 min; reused afterwards).
#   PROJECT=<project> BUCKET=<bucket> [REGION=us-west1] deploy/ingest/build.sh
set -euo pipefail
: "${PROJECT:?set PROJECT}" "${BUCKET:?set BUCKET}"
REGION=${REGION:-us-west1}
FFMPEG_TAG=ffmpeg9.0.2-x264r3222
cd "$(git -C "$(dirname "$0")" rev-parse --show-toplevel)"
PATHS=(pyproject.toml catalog schema ingest deploy/ingest)
if ! git diff --quiet HEAD -- "${PATHS[@]}"; then
  echo "WARN: uncommitted changes under ${PATHS[*]} are NOT in the image (git archive HEAD)" >&2
fi
COMMIT=$(git rev-parse --short=12 HEAD)
TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT
git archive --format=tar.gz -o "$TMP/src.tgz" HEAD "${PATHS[@]}"
SUBMIT=(gcloud builds submit "$TMP/src.tgz" --project "$PROJECT" --region "$REGION"
  --gcs-source-staging-dir "gs://$BUCKET/cloudbuild/source"
  --service-account "projects/$PROJECT/serviceAccounts/cedar-watch-build@$PROJECT.iam.gserviceaccount.com")
REPO=$REGION-docker.pkg.dev/$PROJECT/cedar-watch
if ! gcloud artifacts docker images describe "$REPO/ffmpeg:$FFMPEG_TAG" --project "$PROJECT" >/dev/null 2>&1; then
  "${SUBMIT[@]}" --config deploy/ingest/cloudbuild-ffmpeg.yaml --substitutions "_REGION=$REGION,_FFMPEG_TAG=$FFMPEG_TAG"
fi
"${SUBMIT[@]}" --config deploy/ingest/cloudbuild.yaml \
  --substitutions "_REGION=$REGION,_TAG=$COMMIT,_GIT_COMMIT=$COMMIT,_FFMPEG_TAG=$FFMPEG_TAG"
echo "image: $REPO/ingest:$COMMIT"
