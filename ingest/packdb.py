"""pack.db ⇄ pack.json (spec §8): create from DDL, deterministic write, read back, round-trip.

In-memory `pack`: {"pack_meta": {k: v}, "asset": {...}, "<table>": [row, ...]} (same as pack.json).
`embeddings` and `text_fts` are not exported to JSON; the caller supplies both when writing the db (`text_fts` is derived, `embeddings` come from the sidecar, see `ingest/embed.py`).
"""
from __future__ import annotations

import functools
import os
import pathlib
import sqlite3
import tempfile

from ingest.util import DDL_PATH

NO_JSON = {"embeddings", "text_fts"}


@functools.lru_cache(maxsize=1)
def ddl() -> str:
    return DDL_PATH.read_text(encoding="utf-8")


@functools.lru_cache(maxsize=1)
def schema_info() -> dict:
    """{table: {"cols": [...], "pk": [...], "notnull": set, "types": {...}}}, in DDL order."""
    db = sqlite3.connect(":memory:")
    db.executescript(ddl())
    out = {}
    for (name,) in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'text_fts%' "
                              "AND name NOT LIKE 'sqlite_%' ORDER BY rowid"):
        info = db.execute(f"PRAGMA table_info({name})").fetchall()
        pk = [r[1] for r in sorted((r for r in info if r[5]), key=lambda r: r[5])]
        out[name] = {"cols": [r[1] for r in info], "pk": pk, "notnull": {r[1] for r in info if r[3]},
                     "types": {r[1]: r[2] for r in info}}
    db.close()
    return out


def tables() -> list[str]:
    return list(schema_info())


def row_key(table: str, row: dict) -> tuple:
    info = schema_info()[table]
    cols = info["pk"] or info["cols"]
    return tuple("" if row.get(c) is None else row.get(c) for c in cols)


def _sort_key(table: str):
    def k(row):
        return tuple((0, v) if isinstance(v, (int, float)) else (1, str(v)) for v in row_key(table, row))
    return k


def empty_pack() -> dict:
    p = {t: [] for t in tables() if t not in ("pack_meta", "asset")}
    p["pack_meta"] = {}
    p["asset"] = {}
    return p


def _coerce(v, typ: str):
    if v is None:
        return None
    if typ == "REAL" and isinstance(v, (int, float)) and not isinstance(v, bool):
        return float(v)
    if typ == "INTEGER" and isinstance(v, (bool, float)) and float(v).is_integer():
        return int(v)
    return v


def _row(table: str, r: dict) -> dict:
    info = schema_info()[table]
    return {c: _coerce(r.get(c), info["types"][c]) for c in info["cols"]}


def normalize_pack(pack: dict) -> dict:
    """Fill missing columns with None, drop unknown keys, coerce numbers to column types, sort by primary key: same content, same serialization."""
    info = schema_info()
    out = {"pack_meta": {k: str(v) for k, v in sorted(pack.get("pack_meta", {}).items())},
           "asset": _row("asset", pack.get("asset", {}))}
    for t in tables():
        if t in ("pack_meta", "asset") or t in NO_JSON:
            continue
        rows = [_row(t, r) for r in pack.get(t, [])]
        out[t] = sorted(rows, key=_sort_key(t))
    return out


def write_db(pack: dict, path: pathlib.Path, fts_rows: list[dict] | None = None,
             embeddings: list[dict] | None = None) -> None:
    """Create and populate the db; writes a temp file, then renames. Failures (constraints / foreign keys) raise sqlite3 errors."""
    pack = normalize_pack(pack)
    path = pathlib.Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".pack.", suffix=".db")
    os.close(fd)
    os.unlink(tmp)
    db = sqlite3.connect(tmp)
    try:
        db.execute("PRAGMA journal_mode=DELETE")
        db.executescript(ddl())
        db.execute("PRAGMA foreign_keys=ON")
        info = schema_info()
        with db:
            for k, v in pack["pack_meta"].items():
                db.execute("INSERT INTO pack_meta(key, value) VALUES (?, ?)", (k, v))
            if any(v is not None for v in pack["asset"].values()):
                cols = info["asset"]["cols"]
                db.execute(f"INSERT INTO asset({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                           [pack["asset"][c] for c in cols])
            for t in insert_order():
                cols = info[t]["cols"]
                sql = f"INSERT INTO {t}({','.join(cols)}) VALUES ({','.join('?' * len(cols))})"
                for r in pack[t]:
                    db.execute(sql, [r[c] for c in cols])
            for r in fts_rows or []:
                db.execute("INSERT INTO text_fts(item_type, item_id, t_s, lang, text) VALUES (?,?,?,?,?)",
                           (r["item_type"], r["item_id"], r["t_s"], r["lang"], r["text"]))
            for r in sorted(embeddings or [], key=lambda r: (r["item_type"], r["item_id"], r["model"])):
                db.execute("INSERT INTO embeddings(item_type, item_id, model, dim, vector) VALUES (?,?,?,?,?)",
                           (r["item_type"], r["item_id"], r["model"], r["dim"], r["vector"]))
        bad = db.execute("PRAGMA foreign_key_check").fetchall()
        if bad:
            raise sqlite3.IntegrityError(f"foreign_key_check: {bad[:5]}")
        if pack["pack_meta"].get("distribution") == "device":
            # spec §2: device packs carry no review_log (not even an empty table; the online service refuses to load a pack that has it)
            if pack["review_log"]:
                raise ValueError("device pack must not carry review_log rows")
            db.execute("DROP TABLE review_log")
        db.execute("VACUUM")
        db.close()
        os.replace(tmp, path)
    except BaseException:
        db.close()
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def insert_order() -> list[str]:
    """Parent tables before child tables (foreign keys)."""
    first = ["title_info", "shots", "segments", "subtitle_tracks", "cues", "characters"]
    rest = [t for t in tables() if t not in first and t not in ("pack_meta", "asset") and t not in NO_JSON]
    return first + rest


def read_db(path: pathlib.Path) -> dict:
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    info = schema_info()
    pack = {"pack_meta": {r["key"]: r["value"] for r in db.execute("SELECT key, value FROM pack_meta")}}
    a = db.execute("SELECT * FROM asset").fetchone()
    pack["asset"] = dict(a) if a else {c: None for c in info["asset"]["cols"]}
    present = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    for t in tables():
        if t in ("pack_meta", "asset") or t in NO_JSON:
            continue
        pack[t] = [dict(r) for r in db.execute(f"SELECT * FROM {t}")] if t in present else []
    db.close()
    return normalize_pack(pack)


def read_fts(path: pathlib.Path) -> list[dict]:
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    rows = [dict(zip(("item_type", "item_id", "t_s", "lang", "text"), r))
            for r in db.execute("SELECT item_type, item_id, t_s, lang, text FROM text_fts")]
    db.close()
    return rows


def read_embeddings(path: pathlib.Path) -> list[dict]:
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    rows = [dict(zip(("item_type", "item_id", "model", "dim", "vector"), r))
            for r in db.execute("SELECT item_type, item_id, model, dim, vector FROM embeddings ORDER BY item_type, item_id, model")]
    db.close()
    return rows
