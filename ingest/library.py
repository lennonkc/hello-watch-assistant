"""Library asset registration and metadata-mismatch detection → `.cache/ingest/library.json`; ingest progress summary → `.cache/ingest/library_status.json`.

Assets are matched to catalog entries by file name (Chinese name = `title_zh` first, then the normalized English name). Mismatches fall into three kinds:

- `duration`: asset duration differs from `catalog.duration_s` by more than `DURATION_TOL_S`;
- `content`: the work title / series marker read on screen in the `identify` stage disagrees with the catalog entry (e.g. the entry is a film, the asset a series trailer);
- `kind`: the identified asset form (trailer / feature / episode) disagrees with the title config's `asset_kind`.
"""
from __future__ import annotations

import pathlib
import re
import unicodedata

from ingest.util import CACHE, CATALOG_PATH, TITLES_DIR, read_json, sha256_file, work_dir, write_json

DURATION_TOL_S = 2.0
LIBRARY_PATH = CACHE / "ingest" / "library.json"
STATUS_PATH = CACHE / "ingest" / "library_status.json"


def _norm_title(s: str) -> str:
    s = unicodedata.normalize("NFKC", s or "").lower()
    s = re.sub(r"\b(ii)\b", "2", s)
    s = re.sub(r"\b(iii)\b", "3", s)
    return re.sub(r"[^0-9a-z㐀-鿿]+", "", s)


def split_filename(name: str) -> tuple[str, str]:
    """'<zh title> Sonic the Hedgehog 2.mp4' → ('<zh title>', 'Sonic the Hedgehog 2'). The Chinese part is everything before the first space."""
    stem = pathlib.Path(name).stem
    zh, _, en = stem.partition(" ")
    return zh.strip(), en.strip()


def match_catalog(filename: str, items: list[dict]) -> dict | None:
    zh, en = split_filename(filename)
    for it in items:
        if it.get("title_zh") and _norm_title(it["title_zh"]) == _norm_title(zh):
            return it
    ne = _norm_title(en)
    for it in items:
        nt = _norm_title(it["title"])
        if ne and (nt == ne or nt.startswith(ne) or ne.startswith(nt)):
            return it
    return None


def title_config(catalog_id: str) -> dict | None:
    p = TITLES_DIR / f"{catalog_id}.json"
    return read_json(p) if p.exists() else None


def content_mismatches(item: dict, title: dict | None, ident: dict | None) -> list[dict]:
    """Detect content mismatches from the identify stage. Based only on what is read on screen / heard in dialogue, never on outside knowledge."""
    out = []
    if not ident:
        return out
    doc = ident.get("doc") or {}
    seen = doc.get("title_on_screen") or ""
    series = doc.get("series_marker") or ""
    form = doc.get("asset_form") or ""
    if seen and _norm_title(seen) and not _titles_agree(seen, item):
        out.append({"kind": "content", "detail": f"on-screen title {seen!r} does not match catalog title {item['title']!r}"})
    # A series marker only counts if the model also reports a mismatch or the form is itself episodic (Cosmos's "chapter 1" is part of the title)
    if series and (doc.get("matches_catalog") is False or form in ("episode_trailer", "episode")):
        out.append({"kind": "content", "detail": f"on-screen series / episode marker {series!r}; catalog entry is a "
                                                 f"{item.get('type', 'movie')}"})
    if doc.get("matches_catalog") is False:
        out.append({"kind": "content", "detail": f"identify: {doc.get('mismatch_reason') or 'content does not match catalog'}"})
    kind = (title or {}).get("asset_kind")
    expect = {"trailer": "trailer", "teaser": "trailer", "short_film": "short", "feature_excerpt": "clip",
              "episode_trailer": "trailer", "episode": "clip", "other": None}.get(form)
    if kind and expect and expect != kind:
        out.append({"kind": "kind", "detail": f"identified form {form!r} vs configured asset_kind {kind!r}"})
    return out


def _titles_agree(seen: str, item: dict) -> bool:
    a = _norm_title(seen)
    for t in (item.get("title"), item.get("title_zh")):
        b = _norm_title(t or "")
        if b and (a == b or a in b or b in a):
            return True
    return False


def register_assets(asset_dir: pathlib.Path) -> dict:
    items = read_json(CATALOG_PATH)["items"]
    prev = {a["file"]: a for a in read_json(LIBRARY_PATH)["assets"]} if LIBRARY_PATH.exists() else {}
    from ingest.stages.register import ffprobe
    assets = []
    for f in sorted(asset_dir.glob("*.mp4")):
        old = prev.get(f.name)
        st = f.stat()
        if old and old.get("bytes") == st.st_size and old.get("mtime") == int(st.st_mtime):
            sha = old["asset_sha256"]
        else:
            sha = sha256_file(f)
        probe = ffprobe(f)
        dur = round(float(probe["format"]["duration"]), 3)
        v = next(s for s in probe["streams"] if s["codec_type"] == "video")
        audio = [s for s in probe["streams"] if s["codec_type"] == "audio"]
        it = match_catalog(f.name, items)
        title = title_config(it["id"]) if it else None
        ident_p = work_dir(sha) / "stages" / "identify.json"
        ident = read_json(ident_p) if ident_p.exists() else None
        mism = []
        if it is None:
            mism.append({"kind": "catalog", "detail": "no catalog entry matches the file name"})
        else:
            d = round(dur - it["duration_s"], 3)
            if abs(d) > DURATION_TOL_S:
                mism.append({"kind": "duration", "detail": f"asset {dur:.1f}s vs catalog {it['duration_s']}s ({d:+.1f}s)"})
            mism += content_mismatches(it, title, ident)
        if title and title.get("expected_asset_sha256") and sha not in title["expected_asset_sha256"]:
            mism.append({"kind": "sha256", "detail": "asset sha256 not in ingest/titles expected list"})
        assets.append({
            "file": f.name, "bytes": st.st_size, "mtime": int(st.st_mtime), "asset_sha256": sha, "duration_s": dur,
            "width": v.get("width"), "height": v.get("height"), "audio_tracks": len(audio),
            "catalog_id": it["id"] if it else None, "catalog_title": it["title"] if it else None,
            "catalog_title_zh": it.get("title_zh") if it else None,
            "catalog_duration_s": it["duration_s"] if it else None,
            "source_class": ("blender" if (title or {}).get("license", "").startswith("CC-") else "trailer") if title else None,
            "asset_kind": (title or {}).get("asset_kind"),
            "identified": _ident_summary(ident),
            "mismatches": mism})
    lib = {"format": "cedar-asset-library/1", "duration_tol_s": DURATION_TOL_S, "count": len(assets), "assets": assets}
    write_json(LIBRARY_PATH, lib)
    return lib


