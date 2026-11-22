"""Knowledge-pack store: which films the demo offers, where their packs live on the server, and access to the private
preview titles.

Master packs contain `review_log` and every future item, so they stay on the server: the browser only ever receives
answers, thumbnails and a playback URL. In the cloud the packs are copied from Cloud Storage to local disk at startup
(about 66 MB for the three demo films) and read with SQLite from there.

Private preview titles (`WATCH_PRIVATE_TITLES`, a JSON file: `{"access_sha256": [hex, …], "titles": [{"id", "sha",
"title", "synopsis", "license", "attribution", "dist", "video_object"}]}`) are loaded after the public catalog. They are
hidden (every endpoint answers 404) unless the request carries an access code whose sha256 is listed. Their pack is at
`<packs prefix>/<sha>/<dist>/pack.db`; only `pack.db` and `MANIFEST.json` are downloaded at startup, thumbnails are
fetched from the bucket on first use and kept on local disk (Cloud Run's disk is memory) up to `WATCH_THUMB_CACHE_MAX`
files per title, least recently used evicted first.
"""
from __future__ import annotations

import collections
import concurrent.futures as cf
import contextlib
import dataclasses
import hashlib
import hmac
import json
import logging
import os
import pathlib
import re
import sqlite3
import threading
import time

from ingest import retrieval
from server import config

log = logging.getLogger(__name__)
HEX64 = re.compile(r"^[0-9a-f]{64}$")
DISTS = ("master", "device-auto")
PLAYBACK = ("standard", "preview")


# ── bucket access (google-cloud-storage; tests replace `bucket_for`) ─

class GcsBucket:
    def __init__(self, name: str) -> None:
        from google.cloud import storage
        self.name = name
        self.bucket = storage.Client().bucket(name)

    def names(self, prefix: str) -> list[str]:
        return [b.name for b in self.bucket.list_blobs(prefix=prefix)]

    def download_many(self, prefix: str, rel_names: list[str], dest: pathlib.Path) -> None:
        """`prefix + rel` → `dest / rel` for each name, 16 at a time; raises on the first failure."""
        from google.cloud.storage import transfer_manager
        results = transfer_manager.download_many_to_path(
            self.bucket, rel_names, destination_directory=str(dest), blob_name_prefix=prefix, max_workers=16,
            worker_type="thread")
        bad = [r for r in results if isinstance(r, Exception)]
        if bad:
            raise RuntimeError(f"download failed: {bad[0].__class__.__name__}")

    def read(self, name: str) -> bytes | None:
        """The object's bytes, None when it does not exist (other errors raise)."""
        from google.api_core import exceptions
        try:
            return self.bucket.blob(name).download_as_bytes()
        except exceptions.NotFound:
            return None


_BUCKETS: dict[str, GcsBucket] = {}
_BUCKETS_LOCK = threading.Lock()


def bucket_for(name: str):
    with _BUCKETS_LOCK:
        if name not in _BUCKETS:
            _BUCKETS[name] = GcsBucket(name)
        return _BUCKETS[name]


def split_gs(url: str) -> tuple[str, str]:
    """gs://bucket/some/prefix → ("bucket", "some/prefix")."""
    name, _, prefix = url.removeprefix("gs://").partition("/")
    return name, prefix.strip("/")


def _key(*parts: str) -> str:
    return "/".join(p.strip("/") for p in parts if p and p.strip("/"))


# ── thumbnails on demand ─────────────────────────────────────────────

_POOL: cf.ThreadPoolExecutor | None = None


def _pool() -> cf.ThreadPoolExecutor:
    global _POOL
    with _BUCKETS_LOCK:
        if _POOL is None:
            _POOL = cf.ThreadPoolExecutor(8, thread_name_prefix="thumbs")
        return _POOL


