"""Knowledge-pack store: which films the demo offers and where their master packs live on the server.

Master packs contain `review_log` and every future item, so they stay on the server: the browser only ever receives
answers, thumbnails and a playback URL. In the cloud the packs are copied from Cloud Storage to local disk at startup
(about 66 MB for the three demo films) and read with SQLite from there.
"""
from __future__ import annotations

import concurrent.futures as cf
import dataclasses
import json
import logging
import pathlib
import sqlite3

from ingest import retrieval
from server import config

log = logging.getLogger(__name__)


@dataclasses.dataclass(frozen=True)
class Title:
    id: str
    sha: str
    title: str
    synopsis: str
    license: str
    attribution: str
    root: pathlib.Path               # <sha>/master

    @property
    def db_path(self) -> pathlib.Path:
        return self.root / "pack.db"

    def thumb(self, t: int) -> pathlib.Path:
        return self.root / "thumbs" / f"t{int(t):05d}.jpg"

    @property
    def pack(self) -> dict:
        return retrieval.load_pack(self.db_path)          # cached on (path, mtime)


# What the online service reads from a pack (ingest.retrieval, server.knowledge / context / agent). `packdb.read_db`
# treats a missing table as empty, so a pack without one of these still loads, with that part of the answer missing.
SERVED_TABLES = ("asset", "pack_meta", "subtitle_tracks", "cues", "characters", "character_aliases", "character_looks",
                 "character_facts", "identity_links", "appearances", "scenes", "events", "objects", "object_sightings",
                 "item_characters", "item_cues", "thumbnails", "embeddings", "character_exemplars", "shot_captions",
                 "shot_entities", "cue_speakers", "recaps", "video_windows", "blocklist", "canaries")
EXPECT_ROWS = ("cues", "characters", "scenes", "thumbnails", "embeddings", "shot_captions", "cue_speakers", "recaps",
               "character_exemplars", "blocklist")
# item_type → (table, id column) of the rows that carry vectors
EMBEDDED = {"cue": ("cues", "cue_id"), "scene": ("scenes", "scene_id"), "event": ("events", "event_id"),
            "character_fact": ("character_facts", "fact_id"), "object": ("objects", "object_id"),
            "shot_caption": ("shot_captions", "shot_caption_id"), "recap": ("recaps", "recap_id"),
            "video_window": ("video_windows", "window_id")}


def check_pack(db_path) -> list[str]:
    """What the online service reads and this pack lacks or holds in a shape the service cannot use (empty list: all
    there). Reported, not repaired: a pack with gaps still serves, with weaker answers."""
    from ingest import embed

    issues = []
    db = sqlite3.connect(f"file:{pathlib.Path(db_path)}?mode=ro", uri=True)
    try:
        present = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        missing = [t for t in SERVED_TABLES if t not in present]
        if missing:
            issues.append("missing tables: " + ", ".join(missing))
        count = {t: db.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in SERVED_TABLES if t in present}
        empty = [t for t in EXPECT_ROWS if count.get(t) == 0]
        if empty:
            issues.append("no rows in: " + ", ".join(empty))
        meta = dict(db.execute("SELECT key, value FROM pack_meta").fetchall()) if "pack_meta" in present else {}
        dist = meta.get("distribution", "")
        if dist in ("device", "device-auto") and "review_log" in present:
            issues.append(f"{dist} pack carries a review_log table")
        if not any(k.startswith("gate_margin_") for k in meta):
            issues.append("pack_meta has no gate margins (the spec defaults apply)")
        if "asset" in present:
            a = db.execute("SELECT duration_s, story_start_s, story_end_s FROM asset").fetchone()
            if a is None:
                issues.append("asset row missing")
            elif not (0 <= a[1] < a[2] <= a[0] + 1):
                issues.append(f"story bounds {a[1]}–{a[2]} outside the duration {a[0]}")
        if count.get("embeddings"):
            for model, dim, n, bad in db.execute("SELECT model, dim, count(*), sum(length(vector) != dim * 4) "
                                                 "FROM embeddings GROUP BY model, dim"):
                if model != embed.MODEL:
                    issues.append(f"{n} vectors from {model} (queries use {embed.MODEL}): never ranked")
                elif dim != embed.DIM:
                    issues.append(f"{n} vectors of dim {dim} (query vectors have {embed.DIM})")
                if bad:
                    issues.append(f"{bad} {model} vectors whose length is not dim × 4 bytes")
            have = dict(db.execute("SELECT item_type, count(*) FROM embeddings WHERE model = ? GROUP BY item_type",
                                   (embed.MODEL,)).fetchall())
            thin = []
            for it, (table, _col) in EMBEDDED.items():
                rows = count.get(table, 0)
                if rows and have.get(it, 0) < 0.9 * rows:
                    thin.append(f"{it} {have.get(it, 0)}/{rows}")
            if thin:
                issues.append("vectors cover under 90 % of: " + ", ".join(thin))
        if count.get("cues") and "cue_speakers" in present:
            dlg = db.execute("SELECT count(*) FROM cues WHERE kind = 'dialogue'").fetchone()[0]
            named = db.execute("SELECT count(*) FROM cue_speakers s JOIN cues c ON c.cue_id = s.cue_id "
                               "WHERE c.kind = 'dialogue' AND s.character_id IS NOT NULL").fetchone()[0]
            if dlg and named < 0.3 * dlg:
                issues.append(f"only {named} of {dlg} dialogue cues have a speaker")
    finally:
        db.close()
    return issues


