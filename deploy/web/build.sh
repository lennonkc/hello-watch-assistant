#!/bin/bash
# Build the web service image from the committed code (git archive HEAD) with Cloud Build.
#   PROJECT=<project> BUCKET=<bucket> [REGION=us-west1] deploy/web/build.sh
# Build logs go to stderr; the only stdout line is the image name, so callers can do IMAGE=$(deploy/web/build.sh).
# An image that already exists for this commit is reused (same commit = same image).
set -euo pipefail
: "${PROJECT:?set PROJECT}" "${BUCKET:?set BUCKET}"
REGION=${REGION:-us-west1}
REPO_NAME=${REPO_NAME:-cedar-watch}
BUILD_SA=${BUILD_SA:-cedar-watch-build@$PROJECT.iam.gserviceaccount.com}
ROOT=$(git -C "$(dirname "$0")" rev-parse --show-toplevel)
cd "$ROOT"

PATHS="pyproject.toml requirements-web.txt catalog schema ingest prompts server web deploy/web"
# shellcheck disable=SC2086
if ! git diff --quiet HEAD -- $PATHS; then
  echo "WARN: uncommitted changes are NOT in the image (git archive HEAD)" >&2
fi
COMMIT=$(git rev-parse --short=12 HEAD)
IMAGE=$REGION-docker.pkg.dev/$PROJECT/$REPO_NAME/web:$COMMIT

if gcloud artifacts docker images describe "$IMAGE" --project "$PROJECT" >/dev/null 2>&1; then
  echo "image for $COMMIT already built; reusing it" >&2
else
  SRC=$(mktemp -d)
  trap 'rm -rf "$SRC"' EXIT
  # shellcheck disable=SC2086
  git archive --format=tar.gz -o "$SRC/src.tgz" HEAD $PATHS
  gcloud builds submit "$SRC/src.tgz" --project "$PROJECT" --region "$REGION" --config deploy/web/cloudbuild.yaml \
    --gcs-source-staging-dir "gs://$BUCKET/cloudbuild/source" --substitutions "_REGION=$REGION,_REPO=$REPO_NAME,_TAG=$COMMIT" \
    --service-account "projects/$PROJECT/serviceAccounts/$BUILD_SA" >&2
fi
echo "$IMAGE"