class RemoteThumbs:
    """`tNNNNN.jpg` fetched from `<bucket>/<prefix>` the first time it is asked for, then served from `local_dir`.
    Thread-safe: concurrent requests for one frame share one download; a frame the bucket does not have is not asked
    for again for `miss_ttl_s` (a failed download: `error_ttl_s`). At most `max_files` frames are kept, least recently
    used deleted first."""

    def __init__(self, bucket, prefix: str, local_dir: pathlib.Path, max_files: int | None = None,
                 miss_ttl_s: float = 600.0, error_ttl_s: float = 30.0, wait_s: float = 10.0) -> None:
        self.bucket, self.prefix, self.local_dir = bucket, prefix.strip("/"), pathlib.Path(local_dir)
        self.max_files = max(1, max_files if max_files is not None else config.THUMB_CACHE_MAX)
        self.miss_ttl_s, self.error_ttl_s, self.wait_s = miss_ttl_s, error_ttl_s, wait_s
        self.lock = threading.Lock()
        self.kept: collections.OrderedDict[int, None] = collections.OrderedDict()   # least recently used first
        self.misses: dict[int, float] = {}                                          # t → monotonic retry time
        self.inflight: dict[int, threading.Event] = {}
        self.fetched = 0
        self.local_dir.mkdir(parents=True, exist_ok=True)
        for p in sorted(self.local_dir.glob("t*.jpg"), key=lambda p: p.stat().st_mtime):
            if p.stem[1:].isdigit():
                self.kept[int(p.stem[1:])] = None
        with self.lock:
            self._evict()

    def path(self, t: int) -> pathlib.Path:
        return self.local_dir / f"t{int(t):05d}.jpg"

    def ensure(self, t: int) -> pathlib.Path:
        """The local path of frame `t`, downloaded first if needed; the file is absent when the bucket has none."""
        t = int(t)
        p = self.path(t)
        with self.lock:
            if t in self.kept and p.exists():
                self.kept.move_to_end(t)
                return p
            if self.misses.get(t, 0.0) > time.monotonic():
                return p
            ev = self.inflight.get(t)
            owner = ev is None
            if owner:
                ev = self.inflight[t] = threading.Event()
        if not owner:
            ev.wait(self.wait_s)
            return p
        try:
            data, ttl = None, self.miss_ttl_s
            try:
                data = self.bucket.read(_key(self.prefix, p.name))
            except Exception as e:                       # network / permission: retry soon, never fail the request
                ttl = self.error_ttl_s
                log.debug("thumbnail fetch failed: %s", e.__class__.__name__)
            if data:
                tmp = p.with_name(f".{p.name}.{threading.get_ident()}.tmp")
                tmp.write_bytes(data)
                os.replace(tmp, p)
            with self.lock:
                if data:
                    self.fetched += 1
                    self.kept[t] = None
                    self.kept.move_to_end(t)
                    self.misses.pop(t, None)
                    self._evict()
                else:
                    if len(self.misses) > 50_000:
                        self.misses.clear()
                    self.misses[t] = time.monotonic() + ttl
        finally:
            with self.lock:
                self.inflight.pop(t, None)
            ev.set()
        return p

    def _evict(self) -> None:                            # lock held
        while len(self.kept) > self.max_files:
            old, _ = self.kept.popitem(last=False)
            with contextlib.suppress(OSError):
                self.path(old).unlink()

    def prefetch(self, ts) -> list[int]:
        """Fetch several frames concurrently; returns the seconds that are available locally afterwards."""
        want = sorted({int(t) for t in ts if int(t) >= 0})
        if not want:
            return []
        list(_pool().map(self.ensure, want))
        return [t for t in want if self.path(t).exists()]


# ── titles ───────────────────────────────────────────────────────────

