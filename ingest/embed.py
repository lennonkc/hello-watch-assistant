"""Gemini Embedding 2 vectors: text items + video windows, written to `embeddings` / `video_windows`.

- Only items with a reveal time (`TEXT_TYPES`) and video windows are embedded; vectors never decide spoiler
  eligibility (spec §5: retrieval gates first, then ranks; see `ingest/retrieval.py`).
- Vectors are cached by content key in the sidecar `<wd>/post/embeddings.db` (SQLite, key = sha256 of model | dim | payload);
  `pack.json` / `rows.json` carry no vectors (spec §8). On export, `pack_rows` looks up the cache for the items in the pack
  and hands them to `packdb.write_db` for pack.db.
- Review patches change text (automated pre-review often edits summaries): build embeds both the auto rows and the text
  after replaying `patches/` (`reviewed_view`); export looks up the current master / device text. If a later patch changes
  text, cache misses get no vector until build is re-run.
- Env: `CEDAR_EMBED=0` skips paid calls (windows only, existing cache only); `CEDAR_EMBED_VIDEO=0` embeds text only.
"""
from __future__ import annotations

import hashlib
import math
import os
import pathlib
import sqlite3
import struct
import time

MODEL = "gemini-embedding-2"
DIM = 1536
TEXT_TYPES = ("cue", "scene", "event", "character_fact", "object", "shot_caption", "recap")
DOC_PREFIX = "title: none | text: {}"
QUERY_PREFIX = "task: search result | query: {}"
WIN_LEN = 30.0
WIN_OVERLAP = 5.0
TEXT_BATCH = 100
VIDEO_MODES = ("av", "frames")       # on export, prefer window vectors that include audio


def sidecar_path(wd: pathlib.Path) -> pathlib.Path:
    return pathlib.Path(wd) / "post" / "embeddings.db"


# ── Video windows ────────────────────────────────────────────────────

def plan_windows(start: float, end: float, length: float = WIN_LEN, overlap: float = WIN_OVERLAP) -> list[tuple[float, float]]:
    """Split [start, end] evenly into windows ≤ length with ≥ overlap between neighbours; ends align to segment bounds."""
    span = end - start
    if span <= length:
        return [(start, end)]
    n = math.ceil((span - overlap) / (length - overlap))
    step = (span - length) / (n - 1)
    return [(round(start + i * step, 3), round(min(end, start + i * step + length), 3)) for i in range(n)]


def video_windows(segments: list[dict]) -> list[dict]:
    out = []
    for sg in sorted(segments, key=lambda s: s["start_s"]):
        for i, (a, b) in enumerate(plan_windows(sg["start_s"], sg["end_s"])):
            out.append({"window_id": f"{sg['segment_id']}:w{i:02d}", "segment_id": sg["segment_id"], "start_s": a, "end_s": b})
    return out


# ── Payloads and content keys ────────────────────────────────────────

def text_items(pack: dict) -> list[tuple[str, str, str]]:
    """[(item_type, item_id, text to embed)]; uses the English primary fields (the model is multilingual, so Chinese queries still match)."""
    out = []
    for c in pack.get("cues", []):
        if c["kind"] != "lyrics":
            out.append(("cue", c["cue_id"], c["text"]))
    for s in pack.get("scenes", []):
        loc = f"{s['location_en']}. " if s.get("location_en") else ""
        out.append(("scene", s["scene_id"], loc + s["summary_en"]))
    for e in pack.get("events", []):
        out.append(("event", e["event_id"], e["text_en"]))
    for f in pack.get("character_facts", []):
        out.append(("character_fact", f["fact_id"], f["text_en"]))
    for o in pack.get("objects", []):
        out.append(("object", o["object_id"], f"{o['name_en']}: {o['description_en']}"))
    for s in pack.get("shot_captions", []):
        out.append(("shot_caption", s["shot_caption_id"], s["caption_en"]))
    for r in pack.get("recaps", []):
        out.append(("recap", r["recap_id"], r["text_en"]))
    return [x for x in out if x[2] and x[2].strip()]


def text_key(text: str, model: str = MODEL, dim: int = DIM) -> str:
    return hashlib.sha256(f"{model}|{dim}|text|{DOC_PREFIX.format(text)}".encode()).hexdigest()


def window_key(asset_sha: str, w: dict, mode: str, model: str = MODEL, dim: int = DIM) -> str:
    """mode tells backends apart: av = picture + audio (enterprise), frames = picture only (developer); vectors differ, so caches never mix."""
    return hashlib.sha256(f"{model}|{dim}|video|{mode}|{asset_sha}|{w['start_s']:.3f}|{w['end_s']:.3f}".encode()).hexdigest()


# ── Vector encoding and sidecar ──────────────────────────────────────

def pack_vector(v) -> bytes:
    """L2-normalize (1536-d is a Matryoshka truncation, so returned vectors may not be normalized), then store as float32 LE."""
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return struct.pack(f"<{len(v)}f", *(x / n for x in v))


def unpack_vector(b: bytes) -> list[float]:
    return list(struct.unpack(f"<{len(b) // 4}f", b))


