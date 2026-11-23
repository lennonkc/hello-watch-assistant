"""One-off operations behind `tools/ops/run.sh job` (`JOB=<name>` in tools/ops/params.env; see tools/ops/README.md).

    inspect-packs          what exists for each film (ingest cache, device packs, bucket prefixes) and pack health
    publish-public-packs   fixed master packs of the public films → gs://<bucket>/<PACKS_PREFIX>/<sha>/master/
    publish-private-title  thumbnails, pack and a low-bitrate preview copy of each private preview title, then the
                           private titles file (gs://<bucket>/<PRIVATE_PREFIX>/titles.json)

Job logs are read by people: every job prints at most ~75 lines, and nothing about a private preview title beyond what
the job description promises (no title, synopsis or file names). Bucket operations go through the `gcloud` CLI (the
runner's own login), so no application-default credentials are needed.

Environment (exported by run.sh / job.sh): OPS_ROOT, BUCKET, PROJECT, SERVICE_ACCOUNT, HELLO_WATCH_INGEST_CACHE,
HELLO_WATCH_DEVICE_PACKS_DIR, HELLO_WATCH_MOVIES_DIR, and the job parameters PACKS_PREFIX, PRIVATE_PREFIX,
PRIVATE_TITLE_SHAS, PRIVATE_ACCESS_SHA256.
"""
from __future__ import annotations

import collections
import hashlib
import json
import os
import pathlib
import re
import shutil
import sqlite3
import statistics
import subprocess
import sys
import tempfile
import time

ROOT = pathlib.Path(os.environ.get("OPS_ROOT") or pathlib.Path(__file__).resolve().parents[2])
WORK = ROOT / ".cache" / "ops-work"
LIVE_PREFIX = "demo-packs"                 # what production serves today: read only, never written by a job
DISTS = ("master", "device-auto", "device")
HEX64 = re.compile(r"^[0-9a-f]{64}$")
VIDEO_EXT = {".mp4", ".m4v", ".mov", ".mkv", ".avi", ".webm"}
LS_ROW = re.compile(r"\s*(\d+)\s+(\S+)\s+(gs://\S+)\s*$")
SKIP_TABLES = {"review_log", "embeddings", "pack_meta"}
PLACEHOLDERS = {"{oNN}": re.compile(r"\{\s*o\d{1,3}\s*\}"), "{other}": re.compile(r"\{\s*other\s*\}", re.I),
                "{cNN}": re.compile(r"\{\s*c\d{1,3}\s*\}")}
ANY_BRACE = re.compile(r"\{[^{}\s]{1,24}\}")


class OpsError(RuntimeError):
    pass


def env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


def env_path(name: str) -> pathlib.Path | None:
    v = env(name)
    return pathlib.Path(v).expanduser() if v else None


def say(s: str = "") -> None:
    print(s, flush=True)


def human(n: float) -> str:
    for u in ("B", "KB", "MB", "GB"):
        if n < 1024 or u == "GB":
            return f"{int(n)} B" if u == "B" else f"{n:.1f} {u}"
        n /= 1024.0
    return ""


def hms(t) -> str:
    if t is None:
        return "-"
    t = int(round(float(t)))
    return f"{t // 3600}:{t // 60 % 60:02d}:{t % 60:02d}"


def norm_prefix(v: str) -> str:
    """`./demo-packs/`, `//demo-packs` → `demo-packs` (one spelling per prefix, so the guard cannot be sidestepped)."""
    parts = [x for x in v.strip().split("/") if x not in ("", ".")]
    return "/".join(parts)


def live_prefixes() -> set[str]:
    """What production serves: the literal default, plus the prefix of LIVE_PACKS_GCS (exported by run.sh from the
    revision that has the traffic: after a promote that is demo-packs-next, not demo-packs)."""
    out = {LIVE_PREFIX}
    live = env("LIVE_PACKS_GCS")
    if live.startswith("gs://"):
        name, _, prefix = live[5:].partition("/")
        if prefix and (not env("BUCKET") or name == env("BUCKET")):
            out.add(norm_prefix(prefix))
    return out


def prefix_param(name: str, required: bool = True) -> str:
    raw = env(name)
    v = norm_prefix(raw)
    if not v:
        if required:
            raise OpsError(f"set {name} in tools/ops/params.env")
        return ""
    if raw.strip().startswith("gs:") or ".." in v.split("/") or not re.fullmatch(r"[A-Za-z0-9._/-]+", v):
        raise OpsError(f"{name} must be a path inside the bucket, e.g. demo-packs-next (got {raw!r})")
    for live in live_prefixes():
        if v == live or v.startswith(live + "/"):
            raise OpsError(f"{name} must not be the prefix production serves ({live!r})")
    return v


def private_shas() -> list[str]:
    shas = [s.strip().lower() for s in re.split(r"[\s,]+", env("PRIVATE_TITLE_SHAS")) if s.strip()]
    bad = [s for s in shas if not HEX64.match(s)]
    if bad:
        raise OpsError(f"PRIVATE_TITLE_SHAS: not a sha256: {bad[0][:16]}…")
    return shas


# ── bucket (gcloud CLI) ──────────────────────────────────────────────

def bucket() -> str:
    b = env("BUCKET").removeprefix("gs://").strip("/")
    if not b:
        raise OpsError("bucket unknown (the service has no CEDAR_INGEST_BUCKET, or gcloud cannot describe it)")
    return b


def gcloud(*args: str, timeout: int = 3600) -> subprocess.CompletedProcess:
    if not shutil.which("gcloud"):
        raise OpsError("gcloud not found")
    return subprocess.run(["gcloud", *args], capture_output=True, text=True, timeout=timeout)


def _err(r: subprocess.CompletedProcess) -> str:
    lines = [ln for ln in (r.stderr or r.stdout or "").splitlines() if ln.strip()]
    return lines[-1][:200] if lines else f"exit {r.returncode}"


def gls(url: str) -> list[tuple[int, str, str]]:
    """[(bytes, date, url)] of every object under `url` (recursive); [] when nothing matches."""
    r = gcloud("storage", "ls", "-l", "-r", url)
    if r.returncode:
        if "matched no objects" in (r.stderr + r.stdout) or "not found" in (r.stderr + r.stdout).lower():
            return []
        raise OpsError(f"gcloud storage ls {url}: {_err(r)}")
    out = []
    for line in r.stdout.splitlines():
        m = LS_ROW.match(line)
        if m and not m.group(3).endswith("/"):
            out.append((int(m.group(1)), m.group(2)[:10], m.group(3)))
    return out


def gcp(src: str, dst: str) -> None:
    r = gcloud("storage", "cp", "--no-user-output-enabled", src, dst)
    if r.returncode:
        raise OpsError(f"gcloud storage cp {src.rsplit('/', 1)[-1]}: {_err(r)}")


def group_listing(rows: list[tuple[int, str, str]], base: str) -> dict:
    """{(sha, dist): {"pack.db": (bytes, date), "MANIFEST.json": …, "thumbs": n, "bytes": n}} for `base/<sha>/<dist>/…`."""
    groups: dict = {}
    for size, date, url in rows:
        parts = url[len(base) + 1:].split("/")
        if len(parts) < 3:
            continue
        g = groups.setdefault((parts[0], parts[1]), {"thumbs": 0, "bytes": 0, "other": 0})
        g["bytes"] += size
        name = "/".join(parts[2:])
        if name in ("pack.db", "MANIFEST.json", "pack.json"):
            g[name] = (size, date)
        elif name.startswith("thumbs/"):
            g["thumbs"] += 1
        else:
            g["other"] += 1
    return groups


def listing_line(g: dict | None) -> str:
    if not g:
        return "-"
    db = g.get("pack.db")
    man = g.get("MANIFEST.json")
    return (f"pack.db {human(db[0]) + ' ' + db[1] if db else '-'} · MANIFEST {man[1] if man else '-'} · "
            f"thumbs {g['thumbs']} · {human(g['bytes'])}")


# ── local packs ──────────────────────────────────────────────────────

def cache_root() -> pathlib.Path | None:
    """The ingest cache (the directory holding `ingest/<sha>/`), whether the setting names it or its `ingest/`."""
    c = env_path("HELLO_WATCH_INGEST_CACHE")
    if c is None:
        return None
    if (c / "ingest").is_dir():
        return c
    if c.name == "ingest":
        return c.parent
    return c


def cache_wd(sha: str) -> pathlib.Path | None:
    c = cache_root()
    if c is None:
        return None
    for d in (c / "ingest" / sha, c / sha):
        if d.is_dir():
            return d
    return None


def device_pack_dirs(sha: str) -> list[pathlib.Path]:
    base = env_path("HELLO_WATCH_DEVICE_PACKS_DIR")
    if base is None or not (base / sha).is_dir():
        return []
    d = base / sha
    found = [d] if (d / "pack.db").exists() else []
    for sub in sorted(d.iterdir()):
        if sub.is_dir() and (sub / "pack.db").exists():
            found.append(sub)
    return found


def read_manifest(d: pathlib.Path) -> dict:
    p = d / "MANIFEST.json"
    try:
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    except (OSError, ValueError):
        return {"_error": "unreadable"}


def open_db(path: pathlib.Path) -> sqlite3.Connection:
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    return db


def db_tables(db: sqlite3.Connection) -> list[str]:
    return [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' "
                                     "AND name NOT LIKE 'text_fts_%' ORDER BY name")]