def _ident_summary(ident: dict | None) -> dict | None:
    if not ident:
        return None
    d = ident.get("doc") or {}
    return {k: d.get(k) for k in ("asset_form", "title_on_screen", "series_marker", "matches_catalog", "mismatch_reason",
                                  "dialogue_language", "license_notice")}


MISMATCH_KINDS = ("catalog", "content", "kind", "duration")


def asset_mismatch_note(lib_asset: dict) -> str | None:
    """Value for pack_meta.asset_mismatch: mismatch kinds only (e.g. 'content;duration'), None if there are none.

    No free text: identification reasons may contain uploader handles, unrelated titles, etc., and would ship in the device pack; details stay in the ingest-side library.json.
    """
    kinds = {m["kind"] for m in lib_asset.get("mismatches", [])}
    return ";".join(k for k in MISMATCH_KINDS if k in kinds) or None


def library_entry(sha: str) -> dict | None:
    if not LIBRARY_PATH.exists():
        return None
    return next((a for a in read_json(LIBRARY_PATH)["assets"] if a["asset_sha256"] == sha), None)


def status() -> dict:
    """Per-asset ingest progress: pack_version, review_complete, failed checks, items needing human review, spend."""
    from ingest.ledger import Ledger
    lib = read_json(LIBRARY_PATH)
    rows, total = [], 0.0
    for a in lib["assets"]:
        wd = work_dir(a["asset_sha256"])
        led = Ledger(a["asset_sha256"])
        spent = led.spent()
        total += spent
        ran = [e for e in led.entries() if e["status"] == "ran"]
        row = {"file": a["file"], "catalog_id": a["catalog_id"], "asset_sha256": a["asset_sha256"],
               "source_class": a["source_class"], "duration_s": a["duration_s"],
               "mismatches": [m["kind"] for m in a["mismatches"]], "cost_usd": round(spent, 4),
               "elapsed_s": round(sum(e["elapsed_s"] for e in ran), 1)}
        ck = wd / "checks.json"
        if ck.exists():
            c = read_json(ck)
            nh = read_json(wd / "review" / "needs_human.json")
            row.update(pack_version=c["pack_version"], review_complete=c["review_complete"],
                       checks_failed=[f"{x['id']} {x['name']} ({len(x['issues'])})" for x in c["checks"] if not x["ok"]],
                       flagged=len(c.get("flagged_items", [])), needs_human=nh["counts"]["pending_human"],
                       needs_human_by_type=_by_type(nh["pending_human"]),
                       prereview_doubts=nh["counts"].get("prereview_doubts", 0),
                       prereviewed=any(p.name.endswith("claude-prereview.json") for p in (wd / "patches").glob("*.json"))
                       if (wd / "patches").exists() else False)
        else:
            row.update(pack_version=None, review_complete=False, checks_failed=None, needs_human=None)
        rows.append(row)
    out = {"format": "cedar-library-status/1", "assets": rows, "total_cost_usd": round(total, 4),
           "needs_human_total": sum(r.get("needs_human") or 0 for r in rows)}
    write_json(STATUS_PATH, out)
    return out


def _by_type(items: list[dict]) -> dict:
    out: dict[str, int] = {}
    for i in items:
        out[i["item_type"]] = out.get(i["item_type"], 0) + 1
    return dict(sorted(out.items()))


def import_cast(source: pathlib.Path) -> dict[str, int]:
    """Extract real-person names from an upstream catalog export with `cast` / `directors` (never committed) → `.cache/sources/<id>/cast.json`.

    Used only for the in-pack blocklist; placeholder values equal to the title (e.g. cast = title) are dropped.
    """
    ours = {it["id"]: it for it in read_json(CATALOG_PATH)["items"]}
    counts = {}
    for it in read_json(source)["items"]:
        mine = ours.get(it.get("id"))
        if not mine:
            continue
        names = [c["name"] if isinstance(c, dict) else c for c in it.get("cast") or []] + list(it.get("directors") or [])
        bad = {_norm_title(mine["title"]), _norm_title(mine.get("title_zh") or "")}
        names = list(dict.fromkeys(" ".join(n.split()) for n in names if n and _norm_title(n) not in bad))
        write_json(CACHE / "sources" / it["id"] / "cast.json", {"names": names, "source": "catalog cast / directors"})
        counts[it["id"]] = len(names)
    return counts
