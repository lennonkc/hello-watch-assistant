"""GCS I/O: directory mirroring (only changed objects are transferred), append-only publishing, JSON objects. Credentials via ADC."""
from __future__ import annotations

import base64
import json
import os
import pathlib

ENV_BUCKET = "CEDAR_INGEST_BUCKET"


class ImmutableConflict(RuntimeError):
    """An object with different content already exists at an immutable path."""


def bucket_name(arg: str | None = None) -> str:
    name = arg or os.environ.get(ENV_BUCKET)
    if not name:
        raise SystemExit(f"bucket not set: pass --bucket or set {ENV_BUCKET}")
    return name.removeprefix("gs://").rstrip("/")


def bucket(name: str):
    from google.cloud import storage
    return storage.Client().bucket(name)


def crc32c_b64(path: pathlib.Path) -> str:
    """GCS-style crc32c (base64, big-endian). Always compare by crc32c: composite objects from parallel chunked uploads have no md5."""
    import google_crc32c
    h = google_crc32c.Checksum()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return base64.b64encode(h.digest()).decode()


CHUNK = 8 << 20      # large files use chunked resumable upload
TIMEOUT = 600
WORKERS = 16         # many small objects (thumbnails etc.), transfer concurrently


def _parallel(fn, items) -> None:
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(WORKERS) as ex:
        for _ in ex.map(fn, items):
            pass


def upload_file(bkt, name: str, path: pathlib.Path, **kw) -> None:
    b = bkt.blob(name, chunk_size=CHUNK if path.stat().st_size > CHUNK else None)
    b.upload_from_filename(str(path), timeout=TIMEOUT, **kw)


def uri(bkt, name: str) -> str:
    return f"gs://{bkt.name}/{name}"


def list_crc(bkt, prefix: str) -> dict[str, str]:
    """All objects under prefix → {relative path: crc32c (base64)}."""
    prefix = prefix.rstrip("/") + "/"
    return {b.name[len(prefix):]: b.crc32c for b in bkt.client.list_blobs(bkt, prefix=prefix)}


def local_files(root: pathlib.Path) -> list[str]:
    return sorted(p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file() and not p.is_symlink())


def plan_mirror(local: dict[str, str], remote: dict[str, str]) -> tuple[list[str], list[str]]:
    """Local {rel: crc32c} vs remote {rel: crc32c} → (to upload, to delete). Identical content is not re-sent, so a versioned bucket does not pile up old generations."""
    up = sorted(r for r, m in local.items() if remote.get(r) != m)
    rm = sorted(r for r in remote if r not in local)
    return up, rm


def push_dir(bkt, src: pathlib.Path, prefix: str, delete: bool = True) -> dict:
    """Mirror a local dir to `prefix/` (extra remote objects are deleted; still recoverable in a versioned bucket)."""
    prefix = prefix.rstrip("/")
    local = {r: crc32c_b64(src / r) for r in local_files(src)}
    up, rm = plan_mirror(local, list_crc(bkt, prefix))
    _parallel(lambda r: upload_file(bkt, f"{prefix}/{r}", src / r), up)
    if delete:
        _parallel(lambda r: bkt.blob(f"{prefix}/{r}").delete(), rm)
    return {"uploaded": len(up), "deleted": len(rm) if delete else 0, "unchanged": len(local) - len(up)}


def pull_dir(bkt, prefix: str, dest: pathlib.Path) -> int:
    """Download every object under `prefix/` into dest (local files with matching crc32c are skipped). Returns the download count."""
    prefix = prefix.rstrip("/") + "/"
    todo = []
    for b in bkt.client.list_blobs(bkt, prefix=prefix):
        rel = b.name[len(prefix):]
        if not rel or rel.endswith("/"):
            continue
        p = dest / rel
        if p.is_file() and crc32c_b64(p) == b.crc32c:
            continue
        p.parent.mkdir(parents=True, exist_ok=True)
        todo.append((b, p))
    _parallel(lambda bp: bp[0].download_to_filename(str(bp[1]), timeout=TIMEOUT), todo)
    return len(todo)


def publish_dir(bkt, src: pathlib.Path, prefix: str) -> dict:
    """Append-only: create an object only if absent (if_generation_match=0); an existing one must match crc32c, else ImmutableConflict."""
    from google.api_core.exceptions import PreconditionFailed
    prefix = prefix.rstrip("/")
    remote = list_crc(bkt, prefix)

    def one(r: str) -> bool:
        m = crc32c_b64(src / r)
        if r in remote:
            if remote[r] != m:
                raise ImmutableConflict(f"{uri(bkt, prefix + '/' + r)} exists with different content")
            return False
        try:
            upload_file(bkt, f"{prefix}/{r}", src / r, if_generation_match=0)
            return True
        except PreconditionFailed:
            if bkt.get_blob(f"{prefix}/{r}").crc32c != m:
                raise ImmutableConflict(f"{uri(bkt, prefix + '/' + r)} raced with different content")
            return False

    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(WORKERS) as ex:
        created = sum(ex.map(one, local_files(src)))
    return {"created": created, "unchanged": len(local_files(src)) - created}


def read_json(bkt, name: str):
    b = bkt.get_blob(name)
    return None if b is None else json.loads(b.download_as_bytes())


def write_json(bkt, name: str, obj, if_generation_match: int | None = None) -> None:
    data = json.dumps(obj, ensure_ascii=False, indent=1, sort_keys=True) + "\n"
    kw = {} if if_generation_match is None else {"if_generation_match": if_generation_match}
    bkt.blob(name).upload_from_string(data, content_type="application/json", **kw)