def meta(db: sqlite3.Connection) -> dict:
    try:
        return {r[0]: r[1] for r in db.execute("SELECT key, value FROM pack_meta")}
    except sqlite3.Error:
        return {}


def health(path: pathlib.Path) -> dict:
    """Raw placeholders left in text, blank cues, cue order: what the re-export has to fix."""
    db = open_db(path)
    ph = collections.Counter()
    where = collections.Counter()
    braces: list[tuple[str, str, str]] = []           # (table.column, token, context) of other {…} tokens
    for t in db_tables(db):
        if t in SKIP_TABLES or t == "text_fts":
            continue
        cols = [c[1] for c in db.execute(f"PRAGMA table_info({t})") if (c[2] or "").upper() == "TEXT"]
        if not cols:
            continue
        for row in db.execute(f"SELECT {','.join(cols)} FROM {t}"):
            for col, v in zip(cols, row):
                if not v or "{" not in v:
                    continue
                hit = 0
                for k, rx in PLACEHOLDERS.items():
                    n = len(rx.findall(v))
                    ph[k] += n
                    hit += n
                others = [m for m in ANY_BRACE.finditer(v)
                          if not any(rx.fullmatch(m.group(0)) for rx in PLACEHOLDERS.values())]
                ph["{…}"] += len(others)
                for m in others:
                    a, b = max(0, m.start() - 30), min(len(v), m.end() + 30)
                    braces.append((f"{t}.{col}", m.group(0), v[a:b].replace("\n", " ")))
                if hit:
                    where[t] += hit
    blank = noword = inv = 0
    has_cues = "cues" in db_tables(db)
    if has_cues:
        last: dict = {}
        rows = db.execute("SELECT cue_id, track, start_s, text FROM cues").fetchall()
        for r in sorted(rows, key=lambda r: (r["track"], _cue_no(r["cue_id"]))):
            txt = r["text"] or ""
            if not txt.strip():
                blank += 1
            elif not re.sub(r"[\W_]+", "", txt):
                noword += 1
            if r["track"] in last and r["start_s"] < last[r["track"]] - 1e-6:
                inv += 1
            last[r["track"]] = r["start_s"]
    m = meta(db)
    db.close()
    return {"ph": ph, "where": where, "blank": blank, "noword": noword, "inversions": inv, "meta": m,
            "braces": braces}


# Columns the online service never puts into an answer, evidence or the model's context (ids, provenance, geometry)
INTERNAL_COLS = re.compile(r"(^|_)(id|source|bbox|path|sha256|method|diar_label|review_state|field|models|url|"
                           r"alignment_note|license|track|kind|lang|match|term|person_ref|ref_type|ref_id)$")


def brace_lines(h: dict, limit: int = 4) -> list[str]:
    """Where the other {…} tokens are and whether a viewer could see them."""
    by = collections.Counter((col, tok) for col, tok, _ctx in h["braces"])
    out = []
    for (col, tok), n in by.most_common(limit):
        ctx = next(c for cc, t, c in h["braces"] if cc == col and t == tok)
        seen = "internal column, never shown" if INTERNAL_COLS.search(col.split(".", 1)[1]) else \
            "text the service can show (answers / evidence)"
        out.append(f"    {tok} ×{n} in {col} ({seen}): …{ctx}…")
    if len(by) > limit:
        out.append(f"    … {len(by) - limit} more kinds")
    return out


def _cue_no(cue_id: str):
    tail = cue_id.rsplit(":", 1)[-1]
    return (0, int(tail), "") if tail.isdigit() else (1, 0, tail)


def health_line(h: dict) -> str:
    ph = h["ph"]
    m = h["meta"]
    ver = f"v{m.get('pack_version', '?')} {m.get('built_at', '')[:10]} {m.get('distribution', '?')}"
    tops = ",".join(f"{t}:{n}" for t, n in h["where"].most_common(3))
    return (f"{ver} · placeholders oNN {ph['{oNN}']} other {ph['{other}']} cNN {ph['{cNN}']} "
            f"(+{ph['{…}']} other braces){' [' + tops + ']' if tops else ''} · cues blank {h['blank']} "
            f"no-letters {h['noword']} out-of-order {h['inversions']}")


# ── catalog ──────────────────────────────────────────────────────────

def public_films() -> list[dict]:
    cat = json.loads((ROOT / "catalog" / "catalog.json").read_text(encoding="utf-8"))
    out = []
    for it in cat["items"]:
        spec = json.loads((ROOT / "ingest" / "titles" / f"{it['id']}.json").read_text(encoding="utf-8"))
        out.append({"id": it["id"], "title": it["title"], "sha": spec["expected_asset_sha256"][0]})
    return out


def repo_roots() -> list[pathlib.Path]:
    """Git checkouts holding the ingest cache and the device packs (walk up at most 6 levels)."""
    roots = []
    for name in ("HELLO_WATCH_INGEST_CACHE", "HELLO_WATCH_DEVICE_PACKS_DIR"):
        p = env_path(name)
        for _ in range(7):
            if p is None or p == p.parent:
                break
            if (p / ".git").exists():
                if p not in roots:
                    roots.append(p)
                break
            p = p.parent
    return roots


PRUNE = {".git", "node_modules", ".cache", ".venv", "venv", "__pycache__", "build", ".gradle", "Pods", "DerivedData",
         "thumbs", ".worktrees", "worktrees", "packs"}


def json_files(root: pathlib.Path, max_depth: int = 5, max_files: int = 6000):
    n = 0
    base = len(root.parts)
    for d, dirs, files in os.walk(root):
        depth = len(pathlib.Path(d).parts) - base
        dirs[:] = [x for x in sorted(dirs) if x not in PRUNE and not x.startswith(".") and depth < max_depth]
        for f in sorted(files):
            if f.endswith(".json"):
                n += 1
                if n > max_files:
                    return
                yield pathlib.Path(d) / f


def _items_with(obj, needles: set[str], path: str = ""):
    """Dicts inside `obj` that hold one of `needles` as a value (directly or in a list of strings)."""
    if isinstance(obj, dict):
        vals = list(obj.values())
        if any(v in needles for v in vals if isinstance(v, str)) or any(
                isinstance(v, list) and any(x in needles for x in v if isinstance(x, str)) for v in vals):
            yield path or "/", obj
        for k, v in obj.items():
            if isinstance(v, (dict, list)):
                yield from _items_with(v, needles, f"{path}/{k}")
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            if isinstance(v, (dict, list)):
                yield from _items_with(v, needles, f"{path}[{i}]")
    # a dict keyed by the id itself ({"<id>": {...}}) is caught below by the caller


def catalog_refs(needles: set[str]) -> list[tuple[pathlib.Path, str, list[str]]]:
    """[(file, where in the file, field names)] of local catalog JSON entries that mention one of `needles`."""
    hits = []
    raw = [n.encode() for n in needles]
    for root in repo_roots():
        for f in json_files(root):
            try:
                if f.stat().st_size > 30 << 20:
                    continue
                data = f.read_bytes()
            except OSError:
                continue
            if not any(n in data for n in raw):
                continue
            try:
                doc = json.loads(data)
            except ValueError:
                hits.append((f, "(not JSON)", []))
                continue
            found = list(_items_with(doc, needles))
            if isinstance(doc, dict):
                for k, v in doc.items():
                    if isinstance(v, dict) and k in needles:
                        found.append((f"/{k}", v))
                for k in ("items", "titles", "entries"):
                    if isinstance(doc.get(k), dict):
                        for kk, v in doc[k].items():
                            if kk in needles and isinstance(v, dict):
                                found.append((f"/{k}/{kk}", v))
            for where, item in found[:3]:
                hits.append((f, where, sorted(item.keys())))
            if not found:
                hits.append((f, "(mentioned outside an object)", []))
    return hits


def library_ids(sha: str) -> list[str]:
    """Catalog ids the ingest library (`ingest/library.json` in the cache) registered for `sha`."""
    c = cache_root()
    lib = c / "ingest" / "library.json" if c else None
    try:
        assets = json.loads(lib.read_text(encoding="utf-8")).get("assets", []) if lib and lib.exists() else []
    except (OSError, ValueError):
        return []
    return [a["catalog_id"] for a in assets if a.get("asset_sha256") == sha and a.get("catalog_id")]


# ── film files ───────────────────────────────────────────────────────

