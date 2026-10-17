# Ingest Pipeline on Google Cloud (Cloud Run Jobs + GCS)

This document explains how to run the [ingest pipeline](../ingest/README.md) on Google Cloud. Assets live in GCS; a
Cloud Run Job runs **exactly the same** `python -m ingest run` as locally; the exported master / device packs are
published back to GCS **append-only**, by version. With the Developer API backend, whole films are additionally registered
with Gemini Files; the registration goes into the sidecar of [knowledge-pack.md §4](knowledge-pack.md#4-sidecar-cloud-registration-not-inside-the-pack)
and is renewed on a schedule.

`$PROJECT`, `$BUCKET` etc. are placeholders; the default region is `us-west1`. All cloud resource names start with `cedar-watch-`.

## 1. Architecture

```
library mp4 ──upload_assets.py──▶ gs://$BUCKET/assets/<sha256>.mp4 + assets/index.json
                                          │
             gcloud run jobs execute cedar-watch-ingest --tasks N (parallelism defaults to 2)
                                          ▼
  ┌──────────── Cloud Run Job task i / N (image = deploy/ingest/Dockerfile) ─────────────────┐
  │ 1. Pick this task's share of assets by CLOUD_RUN_TASK_INDEX (sorted; items i, i+N, …)     │
  │ 2. Pull the original (sha256 verified), work/<sha>/ (stage cache / ledger / patches /     │
  │    versions.json), subtitles                                                              │
  │ 3. python -m ingest run (same pipeline as local; stages with unchanged fingerprints skip) │
  │ 4. Write back work/<sha>/ (only objects whose content changed)                            │
  │ 5. Publish packs/<sha>/v<N>/{master,device}/ (existing objects are only crc32c-checked,   │
  │    never overwritten) + LATEST                                                            │
  └───────────────────────────────────────────────────────────────────────────────────────────┘
                                          │
  cedar-watch-renew (Cloud Scheduler every 6 h) ── registry/<sha>.json (spec §4) ◀── cloud register
```

### Bucket layout

| Path | Contents | Mutability |
|---|---|---|
| `assets/<sha256>.mp4` | Original asset, named by content sha256 | Immutable |
| `assets/index.json` | `cedar-asset-index/1`: `asset_sha256`, `catalog_id` (library content_id), `title`, `duration_s`, `file` (original filename), `bytes`, `crc32c` | Merged updates |
| `titles/<catalog_id>.json` | Additional title configuration; **a file of the same name in the image's `ingest/titles/` takes precedence** | Mutable |
| `sources/<catalog_id>/subs/…` | Official subtitle source files (same as local `.cache/sources/`) | Append-only |
| `work/<sha256>/…` | Pipeline working directory (= local `.cache/ingest/<sha>/`): segment files, thumbnails, stage cache, `ledger.jsonl`, `patches/`, `versions.json` | Mutable (bucket versioning as backstop) |
| `packs/<sha256>/v<N>/master/`, `…/device/` | Exported packs (`MANIFEST.json`, `pack.db`, `pack.json`, `thumbs/`) | **Immutable** |
| `packs/<sha256>/LATEST` | `{"pack_version", "built_at", "review_complete", "path", "manifest_sha256"}`; only moves forward | Mutable |
| `registry/<sha256>.json` | Spec §4 sidecar (`asset_file` + optional `segments`) | Updated on renewal |
| `cloudbuild/source/` | Cloud Build source staging | — |

Object versioning is enabled on the bucket; lifecycle rules delete noncurrent versions older than 30 days and keep at most
5 old versions per object.

### Idempotence and versions

- The pack content hash (`versions.json`) depends only on row content; re-exporting identical content yields the same
  `pack_version`, the same `built_at` and the same bytes. `work/<sha>/` keeps `versions.json` across runs, so cloud re-runs
  do not mint new versions. Publishing uses `if_generation_match=0` (create-only); an existing object with a different
  crc32c raises `ImmutableConflict` instead of silently overwriting a published version.
- **Byte-level reproducibility (measured on Sintel)**:
  - Export (`pack.db` / `pack.json` / MANIFEST) depends only on stage outputs in the working directory and on the bundled
    SQLite version. With the image pinned to CPython 3.13.12 (uv / python-build-standalone, SQLite 3.50.4) and exact Python
    package versions (`deploy/ingest/requirements-cloud.txt` + `overrides-cloud.txt`), **the same working directory exports
    byte-identical packs on a local Mac and in the cloud**.
  - Media-derived stages (`shots` / `segments` / `thumbs`) are **not byte-identical across CPU architectures**: even with
    FFmpeg 9.0.2 + x264 r3222 and identical OpenCV / Pillow versions, swscale pixel conversion and x264 CRF rate control
    round differently (floating point / SIMD) on Apple Silicon (arm64) vs Cloud Run (x86_64) — on Sintel, cut points
    differ at the one-frame level (216 vs 215), and all 10 segments and 889 thumbnails have different sha256. Re-running
    inside the same image is fully deterministic (two forced re-runs produced the same `pack_version`, with all 1784
    objects unchanged).
  - Conclusion: **run an asset's media stages in one place only — the cloud is authoritative.** To change review / patches
    locally, pull the working directory with `cloud pull --work`, then run `apply-patch` / `export` on it; after pushing it
    back, the cloud export is byte-identical to the local one. Do not re-run `shots` / `segments` / `thumbs` locally.
- Paid stages keep the `--budget` gate (default $5 per asset; the ledger accumulates across runs inside `work/`).

## 2. One-time setup

Prerequisites: billing enabled on the project; `gcloud auth login` + `gcloud auth application-default login`.

```bash
PROJECT=<your-project>; REGION=us-west1; BUCKET=<your-bucket>     # suggested bucket name: cedar-watch-ingest-<suffix>
ING=cedar-watch-ingest@$PROJECT.iam.gserviceaccount.com
BLD=cedar-watch-build@$PROJECT.iam.gserviceaccount.com

gcloud services enable run.googleapis.com cloudbuild.googleapis.com secretmanager.googleapis.com \
  artifactregistry.googleapis.com storage.googleapis.com --project $PROJECT

# Bucket: single region, uniform access, public access prevented, versioning + old-version cleanup
gcloud storage buckets create gs://$BUCKET --project $PROJECT --location $REGION \
  --uniform-bucket-level-access --public-access-prevention
gcloud storage buckets update gs://$BUCKET --versioning
cat > /tmp/lifecycle.json <<'EOF'
{"rule":[{"action":{"type":"Delete"},"condition":{"isLive":false,"daysSinceNoncurrentTime":30}},
         {"action":{"type":"Delete"},"condition":{"isLive":false,"numNewerVersions":5}}]}
EOF
gcloud storage buckets update gs://$BUCKET --lifecycle-file=/tmp/lifecycle.json

# Image repository, service accounts
gcloud artifacts repositories create cedar-watch --repository-format=docker --location $REGION --project $PROJECT
gcloud iam service-accounts create cedar-watch-ingest --project $PROJECT   # runtime identity of the ingest / renew jobs
gcloud iam service-accounts create cedar-watch-build  --project $PROJECT   # image build identity

# Gemini API key → Secret Manager (read from a file, never on the command line)
gcloud secrets create cedar-watch-gemini-api-key --project $PROJECT \
  --replication-policy=user-managed --locations=$REGION --data-file=$HOME/.config/cedar/gemini_api_key

# Least privilege
gcloud storage buckets add-iam-policy-binding gs://$BUCKET --member=serviceAccount:$ING --role=roles/storage.objectAdmin
gcloud secrets add-iam-policy-binding cedar-watch-gemini-api-key --project $PROJECT \
  --member=serviceAccount:$ING --role=roles/secretmanager.secretAccessor
gcloud artifacts repositories add-iam-policy-binding cedar-watch --location $REGION --project $PROJECT \
  --member=serviceAccount:$BLD --role=roles/artifactregistry.writer
gcloud storage buckets add-iam-policy-binding gs://$BUCKET --member=serviceAccount:$BLD --role=roles/storage.objectViewer
gcloud projects add-iam-policy-binding $PROJECT --member=serviceAccount:$BLD --role=roles/logging.logWriter --condition=None

# Prompts → Secret Manager (bundles prompts/ into one JSON, adds a new secret version and grants $ING read access;
# after editing prompts just re-run this — no image rebuild needed)
PROJECT=$PROJECT deploy/prompts/push_prompts.sh

# Image + jobs (build.sh packages the committed HEAD; deploy_jobs.sh defaults to the image tag of HEAD)
PROJECT=$PROJECT BUCKET=$BUCKET deploy/ingest/build.sh
PROJECT=$PROJECT BUCKET=$BUCKET deploy/ingest/deploy_jobs.sh
```

Job specs are in `deploy/ingest/job.yaml` (ingest: 2 vCPU / 8 GiB, 60 min task timeout, 1 retry, parallelism 2 by default)
and `deploy/ingest/renew-job.yaml` (renewal: 1 vCPU / 4 GiB, 30 min timeout). Both are egress-only and expose no HTTP.
The API key is injected as the secret env var `CEDAR_GEMINI_API_KEY`; on startup the process writes it to a 0600 temp
file for `ingest.gemini` and removes it from the environment.

**Prompts are not baked into the image** (the Dockerfile does not copy `prompts/`, and the public repository ships no
prompt text). In `job.yaml`, `CEDAR_PROMPTS_SECRET` points to the `latest` version of the `cedar-watch-prompts` secret;
`ingest/promptbook.py` reads it with ADC at startup and validates every template's placeholders, failing fast on a missing
prompt or mismatched placeholders. Locally it falls back to `CEDAR_PROMPTS_DIR`, then to the repository's `prompts/`
directory (one `<name>.txt` per prompt).

Cloud Run's writable filesystem is in memory, so `.cache` counts against the memory limit: the largest asset (≈ 500 MB)
plus segments and thumbnails needs ≈ 1.5 GB; 8 GiB is plenty.

### Gemini file registration

`--mode` of `cloud register` / `cloud renew`:

| Mode | How | Lifetime (always taken from the API's returned `expirationTime`) |
|---|---|---|
| `gcs` | REST `files:register` registers the bucket object directly (no copy, ≤ 2 GB per file) | Documented as up to 30 days per registration |
| `upload` | Download from the bucket, then upload via the Files API | 48 h (measured: upload time + 48 h) |
| `auto` (default) | Try `gcs`, fall back to `upload` | — |

Limitations of `gcs` mode (measured):

- OAuth only; it cannot be combined with an API key (otherwise 401 `OVERLOADED_CREDENTIALS`). The google-genai 2.30.1
  Developer API client always sends the key, so this path calls REST directly. Files belong to the project of the OAuth
  credentials, and the API key must belong to the same project.
- Requires the `devstorage.read_only` scope; `gcloud auth application-default login` does not grant it by default, so
  local user credentials get 403 `ACCESS_TOKEN_SCOPE_INSUFFICIENT`.
- If the project binds API keys to service accounts and restricts service accounts from calling the Gemini API directly,
  the job's service account gets 403 `Access to Gemini API is restricted with service accounts`; `auto` then falls back to `upload`.
- The Gemini API service agent also needs read access to the bucket:
  `gcloud beta services identity create --service=generativelanguage.googleapis.com --project $PROJECT`,
  then grant the printed service agent `roles/storage.objectViewer` on the bucket.

With `upload`, the renewal job checks every 6 h and re-uploads when < 12 h remain, i.e. each film is re-uploaded about
every 36 h. Old files are not deleted proactively but left to expire: a client may hold an older sidecar copy, and
deleting early would break it before its expiry. **Clients that cache the sidecar must refresh it at least every 12 h**,
otherwise they will use an expired `gemini_file`.

### Gemini backend: Developer API vs Agent Platform (`CEDAR_GEMINI_BACKEND`)

Ingest's Gemini calls support two backends, selected by `CEDAR_GEMINI_BACKEND` (`deploy_jobs.sh` fills it into the job
from `GEMINI_BACKEND`; default `developer`):

| | `developer` (default) | `enterprise` |
|---|---|---|
| Service | Gemini Developer API | Gemini Enterprise Agent Platform (formerly Vertex AI) |
| Auth | API key (Secret Manager → 0600 temp file) | ADC: locally `gcloud auth application-default login`; in the cloud, the job's service account |
| Client | `genai.Client(api_key=…)` | `genai.Client(enterprise=True, project=$CEDAR_GCP_PROJECT, location="global")` |
| Whole-film video | Files API upload / registration, 48 h expiry, kept alive by the renewal job | Uses `gs://$BUCKET/assets/<sha>.mp4` directly — no expiry, no renewal; `ensure_asset_file` does not touch the Files API |
| Video clipping + structured output (3.8-flash) | Supported | Supported (`start_offset` / `end_offset`, `response_json_schema`; a 15 s clip costs 900 video + 375 audio tokens) |
| Transcription (3.5-transcribe) | Interactions API (current) | **Interactions API not supported**; would need `generate_content` + `audio_transcription_config` with a different output format; **pinned to developer for now** |
| Embeddings (embedding-2) | Text can be batched; video ≤ 32 frames, no audio track (360p clips sent inline) | Text one item per request (called concurrently); video via gs:// + offsets, `audio_track_extraction=True` includes audio, ≤ 120 frames |

Smoke-test findings (google-genai 2.30.1):

- **Location must be `global`**: 3.8-flash / 3.5-transcribe / embedding-2 all return 404 in `us-west1`
  (`Publisher model … was not found`); embedding-2 also supports `us` / `eu`. The older `vertexai=True` is equivalent to
  `enterprise=True`; the env var `GOOGLE_GENAI_USE_ENTERPRISE` is also honoured. `CEDAR_GCP_LOCATION` overrides the location.
- **gs:// access**: `generate_content` reads same-project buckets directly. The first `embed_content` call may return
  400 `Service agents are being provisioned`, then 403 (`service-<PROJECT_NUMBER>@gcp-sa-aiplatform.iam.gserviceaccount.com`);
  it recovers automatically after ≈ 5 minutes. If 403 persists, grant that service agent `roles/storage.objectViewer` on the bucket.
- Transcription on `enterprise` works via `generate_content(model="gemini-3.5-transcribe", …)` with an
  `AudioTranscriptionConfig` (word timestamps, diarization, verbatim mode, language codes) and a `gs://` `FileData` part;
  results are in `part.audio_transcription` (`words[{word, start_offset, end_offset}]`). Whole-film gs:// transcription
  works (Sintel ≈ 22k audio tokens ≈ $0.05), but `mime_type` must be `audio/mp4` (`video/mp4` returns empty). Migrating
  requires changing the parser in `subtitles.words_from_transcript`; not done yet.

Before switching to `enterprise`:

```bash
gcloud services enable aiplatform.googleapis.com --project $PROJECT
gcloud projects add-iam-policy-binding $PROJECT --member=serviceAccount:$ING --role=roles/aiplatform.user --condition=None
# If video embeddings keep returning 403: let the Agent Platform service agent read the bucket
gcloud storage buckets add-iam-policy-binding gs://$BUCKET \
  --member=serviceAccount:service-<PROJECT_NUMBER>@gcp-sa-aiplatform.iam.gserviceaccount.com --role=roles/storage.objectViewer
PROJECT=$PROJECT BUCKET=$BUCKET GEMINI_BACKEND=enterprise deploy/ingest/deploy_jobs.sh
```

Transcription still uses developer, so keep the `cedar-watch-gemini-api-key` secret. Assets must first be uploaded to the
bucket with `upload_assets.py` (for assets not in the bucket, video embeddings fall back to developer's visual-only mode,
and the vision stages fail with a hint to upload or switch to developer). Under `enterprise`, the vision stages no longer
depend on the Files API; the renewal job only serves the developer backend and client-side sidecars.

### Renewal schedule (Cloud Scheduler; also enable `cloudscheduler.googleapis.com`)

```bash
gcloud iam service-accounts create cedar-watch-scheduler --project $PROJECT
gcloud run jobs add-iam-policy-binding cedar-watch-renew --region $REGION --project $PROJECT \
  --member=serviceAccount:cedar-watch-scheduler@$PROJECT.iam.gserviceaccount.com --role=roles/run.invoker
gcloud scheduler jobs create http cedar-watch-renew-6h --project $PROJECT --location $REGION \
  --schedule="0 */6 * * *" --time-zone=Etc/UTC --http-method=POST \
  --uri="https://run.googleapis.com/v2/projects/$PROJECT/locations/$REGION/jobs/cedar-watch-renew:run" \
  --oauth-service-account-email=cedar-watch-scheduler@$PROJECT.iam.gserviceaccount.com
```

## 3. Everyday commands

```bash
export CEDAR_INGEST_BUCKET=$BUCKET
PY=.venv/bin/python        # local env: see §1 "Byte-level reproducibility" (uv venv + requirements-cloud.txt + overrides-cloud.txt)

# Upload assets (idempotent: skipped if present with matching crc32c) and merge into assets/index.json
$PY deploy/ingest/upload_assets.py --src <library-dir> [--only <title> --only <title> …]
# Upload extra title configs / official subtitles (only needed for title configs not in the image)
$PY deploy/ingest/upload_assets.py --titles <dir> --sources .cache/sources

# Run ingest: each task processes one shard; task count = number of assets processed in parallel
gcloud run jobs execute cedar-watch-ingest --region us-west1 --tasks 2 --wait \
  --args=cloud,run,--sha,<sha1>,<sha2>
gcloud run jobs execute cedar-watch-ingest --region us-west1 --tasks 4 --args=cloud,run,--all
#   Optional: --budget,<USD>, --force,<stage>,<stage>… (same as local run; gcloud does not allow the same flag twice
#   inside --args, so multi-value options are one flag followed by several values); logs print one `RESULT {…}` line per asset

# Pull a published pack (default: LATEST device pack; every file verified against MANIFEST)
$PY -m ingest cloud pull --sha <sha> [--version 3] [--dist master] --dest packs/
# Review: pull the working dir → apply-patch → push back → re-run in the cloud (export only) to publish a new version
$PY -m ingest cloud pull --sha <sha> --work
$PY -m ingest apply-patch <patch.json> --sha <sha>
$PY -m ingest cloud push-work --sha <sha>
gcloud run jobs execute cedar-watch-ingest --region us-west1 --args=cloud,run,--sha,<sha>

# Gemini file registration → registry/<sha>.json (spec §4; expires_at taken from the API response)
$PY -m ingest cloud register --sha <sha> [--segments] [--mode auto|gcs|upload]
# Renewal: re-register entries whose expires_at is missing or < 12 h away (this is what the scheduled job runs)
gcloud run jobs execute cedar-watch-renew --region us-west1 --wait
```

`cloud register` rebuilds the asset's sidecar each time (`segments` is empty without `--segments`); `cloud renew` only
updates `gemini_file` / `expires_at` of expiring entries. Sidecar writes use a generation precondition, so concurrent
writers fail instead of overwriting each other.

## 4. Cost estimate (us-west1, list prices as of 2026-10)

| Item | Usage | Estimate |
|---|---|---|
| Gemini (transcription + vision) | ≈ $0.027 per minute of runtime (Sintel, 14.8 min, measured $0.40) | A ~110-minute library ≈ **$3 per full run** |
| Cloud Run Jobs | 2 vCPU × 8 GiB, ≈ 1 min wall clock per minute of runtime (incl. segment encoding) | ≈ $0.05 per asset; < $1 per full run |
| Cloud Build | Default machine, ≈ 20 min per build (FFmpeg compiled from source) | Within the default pool's 2,500 free minutes/month ≈ $0 |
| GCS Standard | Originals + working dirs (segments ≈ originals) + packs (thumbnails) ≈ 6 GB for a ~110-minute library | ≈ $0.12 / month |
| Artifact Registry | Image ≈ 1 GB | ≈ $0.05 / month |
| Secret Manager | 2 secrets (API key, prompts) | ≈ $0.12 / month |
| Embeddings (gemini-embedding-2) | Text $0.20 / M tokens; video 1 fps × 66 tokens/frame × $12 / M + audio 25 tokens/s × $6.50 / M | ≈ $0.029 per 30 s window |
| Renewal job | Every 6 h; `upload` mode re-uploads each film every 36 h (same region; traffic to Google APIs is free) | ≈ $0.01 / day |

Same-region GCS ↔ Cloud Run traffic and calls to Google APIs incur no egress; pulling packs to a local machine is billed as
internet egress (packs are a few MB, negligible).
