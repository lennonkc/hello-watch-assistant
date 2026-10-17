"""Cloud ingest: GCS layout, Cloud Run Job entry point, GCS registration and renewal of Gemini Files.

Bucket layout (bucket name from `CEDAR_INGEST_BUCKET` / `--bucket`):

    assets/<asset_sha256>.mp4          source asset (named by sha256, written by the upload tool)
    assets/index.json                  asset index (sha, catalog content_id, duration, original file name)
    titles/<catalog_id>.json           extra title configs (used only when absent from ingest/titles/ in the image)
    sources/<catalog_id>/subs/…        official subtitle source files
    work/<asset_sha256>/…              pipeline work dir (stage cache, ledger, patches, version table), mutable
    packs/<asset_sha256>/v<N>/{master,device}/…   exported packs, immutable
    packs/<asset_sha256>/LATEST        latest-version pointer (JSON)
    registry/<asset_sha256>.json       spec §4 sidecar (Gemini files registered from GCS)
"""