def sha256_file(p: pathlib.Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        while chunk := f.read(8 << 20):
            h.update(chunk)
    return h.hexdigest()


def find_film(sha: str, size: int | None) -> tuple[pathlib.Path | None, str]:
    """The film file under HELLO_WATCH_MOVIES_DIR whose sha256 is `sha`: candidates are the file the ingest library
    registered for it and files of the same size as the bucket copy; every candidate is hashed before it counts."""
    movies = env_path("HELLO_WATCH_MOVIES_DIR")
    if movies is None or not movies.is_dir():
        return None, "HELLO_WATCH_MOVIES_DIR not set or missing"
    named: list[pathlib.Path] = []
    c = cache_root()
    lib = c / "ingest" / "library.json" if c else None
    if lib and lib.exists():
        try:
            for a in json.loads(lib.read_text(encoding="utf-8")).get("assets", []):
                if a.get("asset_sha256") == sha and a.get("file"):
                    named.append(movies / a["file"])
                    size = size or a.get("bytes")
        except (OSError, ValueError):
            pass
    cands = [p for p in named if p.is_file()]
    base = len(movies.parts)
    for d, dirs, files in os.walk(movies):
        dirs[:] = [x for x in dirs if not x.startswith(".") and len(pathlib.Path(d).parts) - base < 3]
        for f in files:
            p = pathlib.Path(d) / f
            if p.suffix.lower() in VIDEO_EXT and p not in cands:
                try:
                    if size and p.stat().st_size == size:
                        cands.append(p)
                except OSError:
                    pass
    if not cands and not size:
        return None, "not checked: size unknown (no bucket copy, no ingest library entry)"
    for p in cands[:8]:
        if sha256_file(p) == sha:
            return p, f"found under HELLO_WATCH_MOVIES_DIR ({human(p.stat().st_size)}, sha256 verified)"
    return None, f"not found under HELLO_WATCH_MOVIES_DIR ({len(cands)} candidate(s) of the right size hashed)"


# ── inspect-packs ────────────────────────────────────────────────────

def fetch_pack(url_dir: str, dest: pathlib.Path) -> pathlib.Path | None:
    dest.mkdir(parents=True, exist_ok=True)
    try:
        gcp(f"{url_dir}/pack.db", str(dest / "pack.db"))
    except OpsError:
        return None
    try:
        gcp(f"{url_dir}/MANIFEST.json", str(dest / "MANIFEST.json"))
    except OpsError:
        pass
    return dest / "pack.db"


def describe(path: pathlib.Path, bucket_thumbs: int | None) -> list[str]:
    """Detail of one pack for a private preview title (counts and labels only; no text of the film)."""
    d = path.parent
    man = read_manifest(d)
    db = open_db(path)
    tabs = db_tables(db)
    count = {t: db.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in tabs}
    m = meta(db)
    a = dict(db.execute("SELECT * FROM asset").fetchone() or {})
    L = []
    flags = {k: man.get(k) for k in ("schema_version", "pack_version", "distribution", "built_at", "review_complete",
                                     "allow_auto") if k in man}
    L.append(f"  manifest: {flags or '(none)'} files={len(man.get('files', []))}")
    L.append(f"  pack_meta: dist={m.get('distribution')} v{m.get('pack_version')} built={m.get('built_at')} "
             f"review_complete={m.get('review_complete')} generator={m.get('generator')}")
    rows = [f"{t} {n}" for t, n in sorted(count.items()) if t != "pack_meta"]
    for i in range(0, len(rows), 9):
        L.append(("  rows: " if i == 0 else "        ") + ", ".join(rows[i:i + 9]))
    if "embeddings" in tabs:
        em = db.execute("SELECT model, dim, count(*), sum(length(vector) != dim * 4) FROM embeddings "
                        "GROUP BY model, dim").fetchall()
        kinds = db.execute("SELECT item_type, count(*) FROM embeddings GROUP BY item_type ORDER BY 2 DESC").fetchall()
        L.append("  embeddings: " + ("; ".join(f"{r[0]} dim {r[1]} n={r[2]}{f' BAD-LENGTH {r[3]}' if r[3] else ''}"
                                               for r in em) or "none")
                 + (" · by type " + ", ".join(f"{k} {n}" for k, n in kinds) if kinds else ""))
    fts = db.execute("SELECT count(*) FROM text_fts").fetchone()[0] if "text_fts" in tabs else None
    L.append(f"  text_fts: {fts if fts is not None else 'MISSING'} rows")
    L.append(f"  story: duration {hms(a.get('duration_s'))} · story {hms(a.get('story_start_s'))}–"
             f"{hms(a.get('story_end_s'))} · credits {hms(a.get('credits_start_s'))} · kind {a.get('asset_kind')} "
             f"· dialogue {a.get('dialogue_lang')} · derivatives_ok {a.get('derivatives_ok')}")
    if count.get("recaps"):
        ups = sorted(r[0] for r in db.execute("SELECT upto_s FROM recaps"))
        gaps = [b - a_ for a_, b in zip(ups, ups[1:])]
        rs = collections.Counter(r[0] for r in db.execute("SELECT review_state FROM recaps"))
        L.append(f"  recaps: {len(ups)} · first {hms(ups[0])} last {hms(ups[-1])} · gap median "
                 f"{statistics.median(gaps) / 60 if gaps else 0:.1f} min max {max(gaps) / 60 if gaps else 0:.1f} min"
                 f" · {dict(rs)}")
    else:
        L.append("  recaps: none")
    if count.get("cues"):
        dlg = db.execute("SELECT count(*) FROM cues WHERE kind='dialogue'").fetchone()[0]
        sp = db.execute("SELECT count(*) FROM cues c JOIN cue_speakers s ON s.cue_id = c.cue_id "
                        "WHERE c.kind='dialogue' AND s.character_id IS NOT NULL").fetchone()[0] \
            if "cue_speakers" in tabs else 0
        raw = db.execute("SELECT count(*) FROM cues WHERE kind='dialogue' AND speaker_raw IS NOT NULL "
                         "AND speaker_raw != ''").fetchone()[0]
        tracks = db.execute("SELECT track, count(*) FROM cues GROUP BY track").fetchall()
        L.append(f"  dialogue cues {dlg}: with a speaker {sp} ({100.0 * sp / max(dlg, 1):.0f} %), speaker_raw {raw}"
                 f" · tracks " + ", ".join(f"{t} {n}" for t, n in tracks))
        if "cue_speakers" in tabs:
            top = db.execute(
                "SELECT s.character_id, ch.canonical_name, count(*) FROM cue_speakers s JOIN cues c ON c.cue_id = "
                "s.cue_id LEFT JOIN characters ch ON ch.character_id = s.character_id WHERE c.kind='dialogue' "
                "GROUP BY s.character_id ORDER BY 3 DESC LIMIT 10").fetchall()
            L.append("  top speakers: " + (", ".join(f"{r[0] or 'unknown'} {r[1] or ''} {r[2]}".replace("  ", " ")
                                                      for r in top) or "none"))
    if count.get("characters"):
        rows_c = db.execute("SELECT role, name_known_at_s IS NOT NULL OR name_public = 1 FROM characters").fetchall()
        named = sum(1 for r in rows_c if r[1])
        roles = collections.Counter(r[0] for r in rows_c)
        ex = db.execute("SELECT count(DISTINCT character_id) FROM character_exemplars").fetchone()[0] \
            if "character_exemplars" in tabs else 0
        L.append(f"  characters {len(rows_c)}: named {named}, unnamed {len(rows_c) - named} · {dict(roles)} · "
                 f"with exemplars {ex}")
    bl = db.execute("SELECT count(*), count(DISTINCT person_ref) FROM blocklist").fetchone() \
        if "blocklist" in tabs else (0, 0)
    ca = dict(db.execute("SELECT kind, count(*) FROM canaries GROUP BY kind").fetchall()) if "canaries" in tabs else {}
    L.append(f"  blocklist {bl[0]} terms / {bl[1]} people · canaries {sum(ca.values())} {ca}")
    th_rows = count.get("thumbnails", 0)
    th_files = len(list((d / "thumbs").glob("t*.jpg"))) if (d / "thumbs").is_dir() else 0
    L.append(f"  thumbnails: {th_rows} rows · {th_files} files next to the pack"
             f"{f' · {bucket_thumbs} in the bucket' if bucket_thumbs is not None else ''}")
    states = collections.Counter()
    for t in ("characters", "scenes", "events", "shot_captions", "cue_speakers", "character_exemplars"):
        if count.get(t):
            for (s,) in db.execute(f"SELECT review_state FROM {t}"):
                states[s] += 1
    L.append(f"  review states (characters, scenes, events, captions, speakers, exemplars): {dict(states)}")
    db.close()
    for issue in compat_issues(path):
        L.append(f"  server compat: {issue}")
    return L


def compat_issues(path: pathlib.Path) -> list[str]:
    """What the online service reads from a pack and this one lacks (server/packs.py `check_pack`)."""
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    try:
        from server.packs import check_pack
    except ImportError as e:
        return [f"not checked ({e.__class__.__name__}: {e})"]
    return check_pack(path) or ["nothing missing"]


def inspect_packs() -> int:
    films = public_films()
    try:
        b = bucket()
    except OpsError as e:
        b = ""
        say(f"bucket: {e}")
    prefixes = [LIVE_PREFIX]
    pp = prefix_param("PACKS_PREFIX", required=False)
    if pp:
        prefixes.append(pp)
    say(f"ingest cache: {cache_root() or '(HELLO_WATCH_INGEST_CACHE not set)'}")
    if b:
        r = gcloud("storage", "ls", f"gs://{b}/")
        tops = [u.rstrip("/").rsplit("/", 1)[-1] + "/" for u in r.stdout.split() if u.endswith("/")]
        say(f"bucket top level: {' '.join(tops[:20])}{' …' if len(tops) > 20 else ''}")
    listings = {}
    for p in prefixes:
        listings[p] = group_listing(gls(f"gs://{b}/{p}/"), f"gs://{b}/{p}") if b else {}
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="inspect-", dir=_work()))
    try:
        say("")
        say("== public films (candidate packs: ingest cache dists, bucket prefixes)")
        for f in films:
            sha = f["sha"]
            wd = cache_wd(sha)
            say(f"{f['title']} {sha[:12]}")
            if wd is None:
                say("  cache: none")
            else:
                vers = _versions(wd)
                stages = len(list((wd / "stages").glob("*.json"))) if (wd / "stages").is_dir() else 0
                patches = len(list((wd / "patches").glob("*.json"))) if (wd / "patches").is_dir() else 0
                th = len(list((wd / "thumbs").glob("t*.jpg"))) if (wd / "thumbs").is_dir() else 0
                say(f"  cache: versions {vers} · stages {stages} · patches {patches} · thumbs {th}")
                for dist in DISTS:
                    if (wd / dist / "pack.db").exists():
                        say(f"  cache {dist:<11} {health_line(health(wd / dist / 'pack.db'))}")
            for p in prefixes:
                g = listings[p].get((sha, "master"))
                if not g:
                    say(f"  {p}/master: -")
                    continue
                got = fetch_pack(f"gs://{b}/{p}/{sha}/master", tmp / p / sha)
                say(f"  {p}/master: {listing_line(g)}")
                if got:
                    say(f"    {health_line(health(got))}")
        for sha in private_shas():
            say("")
            say(f"== private preview title {sha[:12]}")
            cands: list[tuple[str, pathlib.Path]] = []
            for d in device_pack_dirs(sha):
                cands.append(("device packs dir" + ("" if d.name == sha else f"/{d.name}"), d / "pack.db"))
            wd = cache_wd(sha)
            for dist in DISTS:
                if wd and (wd / dist / "pack.db").exists():
                    cands.append((f"ingest cache/{dist}", wd / dist / "pack.db"))
            dev = env_path("HELLO_WATCH_DEVICE_PACKS_DIR")
            say(f"  device packs dir: {'set' if dev else 'not set'}{'' if not dev or dev.is_dir() else ' (missing)'}"
                f" · {sha[:12]} there: {'yes' if dev and (dev / sha).is_dir() else 'no'} · ingest cache: "
                f"{'yes, ' + str(_versions(wd)) if wd else 'no'}")
            for where, p in cands:
                m = read_manifest(p.parent)
                say(f"  pack @ {where}: dist {m.get('distribution', '?')} v{m.get('pack_version', '?')} "
                    f"{str(m.get('built_at', ''))[:10]} allow_auto={m.get('allow_auto', False)} "
                    f"· {human(p.stat().st_size)}")
            bucket_thumbs = None
            asset_size = None
            if b:
                looked = prefixes + [x for x in [prefix_param("PRIVATE_PREFIX", required=False)] if x]
                n_found = 0
                for p in looked:
                    groups = group_listing(gls(f"gs://{b}/{p}/{sha}/"), f"gs://{b}/{p}")
                    for (_s, dist), g in sorted(groups.items()):
                        say(f"  bucket {p}/{dist}: {listing_line(g)}")
                        bucket_thumbs = max(bucket_thumbs or 0, g["thumbs"])
                        n_found += 1
                if not n_found:
                    say(f"  bucket packs: none under {', '.join(looked)}")
                asset = gls(f"gs://{b}/assets/{sha}.mp4")
                prev = gls(f"gs://{b}/previews/{sha}-640.mp4")
                asset_size = asset[0][0] if asset else None
                say(f"  bucket assets/: {human(asset[0][0]) + ' ' + asset[0][1] if asset else 'MISSING'} · "
                    f"previews/: {human(prev[0][0]) + ' ' + prev[0][1] if prev else 'none'}")
            if cands:
                where, p = cands[0]
                say(f"  detail of the pack @ {where}:")
                for line in describe(p, bucket_thumbs):
                    say(line)
                db = open_db(p)
                row = db.execute("SELECT content_id FROM asset").fetchone()
                db.close()
                content_id = row[0] if row else None
            else:
                say("  no local pack found")
                content_id = None
            _, film = find_film(sha, asset_size)
            say(f"  film file: {film}")
            needles = {sha} | ({content_id} if content_id else set()) | set(library_ids(sha))
            refs = catalog_refs(needles)
            roots = repo_roots()
            say(f"  local catalogs (searched {len(roots)} checkout(s) near the ingest cache / device packs):")
            for path, where, keys in refs[:8]:
                say(f"    {path} {where}: {', '.join(keys)[:160]}")
            if not refs:
                say("    none mention this sha" + (" or its content id" if content_id else ""))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return 0


