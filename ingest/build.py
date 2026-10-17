"""auto rows → master (replay all review patches) → checks → version → export master / device packs + MANIFEST.

master content = f(auto rows, patches/*.json replayed in order, wordlists, margins). flagged state is derived by checks, never stored as input.
Re-exporting the same content yields the same pack_version, the same built_at, the same bytes.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import pathlib
import shutil

from ingest import GENERATOR, packdb
from ingest.checks import ITEM_TABLE, run_checks
from ingest.derive import MARGINS, derive_canaries, fts_rows
from ingest.patch import PatchError
from ingest.patch import apply_patch as _apply_ops
from ingest.textnorm import wordlists_version
from ingest.util import CACHE, fingerprint, read_json, sha256_bytes, sha256_file, work_dir, write_json

STATE_TABLES = {"characters", "character_looks", "character_facts", "identity_links", "appearances", "scenes", "events", "objects",
                "character_exemplars", "shot_captions", "cue_speakers", "recaps"}
PRE = {"prereviewed", "reviewed"}


def build_auto(ctx) -> dict:
    from ingest.assemble import Assembler
    st = {}
    for name in ("register", "shots", "segments", "subtitles", "align", "thumbs", "vision_full", "vision_seg", "credits",
                 "identify", "vision_shots", "speakers", "transcribe"):
        p = ctx.stage_path(name)
        if p.exists():
            st[name] = read_json(p)
    cast = CACHE / "sources" / ctx.catalog_id / "cast.json"     # catalog cast / director (only feeds the in-pack blocklist; not committed)
    if cast.exists():
        st["cast"] = read_json(cast)
    for need in ("register", "shots", "segments", "subtitles", "thumbs", "vision_full"):
        if need not in st:
            raise RuntimeError(f"build needs stage {need}")
    from ingest.library import asset_mismatch_note, library_entry
    lib = library_entry(ctx.sha)
    note = asset_mismatch_note(lib) if lib else None
    st["asset_mismatch"] = note
    pack, aux = Assembler(st, ctx.title, ctx.catalog_item).build()
    # post-assembly steps: depend on finalized item ids and visibility times; each cached by content fingerprint under <wd>/post/
    from ingest import embed, recaps
    pack = packdb.normalize_pack(pack)
    recaps.add_recaps(ctx, pack)
    embed.add_embeddings(ctx, pack)
    pack["pack_meta"] = {
        "models": json.dumps({"vision": st["vision_full"]["model"],
                              "transcribe": (st.get("transcribe") or {}).get("model") or "gemini-3.5-transcribe",
                              "credits": (st.get("credits") or {}).get("model")}, sort_keys=True),
        "prompt_sha256": json.dumps({"vision_full": st["vision_full"].get("prompt_sha256")}, sort_keys=True)}
    if note:
        pack["pack_meta"]["asset_mismatch"] = note
    pack = packdb.normalize_pack(pack)
    write_json(ctx.wd / "auto" / "rows.json", pack)
    write_json(ctx.wd / "auto" / "aux.json", aux)
    return pack


def patches_dir(sha: str) -> pathlib.Path:
    return work_dir(sha) / "patches"


def materialize(sha: str, extra_patch: dict | None = None) -> tuple[dict, list[str]]:
    pack = read_json(work_dir(sha) / "auto" / "rows.json")
    pack.setdefault("review_log", [])
    names = []
    pd = patches_dir(sha)
    for p in sorted(pd.glob("*.json")) if pd.exists() else []:
        pack = _apply_ops(pack, read_json(p))
        names.append(p.name)
    if extra_patch is not None:
        pack = _apply_ops(pack, extra_patch)
    pack["canaries"] = derive_canaries(pack)
    return packdb.normalize_pack(pack), names


def apply_flags(pack: dict, flag_items: list) -> list[list]:
    applied = []
    for it, iid in flag_items:
        t = ITEM_TABLE.get(it)
        if not t or t[0] not in STATE_TABLES:
            continue
        for r in pack[t[0]]:
            if r[t[1]] == iid and r["review_state"] != "rejected":
                r["review_state"] = "flagged"
                applied.append([it, iid])
    return applied


def device_view(master: dict) -> dict:
    """spec §2: strip review_log, keep only prereviewed / reviewed rows (also dropping dangling link rows)."""
    d = json.loads(json.dumps(master))
    d["review_log"] = []
    for t in STATE_TABLES:
        d[t] = [r for r in d[t] if r["review_state"] in PRE]
    chars = {c["character_id"] for c in d["characters"]}
    d["character_aliases"] = [a for a in d["character_aliases"] if a["character_id"] in chars]
    for t in ("character_looks", "character_facts", "appearances"):
        d[t] = [r for r in d[t] if r["character_id"] in chars]
    d["identity_links"] = [k for k in d["identity_links"] if k["a_character_id"] in chars and k["b_character_id"] in chars]
    exists = {it: {r[key] for r in d[tab]} for it, (tab, key) in ITEM_TABLE.items()}
    d["item_characters"] = [x for x in d["item_characters"] if x["character_id"] in chars
                            and x["item_id"] in exists.get(x["item_type"], set())]
    d["item_cues"] = [x for x in d["item_cues"] if x["item_id"] in exists.get(x["item_type"], set())]
    objs = {o["object_id"] for o in d["objects"]}
    d["object_sightings"] = [x for x in d["object_sightings"] if x["object_id"] in objs]
    d["character_exemplars"] = [x for x in d["character_exemplars"] if x["character_id"] in chars]
    d["cue_speakers"] = [x for x in d["cue_speakers"] if x["character_id"] in chars or x["character_id"] is None]
    shots = {s["shot_caption_id"] for s in d["shot_captions"]}
    d["shot_entities"] = [{**x, "character_id": x["character_id"] if x["character_id"] in chars else None,
                           "object_id": x["object_id"] if x["object_id"] in objs else None}
                          for x in d["shot_entities"] if x["shot_caption_id"] in shots
                          and (x["entity_type"] != "character" or x["character_id"] in chars)]
    exists = {**exists, "video_window": {w["window_id"] for w in d["video_windows"]},
              "segment": {s["segment_id"] for s in d["segments"]}}
    d["embeddings"] = [x for x in d.get("embeddings", []) if x["item_id"] in exists.get(x["item_type"], set())]
    # title_info has no review_state, so "must be reviewed" can only be judged from review_log; the device pack has no review_log, so only human-approved fields go in
    human_titles = {r["item_id"] for r in master["review_log"] if r["item_type"] == "title_info"
                    and not r["reviewer"].startswith("claude-") and r["decision"] in ("approve", "edit")}
    d["title_info"] = [t for t in d["title_info"] if t["field"] in human_titles]
    return packdb.normalize_pack(d)


def _versions(sha: str) -> list[dict]:
    p = work_dir(sha) / "versions.json"
    return read_json(p)["versions"] if p.exists() else []


def mint_version(sha: str, content_sha: str, patches: list[str], write: bool) -> tuple[int, str]:
    vs = _versions(sha)
    hit = next((v for v in vs if v["content_sha256"] == content_sha), None)
    if hit:
        return hit["pack_version"], hit["built_at"]
    ver = (vs[-1]["pack_version"] + 1) if vs else 1
    built = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    if write:
        vs.append({"pack_version": ver, "content_sha256": content_sha, "built_at": built, "patches": patches})
        write_json(work_dir(sha) / "versions.json", {"versions": vs})
    return ver, built


def _link_thumbs(src: pathlib.Path, dst: pathlib.Path, rows: list[dict]) -> None:
    dst.mkdir(parents=True, exist_ok=True)
    for r in rows:
        name = pathlib.Path(r["path"]).name
        try:
            os.link(src / name, dst / name)
        except OSError:
            shutil.copy2(src / name, dst / name)


def write_pack_dir(pack: dict, out: pathlib.Path, thumbs_src: pathlib.Path, embeddings: list[dict] | None = None) -> dict:
    tmp = out.with_name(out.name + ".tmp")
    if tmp.exists():
        shutil.rmtree(tmp)
    tmp.mkdir(parents=True)
    packdb.write_db(pack, tmp / "pack.db", fts_rows(pack), embeddings)
    write_json(tmp / "pack.json", pack)
    _link_thumbs(thumbs_src, tmp / "thumbs", pack["thumbnails"])
    files = []
    for p in [tmp / "pack.db", tmp / "pack.json"] + sorted((tmp / "thumbs").iterdir()):
        files.append({"path": str(p.relative_to(tmp)), "sha256": sha256_file(p), "bytes": p.stat().st_size})
    meta = pack["pack_meta"]
    manifest = {"schema_version": int(meta["schema_version"]), "pack_version": int(meta["pack_version"]),
                "distribution": meta["distribution"], "asset_sha256": meta["asset_sha256"], "built_at": meta["built_at"],
                "review_complete": meta["review_complete"] == "1", "files": files}
    write_json(tmp / "MANIFEST.json", manifest)
    if out.exists():
        shutil.rmtree(out)
    os.replace(tmp, out)
    return manifest


def export_all(sha: str, write: bool = True, extra_patch: dict | None = None) -> dict:
    wd = work_dir(sha)
    master, patches = materialize(sha, extra_patch)
    from ingest.library import asset_mismatch_note, library_entry
    lib = library_entry(sha)
    if lib is not None:                      # current library.json wins (write only the mismatch category, not the reason text)
        master["pack_meta"].pop("asset_mismatch", None)
        note = asset_mismatch_note(lib)
        if note:
            master["pack_meta"]["asset_mismatch"] = note
    meta = {**{k: str(v) for k, v in MARGINS.items()}, **master["pack_meta"], "schema_version": "1", "asset_sha256": sha,
            "generator": GENERATOR, "fixture": "0", "wordlists_version": wordlists_version()}
    master["pack_meta"] = {**meta, "distribution": "master", "review_complete": "0", "pack_version": "0", "built_at": ""}
    rep = run_checks(master, fts_rows(master))
    flagged = apply_flags(master, rep["flag_items"])
    review_complete = "1" if rep["ok"] else "0"
    from ingest import embed
    master_emb = embed.pack_rows(master, wd)
    content = {**{k: v for k, v in master.items() if k != "pack_meta"}, "_meta": meta, "_review_complete": review_complete}
    if master_emb:                           # vectors aren't in pack.json but change pack.db bytes, so they must enter the version fingerprint
        content["_embeddings"] = sha256_bytes(b"".join(f"{r['item_type']}|{r['item_id']}|".encode() + r["vector"]
                                                       for r in master_emb))
    content_sha = fingerprint(content)
    ver, built = mint_version(sha, content_sha, patches, write)
    common = {**meta, "pack_version": str(ver), "built_at": built, "review_complete": review_complete}
    master["pack_meta"] = {**common, "distribution": "master"}
    device = device_view(master)
    device["pack_meta"] = {**common, "distribution": "device"}
    res = {"pack_version": ver, "review_complete": review_complete == "1", "flagged": len(flagged), "checks": rep,
           "checks_failed": [f"{c['id']} {c['name']} ({len(c['issues'])})" for c in rep["checks"] if not c["ok"]],
           "master": str(wd / "master"), "device": str(wd / "device"), "patches": patches}
    if write:
        write_pack_dir(master, wd / "master", wd / "thumbs", master_emb)
        write_pack_dir(device, wd / "device", wd / "thumbs", embed.pack_rows(device, wd))
        write_json(wd / "checks.json", {"asset_sha256": sha, "pack_version": ver, "built_at": built,
                                        "review_complete": review_complete == "1", "flagged_items": flagged, **rep})
        write_json(wd / "review" / "needs_human.json", needs_human(master, rep))
    return res


DOUBT_TAG = "[需人工]"


def needs_human(master: dict, rep: dict) -> dict:
    """Human review list: pending-human items from checks 7 / 8 + items flagged by checks + doubtful items tagged `DOUBT_TAG` ("needs human") by automated pre-review."""
    items = [{"check": c["id"], "item_type": i["item_type"], "item_id": i["item_id"], "msg": i["msg"]}
             for c in rep["checks"] if c["id"] in (7, 8) for i in c["issues"]]
    flagged = [{"item_type": t, "item_id": i} for t, i in rep["flag_items"]]
    doubts, seen = [], set()
    for r in master["review_log"]:
        if r["reviewer"].startswith("claude-") and (r["why"] or "").startswith(DOUBT_TAG) and (r["item_type"], r["item_id"]) not in seen:
            seen.add((r["item_type"], r["item_id"]))
            doubts.append({"item_type": r["item_type"], "item_id": r["item_id"], "why": r["why"]})
    return {"pack_version": master["pack_meta"]["pack_version"], "pending_human": items, "flagged": flagged,
            "prereview_doubts": doubts,
            "counts": {"pending_human": len(items), "flagged": len(flagged), "prereview_doubts": len(doubts)}}


def apply_patch(sha: str, path: pathlib.Path) -> dict:
    patch = read_json(path)
    try:
        export_all(sha, write=False, extra_patch=patch)   # validate in memory first
    except PatchError as e:
        raise SystemExit(f"patch rejected: {e}")
    pd = patches_dir(sha)
    pd.mkdir(parents=True, exist_ok=True)
    n = len(list(pd.glob("*.json"))) + 1
    write_json(pd / f"{n:03d}-{patch['reviewer']}.json", patch)
    return export_all(sha)