@dataclasses.dataclass(frozen=True)
class Title:
    id: str
    sha: str
    title: str
    synopsis: str
    license: str
    attribution: str
    root: pathlib.Path                       # <sha>/<dist>: pack.db, MANIFEST.json, thumbs/
    hidden: bool = False                     # private preview title: 404 everywhere unless unlocked
    dist: str = "master"                     # "master" | "device-auto" (gate with allow_auto either way)
    video_object: str = ""                   # bucket-relative; default assets/<sha>.mp4
    playback: str = "standard"               # "standard" | "preview" (a low-bitrate copy)
    remote_thumbs: RemoteThumbs | None = dataclasses.field(default=None, compare=False, repr=False)

    def __post_init__(self) -> None:
        if not self.video_object:
            object.__setattr__(self, "video_object", f"assets/{self.sha}.mp4")

    @property
    def db_path(self) -> pathlib.Path:
        return self.root / "pack.db"

    def thumb(self, t: int) -> pathlib.Path:
        """Local path of the thumbnail at second `t` (fetched from the bucket first for on-demand titles); the file
        does not exist when the film has no frame there."""
        t = int(t)
        if self.remote_thumbs is not None and t >= 0:
            return self.remote_thumbs.ensure(t)
        return self.root / "thumbs" / f"t{t:05d}.jpg"

    def prefetch(self, ts) -> list[int]:
        """Make several thumbnails local at once (concurrent downloads for on-demand titles); returns the seconds
        whose file exists afterwards."""
        if self.remote_thumbs is not None:
            return self.remote_thumbs.prefetch(ts)
        return [int(t) for t in ts if self.thumb(t).exists()]

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


def _read_source(src: str) -> bytes:
    if src.startswith("gs://"):
        name, key = split_gs(src)
        data = bucket_for(name).read(key)
        if data is None:
            raise FileNotFoundError("no such object")
        return data
    return pathlib.Path(src).expanduser().read_bytes()


def private_spec(e) -> dict:
    """One entry of the private titles file, checked; raises ValueError (the message never quotes the entry)."""
    if not isinstance(e, dict):
        raise ValueError("entry is not an object")
    sha = str(e.get("sha") or "").lower()
    if not HEX64.match(sha):
        raise ValueError("sha is not a sha256")
    tid = str(e.get("id") or "").strip()
    if not tid or "/" in tid or len(tid) > 100:
        raise ValueError("bad id")
    title = str(e.get("title") or "").strip()
    if not title:
        raise ValueError("no title")
    dist = e.get("dist") or "master"
    if dist not in DISTS:
        raise ValueError("dist must be master or device-auto")
    video = str(e.get("video_object") or f"assets/{sha}.mp4").strip()
    if video.startswith(("/", "gs://")) or ".." in video.split("/"):
        raise ValueError("video_object must be a path inside the bucket")
    playback = e.get("playback") or "preview"
    if playback not in PLAYBACK:
        raise ValueError("playback must be standard or preview")
    return {"id": tid, "sha": sha, "title": title, "synopsis": str(e.get("synopsis") or ""),
            "license": str(e.get("license") or ""), "attribution": str(e.get("attribution") or ""), "dist": dist,
            "video_object": video, "playback": playback}


def _ensure_pack(remote: tuple[str, str] | None, base: pathlib.Path, sha: str, dist: str, whole: bool) -> pathlib.Path:
    """`base/<sha>/<dist>/` with pack.db (and MANIFEST.json); from the bucket when it is not on disk yet. `whole` also
    copies thumbs/ (public titles), otherwise thumbnails come on demand."""
    root = base / sha / dist
    if remote is None or (root / "pack.db").exists():
        return root
    name, prefix = remote
    b = bucket_for(name)
    key = _key(prefix, sha, dist) + "/"
    if whole:
        rel = [n[len(key):] for n in b.names(key) if not n.endswith("/")]
    else:
        rel = [n for n in ("pack.db", "MANIFEST.json") if key + n in b.names(key + n)]
    if "pack.db" not in rel:
        return root
    root.mkdir(parents=True, exist_ok=True)
    b.download_many(key, rel, root)
    log.info("downloaded %d files for %s", len(rel), "a private title" if not whole else sha[:12])
    return root