def _versions(wd: pathlib.Path) -> str:
    p = wd / "versions.json"
    try:
        vs = json.loads(p.read_text(encoding="utf-8"))["versions"] if p.exists() else []
    except (OSError, ValueError, KeyError):
        return "unreadable"
    if not vs:
        return "none"
    return f"{len(vs)} (latest v{vs[-1]['pack_version']} {vs[-1]['built_at'][:10]})"


def _work() -> pathlib.Path:
    WORK.mkdir(parents=True, exist_ok=True)
    return WORK


# ── uploads ──────────────────────────────────────────────────────────

def upload_tree(local: pathlib.Path, dest: str, rels: list[str], last: tuple[str, ...] = ("pack.json", "MANIFEST.json",
                                                                                         "pack.db")) -> dict:
    """Upload `local/<rel>` → `dest/<rel>` for every rel whose object is missing or has another size (resumable).
    Files named in `last` go after everything else, pack.db at the very end, so a server that finds pack.db finds
    the rest too. Returns {"uploaded", "bytes", "skipped", "dest_files", "dest_bytes", "mismatch"}."""
    have = {url[len(dest) + 1:]: size for size, _d, url in gls(dest + "/")}
    size = {r: (local / r).stat().st_size for r in rels}
    # thumbnails by size; the pack files by content (a re-exported pack can have the same size)
    todo = [r for r in rels if have.get(r) != size[r] or (r in last and not same_content(local / r, f"{dest}/{r}"))]
    groups: dict[str, list[str]] = collections.defaultdict(list)
    tail = []
    for r in todo:
        (tail if r in last else groups[str(pathlib.PurePosixPath(r).parent)]).append(r)
    batches = [(d, rs) for d, rs in sorted(groups.items())] + [
        (str(pathlib.PurePosixPath(r).parent), [r]) for r in sorted(tail, key=last.index)]
    for d, rs in batches:
        target = dest + "/" + ("" if d == "." else d + "/")
        r = subprocess.run(["gcloud", "storage", "cp", "--no-user-output-enabled", "-I", target], text=True,
                           input="\n".join(str(local / x) for x in rs) + "\n", capture_output=True, timeout=4 * 3600)
        if r.returncode:
            raise OpsError(f"upload to {target} failed: {_err(r)}")
    after = {url[len(dest) + 1:]: sz for sz, _d, url in gls(dest + "/")}
    mismatch = [r for r in rels if after.get(r) != size[r]
                or (r in last and r in todo and not same_content(local / r, f"{dest}/{r}"))]
    return {"uploaded": len(todo), "bytes": sum(size[r] for r in todo), "skipped": len(rels) - len(todo),
            "dest_files": len(after), "dest_bytes": sum(after.values()), "mismatch": mismatch}


def remote_hashes(url: str) -> dict:
    """{"md5": base64, "crc32c": base64} of one object (empty when it does not exist)."""
    r = gcloud("storage", "objects", "describe", url, "--format=json")
    if r.returncode:
        if "not found" in (r.stderr + r.stdout).lower() or "matched no objects" in (r.stderr + r.stdout):
            return {}
        raise OpsError(f"gcloud storage objects describe: {_err(r)}")
    try:
        d = json.loads(r.stdout)
    except ValueError:
        raise OpsError("gcloud storage objects describe: not JSON") from None
    d = d[0] if isinstance(d, list) and d else d
    return {k: d.get(f"{k}_hash") for k in ("md5", "crc32c") if isinstance(d, dict) and d.get(f"{k}_hash")}


def local_hashes(path: pathlib.Path) -> dict:
    import base64
    md5 = hashlib.md5()
    try:
        import google_crc32c
        crc = google_crc32c.Checksum()
    except ImportError:
        crc = None
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            md5.update(chunk)
            if crc is not None:
                crc.update(chunk)
    out = {"md5": base64.b64encode(md5.digest()).decode()}
    if crc is not None:
        out["crc32c"] = base64.b64encode(crc.digest()).decode()
    return out


def same_content(path: pathlib.Path, url: str) -> bool:
    """The object holds exactly this file: md5 when the bucket has one (not for composite uploads), else crc32c."""
    rh = remote_hashes(url)
    if not rh:
        return False
    lh = local_hashes(path)
    for k in ("md5", "crc32c"):
        if rh.get(k) and lh.get(k):
            return rh[k] == lh[k]
    raise OpsError(f"cannot compare {path.name}: no common hash with the bucket")


def upload_line(u: dict) -> str:
    ok = "matches local (pack files by content hash, the rest by size)" if not u["mismatch"] else \
        f"MISMATCH {len(u['mismatch'])}: {', '.join(u['mismatch'][:3])}"
    return (f"uploaded {u['uploaded']} files ({human(u['bytes'])}), {u['skipped']} already there · destination "
            f"{u['dest_files']} objects, {human(u['dest_bytes'])} · {ok}")


def pack_files(d: pathlib.Path) -> list[str]:
    rels = [n for n in ("pack.db", "MANIFEST.json", "pack.json") if (d / n).is_file()]
    if (d / "thumbs").is_dir():
        rels += [f"thumbs/{p.name}" for p in sorted((d / "thumbs").glob("t*.jpg"))]
    return rels


