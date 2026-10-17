"""Service configuration, all from environment variables (no project / bucket names in code).

| Variable | Meaning |
|---|---|
| `WATCH_PACKS_DIR` | Local directory with `<asset_sha>/master/{pack.db, thumbs/}` (local dev: the ingest cache) |
| `WATCH_PACKS_GCS` | `gs://<bucket>/<prefix>` with the same layout; downloaded to `WATCH_CACHE_DIR` at startup |
| `WATCH_VIDEO_DIR` | Local directory with `<asset_sha>.mp4` or `<asset_sha>/proxy_640.mp4`, streamed directly |
| `CEDAR_INGEST_BUCKET` | Bucket holding `assets/<sha>.mp4`: signed playback URLs and the near-window video for Gemini |
| `CEDAR_GCP_PROJECT` | Project for Gemini on Agent Platform (location is always `global`) |
| `WATCH_AGENT_PROMPTS_SECRET` / `WATCH_AGENT_PROMPTS_DIR` | Agent prompts (see `server/prompts.py`) |
"""
from __future__ import annotations

import os
import pathlib

REPO = pathlib.Path(__file__).resolve().parent.parent
WEB_DIR = REPO / "web"

MODEL = os.environ.get("WATCH_MODEL", "gemini-3.8-flash")
FEATURED_TITLE = os.environ.get("WATCH_FEATURED_TITLE", "Sintel")
LOCATION = "global"                                   # Agent Platform: regional endpoints 404 for these models


def project() -> str | None:
    return os.environ.get("CEDAR_GCP_PROJECT") or os.environ.get("GOOGLE_CLOUD_PROJECT")


def env_path(name: str) -> pathlib.Path | None:
    v = os.environ.get(name)
    return pathlib.Path(v).expanduser() if v else None


PACKS_DIR = env_path("WATCH_PACKS_DIR")
PACKS_GCS = os.environ.get("WATCH_PACKS_GCS", "").rstrip("/")
CACHE_DIR = env_path("WATCH_CACHE_DIR") or pathlib.Path("/tmp/watch-packs")
VIDEO_DIR = env_path("WATCH_VIDEO_DIR")
ASSET_BUCKET = os.environ.get("CEDAR_INGEST_BUCKET", "").removeprefix("gs://").rstrip("/")

# Abuse / cost protection for a public URL
ASK_PER_MINUTE = int(os.environ.get("WATCH_ASK_PER_MINUTE", "6"))
ASK_PER_DAY_PER_IP = int(os.environ.get("WATCH_ASK_PER_DAY_PER_IP", "80"))
ASK_PER_DAY_TOTAL = int(os.environ.get("WATCH_ASK_PER_DAY_TOTAL", "1500"))
QUESTION_MAX_CHARS = 300

NEAR_WINDOW_S = float(os.environ.get("WATCH_NEAR_WINDOW_S", "30"))
LOOK_VIDEO = os.environ.get("WATCH_LOOK_VIDEO", "0") == "1"      # near window as a gs:// video clip instead of stills
