"""Gemini client: two backends, upload reuse / verification, calls and usage logging, Gemini Embedding 2. The key is only read from a file, never written or printed.

Backend is chosen by `CEDAR_GEMINI_BACKEND` (default developer, same behaviour as before):
- developer: Gemini Developer API (API key); video goes through the Files API (48 h expiry; `ensure_file` reuses / re-uploads).
- enterprise: Gemini Enterprise Agent Platform (formerly Vertex AI), ADC / service-account auth, location fixed to `global`
  (3.8-flash / 3.5-transcribe / embedding-2 all 404 in us-west1); video uses the GCS asset `gs://<CEDAR_INGEST_BUCKET>/assets/<sha>.mp4`
  directly, bypassing the Files API. Project from `CEDAR_GCP_PROJECT` (or `GOOGLE_CLOUD_PROJECT`).
Transcription (gemini-3.5-transcribe) has no Interactions API on enterprise, so it stays on developer for now (`client("developer")`).
"""
from __future__ import annotations

import base64
import concurrent.futures as cf
import datetime as dt
import os
import pathlib
import subprocess
import tempfile
import time

KEY_FILE = pathlib.Path(os.path.expanduser(os.environ.get("CEDAR_GEMINI_KEY_FILE", "~/.config/cedar/gemini_api_key")))
VISION_MODEL = "gemini-3.8-flash"
TRANSCRIBE_MODEL = "gemini-3.5-transcribe"
EMBED_MODEL = "gemini-embedding-2"
EMBED_DIM = 1536
MIN_REMAINING = dt.timedelta(hours=2)
BACKENDS = ("developer", "enterprise")
ENTERPRISE_LOCATION = "global"


def backend() -> str:
    b = os.environ.get("CEDAR_GEMINI_BACKEND", "developer")
    if b not in BACKENDS:
        raise ValueError(f"CEDAR_GEMINI_BACKEND must be one of {BACKENDS}, got {b!r}")
    return b


def client(which: str | None = None):
    which = which or backend()
    from google import genai
    if which == "enterprise":
        project = os.environ.get("CEDAR_GCP_PROJECT") or os.environ.get("GOOGLE_CLOUD_PROJECT")
        if not project:
            raise RuntimeError("enterprise backend needs CEDAR_GCP_PROJECT (or GOOGLE_CLOUD_PROJECT)")
        return genai.Client(enterprise=True, project=project,
                            location=os.environ.get("CEDAR_GCP_LOCATION", ENTERPRISE_LOCATION))
    for v in ("GOOGLE_GENAI_USE_VERTEXAI", "GOOGLE_GENAI_USE_ENTERPRISE", "GOOGLE_CLOUD_PROJECT", "GOOGLE_CLOUD_LOCATION"):
        os.environ.pop(v, None)
    return genai.Client(api_key=KEY_FILE.read_text().strip(), vertexai=False)


def gcs_asset_uri(sha: str) -> str:
    bucket = os.environ.get("CEDAR_INGEST_BUCKET", "").removeprefix("gs://").rstrip("/")
    if not bucket:
        raise RuntimeError("enterprise backend needs CEDAR_INGEST_BUCKET (assets at gs://<bucket>/assets/<sha>.mp4)")
    return f"gs://{bucket}/assets/{sha}.mp4"


def gcs_exists(uri: str) -> bool:
    from google.cloud import storage
    b, _, name = uri.removeprefix("gs://").partition("/")
    return storage.Client().bucket(b).blob(name).exists()


def gcs_asset_record(sha: str) -> dict:
    """enterprise: the whole film is a GCS asset (uploaded by deploy/ingest/upload_assets.py); never expires, no renewal needed."""
    uri = gcs_asset_uri(sha)
    if not gcs_exists(uri):
        raise RuntimeError(f"{uri} missing: upload it with deploy/ingest/upload_assets.py, or use CEDAR_GEMINI_BACKEND=developer")
    return {"gemini_file": None, "uri": uri, "mime_type": "video/mp4", "size_bytes": None, "expires_at": None, "how": "gcs"}


def _hash_matches(f, sha_hex: str) -> bool:
    h = getattr(f, "sha256_hash", None)
    if not h:
        return False
    try:
        return base64.b64decode(h).decode() == sha_hex
    except Exception:
        return h == sha_hex