def sa_can_read(b: str) -> str:
    """Whether the service's account has a bucket-wide read role (covers every prefix)."""
    sa = env("SERVICE_ACCOUNT")
    if not sa:
        return "service account unknown"
    r = gcloud("storage", "buckets", "get-iam-policy", f"gs://{b}", "--format=json")
    if r.returncode:
        return f"cannot read the bucket policy ({_err(r)})"
    roles = sorted({x["role"] for x in json.loads(r.stdout or "{}").get("bindings", [])
                    if f"serviceAccount:{sa}" in x.get("members", []) and not x.get("condition")})
    readers = {"roles/storage.objectViewer", "roles/storage.objectAdmin", "roles/storage.admin",
               "roles/storage.legacyObjectReader"}
    return (f"yes ({', '.join(r for r in roles if r in readers)}, bucket-wide)" if readers & set(roles)
            else f"NO bucket-wide read role (has: {', '.join(roles) or 'none'})")


# ── publish-public-packs ─────────────────────────────────────────────

def publish_public_packs() -> int:
    """The ingest cache's master pack of each public film (pack.db, MANIFEST.json, pack.json, thumbs/) →
    gs://<bucket>/<PACKS_PREFIX>/<sha>/master/, if it has no raw placeholders. Never the live prefix."""
    b, prefix = bucket(), prefix_param("PACKS_PREFIX")
    bad = 0
    for f in public_films():
        sha = f["sha"]
        wd = cache_wd(sha)
        src = wd / "master" if wd else None
        if src is None or not (src / "pack.db").exists():
            say(f"{f['title']} {sha[:12]}: no master pack in the ingest cache — skipped")
            bad += 1
            continue
        h = health(src / "pack.db")
        man = read_manifest(src)
        raw = h["ph"]["{oNN}"] + h["ph"]["{other}"] + h["ph"]["{cNN}"]
        say(f"{f['title']} {sha[:12]}: cache master {health_line(h)}")
        say(f"  manifest v{man.get('pack_version')} built {man.get('built_at')} · {len(man.get('files', []))} files listed")
        for line in brace_lines(h):
            say(line)
        if raw or h["blank"]:
            say(f"  NOT published: {raw} raw placeholders, {h['blank']} blank cues (re-export first)")
            bad += 1
            continue
        rels = pack_files(src)
        listed = {x["path"] for x in man.get("files", [])}
        missing = sorted(listed - set(rels))
        if missing:
            say(f"  NOT published: {len(missing)} files of the manifest are missing locally ({', '.join(missing[:3])})")
            bad += 1
            continue
        u = upload_tree(src, f"gs://{b}/{prefix}/{sha}/master", rels)
        say(f"  → {prefix}/{sha[:12]}/master: {upload_line(u)}")
        bad += bool(u["mismatch"])
    say(f"service account can read gs://{b}/{prefix}/: {sa_can_read(b)}")
    return 1 if bad else 0


# ── publish-private-title ────────────────────────────────────────────

ATTRIBUTION = "Private preview for evaluation. Not for redistribution."
PREVIEW = {"width": 640, "video_kbps": 400, "audio_kbps": 64, "gop_s": 2}
SENTENCE_END = re.compile(r"[.!?](?=\s|$)|[\u3002\uff01\uff1f]")      # . ! ? before a space; 。！？


def short_synopsis(text: str, limit: int = 260) -> str:
    """The synopsis cut at the last sentence end within `limit` characters (else at a word, with …)."""
    t = " ".join((text or "").split())
    if len(t) <= limit:
        return t
    ends = [m.end() for m in SENTENCE_END.finditer(t[:limit])]
    if ends and ends[-1] >= 40:
        return t[:ends[-1]].strip()
    cut = t[:limit - 1].rsplit(" ", 1)[0]
    return cut.rstrip(",;:") + "…"


