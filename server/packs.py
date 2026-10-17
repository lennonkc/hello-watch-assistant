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


def _catalog() -> list[dict]:
    doc = json.loads((config.REPO / "catalog" / "catalog.json").read_text(encoding="utf-8"))
    out = []
    for item in doc["items"]:
        spec = json.loads((config.REPO / "ingest" / "titles" / f"{item['id']}.json").read_text(encoding="utf-8"))
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
