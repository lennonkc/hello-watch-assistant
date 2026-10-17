"""Cloud Run Job entry point: per asset, pull the source + work dir → run the same pipeline as locally → push the work dir back → publish immutable versioned packs.

Each task takes its share of the asset list by `CLOUD_RUN_TASK_INDEX / CLOUD_RUN_TASK_COUNT`; `.cache` lives on the container's local disk.
The work dir (stage cache, ledger, patches, `versions.json`) is mirrored wholesale to `work/<sha>/`, so a rerun only fills missing stages;
re-exporting identical content yields the same `pack_version` / same bytes, and publishing only verifies existing objects, never overwrites them.
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import shutil
import tempfile
import time

from ingest.cloud import gcs
from ingest.util import CACHE, TITLES_DIR, read_json, sha256_file, work_dir, write_json

ENV_KEY = "CEDAR_GEMINI_API_KEY"


def shard(shas: list[str], index: int, count: int) -> list[str]:
    """Stable sharding: items i, i+count, … of the sorted, deduplicated list."""
    if count < 1 or not 0 <= index < count:
        raise ValueError(f"bad task index {index}/{count}")
    return sorted(set(shas))[index::count]


def task_slot() -> tuple[int, int]:
    return int(os.environ.get("CLOUD_RUN_TASK_INDEX", "0")), int(os.environ.get("CLOUD_RUN_TASK_COUNT", "1"))


def next_latest(old: dict | None, new: dict) -> dict | None:
    """LATEST only moves forward: replace only when the new version ≥ the old (same version and content counts as unchanged). Returns what to write; None = no write."""
    if old is None or new["pack_version"] > old["pack_version"]:
        return new
    if new["pack_version"] == old["pack_version"] and new != old:
        return new
    return None


def install_key_from_env() -> None:
    """The secret is injected as an env var: write it to a 0600 temp file for ingest.gemini (which only reads files) and remove it from the environment."""
    key = os.environ.pop(ENV_KEY, None)
    if not key:
        return
    d = pathlib.Path(tempfile.mkdtemp(prefix="cedar-key-"))
    p = d / "gemini_api_key"
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(key.strip())
    os.environ["CEDAR_GEMINI_KEY_FILE"] = str(p)
    from ingest import gemini
    gemini.KEY_FILE = p


def merged_titles(bkt, catalog_ids: set[str]) -> pathlib.Path:
    """ingest/titles/ in the image wins; missing ones are filled from titles/<id>.json in the bucket."""
    d = pathlib.Path(tempfile.mkdtemp(prefix="cedar-titles-"))
    for p in TITLES_DIR.glob("*.json"):
        shutil.copy2(p, d / p.name)
    for cid in catalog_ids:
        if not (d / f"{cid}.json").exists():
            t = gcs.read_json(bkt, f"titles/{cid}.json")
            if t is not None:
                write_json(d / f"{cid}.json", t)
    return d


def fetch_asset(bkt, sha: str, dest_dir: pathlib.Path, file_name: str | None = None) -> pathlib.Path:
    """Download to dest_dir/<sha>/<original file name> (original name kept so asset.json's asset_name matches a local ingest)."""
    p = dest_dir / sha / (file_name or f"{sha}.mp4")
    if not (p.is_file() and sha256_file(p) == sha):
        p.parent.mkdir(parents=True, exist_ok=True)
        blob = bkt.get_blob(f"assets/{sha}.mp4")
        if blob is None:
            raise FileNotFoundError(gcs.uri(bkt, f"assets/{sha}.mp4"))
        blob.download_to_filename(str(p), timeout=gcs.TIMEOUT)
        got = sha256_file(p)
        if got != sha:
            raise RuntimeError(f"asset sha256 mismatch: object assets/{sha}.mp4 hashes to {got}")
    return p


def publish_packs(bkt, sha: str) -> dict:
    """master / device → packs/<sha>/v<N>/, then advance LATEST."""
    wd = work_dir(sha)
    man = {d: read_json(wd / d / "MANIFEST.json") for d in ("master", "device")}
    ver = man["master"]["pack_version"]
    if man["device"]["pack_version"] != ver:
        raise RuntimeError(f"master v{man['master']['pack_version']} != device v{man['device']['pack_version']}")
    base = f"packs/{sha}/v{ver}"
    res = {d: gcs.publish_dir(bkt, wd / d, f"{base}/{d}") for d in ("master", "device")}
    latest = {"asset_sha256": sha, "pack_version": ver, "built_at": man["master"]["built_at"],
              "review_complete": man["device"]["review_complete"], "path": f"{base}/",
              "manifest_sha256": {d: sha256_file(wd / d / "MANIFEST.json") for d in ("master", "device")}}
    blob = bkt.get_blob(f"packs/{sha}/LATEST")
    old = json.loads(blob.download_as_bytes()) if blob else None
    upd = next_latest(old, latest)
    if upd is not None:
        gcs.write_json(bkt, f"packs/{sha}/LATEST", upd, if_generation_match=blob.generation if blob else 0)
    return {"pack_version": ver, "path": gcs.uri(bkt, base + "/"), "objects": res, "latest_updated": upd is not None}


def run_one(bkt, sha: str, entry: dict, titles_dir: pathlib.Path, args) -> dict:
    from ingest import cli
    t0 = time.time()
    asset = fetch_asset(bkt, sha, CACHE / "assets", entry.get("file"))
    wd = work_dir(sha)
    restored = gcs.pull_dir(bkt, f"work/{sha}", wd)
    cid = entry["catalog_id"]
    gcs.pull_dir(bkt, f"sources/{cid}", CACHE / "sources" / cid)
    cli.TITLES_DIR = titles_dir
    force = [x for f in (args.force or []) for x in f.split(",") if x]
    ns = argparse.Namespace(asset=str(asset), catalog_id=cid, stages=args.stages, force=force,
                            subs_dir=None, budget=args.budget)
    try:
        rc = cli.cmd_run(ns)
    finally:
        saved = gcs.push_dir(bkt, wd, f"work/{sha}")
    out = {"asset_sha256": sha, "catalog_id": cid, "rc": rc, "work_restored": restored, "work_saved": saved}
    if rc == 0 and (wd / "master" / "MANIFEST.json").exists():
        out["published"] = publish_packs(bkt, sha)
    from ingest.ledger import Ledger
    out["spent_usd"] = Ledger(sha).spent()
    out["elapsed_s"] = round(time.time() - t0, 1)
    return out


def select_shas(bkt, args) -> tuple[list[str], dict]:
    index = gcs.read_json(bkt, "assets/index.json") or {"assets": []}
    by_sha = {a["asset_sha256"]: a for a in index["assets"]}
    if args.all:
        shas = list(by_sha)
    else:
        shas = [s for chunk in (args.sha or []) for s in chunk.split(",") if s]
    if not shas:
        raise SystemExit("nothing to do: pass --sha <sha>[,<sha>…] or --all")
    missing = [s for s in shas if s not in by_sha]
    if missing:
        raise SystemExit(f"not in assets/index.json: {missing}")
    return shas, by_sha


def cmd_run(args) -> int:
    bkt = gcs.bucket(gcs.bucket_name(args.bucket))
    install_key_from_env()
    shas, by_sha = select_shas(bkt, args)
    i, n = task_slot()
    mine = shard(shas, i, n)
    print(json.dumps({"task": f"{i}/{n}", "assets": mine}), flush=True)
    titles = merged_titles(bkt, {by_sha[s]["catalog_id"] for s in mine})
    failed = 0
    for sha in mine:
        try:
            res = run_one(bkt, sha, by_sha[sha], titles, args)
        except Exception as e:  # one failed asset does not take down the rest of the task
            res = {"asset_sha256": sha, "rc": 1, "error": f"{type(e).__name__}: {e}"}
        failed += res["rc"] != 0
        print("RESULT " + json.dumps(res, ensure_ascii=False, sort_keys=True), flush=True)
    return 1 if failed else 0


def cmd_pull(args) -> int:
    """Pull a published pack (default: LATEST device pack) and verify against MANIFEST; --work pulls the work dir into local .cache."""
    bkt = gcs.bucket(gcs.bucket_name(args.bucket))
    if args.work:
        n = gcs.pull_dir(bkt, f"work/{args.sha}", work_dir(args.sha))
        print(f"work/{args.sha} → {work_dir(args.sha)} ({n} downloaded)")
        return 0
    if args.version == "latest":
        latest = gcs.read_json(bkt, f"packs/{args.sha}/LATEST")
        if latest is None:
            raise SystemExit(f"no packs/{args.sha}/LATEST")
        ver = latest["pack_version"]
    else:
        ver = int(args.version.removeprefix("v"))
    dest = pathlib.Path(args.dest) / args.sha / f"v{ver}" / args.dist
    n = gcs.pull_dir(bkt, f"packs/{args.sha}/v{ver}/{args.dist}", dest)
    bad = verify_manifest(dest)
    print(f"v{ver}/{args.dist} → {dest} ({n} downloaded, {'OK' if not bad else 'MANIFEST MISMATCH: ' + ', '.join(bad)})")
    return 1 if bad else 0


def verify_manifest(d: pathlib.Path) -> list[str]:
    man = read_json(d / "MANIFEST.json")
    listed = {f["path"] for f in man["files"]}
    bad = [f["path"] for f in man["files"] if not (d / f["path"]).is_file() or sha256_file(d / f["path"]) != f["sha256"]]
    extra = [r for r in gcs.local_files(d) if r != "MANIFEST.json" and r not in listed]
    return bad + [f"unlisted:{r}" for r in extra]


def cmd_push_work(args) -> int:
    """Local work dir (e.g. after apply-patch) → work/<sha>/; the next cloud run exports from it."""
    bkt = gcs.bucket(gcs.bucket_name(args.bucket))
    src = pathlib.Path(args.src) if args.src else work_dir(args.sha)
    if not (src / "asset.json").exists() or read_json(src / "asset.json")["asset_sha256"] != args.sha:
        raise SystemExit(f"{src} is not the work dir of {args.sha}")
    print(json.dumps(gcs.push_dir(bkt, src, f"work/{args.sha}")))
    return 0