def local_catalog_entry(sha: str, content_id: str | None) -> dict:
    """Title and synopsis of a private preview title from the Mac's local catalog (never from this repo): the
    `ingest/titles/<catalog id>.json` whose expected_asset_sha256 lists `sha` (its `title`), and that id's item in
    `catalog/catalog.json` (`synopsis_full`). `license_note` is never read."""
    for root in repo_roots():
        if root.resolve() == ROOT.resolve():
            continue
        tdir = root / "ingest" / "titles"
        spec, cid = None, None
        if content_id and (tdir / f"{content_id}.json").is_file():
            cid, spec = content_id, json.loads((tdir / f"{content_id}.json").read_text(encoding="utf-8"))
        elif tdir.is_dir():
            for p in sorted(tdir.glob("*.json")):
                try:
                    d = json.loads(p.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    continue
                if sha in (d.get("expected_asset_sha256") or []):
                    cid, spec = d.get("catalog_id") or p.stem, d
                    break
        if not spec or not spec.get("title"):
            continue
        syn = ""
        cat = root / "catalog" / "catalog.json"
        if cat.is_file():
            items = json.loads(cat.read_text(encoding="utf-8")).get("items", [])
            it = next((i for i in items if i.get("id") == cid), None)
            syn = (it or {}).get("synopsis_full") or ""
        return {"title": spec["title"], "synopsis": short_synopsis(syn), "catalog_id": cid, "root": root}
    raise OpsError("no local catalog entry for this sha (looked for ingest/titles/*.json next to the ingest cache "
                   "and the device packs)")


def best_private_pack(sha: str) -> tuple[str, pathlib.Path, dict]:
    """The newest servable pack (device-auto or master) among the device packs dir and the ingest cache."""
    cands = []
    for d in device_pack_dirs(sha):
        cands.append(("device packs dir", d))
    wd = cache_wd(sha)
    for dist in ("device-auto", "master"):
        if wd and (wd / dist / "pack.db").exists():
            cands.append((f"ingest cache/{dist}", wd / dist))
    best = None
    for where, d in cands:
        m = read_manifest(d)
        if m.get("distribution") not in ("device-auto", "master"):
            continue
        key = (int(m.get("pack_version") or 0), str(m.get("built_at") or ""))
        if best is None or key > best[0]:
            best = (key, where, d, m)
    if best is None:
        raise OpsError("no device-auto or master pack found (device packs dir, ingest cache)")
    return best[1], best[2], best[3]


def ffprobe_json(path: pathlib.Path) -> dict:
    r = subprocess.run(["ffprobe", "-v", "error", "-show_format", "-show_streams", "-of", "json", str(path)],
                       capture_output=True, text=True, timeout=300)
    if r.returncode:
        raise OpsError(f"ffprobe failed: {_err(r)}")
    return json.loads(r.stdout)


def _fps(stream: dict) -> float:
    num, _, den = str(stream.get("avg_frame_rate") or stream.get("r_frame_rate") or "24/1").partition("/")
    try:
        return float(num) / float(den or 1) or 24.0
    except (ValueError, ZeroDivisionError):
        return 24.0


def preview_cmd(src: pathlib.Path, out: pathlib.Path, fps: float) -> list[str]:
    """640 px wide H.264 (about 400 kbps, a keyframe every 2 s, no scene-cut keyframes in between) + 64 kbps stereo
    AAC, faststart; container metadata, chapters and subtitle / data streams dropped (no title in the file)."""
    g = max(1, round(PREVIEW["gop_s"] * fps))
    v = PREVIEW["video_kbps"]
    return ["ffmpeg", "-hide_banner", "-nostdin", "-y", "-i", str(src),
            "-map", "0:v:0", "-map", "0:a:0?", "-sn", "-dn", "-map_metadata", "-1", "-map_chapters", "-1",
            "-vf", f"scale={PREVIEW['width']}:-2", "-c:v", "libx264", "-preset", "medium", "-profile:v", "high",
            "-pix_fmt", "yuv420p", "-b:v", f"{v}k", "-maxrate", f"{int(v * 1.5)}k", "-bufsize", f"{v * 2}k",
            "-g", str(g), "-keyint_min", str(g), "-sc_threshold", "0",
            "-c:a", "aac", "-b:a", f"{PREVIEW['audio_kbps']}k", "-ac", "2",
            "-movflags", "+faststart", "-f", "mp4", "-progress", "pipe:1", "-nostats", str(out)]


def make_preview(sha: str, src: pathlib.Path, duration: float, rebuild: bool = False) -> pathlib.Path:
    out = _work() / f"{sha}-640.mp4"
    if rebuild:                                           # PREVIEW_REBUILD=1: never reuse an earlier local encode
        for old in (out, out.with_name(out.name + ".part")):
            old.unlink(missing_ok=True)
    if out.exists():
        try:
            d = float(ffprobe_json(out)["format"]["duration"])
            if abs(d - duration) < 5:
                say(f"  preview: reusing the local encode ({human(out.stat().st_size)})")
                return out
        except (OpsError, KeyError, ValueError):
            pass
    probe = ffprobe_json(src)
    vs = next((x for x in probe["streams"] if x.get("codec_type") == "video"), None)
    if vs is None:
        raise OpsError("the film file has no video stream")
    fps = _fps(vs)
    est = (PREVIEW["video_kbps"] + PREVIEW["audio_kbps"]) * 1000 / 8 * duration
    say(f"  preview: source {vs.get('width')}x{vs.get('height')} {fps:.3f} fps · target {PREVIEW['width']} px, "
        f"~{PREVIEW['video_kbps']}+{PREVIEW['audio_kbps']} kbps, keyframe every {PREVIEW['gop_s']} s · "
        f"estimate {human(est)}")
    part = out.with_name(out.name + ".part")
    t0 = time.monotonic()
    proc = subprocess.Popen(preview_cmd(src, part, fps), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        _follow_encode(proc, duration, t0)
    except BaseException:                                 # failed or interrupted: no half-written file stays behind
        proc.kill()
        part.unlink(missing_ok=True)
        raise
    os.replace(part, out)
    return _report_preview(out, t0)


def _follow_encode(proc: subprocess.Popen, duration: float, t0: float) -> None:
    err_tail = collections.deque(maxlen=8)
    import threading
    threading.Thread(target=lambda: [err_tail.append(x.rstrip()) for x in proc.stderr], daemon=True).start()
    next_pct = 25
    for line in proc.stdout:
        if line.startswith("out_time_us=") or line.startswith("out_time_ms="):
            try:
                done = int(line.split("=", 1)[1]) / 1e6
            except ValueError:
                continue
            pct = 100 * done / max(duration, 1)
            if pct >= next_pct:
                el = time.monotonic() - t0
                say(f"    {next_pct}% after {el / 60:.1f} min ({done / max(el, 1e-6):.1f}× real time)")
                next_pct += 25
    if proc.wait() != 0:
        raise OpsError("ffmpeg failed: " + " | ".join(list(err_tail)[-3:]))


def _report_preview(out: pathlib.Path, t0: float) -> pathlib.Path:
    p = ffprobe_json(out)
    v = next(x for x in p["streams"] if x.get("codec_type") == "video")
    a = next((x for x in p["streams"] if x.get("codec_type") == "audio"), {})
    dur = float(p["format"]["duration"])
    say(f"  preview: {v.get('width')}x{v.get('height')} {v.get('codec_name')} · {human(out.stat().st_size)} · "
        f"{8 * out.stat().st_size / dur / 1000:.0f} kbps overall (video {int(v.get('bit_rate') or 0) // 1000}, "
        f"audio {int(a.get('bit_rate') or 0) // 1000} kbps {a.get('channels', '?')} ch) · "
        f"{hms(dur)} · encoded in {(time.monotonic() - t0) / 60:.1f} min")
    tags = {k.lower() for k in (p["format"].get("tags") or {})} - {"major_brand", "minor_version",
                                                                  "compatible_brands", "encoder"}
    if tags:
        say(f"  WARNING: container tags left in the preview: {', '.join(sorted(tags))}")
    return out


def write_titles_file(b: str, private_prefix: str, entries: list[dict], codes: list[str]) -> dict:
    """Merge `entries` (by sha) into gs://<b>/<private_prefix>/titles.json and write it with `codes`. An existing file
    must download and parse: anything else aborts before writing (an unreadable file is never replaced blindly)."""
    doc_url = f"gs://{b}/{private_prefix}/titles.json"
    old = {}
    tmp = _work() / "titles.json"
    if gls(doc_url):
        gcp(doc_url, str(tmp))
        try:
            prev = json.loads(tmp.read_text(encoding="utf-8"))
            if not isinstance(prev, dict) or not isinstance(prev.get("titles", []), list):
                raise ValueError("not a {access_sha256, titles} object")
            old = {e.get("sha"): e for e in prev.get("titles", []) if isinstance(e, dict)}
        except ValueError as e:
            tmp.unlink(missing_ok=True)
            raise OpsError(f"{private_prefix}/titles.json exists but is unreadable ({e.__class__.__name__}); "
                           "not overwriting it") from None
    merged = {**old, **{e["sha"]: e for e in entries}}
    doc = {"access_sha256": codes, "titles": list(merged.values())}
    tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
    try:
        gcp(str(tmp), doc_url)
    finally:
        tmp.unlink(missing_ok=True)
    say(f"titles file: {private_prefix}/titles.json · {len(merged)} title(s) ({len(old)} before) · "
        f"{len(codes)} access hash(es)")
    return doc


def publish_private_title() -> int:
    """For each sha in PRIVATE_TITLE_SHAS: its pack (newest device-auto / master) with the thumbnails next to it →
    gs://<bucket>/<PACKS_PREFIX>/<sha>/<dist>/; a low-bitrate preview → gs://<bucket>/previews/<sha>-640.mp4; then
    the private titles file gs://<bucket>/<PRIVATE_PREFIX>/titles.json. Resumable."""
    b = bucket()
    packs_prefix, private_prefix = prefix_param("PACKS_PREFIX"), prefix_param("PRIVATE_PREFIX")
    shas = private_shas()
    if not shas:
        raise OpsError("set PRIVATE_TITLE_SHAS")
    codes = [h.strip().lower() for h in env("PRIVATE_ACCESS_SHA256").split(",") if h.strip()]
    if not codes or not all(HEX64.match(h) for h in codes):
        raise OpsError("PRIVATE_ACCESS_SHA256 must be one or more sha256 hex digests, comma-separated")
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        raise OpsError("ffmpeg / ffprobe not found")
    entries = []
    for i, sha in enumerate(shas, 1):
        say(f"== private preview title #{i} {sha[:12]}")
        where, d, man = best_private_pack(sha)
        dist = man["distribution"]
        db = open_db(d / "pack.db")
        n_thumb_rows = db.execute("SELECT count(*) FROM thumbnails").fetchone()[0]
        a = dict(db.execute("SELECT * FROM asset").fetchone())
        db.close()
        rels = pack_files(d)
        n_thumbs = sum(1 for r in rels if r.startswith("thumbs/"))
        say(f"  pack: {where} · {dist} v{man.get('pack_version')} {str(man.get('built_at', ''))[:10]} · "
            f"thumbnails {n_thumbs} files / {n_thumb_rows} rows · {hms(a['duration_s'])}")
        issues = compat_issues(d / "pack.db")
        say(f"  server compat: {'; '.join(issues)}")
        if n_thumbs < 0.99 * max(n_thumb_rows, 1):
            raise OpsError(f"only {n_thumbs} thumbnail files for {n_thumb_rows} rows: not published")
        u = upload_tree(d, f"gs://{b}/{packs_prefix}/{sha}/{dist}", rels)
        say(f"  → {packs_prefix}/{sha[:12]}/{dist}: {upload_line(u)}")
        if u["mismatch"]:
            raise OpsError("pack upload incomplete")
        cat = local_catalog_entry(sha, a.get("content_id"))
        say(f"  catalog: {cat['root']} (title from ingest/titles, synopsis from catalog/catalog.json synopsis_full)")
        say(f"  synopsis ({len(cat['synopsis'])} chars): {cat['synopsis'] or '(empty)'}")
        target = f"gs://{b}/previews/{sha}-640.mp4"
        have = gls(target)
        local_out = WORK / f"{sha}-640.mp4"
        if have and env("PREVIEW_REBUILD") != "1" and (not local_out.exists() or local_out.stat().st_size == have[0][0]):
            say(f"  preview: already uploaded ({human(have[0][0])} {have[0][1]})")
        else:
            asset = gls(f"gs://{b}/assets/{sha}.mp4")
            film, how = find_film(sha, asset[0][0] if asset else None)
            say(f"  film file: {how}")
            if film is None:
                raise OpsError("film file not found locally; not downloading the original")
            out = make_preview(sha, film, float(a["duration_s"]), rebuild=env("PREVIEW_REBUILD") == "1")
            u2 = upload_tree(out.parent, f"gs://{b}/previews", [out.name], last=())
            say(f"  → previews/{sha[:12]}-640.mp4: {upload_line(u2)}")
        entries.append({"id": f"pv-{sha[:12]}", "sha": sha, "title": cat["title"], "synopsis": cat["synopsis"],
                        "license": "private-preview", "attribution": ATTRIBUTION, "dist": dist,
                        "video_object": f"previews/{sha}-640.mp4", "playback": "preview"})
    write_titles_file(b, private_prefix, entries, codes)
    say(f"service account can read the bucket: {sa_can_read(b)}")
    return 0


# ── bench-show ───────────────────────────────────────────────────────

def _clip(s, n: int) -> str:
    s = " ⏎ ".join(x.strip() for x in str(s or "").splitlines() if x.strip())
    return s if len(s) <= n else s[:n - 1] + "…"


def _failing_kind(r: dict) -> str | None:
    llm = r.get("llm") or {}
    if r.get("status") != "ok":
        return "error"
    if r.get("rule_leaks") or r.get("real_names"):
        return "leak (rule gate)"
    if llm.get("leak"):
        return "leak (judge only)"
    if llm.get("verdict") in ("incorrect", "nonanswer"):
        return llm["verdict"]
    if llm.get("verdict") == "declined" and r.get("expect") == "answer":
        return "declined, answer was on screen"
    if llm.get("appropriate") is False:
        return "inappropriate"
    if r.get("fallback"):
        return f"fallback ({r['fallback']})"
    if r.get("suspects"):
        return "suspect (rule gate)"
    return None


def bench_show(max_lines: int = 118) -> int:
    """Per case one compact block from a graded run: question, answer, rule hits (re-checked with this commit's rules
    when the packs of the last bench are present), the judge's reason, timings and tools."""
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    from bench import common as C
    from bench import judge as J

    runs = ROOT / ".cache" / "bench" / "runs"
    name = env("BENCH_SHOW_RUN")
    if not name:
        cands = sorted((d for d in runs.glob("*") if (d / "judged.jsonl").exists()), key=lambda d: d.stat().st_mtime)
        if not cands:
            raise OpsError(f"no graded run under {runs}")
        name = cands[-1].name
    run = runs / name
    if not (run / "results.jsonl").exists():
        raise OpsError(f"no run {name!r} under {runs}")
    results = {r["id"]: r for r in C.read_jsonl(run / "results.jsonl")}
    judged = {r["id"]: r for r in C.read_jsonl(run / "judged.jsonl")}
    gen = ROOT / ".cache" / "bench" / "generated" / name                  # a generated run's own cases
    cases = {c["id"]: c for c in C.read_cases([gen] if gen.is_dir() else None, strict=False)}
    show_private = env("BENCH_SHOW_PRIVATE") == "1"
    want = [x.strip() for x in env("BENCH_SHOW_CASES").split(",") if x.strip()]
    if not want:
        want = [i for i, r in judged.items() if _failing_kind(r)]
    packs = ROOT / ".cache" / "packs"
    recheck = packs.is_dir()
    if recheck:
        J.PACKS = packs
    say(f"run {name}: {len(results)} results, {len(judged)} graded · showing {len(want)} case(s)"
        f" · rule hits {'re-checked with this commit' if recheck else 'as graded (no .cache/packs)'}")
    lines = 1
    for n, cid in enumerate(want):
        r, j, c = results.get(cid), judged.get(cid) or {}, cases.get(cid)
        block = []
        if r is None or c is None:
            block.append(f"-- {cid}: {'not in this run' if r is None else 'unknown case id'}")
        else:
            kind = _failing_kind(j) if j else None
            if c.get("private") and not show_private:              # ids and numbers only (BENCH_SHOW_PRIVATE=1: all)
                block.append(f"-- {cid} · private preview title · P {c['P']} {c['lang']} · {c['category']} · expect "
                             f"{c['expect']}" + (f" · [{kind}]" if kind else "")
                             + f" · {len(j.get('rule_leaks') or [])} rule leak(s), {len(j.get('suspects') or [])} "
                             f"suspect(s), judge {(j.get('llm') or {}).get('verdict')}, server "
                             f"{r.get('server_latency_ms')} ms")
                if lines + len(block) > max_lines:
                    break
                for b in block:
                    say(b)
                lines += len(block)
                continue
            block.append(f"-- {cid} · {c['title']} · P {c['P']} {c['lang']} · {c['category']} · expect {c['expect']}"
                         + (f" · [{kind}]" if kind else ""))
            hist = " | ".join(h["question"] for h in c.get("history") or [])
            block.append(f"  Q: {_clip(c['question'], 200)}" + (f"   (after: {_clip(hist, 120)})" if hist else ""))
            if r.get("status") != "ok":
                block.append(f"  error: {_clip(r.get('error'), 300)} · HTTP {r.get('http_status')} · "
                             f"stream {r.get('stream_end', '?')} · {r.get('t_total_ms')} ms")
            block.append(f"  A: {_clip(r.get('answer'), 400) or '(none)'}")
            hits = j.get("rule_hits")
            if recheck and r.get("status") == "ok":
                try:
                    hits = J.check_channels(c, J.channels(c, r), J.time_problems(c, r))["rule_hits"]
                except SystemExit as e:
                    block.append(f"  rule: not re-checked ({e})")
            for h in (hits or [])[:4]:
                block.append(f"  rule: {h['rule']}  {_clip(h['match'], 110)}")
            if hits and len(hits) > 4:
                block.append(f"  rule: … {len(hits) - 4} more")
            if hits is None and (j.get("rule_leaks") or j.get("suspects")):
                block.append(f"  rule: {', '.join((j.get('rule_leaks') or []) + (j.get('suspects') or []))[:200]}")
            llm = j.get("llm") or {}
            if llm:
                leak = f" · leak {_clip(llm.get('leak_quote'), 80)!r} ⇐ {_clip(llm.get('leak_fact'), 80)}" \
                    if llm.get("leak") else ""
                block.append(f"  judge: {llm.get('verdict')} · appropriate {llm.get('appropriate')}{leak} — "
                             f"{_clip(llm.get('rationale'), 220)}")
            t = r.get("timing") or {}
            block.append(f"  time: server {r.get('server_latency_ms')} ms (first text {t.get('first_text_ms')}) · "
                         f"client {r.get('t_total_ms')} ms (first text {r.get('t_first_text_ms')}) · tools "
                         f"{', '.join(r.get('tools') or []) or '-'}")
        if lines + len(block) > max_lines:
            say(f"... {len(want) - n} more case(s): {', '.join(want[n:])[:300]}")
            break
        for b in block:
            say(b)
        lines += len(block)
    return 0


# ── logs ─────────────────────────────────────────────────────────────

def _svc() -> dict:
    project, region, service = env("PROJECT"), env("REGION", "us-west1"), env("SERVICE", "hello-watch")
    if not project:
        raise OpsError("project unknown")
    r = gcloud("run", "services", "describe", service, "--project", project, "--region", region, "--format=json")
    if r.returncode:
        raise OpsError(f"cannot describe {service}: {_err(r)}")
    return json.loads(r.stdout)


def logs() -> int:
    """Warnings and errors of the tagged revision, plus the one-line question records flagged error / cut_off /
    retried. Read-only."""
    since = env("LOGS_SINCE", "2h")
    if not re.fullmatch(r"\d{1,4}[smhd]", since):
        raise OpsError(f"LOGS_SINCE must look like 30m, 2h or 1d (got {since!r})")
    svc = _svc()
    tag = env("TAG", "next")
    rev = next((t.get("revisionName") for t in svc.get("status", {}).get("traffic", []) if t.get("tag") == tag), None)
    service = svc.get("metadata", {}).get("name") or env("SERVICE", "hello-watch")
    flt = [f'resource.type="cloud_run_revision"', f'resource.labels.service_name="{service}"']
    if rev:
        flt.append(f'resource.labels.revision_name="{rev}"')
    flagged = ('(textPayload:"ask {" AND (textPayload:"\\"status\\": \\"error\\"" OR '
               'textPayload:"\\"cut_off\\": true" OR textPayload:"\\"retried\\": true"))')
    flt.append(f'(severity>=WARNING OR textPayload:" WARNING " OR textPayload:" ERROR " OR {flagged})')
    if env("LOGS_FILTER"):
        flt.append(f"({env('LOGS_FILTER')})")
    r = gcloud("logging", "read", " AND ".join(flt), "--project", env("PROJECT"), f"--freshness={since}",
               "--limit=300", "--format=json", "--order=desc")
    if r.returncode:
        raise OpsError(f"gcloud logging read: {_err(r)}")
    entries = json.loads(r.stdout or "[]")
    say(f"revision {rev or '(tag ' + tag + ' not found: whole service)'} · last {since} · {len(entries)} entries"
        f"{' (limit reached)' if len(entries) >= 300 else ''}")
    sev = collections.Counter(e.get("severity", "DEFAULT") for e in entries)
    if entries:
        say("by severity: " + ", ".join(f"{k} {v}" for k, v in sev.most_common()))
    shown = list(reversed(entries[:74]))
    if len(entries) > 74:
        say(f"(oldest {len(entries) - 74} not shown)")
    for e in shown:
        ts = str(e.get("timestamp", ""))[11:19]
        if e.get("httpRequest"):
            h = e["httpRequest"]
            msg = (f"{h.get('requestMethod', '')} {str(h.get('requestUrl', '')).split('?')[0][-80:]} "
                   f"{h.get('status', '')} {h.get('latency', '')}")
        else:
            msg = e.get("textPayload") or (e.get("jsonPayload") or {}).get("message") or json.dumps(
                e.get("jsonPayload") or {}, ensure_ascii=False)
        say(f"{ts} {e.get('severity', 'DEFAULT')[:7]:<7} {_clip(msg, 300)}")
    return 0


# ── rejudge ──────────────────────────────────────────────────────────

def rejudge() -> int:
    """Grade stored runs again with this commit's judge and cases (BENCH_REJUDGE_RUNS=<id>[,<id>], optional
    BENCH_COMPARE=<id> side by side): reads results.jsonl only, never asks the service. Rule gate against the packs of
    the last bench (.cache/packs); LLM judge unless BENCH_LLM=0 or there is no Gemini key. Prints the summary."""
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    from bench import judge as J

    runs_dir = ROOT / ".cache" / "bench" / "runs"
    ids = [x.strip() for x in env("BENCH_REJUDGE_RUNS").split(",") if x.strip()]
    if not ids:
        raise OpsError("set BENCH_REJUDGE_RUNS=<run id>[,<run id>] (under .cache/bench/runs)")
    for i in ids + ([env("BENCH_COMPARE")] if env("BENCH_COMPARE") else []):
        if not re.fullmatch(r"[A-Za-z0-9._+-]+", i) or not (runs_dir / i.split("+")[0] / "results.jsonl").exists():
            raise OpsError(f"no stored run {i!r} under .cache/bench/runs")
    packs = ROOT / ".cache" / "packs"
    common = ["--packs", str(packs)] if packs.is_dir() else ["--no-rules"]
    if not packs.is_dir():
        say("WARN: no .cache/packs (run a bench first): rule results come from the cache only")
    key = env("CEDAR_GEMINI_KEY_FILE")
    if env("BENCH_LLM", "1") == "0" or not (key and pathlib.Path(key).is_file()):
        common.append("--no-llm")
        say("LLM judge: off (BENCH_LLM=0 or no Gemini key); cached grades are used where they still match")
    if env("BENCH_SHOW_PRIVATE") == "1":
        common.append("--show-private")
    gen = ROOT / ".cache" / "bench" / "generated"

    def cases_for(run_id: str) -> list[str]:
        d = gen / run_id.split("+")[0]
        return ["--cases", str(d)] if d.is_dir() else []

    batches = [[env("BENCH_COMPARE")] + ids] if env("BENCH_COMPARE") else [[i] for i in ids]
    for batch in batches:
        say(f"== rejudge {' vs '.join(batch)}")
        rc = J.main(["+".join(str(runs_dir / x) for x in r.split("+")) for r in batch] + cases_for(batch[-1])
                    + common + ["--summary"])
        if rc:
            return rc
    return 0


# ── bench prep (run by `run.sh bench` before every run, not a JOB) ────

def _health(base: str) -> dict:
    import urllib.request
    with urllib.request.urlopen(urllib.request.Request(base.rstrip("/") + "/api/health",
                                                       headers={"User-Agent": "hello-watch-bench/1"}), timeout=90) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def _pack_version(db: pathlib.Path) -> str | None:
    con = open_db(db)
    try:
        return meta(con).get("pack_version")
    finally:
        con.close()


def bench_prep() -> int:
    """The packs the benchmarked URL serves → .cache/packs/<sha>/<dist>/pack.db (refused when /api/health reports
    other pack versions), `.cache/bench/served.json`, the hand-written cases linted against those packs (bounded),
    and with BENCH_GENERATE=<n> generated cases (BENCH_TITLES public | private | all) in BENCH_GEN_DIR. A private
    preview title is named only "private preview title #n"."""
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    from bench import common as C
    from bench import gen_cases as G
    from bench import judge as J

    packs_gcs = env("PACKS_GCS").rstrip("/")
    if not packs_gcs.startswith("gs://"):
        raise OpsError("PACKS_GCS (gs://…) is not set")
    gen_n = env("BENCH_GENERATE")
    which = env("BENCH_TITLES") or "public"
    if gen_n and not gen_n.isdigit():
        raise OpsError(f"BENCH_GENERATE must be a number of cases per film (got {gen_n!r})")
    if which not in ("public", "private", "all"):
        raise OpsError(f"BENCH_TITLES must be public | private | all (got {which!r})")
    root = ROOT / ".cache" / "packs"
    served = [{"key": f["sha"][:8], "id": f["id"], "sha": f["sha"], "title": f["title"], "dist": "master",
               "private": False, "label": f["title"]} for f in public_films()]
    pvt = env("PRIVATE_TITLES_GCS")
    if gen_n and which != "public":
        if not pvt:
            raise OpsError("BENCH_TITLES includes private titles but the service has no WATCH_PRIVATE_TITLES")
        local = ROOT / ".cache" / "bench" / "private-titles.json"
        local.parent.mkdir(parents=True, exist_ok=True)
        gcp(pvt, str(local))
        try:
            entries = json.loads(local.read_text(encoding="utf-8")).get("titles") or []
        except (ValueError, AttributeError) as e:
            raise OpsError(f"private titles file unreadable: {type(e).__name__}") from None
        for i, e in enumerate(sorted(entries, key=lambda e: str(e.get("sha"))), 1):
            served.append({"key": f"pv{i}", "id": e["id"], "sha": e["sha"], "title": e["title"],
                           "dist": e.get("dist") or "device-auto", "private": True,
                           "label": f"private preview title #{i}"})
    say(f"packs from {packs_gcs}")
    for t in served:
        dest = root / t["sha"] / t["dist"]
        dest.mkdir(parents=True, exist_ok=True)
        gcp(f"{packs_gcs}/{t['sha']}/{t['dist']}/pack.db", str(dest / "pack.db"))
        t["version"] = _pack_version(dest / "pack.db")
        con = open_db(dest / "pack.db")
        try:
            t["duration_s"] = con.execute("SELECT duration_s FROM asset").fetchone()[0]
        finally:
            con.close()
        say(f"  {t['label'][:34]:34} {t['dist']:11} v{t['version']}  {human((dest / 'pack.db').stat().st_size)}")
    base = env("BENCH_BASE")
    if base:
        try:
            h = _health(base)
        except Exception as e:                                   # noqa: BLE001
            raise OpsError(f"{base}/api/health: {type(e).__name__}") from None
        reported = {x.get("sha"): str(x.get("version")) for x in h.get("packs") or []}
        if not reported:
            say("  WARN: the server does not report its pack versions (older revision); not compared")
        for t in served:
            if not t["private"] and reported and reported.get(t["sha"]) != str(t["version"]):
                raise OpsError(f"{t['label']}: the server serves pack v{reported.get(t['sha'])}, {packs_gcs} has "
                               f"v{t['version']}: grading would use other packs than the ones answering")
        if reported:
            say(f"  server {h.get('revision') or '?'} serves the same pack versions")
    out = ROOT / ".cache" / "bench" / "served.json"
    out.write_text(json.dumps(served, ensure_ascii=False, indent=1), encoding="utf-8")
    J.PACKS = root
    C._PACKS.clear()
    say("lint: hand-written cases against these packs")
    import contextlib
    import io
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = J.lint(C.read_cases(strict=False), with_packs=True, max_lines=20)
    for line in buf.getvalue().splitlines()[:24]:
        say("  " + line)
    if rc:
        say("  WARN: hand-written cases do not match the served packs (above); the run goes ahead")
    if not gen_n:
        return 0
    gen_dir = pathlib.Path(env("BENCH_GEN_DIR") or ROOT / ".cache" / "bench" / "generated" / "latest")
    say(f"generate: {gen_n} cases per film, titles {which}, seed {env('BENCH_SEED') or 1} → {gen_dir.name}")
    for line in G.generate_served(served, root, gen_dir, which, int(gen_n), int(env("BENCH_SEED") or 1)):
        say("  " + line[:300])
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = J.lint(C.read_cases([gen_dir], strict=False), with_packs=True, titles=J.served_titles(served),
                    show_private=env("BENCH_SHOW_PRIVATE") == "1", max_lines=15)
    for line in buf.getvalue().splitlines()[:18]:
        say("  " + line)
    if rc:
        raise OpsError("generated cases fail the lint (above): not running them")
    return 0


# ── untag ────────────────────────────────────────────────────────────

def _split(traffic: list[dict]) -> list[tuple[str, int]]:
    """[(revision, percent)] of the entries that carry traffic, sorted: what a tag change must leave alone."""
    out: dict[str, int] = {}
    for t in traffic:
        pct = int(t.get("percent") or 0)
        if pct:
            rev = t.get("revisionName") or ("LATEST" if t.get("latestRevision") else "?")
            out[rev] = out.get(rev, 0) + pct
    return sorted(out.items())


def _traffic_lines(traffic: list[dict]) -> None:
    for t in traffic:
        rev = t.get("revisionName") or ("LATEST" if t.get("latestRevision") else "?")
        say(f"  {rev:<40} {int(t.get('percent') or 0):>4}%  tag={t.get('tag') or '-':<8} {t.get('url') or ''}")


def untag() -> int:
    """Removes the TAG traffic tag, so its URL (https://TAG---<service host>) stops answering; the traffic split
    stays as it is (checked after, and put back if gcloud changed it). The next deploy-staging tags again."""
    tag = env("TAG", "next")
    project, region, service = env("PROJECT"), env("REGION", "us-west1"), env("SERVICE", "hello-watch")
    traffic = _svc().get("status", {}).get("traffic", [])
    say("traffic before:")
    _traffic_lines(traffic)
    tagged = [t for t in traffic if t.get("tag") == tag]
    if not tagged:
        say(f"no tag {tag}: nothing to remove")
        return 0
    rev = tagged[0].get("revisionName") or "?"
    before = _split(traffic)
    r = gcloud("run", "services", "update-traffic", service, "--project", project, "--region", region,
               "--remove-tags", tag, "--quiet", timeout=600)
    if r.returncode:
        raise OpsError(f"update-traffic --remove-tags {tag}: {_err(r)}")
    traffic = _svc().get("status", {}).get("traffic", [])
    say("traffic after:")
    _traffic_lines(traffic)
    if _split(traffic) != before:
        want = ",".join(f"{k}={v}" for k, v in before)
        say(f"WARN: the split changed; putting it back: --to-revisions {want}")
        r = gcloud("run", "services", "update-traffic", service, "--project", project, "--region", region,
                   "--to-revisions", want, "--quiet", timeout=600)
        if r.returncode:
            raise OpsError(f"traffic split changed and could not be restored ({_err(r)}): run gcloud run services "
                           f"update-traffic {service} --project {project} --region {region} --to-revisions {want}")
        raise OpsError(f"traffic split changed and was put back to {want}; tag {tag} state: see traffic above")
    if any(t.get("tag") == tag for t in traffic):
        raise OpsError(f"tag {tag} is still there")
    say(f"tag {tag} removed from {rev}; traffic unchanged: " + ", ".join(f"{k} {v}%" for k, v in before))
    say(f"to put it back: gcloud run services update-traffic {service} --project {project} --region {region} "
        f"--update-tags {tag}={rev}")
    return 0


JOBS = {"inspect-packs": inspect_packs, "publish-public-packs": publish_public_packs,
        "publish-private-title": publish_private_title, "bench-show": bench_show, "logs": logs,
        "rejudge": rejudge, "bench-prep": bench_prep, "untag": untag}


def main(argv: list[str]) -> int:
    name = argv[1] if len(argv) > 1 else env("JOB")
    fn = JOBS.get(name)
    if fn is None:
        say(f"ERROR: unknown job {name!r} (one of: {', '.join(JOBS)})")
        return 2
    try:
        return fn()
    except OpsError as e:
        say(f"ERROR: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
