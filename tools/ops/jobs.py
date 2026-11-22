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


def prefix_param(name: str, required: bool = True) -> str:
    v = env(name).strip("/")
    if not v:
        if required:
            raise OpsError(f"set {name} in tools/ops/params.env")
        return ""
    if v.startswith("gs:") or ".." in v or not re.fullmatch(r"[A-Za-z0-9._/-]+", v):
        raise OpsError(f"{name} must be a path inside the bucket, e.g. demo-packs-next (got {v!r})")
    if v == LIVE_PREFIX or v.startswith(LIVE_PREFIX + "/"):
        raise OpsError(f"{name} must not be the live prefix {LIVE_PREFIX!r}")
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
    for t in db_tables(db):
        if t in SKIP_TABLES or t == "text_fts":
            continue
        cols = [c[1] for c in db.execute(f"PRAGMA table_info({t})") if (c[2] or "").upper() == "TEXT"]
        if not cols:
            continue
        for row in db.execute(f"SELECT {','.join(cols)} FROM {t}"):
            for v in row:
                if not v or "{" not in v:
                    continue
                hit = 0
                for k, rx in PLACEHOLDERS.items():
                    n = len(rx.findall(v))
                    ph[k] += n
                    hit += n
                n_any = len(ANY_BRACE.findall(v)) - hit
                if n_any > 0:
                    ph["{…}"] += n_any
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
    return {"ph": ph, "where": where, "blank": blank, "noword": noword, "inversions": inv, "meta": m}


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
    cases = {c["id"]: c for c in C.read_cases(strict=False)}
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
                    hits = J.rule_check(c, r.get("answer") or "", r.get("evidence_text") or [],
                                        r.get("streamed_text"))["rule_hits"]
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
                         f"{', '.join(r.get('tools') or []) or '-'} · flow {r.get('flow')}")
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


JOBS = {"inspect-packs": inspect_packs, "bench-show": bench_show, "logs": logs}


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