class Cache:
    def __init__(self, path: pathlib.Path):
        self.path = pathlib.Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path)
        self.db.execute("CREATE TABLE IF NOT EXISTS vectors (key TEXT PRIMARY KEY, kind TEXT NOT NULL, model TEXT NOT NULL, "
                        "dim INTEGER NOT NULL, vector BLOB NOT NULL, created_at TEXT NOT NULL)")

    def get_many(self, keys) -> dict[str, bytes]:
        keys = list(dict.fromkeys(keys))
        out = {}
        for i in range(0, len(keys), 500):
            chunk = keys[i:i + 500]
            out.update(self.db.execute(f"SELECT key, vector FROM vectors WHERE key IN ({','.join('?' * len(chunk))})", chunk))
        return out

    def put_many(self, rows: list[tuple[str, str, bytes]], model: str = MODEL, dim: int = DIM) -> None:
        ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        with self.db:
            self.db.executemany("INSERT OR REPLACE INTO vectors(key, kind, model, dim, vector, created_at) VALUES (?,?,?,?,?,?)",
                                [(k, kind, model, dim, v, ts) for k, kind, v in rows])

    def close(self) -> None:
        self.db.close()


def _wanted(pack: dict, asset_sha: str, mode: str) -> list[tuple]:
    """[(item_type, item_id, key, kind, payload)]; payload is the raw text for text items, the window row for windows."""
    out = [(it, iid, text_key(t), "text", t) for it, iid, t in text_items(pack)]
    out += [("video_window", w["window_id"], window_key(asset_sha, w, mode), "video", w) for w in pack.get("video_windows", [])]
    return out


def pack_rows(pack: dict, wd: pathlib.Path) -> list[dict]:
    """Items in the pack → `embeddings` rows (cache hits only); a device view naturally contains only the items it keeps."""
    p = sidecar_path(wd)
    if not p.exists():
        return []
    sha = pack["asset"]["asset_sha256"]
    cands = {}                                  # (item_type, item_id) → candidate keys in priority order
    for mode in VIDEO_MODES:
        for it, iid, k, _kind, _p in _wanted(pack, sha, mode):
            cands.setdefault((it, iid), [])
            if k not in cands[(it, iid)]:
                cands[(it, iid)].append(k)
    cache = Cache(p)
    try:
        got = cache.get_many(k for ks in cands.values() for k in ks)
    finally:
        cache.close()
    rows = []
    for (it, iid), ks in sorted(cands.items()):
        hit = next((k for k in ks if k in got), None)
        if hit:
            rows.append({"item_type": it, "item_id": iid, "model": MODEL, "dim": DIM, "vector": got[hit]})
    return rows


# ── Ingest hooks ─────────────────────────────────────────────────────

def reviewed_view(ctx, pack: dict) -> dict | None:
    """Replay `patches/*.json` over the auto rows (same order as build.materialize); None if there are no patches or replay fails (export would fail too)."""
    from ingest.patch import PatchError, apply_patch
    from ingest.util import read_json
    pd = pathlib.Path(ctx.wd) / "patches"
    files = sorted(pd.glob("*.json")) if pd.exists() else []
    if not files:
        return None
    view = {**pack, "review_log": list(pack.get("review_log") or [])}
    try:
        for f in files:
            view = apply_patch(view, read_json(f))
    except PatchError:
        return None
    return view


def embed_missing(ctx, pack: dict, embedder=None, video: bool | None = None) -> dict:
    """Call the embedding API for cache misses (budget guard first); embedder is injectable (tests), default `gemini.Embedder`. Returns stats."""
    from ingest import gemini
    emb = embedder or gemini.Embedder(ctx)
    want = _wanted(pack, ctx.sha, emb.mode)
    view = reviewed_view(ctx, pack)
    if view is not None:
        keys = {w[2] for w in want}
        want += [w for w in _wanted(view, ctx.sha, emb.mode) if w[2] not in keys]
    if video is None:
        video = os.environ.get("CEDAR_EMBED_VIDEO", "1") != "0"
    cache = Cache(sidecar_path(ctx.wd))
    try:
        have = cache.get_many(w[2] for w in want)
        texts = list({k: p for _it, _iid, k, kind, p in want if kind == "text" and k not in have}.items())
        wins = list({k: p for _it, _iid, k, kind, p in want if kind == "video" and k not in have}.items()) if video else []
        est = emb.estimate([p for _k, p in texts], [w["end_s"] - w["start_s"] for _k, w in wins])
        ctx.ledger.guard(est)
        t0, cost, done = time.time(), 0.0, {"text": 0, "video": 0}
        status = "failed"
        try:
            for i in range(0, len(texts), TEXT_BATCH):
                chunk = texts[i:i + TEXT_BATCH]
                vecs, c = emb.embed_texts([DOC_PREFIX.format(p) for _k, p in chunk])
                cache.put_many([(k, "text", pack_vector(v)) for (k, _p), v in zip(chunk, vecs, strict=True)])
                cost += c
                done["text"] += len(chunk)
            for k, w in wins:
                v, c = emb.embed_video(w["start_s"], w["end_s"])
                cache.put_many([(k, "video", pack_vector(v))])
                cost += c
                done["video"] += 1
            status = "ran"
        finally:                             # log spend to the ledger even on mid-run failure
            stats = {"model": MODEL, "dim": DIM, "video_mode": emb.mode, "items": len(want),
                     "cached": sum(1 for w in want if w[2] in have), "new_text": done["text"], "new_video": done["video"],
                     "est_usd": round(est, 4)}
            if done["text"] or done["video"]:
                ctx.ledger.log("embed", status, time.time() - t0, cost, **stats)
        return stats
    finally:
        cache.close()


def add_embeddings(ctx, pack: dict, embedder=None) -> None:
    """build_auto hook: cut video windows (a regular table, exported with the pack) and embed items missing vectors (sidecar)."""
    pack["video_windows"] = video_windows(pack.get("segments", []))
    pack.setdefault("embeddings", [])          # vectors stay out of rows.json; pack_rows reads them from the sidecar on export
    if embedder is None and os.environ.get("CEDAR_EMBED", "1") == "0":
        return
    embed_missing(ctx, pack, embedder)
