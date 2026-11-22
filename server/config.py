"""Service configuration, all from environment variables (no project / bucket names in code).

| Variable | Meaning |
|---|---|
| `WATCH_CATALOG_ROOT` | Directory holding `catalog/catalog.json` and `ingest/titles/` (default: this repo; point it at a larger catalog for local benchmarks) |
| `WATCH_PACKS_DIR` | Local directory with `<asset_sha>/master/{pack.db, thumbs/}` (local dev: the ingest cache) |
| `WATCH_PACKS_GCS` | `gs://<bucket>/<prefix>` with the same layout; downloaded to `WATCH_CACHE_DIR` at startup |
| `WATCH_VIDEO_DIR` | Local directory with `<asset_sha>.mp4` or `<asset_sha>/proxy_640.mp4` (or the file name of a title's `video_object`), streamed directly |
| `WATCH_PRIVATE_TITLES` | `gs://` object or local path of the private preview titles file (`server/packs.py`); unset = none |
| `WATCH_THUMB_CACHE_MAX` | Thumbnails kept on local disk per private preview title (fetched on demand; default 4000) |
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


CATALOG_ROOT = env_path("WATCH_CATALOG_ROOT") or REPO       # dir with catalog/catalog.json + ingest/titles/<id>.json
PACKS_DIR = env_path("WATCH_PACKS_DIR")
PACKS_GCS = os.environ.get("WATCH_PACKS_GCS", "").rstrip("/")
CACHE_DIR = env_path("WATCH_CACHE_DIR") or pathlib.Path("/tmp/watch-packs")
VIDEO_DIR = env_path("WATCH_VIDEO_DIR")
ASSET_BUCKET = os.environ.get("CEDAR_INGEST_BUCKET", "").removeprefix("gs://").rstrip("/")
PRIVATE_TITLES = os.environ.get("WATCH_PRIVATE_TITLES", "").strip()
THUMB_CACHE_MAX = int(os.environ.get("WATCH_THUMB_CACHE_MAX", "4000"))
ACCESS_COOKIE = "watch_access"
ACCESS_COOKIE_DAYS = 30
UNLOCK_PER_MINUTE = 10                                # access-code attempts per IP

# Abuse / cost protection for a public URL
ASK_PER_MINUTE = int(os.environ.get("WATCH_ASK_PER_MINUTE", "6"))
ASK_PER_DAY_PER_IP = int(os.environ.get("WATCH_ASK_PER_DAY_PER_IP", "80"))
ASK_PER_DAY_TOTAL = int(os.environ.get("WATCH_ASK_PER_DAY_TOTAL", "1500"))
QUESTION_MAX_CHARS = 300

NEAR_WINDOW_S = float(os.environ.get("WATCH_NEAR_WINDOW_S", "30"))
LOOK_VIDEO = os.environ.get("WATCH_LOOK_VIDEO", "0") == "1"      # near window as a gs:// video clip instead of stills

# Online answer path: deadlines (seconds). Every model / embedding call is bounded; see docs/online-qa.md §4a.
EMBED_TIMEOUT_S = float(os.environ.get("WATCH_EMBED_TIMEOUT_S", "1.5"))     # query embedding, one attempt
EMBED_COOLDOWN_S = float(os.environ.get("WATCH_EMBED_COOLDOWN_S", "60"))    # after a failure: full-text ranking only

# Benchmark runs against a deployed revision: requests with this value in `X-Watch-Bench-Token` skip the limiter
BENCH_TOKEN = os.environ.get("WATCH_BENCH_TOKEN", "")

# Answer flow: `context` (server-built context + two tools, streamed) or `tools` (five tools, one answer event).
# Bench-token requests may pick one per request with `X-Watch-Flow`.
FLOW = os.environ.get("WATCH_FLOW", "context")
THINKING_LEVEL = os.environ.get("WATCH_THINKING_LEVEL", "LOW")
TOOL_ROUNDS = int(os.environ.get("WATCH_TOOL_ROUNDS", "1"))                  # context flow; the tools flow allows 3
SOFT_DEADLINE_S = float(os.environ.get("WATCH_SOFT_DEADLINE_S", "6"))        # after it: no more tool calls
STEP_TIMEOUT_S = float(os.environ.get("WATCH_STEP_TIMEOUT_S", "8"))          # longest silence from the agent run
ASK_DEADLINE_S = float(os.environ.get("WATCH_ASK_DEADLINE_S", "10"))         # whole question, retry included
LOOK_TIMEOUT_S = float(os.environ.get("WATCH_LOOK_TIMEOUT_S", "6"))          # nested look_closer model call
LOOK_STILLS = int(os.environ.get("WATCH_LOOK_STILLS", "8"))                  # look_closer: stills from the window, at most
HISTORY_TURNS = int(os.environ.get("WATCH_HISTORY_TURNS", "6"))              # earlier turns sent with a question
SOURCES_TIMEOUT_S = float(os.environ.get("WATCH_SOURCES_TIMEOUT_S", "0.8"))  # evidence + frames after the answer
# X-Forwarded-For entries appended by proxies we trust (Google's front end = 1; add one per load balancer in front)
TRUSTED_PROXY_HOPS = max(1, int(os.environ.get("WATCH_TRUSTED_PROXY_HOPS", "1")))
