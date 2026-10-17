"""Shared utilities: paths, JSON writes (atomic + canonical), sha256, timestamp parsing."""
from __future__ import annotations

import hashlib
import json
import os
import pathlib
import re
import tempfile

REPO = pathlib.Path(__file__).resolve().parent.parent
CACHE = pathlib.Path(os.environ["CEDAR_CACHE"]) if os.environ.get("CEDAR_CACHE") else REPO / ".cache"   # separate worktrees can point at their own cache
DDL_PATH = REPO / "schema" / "knowledge_pack_v1.sql"
CATALOG_PATH = REPO / "catalog" / "catalog.json"
TITLES_DIR = pathlib.Path(__file__).resolve().parent / "titles"


def work_dir(sha: str) -> pathlib.Path:
    return CACHE / "ingest" / sha


def sha256_file(path, bufsize: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(bufsize)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def canonical_json(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def fingerprint(obj) -> str:
    return sha256_bytes(canonical_json(obj).encode())


def write_json(path, obj, indent: int | None = 1) -> None:
    """Atomic write; sorted keys so the same content yields the same bytes."""
    path = pathlib.Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(obj, ensure_ascii=False, indent=indent, sort_keys=True) + "\n"
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(data)
    os.replace(tmp, path)


def read_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def parse_ts(s) -> float | None:
    """'MM:SS', 'MM:SS.s', 'H:MM:SS' or a number → seconds; empty → None."""
    if s is None:
        return None
    if isinstance(s, (int, float)):
        return float(s)
    s = s.strip()
    if not s:
        return None
    if re.fullmatch(r"\d+(\.\d+)?", s):
        return float(s)
    parts = [float(x) for x in s.split(":")]
    if len(parts) == 2:
        return parts[0] * 60 + parts[1]
    if len(parts) == 3:
        return parts[0] * 3600 + parts[1] * 60 + parts[2]
    raise ValueError(f"bad timestamp {s!r}")


def mmss(t: float) -> str:
    t = max(0.0, float(t))
    return f"{int(t) // 60:02d}:{t - (int(t) // 60) * 60:04.1f}"


def r3(x: float | None) -> float | None:
    return None if x is None else round(float(x), 3)
