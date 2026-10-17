"""Review patch (`cedar-review-patch/1`): locate items by table + primary key, apply to master content, and write `review_log`.

    {"format": "cedar-review-patch/1", "asset_sha256": "...", "base_pack_version": 2,
     "reviewer": "reviewer-1", "created_at": "RFC 3339", "seconds_total": 1234.5,
     "ops": [{"op": "approve" | "edit" | "reject" | "flag" | "add" | "delete",
              "table": "character_facts", "key": {"fact_id": "f003"},   # add uses "row"
              "set": {"reveal_at_s": 622.0},                             # edit only
              "why": "...", "spoiler_critical": 0 | 1, "seconds_spent": 12.5, "ts": "RFC 3339"}]}

Automated pre-review (reviewer id `claude-*`) sets touched items to `prereviewed`; any other reviewer sets `reviewed`.
Items a human already reviewed are never downgraded by automated pre-review.
"""
from __future__ import annotations

import copy
import json
import re

from ingest import packdb

FORMAT = "cedar-review-patch/1"
REVIEWER_RE = re.compile(r"^[a-z][a-z0-9-]{1,31}$")
DERIVED_CANARY_KINDS = ("name", "alias")
ITEM_TYPE = {"characters": "character", "character_aliases": "character_alias", "character_looks": "character_look",
             "character_facts": "character_fact", "identity_links": "identity_link", "appearances": "appearance",
             "scenes": "scene", "events": "event", "objects": "object", "object_sightings": "object_sighting",
             "title_info": "title_info", "canaries": "canary", "blocklist": "blocklist", "item_characters": "item_character",
             "item_cues": "item_cue", "asset": "asset", "cues": "cue", "subtitle_tracks": "subtitle_track"}
NO_KEY_FALLBACK = {"appearances": ["character_id", "start_s"]}
EDITABLE_TABLES = set(ITEM_TYPE)


class PatchError(ValueError):
    pass


def is_human(reviewer: str) -> bool:
    return not reviewer.startswith("claude-")


def key_cols(table: str) -> list[str]:
    info = packdb.schema_info()[table]
    return info["pk"] or NO_KEY_FALLBACK.get(table) or info["cols"]


def item_id(table: str, row: dict) -> str:
    return "/".join(str(row[c]) for c in key_cols(table))


def _find(pack, table, key) -> list[dict]:
    rows = [pack["asset"]] if table == "asset" else pack[table]
    cols = key_cols(table)
    if set(key) != set(cols):
        raise PatchError(f"{table}: key must be exactly {cols}, got {sorted(key)}")
    return [r for r in rows if all(_eq(r.get(c), key[c]) for c in cols)]


def _eq(a, b) -> bool:
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return abs(a - b) < 1e-6
    return a == b


def _js(v):
    return None if v is None else json.dumps(v, ensure_ascii=False, sort_keys=True)


def apply_patch(pack: dict, patch: dict) -> dict:
    if patch.get("format") != FORMAT:
        raise PatchError(f"format must be {FORMAT}")
    if patch.get("asset_sha256") != pack["asset"]["asset_sha256"]:
        raise PatchError("patch is for a different asset")
    reviewer = patch.get("reviewer", "")
    if not REVIEWER_RE.match(reviewer):
        raise PatchError("reviewer must be a role id like 'reviewer-1' (no personal names)")
    target = "reviewed" if is_human(reviewer) else "prereviewed"
    pack = copy.deepcopy(pack)
    log = pack.setdefault("review_log", [])
    next_id = max([r["log_id"] for r in log], default=0) + 1
    info = packdb.schema_info()

    def L(table, iid, field, old, new, decision, op, row=None):
        nonlocal next_id
        sc = op.get("spoiler_critical")
        if sc is None:
            sc = (row or {}).get("spoiler_critical", 0) or 0
        log.append({"log_id": next_id, "item_type": ITEM_TYPE[table], "item_id": iid, "field": field,
                    "old_value": _js(old), "new_value": _js(new), "decision": decision, "spoiler_critical": int(bool(sc)),
                    "reviewer": reviewer, "ts": op.get("ts") or patch.get("created_at") or "1970-01-01T00:00:00Z",
                    "seconds_spent": op.get("seconds_spent"), "why": op.get("why")})
        next_id += 1

    def set_state(table, row, state, op, decision):
        if "review_state" not in row:
            return
        old = row["review_state"]
        if state == "prereviewed" and old == "reviewed":
            return
        row["review_state"] = state
        L(table, item_id(table, row), "review_state", old, state, decision, op, row)

    for n, op in enumerate(patch.get("ops", [])):
        kind, table = op.get("op"), op.get("table")
        if table not in EDITABLE_TABLES:
            raise PatchError(f"op {n}: table {table!r} not editable")
        cols = info[table]["cols"]
        if kind == "add":
            row = {c: op["row"].get(c) for c in cols}
            if set(op["row"]) - set(cols):
                raise PatchError(f"op {n}: unknown columns {sorted(set(op['row']) - set(cols))}")
            if table == "canaries" and row["kind"] in DERIVED_CANARY_KINDS:
                raise PatchError(f"op {n}: name/alias canaries are derived; add a character_aliases row instead")
            if "review_state" in cols:
                row["review_state"] = target
            dup = _find(pack, table, {c: row[c] for c in key_cols(table)})
            if dup and "review_state" not in cols and dup[0] == row:
                continue    # link row already derived during assembly (e.g. a present relation); the same row in a patch is a no-op
            if dup:
                raise PatchError(f"op {n}: {table} row {item_id(table, row)} already exists")
            pack[table].append(row)
            L(table, item_id(table, row), "*", None, row, "add", op, row)
            continue
        hits = _find(pack, table, op.get("key") or {})
        if len(hits) != 1:
            raise PatchError(f"op {n}: {table} {op.get('key')} matched {len(hits)} rows")
        row = hits[0]
        iid = item_id(table, row)
        if kind == "approve":
            if "review_state" in row:
                set_state(table, row, target, op, "approve")
            else:
                L(table, iid, None, None, None, "approve", op, row)
        elif kind == "edit":
            sets = op.get("set") or {}
            bad = set(sets) - set(cols) | ({"review_state"} & set(sets))
            if bad or not sets:
                raise PatchError(f"op {n}: cannot set {sorted(bad) or 'nothing'}")
            if table == "canaries" and row["kind"] in DERIVED_CANARY_KINDS:
                raise PatchError(f"op {n}: name/alias canaries are derived")
            for f, v in sets.items():
                if row.get(f) != v:
                    L(table, iid, f, row.get(f), v, "edit", op, row)
                    row[f] = v
            set_state(table, row, target, op, "edit")
        elif kind in ("reject", "flag"):
            if "review_state" not in row:
                raise PatchError(f"op {n}: {table} has no review_state; use delete")
            set_state(table, row, "rejected" if kind == "reject" else "flagged", op, "reject" if kind == "reject" else "edit")
        elif kind == "delete":
            if table == "asset":
                raise PatchError("cannot delete asset")
            pack[table].remove(row)
            L(table, iid, "*", row, None, "reject", op, row)
        else:
            raise PatchError(f"op {n}: unknown op {kind!r}")
    return pack