def _usable(f, sha_hex: str) -> bool:
    exp = getattr(f, "expiration_time", None)
    return (getattr(f.state, "name", str(f.state)) == "ACTIVE" and _hash_matches(f, sha_hex)
            and exp is not None and exp - dt.datetime.now(dt.timezone.utc) > MIN_REMAINING)


def ensure_file(c, path: pathlib.Path, sha_hex: str, known_name: str | None, ascii_name: str):
    """Try the recorded name first, then look up files.list by sha256Hash, and only then upload (via an ASCII-named symlink)."""
    if known_name:
        try:
            f = c.files.get(name=known_name)
            if _usable(f, sha_hex):
                return f, "reused"
        except Exception:
            pass
    for f in c.files.list():
        if _usable(f, sha_hex):
            return f, "adopted"
    # ASCII symlink goes in a temp dir (the media dir may be read-only / outside this repo)
    with tempfile.TemporaryDirectory(prefix="cedar-upload-") as td:
        link = pathlib.Path(td) / ascii_name
        link.symlink_to(path.resolve())
        f = c.files.upload(file=str(link), config={"display_name": ascii_name})
    while getattr(f.state, "name", str(f.state)) == "PROCESSING":
        time.sleep(5)
        f = c.files.get(name=f.name)
    if getattr(f.state, "name", str(f.state)) != "ACTIVE":
        raise RuntimeError(f"upload failed: state {f.state}")
    if not _hash_matches(f, sha_hex):
        raise RuntimeError("uploaded file sha256Hash does not match asset")
    return f, "uploaded"


def file_record(f, how: str) -> dict:
    return {"gemini_file": f.name, "uri": f.uri, "mime_type": f.mime_type, "size_bytes": f.size_bytes,
            "expires_at": f.expiration_time.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "how": how}


def generate_json(c, model: str, parts: list, schema: dict, thinking: str | None = None, max_out: int = 65536):
    from google.genai import types
    cfg = dict(response_mime_type="application/json", response_json_schema=schema, max_output_tokens=max_out)
    if thinking:
        cfg["thinking_config"] = types.ThinkingConfig(thinking_level=thinking)
    t0 = time.time()
    r = c.models.generate_content(model=model, contents=[types.Content(role="user", parts=parts)],
                                  config=types.GenerateContentConfig(**cfg))
    usage = r.usage_metadata.model_dump(exclude_none=True, mode="json") if r.usage_metadata else {}
    finish = str(r.candidates[0].finish_reason) if r.candidates else None
    return r.text, usage, round(time.time() - t0, 1), finish


def video_part(rec: dict, fps: float, start: float | None = None, end: float | None = None):
    from google.genai import types
    vm = {"fps": fps}
    if start is not None:
        vm["start_offset"] = f"{start:.3f}s"
    if end is not None:
        vm["end_offset"] = f"{end:.3f}s"
    return types.Part(file_data=types.FileData(file_uri=rec["uri"], mime_type=rec["mime_type"]),
                      video_metadata=types.VideoMetadata(**vm))


def text_part(s: str):
    from google.genai import types
    return types.Part(text=s)


# ── Gemini Embedding 2 ───────────────────────────────────────────────
# gemini-embedding-2 ignores task_type (silently); the retrieval task is set by a text prefix (see ingest/embed.py); 1536-d output is normalized.
# enterprise: 1 content / 1 video per request; video can include audio (audio_track_extraction) and offsets, 1 fps × 66 tokens/frame, ≤ 120 frames.
# developer: multiple Contents per call; video ≤ 32 frames, no audio, so we cut a 360p clip and send it inline.
EMBED_PRICE = {"text": 0.20, "video": 12.00, "audio": 6.50}      # $/1M input tokens (online price; Batch is half)
VIDEO_TOK_PER_FRAME = 66
AUDIO_TOK_PER_S = 25
EMBED_WORKERS = 8


