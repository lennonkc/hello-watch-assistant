#!/usr/bin/env python3
"""Asset upload: copy catalog mp4 files to gs://<bucket>/assets/<sha>.mp4 (named by sha256) and merge-update assets/index.json.

Idempotent: skipped when the object already exists with a matching crc32c. Catalog entries are matched by file name
(`<Chinese title> <English title>.mp4`; the Chinese title takes precedence).
Can also upload title configs (titles/<catalog_id>.json) and official subtitles (sources/<catalog_id>/subs/).

    python3 deploy/ingest/upload_assets.py --bucket <bucket> --src <movies dir> [--only Sintel --only "Tears of Steel"]
    python3 deploy/ingest/upload_assets.py --bucket <bucket> --titles <dir> --sources .cache/sources   # configs only
"""
from __future__ import annotations

import argparse
import json
import pathlib
import re
import shutil
import subprocess
import sys
import unicodedata

REPO = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from ingest.cloud import gcs  # noqa: E402
from ingest.util import CATALOG_PATH, read_json, sha256_file  # noqa: E402

INDEX = "assets/index.json"


def norm(s: str) -> str:
    s = unicodedata.normalize("NFKC", s or "").lower()
    s = re.sub(r"\bii\b", "2", s)
    s = re.sub(r"\biii\b", "3", s)
    return re.sub(r"[^0-9a-z㐀-鿿]+", "", s)


def match_catalog(filename: str, items: list[dict]) -> dict | None:
    """'<Chinese title> Sintel.mp4' → catalog entry: exact match on the Chinese title first, then an English-title (prefix) match."""
    zh, _, en = pathlib.Path(filename).stem.partition(" ")
    for it in items:
        if it.get("title_zh") and norm(it["title_zh"]) == norm(zh):
            return it
    ne = norm(en)
    for it in items:
        nt = norm(it["title"])
        if ne and (nt == ne or nt.startswith(ne) or ne.startswith(nt)):
            return it
    return None


def duration_s(path: pathlib.Path) -> float:
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
                         check=True, capture_output=True, text=True).stdout
    return round(float(out.strip()), 3)


def merge_index(old: dict | None, entries: list[dict]) -> dict:
    by = {a["asset_sha256"]: a for a in (old or {}).get("assets", [])}
    for e in entries:
        by[e["asset_sha256"]] = e
    assets = sorted(by.values(), key=lambda a: (a.get("title") or "", a["asset_sha256"]))
    return {"format": "cedar-asset-index/1", "count": len(assets), "assets": assets}


def put(bkt, name: str, f: pathlib.Path) -> None:
    """Prefer gcloud storage cp for large files (parallel chunks; several times faster than a single connection through a local proxy); fall back to the client library only without gcloud."""
    if shutil.which("gcloud"):
        subprocess.run(["gcloud", "storage", "cp", "--content-type=video/mp4", "--no-user-output-enabled", str(f),
                        gcs.uri(bkt, name)], check=True)
        if bkt.get_blob(name).crc32c != gcs.crc32c_b64(f):
            raise RuntimeError(f"crc32c mismatch after upload: {gcs.uri(bkt, name)}")
    else:
        gcs.upload_file(bkt, name, f, content_type="video/mp4")


def upload_assets(bkt, src: pathlib.Path, only: list[str]) -> list[dict]:
    items = read_json(CATALOG_PATH)["items"]
    out = []
    for f in sorted(src.glob("*.mp4")):
        if only and not any(o.lower() in f.name.lower() for o in only):
            continue
        it = match_catalog(f.name, items)
        if it is None:
            print(f"SKIP {f.name}: no catalog entry matches", file=sys.stderr)
            continue
        sha = sha256_file(f)
        crc = gcs.crc32c_b64(f)
        name = f"assets/{sha}.mp4"
        blob = bkt.get_blob(name)
        if blob is not None and blob.crc32c == crc:
            how = "exists"
        else:
            put(bkt, name, f)
            how = "uploaded"
        e = {"asset_sha256": sha, "catalog_id": it["id"], "title": it["title"], "duration_s": duration_s(f),
             "file": f.name, "bytes": f.stat().st_size, "crc32c": crc}
        print(f"{how:>8} {gcs.uri(bkt, name)}  {it['title']}  ({f.name})", flush=True)
        out.append(e)
    return out


def upload_titles(bkt, d: pathlib.Path) -> None:
    for p in sorted(d.glob("*.json")):
        t = read_json(p)
        cid = t.get("catalog_id")
        if cid != p.stem:
            print(f"SKIP {p}: catalog_id {cid!r} != file name", file=sys.stderr)
            continue
        gcs.write_json(bkt, f"titles/{cid}.json", t)
        print(f"   title {gcs.uri(bkt, f'titles/{cid}.json')}  {t.get('title')}")


def upload_sources(bkt, root: pathlib.Path, catalog_ids: set[str] | None) -> None:
    for d in sorted(p for p in root.iterdir() if p.is_dir()):
        if catalog_ids is not None and d.name not in catalog_ids:
            continue
        res = gcs.push_dir(bkt, d, f"sources/{d.name}", delete=False)
        print(f" sources {gcs.uri(bkt, f'sources/{d.name}/')}  {res}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--bucket", help="GCS bucket (default: $CEDAR_INGEST_BUCKET)")
    ap.add_argument("--src", help="directory of <中文名> <English>.mp4 assets")
    ap.add_argument("--only", action="append", default=[], help="file-name substring filter (repeatable)")
    ap.add_argument("--titles", help="directory of title configs <catalog_id>.json → titles/")
    ap.add_argument("--sources", help="directory of <catalog_id>/subs/… → sources/ (only ids in the index)")
    args = ap.parse_args(argv)
    bkt = gcs.bucket(gcs.bucket_name(args.bucket))
    if args.src:
        entries = upload_assets(bkt, pathlib.Path(args.src).expanduser(), args.only)
        blob = bkt.get_blob(INDEX)
        old = json.loads(blob.download_as_bytes()) if blob else None
        new = merge_index(old, entries)
        if new != old:
            gcs.write_json(bkt, INDEX, new, if_generation_match=blob.generation if blob else 0)
        print(f"index {gcs.uri(bkt, INDEX)}: {new['count']} assets")
    if args.titles:
        upload_titles(bkt, pathlib.Path(args.titles))
    if args.sources:
        idx = gcs.read_json(bkt, INDEX) or {"assets": []}
        upload_sources(bkt, pathlib.Path(args.sources), {a["catalog_id"] for a in idx["assets"]})
    return 0


if __name__ == "__main__":
    sys.exit(main())