class Store:
    def __init__(self) -> None:
        self.titles: dict[str, Title] = {}
        self.access: set[str] = set()            # sha256 hex of the access codes for private preview titles

    def load(self) -> "Store":
        cat = _catalog()
        remote = None
        if config.PACKS_DIR:
            base = config.PACKS_DIR
        elif config.PACKS_GCS:
            # blobs "<prefix>/<sha>/<dist>/..." land at CACHE_DIR/<prefix>/<sha>/<dist>/...
            remote = split_gs(config.PACKS_GCS)
            base = config.CACHE_DIR / remote[1]
        else:
            raise RuntimeError("set WATCH_PACKS_DIR (local packs) or WATCH_PACKS_GCS (gs://bucket/prefix)")
        for c in cat:
            root = _ensure_pack(remote, base, c["sha"], "master", whole=True)
            if not (root / "pack.db").exists():
                log.warning("no pack for %s at %s; skipped", c["title"], root)
                continue
            self.titles[c["id"]] = Title(c["id"], c["sha"], c["title"], c["synopsis"], c["license"],
                                         c["attribution"], root)
            issues = check_pack(root / "pack.db")
            if issues:
                log.warning("pack %s: %s", c["sha"][:12], "; ".join(issues))
        if config.PRIVATE_TITLES:
            self.load_private(config.PRIVATE_TITLES, remote, base)
        # warm the parsed-pack cache in parallel so the first question is fast
        with cf.ThreadPoolExecutor(4) as ex:
            list(ex.map(lambda t: t.pack, self.titles.values()))
        return self

    def load_private(self, src: str, remote: tuple[str, str] | None, base: pathlib.Path) -> int:
        """Private preview titles from `src` (gs:// object or local path). A missing or broken file, or a bad entry,
        is a warning: the public titles are served either way. Logs counts only."""
        try:
            doc = json.loads(_read_source(src))
            if not isinstance(doc, dict) or not isinstance(doc.get("titles", []), list):
                raise ValueError("not a {access_sha256, titles} object")
        except Exception as e:
            log.warning("private titles: not loaded (%s)", e.__class__.__name__)
            return 0
        self.access = {str(h).lower() for h in doc.get("access_sha256") or [] if HEX64.match(str(h).lower())}
        n = 0
        for i, e in enumerate(doc.get("titles") or [], 1):
            try:
                s = private_spec(e)
                if s["id"] in self.titles:
                    raise ValueError("id already taken")
                root = _ensure_pack(remote, base, s["sha"], s["dist"], whole=False)
                if not (root / "pack.db").exists():
                    raise ValueError("no pack")
            except Exception as err:
                log.warning("private title #%d: skipped (%s)", i, err if isinstance(err, ValueError)
                            else err.__class__.__name__)
                continue
            thumbs = None
            if remote is not None:
                thumbs = RemoteThumbs(bucket_for(remote[0]), _key(remote[1], s["sha"], s["dist"], "thumbs"),
                                      root / "thumbs")
            self.titles[s["id"]] = Title(s["id"], s["sha"], s["title"], s["synopsis"], s["license"],
                                         s["attribution"], root, hidden=True, dist=s["dist"],
                                         video_object=s["video_object"], playback=s["playback"],
                                         remote_thumbs=thumbs)
            issues = check_pack(root / "pack.db")
            if issues:
                log.debug("private title #%d pack: %s", i, "; ".join(issues))
            n += 1
        log.info("private preview titles: %d loaded, %d access code(s)", n, len(self.access))
        return n

    def unlocked(self, code: str | None) -> bool:
        """True when sha256(code) is one of the access hashes (constant-time compare against each)."""
        if not code or not self.access or len(code) > 200:
            return False
        h = hashlib.sha256(code.encode()).hexdigest()
        ok = False
        for a in self.access:
            ok |= hmac.compare_digest(h, a)
        return ok

    def get(self, title_id: str) -> Title:
        t = self.titles.get(title_id)
        if t is None:
            raise KeyError(title_id)
        return t

    def video_file(self, t: Title) -> pathlib.Path | None:
        if not config.VIDEO_DIR:
            return None
        for p in (config.VIDEO_DIR / pathlib.PurePosixPath(t.video_object).name, config.VIDEO_DIR / f"{t.sha}.mp4",
                  config.VIDEO_DIR / t.sha / "proxy_640.mp4"):
            if p.exists():
                return p
        return None


STORE = Store()