def _retry(fn, tries: int = 5):
    for i in range(tries):
        try:
            return fn()
        except Exception as e:                      # 429 / 5xx / dropped connection / 400/403 while a service agent is still provisioning
            import google.auth.exceptions
            import httpx
            msg = str(e)
            transient = isinstance(e, (httpx.TransportError, google.auth.exceptions.TransportError)) or any(x in msg for x in (
                "429", "503", "500", "RESOURCE_EXHAUSTED", "UNAVAILABLE", "provisioned", "PERMISSION_DENIED"))
            if i == tries - 1 or not transient:
                raise
            time.sleep(min(60, 5 * 2 ** i))


class Embedder:
    """Embedder used by embed.embed_missing. mode: av = enterprise gs:// picture + audio; frames = developer inline clip (picture only).
    On enterprise, if the asset is not in GCS, video falls back to frames (text vectors match across backends and can be mixed)."""

    def __init__(self, ctx=None, which: str | None = None):
        self.ctx = ctx
        self.which = which or backend()
        self.c = client(self.which)
        self.mode = "frames"
        self.uri = None
        if self.which == "enterprise" and ctx is not None:
            uri = gcs_asset_uri(ctx.sha)
            if gcs_exists(uri):
                self.mode, self.uri = "av", uri
        self._dev = self.c if self.which == "developer" else None

    def _cfg(self, **kw):
        from google.genai import types
        return types.EmbedContentConfig(output_dimensionality=EMBED_DIM, **kw)

    def estimate(self, texts: list[str], durations: list[float]) -> float:
        t = sum(len(x) for x in texts) / 4 * EMBED_PRICE["text"]
        if self.mode == "av":
            v = sum(min(d, 120) * (VIDEO_TOK_PER_FRAME * EMBED_PRICE["video"] + AUDIO_TOK_PER_S * EMBED_PRICE["audio"])
                    for d in durations)
        else:
            v = sum(min(d, 32) * VIDEO_TOK_PER_FRAME * EMBED_PRICE["video"] for d in durations)
        return (t + v) / 1e6

    def embed_texts(self, texts: list[str]) -> tuple[list[list[float]], float]:
        from google.genai import types
        cost = sum(len(x) for x in texts) / 4 * EMBED_PRICE["text"] / 1e6
        if self.which == "developer":
            r = _retry(lambda: self.c.models.embed_content(
                model=EMBED_MODEL, contents=[types.Content(parts=[types.Part(text=t)]) for t in texts], config=self._cfg()))
            vecs = [e.values for e in r.embeddings]
        else:
            def one(t):
                r = _retry(lambda: self.c.models.embed_content(model=EMBED_MODEL, contents=t, config=self._cfg()))
                return r.embeddings[0].values
            with cf.ThreadPoolExecutor(EMBED_WORKERS) as ex:
                vecs = list(ex.map(one, texts))
        if len(vecs) != len(texts):
            raise RuntimeError(f"embed_content returned {len(vecs)} vectors for {len(texts)} texts")
        return vecs, cost

    def embed_video(self, start: float, end: float) -> tuple[list[float], float]:
        from google.genai import types
        if self.mode == "av":
            part = types.Part(file_data=types.FileData(file_uri=self.uri, mime_type="video/mp4"),
                              video_metadata=types.VideoMetadata(start_offset=f"{start:.3f}s", end_offset=f"{end:.3f}s"))
            r = _retry(lambda: self.c.models.embed_content(model=EMBED_MODEL, contents=types.Content(parts=[part]),
                                                           config=self._cfg(audio_track_extraction=True)))
            return r.embeddings[0].values, self.estimate([], [end - start])
        if self._dev is None:
            self._dev = client("developer")
        with tempfile.TemporaryDirectory(prefix="cedar-embed-") as td:
            clip = pathlib.Path(td) / "w.mp4"
            subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-ss", f"{start:.3f}", "-t", f"{end - start:.3f}",
                            "-i", str(self.ctx.asset), "-vf", "scale=-2:360", "-an", "-c:v", "libx264", "-preset", "veryfast",
                            "-crf", "28", str(clip)], check=True)
            data = clip.read_bytes()
        part = types.Part.from_bytes(data=data, mime_type="video/mp4")
        r = _retry(lambda: self._dev.models.embed_content(model=EMBED_MODEL, contents=types.Content(parts=[part]),
                                                          config=self._cfg()))
        return r.embeddings[0].values, self.estimate([], [end - start])