def _catalog() -> list[dict]:
    doc = json.loads((config.CATALOG_ROOT / "catalog" / "catalog.json").read_text(encoding="utf-8"))
    out = []
    for item in doc["items"]:
        spec = json.loads((config.CATALOG_ROOT / "ingest" / "titles" / f"{item['id']}.json").read_text(encoding="utf-8"))
        out.append({"id": item["id"], "sha": spec["expected_asset_sha256"][0], "title": item["title"],
                    "synopsis": item.get("synopsis_full", ""), "license": spec["license"],
                    "attribution": spec.get("license_note", "")})
    return out


def _download(prefix: str, shas: list[str], dest: pathlib.Path) -> None:
    from google.cloud import storage
    from google.cloud.storage import transfer_manager

    bucket_name, _, base = prefix.removeprefix("gs://").partition("/")
    bucket = storage.Client().bucket(bucket_name)
    for sha in shas:
        names = [b.name for b in bucket.list_blobs(prefix=f"{base}/{sha}/master/".lstrip("/"))]
        results = transfer_manager.download_many_to_path(
            bucket, names, destination_directory=str(dest), max_workers=16, worker_type="thread")
        bad = [r for r in results if isinstance(r, Exception)]
        if bad:
            raise RuntimeError(f"pack download failed for {sha}: {bad[0]}")
        log.info("downloaded %d files for %s", len(names), sha[:12])


class Store:
    def __init__(self) -> None:
        self.titles: dict[str, Title] = {}

    def load(self) -> "Store":
        cat = _catalog()
        if config.PACKS_DIR:
            base = config.PACKS_DIR
        elif config.PACKS_GCS:
            # blobs "<prefix>/<sha>/master/..." land at CACHE_DIR/<prefix>/<sha>/master/...
            base = config.CACHE_DIR / config.PACKS_GCS.removeprefix("gs://").partition("/")[2]
            todo = [c["sha"] for c in cat if not (base / c["sha"] / "master" / "pack.db").exists()]
            if todo:
                _download(config.PACKS_GCS, todo, config.CACHE_DIR)
        else:
            raise RuntimeError("set WATCH_PACKS_DIR (local packs) or WATCH_PACKS_GCS (gs://bucket/prefix)")
        for c in cat:
            root = base / c["sha"] / "master"
            if not (root / "pack.db").exists():
                log.warning("no pack for %s at %s; skipped", c["title"], root)
                continue
            self.titles[c["id"]] = Title(c["id"], c["sha"], c["title"], c["synopsis"], c["license"],
                                         c["attribution"], root)
        # warm the parsed-pack cache in parallel so the first question is fast
        with cf.ThreadPoolExecutor(4) as ex:
            list(ex.map(lambda t: t.pack, self.titles.values()))
        return self

    def get(self, title_id: str) -> Title:
        t = self.titles.get(title_id)
        if t is None:
            raise KeyError(title_id)
        return t

    def video_file(self, t: Title) -> pathlib.Path | None:
        if not config.VIDEO_DIR:
            return None
        for p in (config.VIDEO_DIR / f"{t.sha}.mp4", config.VIDEO_DIR / t.sha / "proxy_640.mp4"):
            if p.exists():
                return p
        return None


STORE = Store()
