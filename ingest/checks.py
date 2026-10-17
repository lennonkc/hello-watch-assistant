"""spec §10 builder checks (11). Any failure → review_complete must not be 1; locatable issues mark the item `flagged`.

Issue entries are `{"msg", "item_type", "item_id", "flag"}`; `flag=False` marks "pending human" issues (e.g. review coverage), which do not change item state.
"""
from __future__ import annotations

import json
import sqlite3
import tempfile

from ingest import packdb
from ingest.derive import margins
from ingest.gate import Gate
from ingest.textnorm import normalize, term_matches, tokens

ID_COLS = {"characters": ["character_id"], "character_looks": ["look_id"], "character_facts": ["fact_id"],
           "identity_links": ["link_id"], "scenes": ["scene_id"], "events": ["event_id"], "objects": ["object_id"],
           "shots": ["shot_id"], "segments": ["segment_id"], "cues": ["cue_id"], "subtitle_tracks": ["track"],
           "title_info": ["field"], "blocklist": ["person_ref"], "item_characters": ["item_id"], "item_cues": ["item_id"],
           "canaries": ["ref_id"], "embeddings": ["item_id"]}
STATEFUL = {"characters": "character_id", "character_looks": "look_id", "character_facts": "fact_id",
            "identity_links": "link_id", "scenes": "scene_id", "events": "event_id", "objects": "object_id"}
ITEM_TABLE = {"scene": ("scenes", "scene_id"), "event": ("events", "event_id"), "character_fact": ("character_facts", "fact_id"),
              "character_look": ("character_looks", "look_id"), "object": ("objects", "object_id"),
              "identity_link": ("identity_links", "link_id"), "cue": ("cues", "cue_id"), "character": ("characters", "character_id"),
              "shot_caption": ("shot_captions", "shot_caption_id"), "recap": ("recaps", "recap_id")}
HUMAN = lambda r: not r["reviewer"].startswith("claude-")  # noqa: E731


def _row(pack, item_type, item_id):
    t = ITEM_TABLE.get(item_type)
    if not t:
        return None
    return next((r for r in pack[t[0]] if r[t[1]] == item_id), None)


def visible_time(pack, item_type, item_id) -> float | None:
    r = _row(pack, item_type, item_id)
    if r is None:
        return None
    if item_type == "cue":
        return r["end_s"]
    if item_type == "character":
        return r["first_appears_s"]
    return r["reveal_at_s"]


def item_interval(pack, item_type, item_id) -> tuple[float, float] | None:
    """Item time for identity isolation: cues use their interval, looks use [from_s, reveal_at_s], everything else its visibility time (scenes = visible after they end)."""
    r = _row(pack, item_type, item_id)
    if r is None:
        return None
    if item_type == "cue":
        return r["start_s"], r["end_s"]
    if item_type == "character_look":
        return r["from_s"], r["reveal_at_s"]
    v = visible_time(pack, item_type, item_id)
    return v, v


def timed_texts(pack) -> list[tuple[str, str, float, str, str | None]]:
    """(item_type, item_id, visibility time, text, owning character). Cues are the film's own text and are excluded."""
    out = []
    ss = pack["asset"]["story_start_s"]
    live = lambda rows: [r for r in rows if r.get("review_state") != "rejected"]  # noqa: E731 - rejected rows are never output
    for s in live(pack["scenes"]):
        out += [("scene", s["scene_id"], s["reveal_at_s"], s[f], None) for f in ("summary_en", "summary_zh", "location_en") if s[f]]
    for e in live(pack["events"]):
        out += [("event", e["event_id"], e["reveal_at_s"], e[f], None) for f in ("text_en", "text_zh") if e[f]]
    for f in live(pack["character_facts"]):
        out += [("character_fact", f["fact_id"], f["reveal_at_s"], f[k], f["character_id"]) for k in ("text_en", "text_zh") if f[k]]
    for lk in live(pack["character_looks"]):
        out += [("character_look", lk["look_id"], lk["reveal_at_s"], lk[k], lk["character_id"])
                for k in ("description_en", "description_zh") if lk[k]]
    for o in live(pack["objects"]):
        out += [("object", o["object_id"], o["reveal_at_s"], o[k], None) for k in ("name_en", "name_zh", "description_en",
                                                                                     "description_zh") if o[k]]
    for k in live(pack["identity_links"]):
        out += [("identity_link", k["link_id"], k["reveal_at_s"], k[f], None) for f in ("text_en", "text_zh") if k[f]]
    for t in pack["title_info"]:
        out += [("title_info", t["field"], ss, t[f], None) for f in ("text_en", "text_zh") if t[f]]
    shot_t = {}
    for s in live(pack.get("shot_captions", [])):
        shot_t[s["shot_caption_id"]] = s["reveal_at_s"]
        out += [("shot_caption", s["shot_caption_id"], s["reveal_at_s"], s[f], None) for f in ("caption_en", "caption_zh") if s[f]]
    for x in pack.get("shot_entities", []):
        if x["shot_caption_id"] in shot_t:
            out += [("shot_caption", x["shot_caption_id"], shot_t[x["shot_caption_id"]], x[f], x["character_id"])
                    for f in ("label_en", "label_zh", "action_en", "action_zh") if x[f]]
    for r in live(pack.get("recaps", [])):
        out += [("recap", r["recap_id"], r["reveal_at_s"], r[f], None) for f in ("text_en", "text_zh") if r[f]]
    for c in live(pack["characters"]):
        if c["first_appears_s"] is not None:
            out += [("character", c["character_id"], c["first_appears_s"], c[f], c["character_id"])
                    for f in ("display_label_pre_name", "display_label_pre_name_zh") if c[f]]
    for a in pack["character_aliases"]:
        out.append(("character", a["character_id"], a["alias_known_at_s"], a["alias"], a["character_id"]))
    return out


class Report:
    def __init__(self):
        self.checks = []

    def add(self, cid: int, name: str, issues: list[dict]):
        self.checks.append({"id": cid, "name": name, "ok": not issues, "issues": issues})


def _issue(msg, item_type=None, item_id=None, flag=True):
    return {"msg": msg, "item_type": item_type, "item_id": item_id, "flag": bool(flag and item_type)}


# ── 1 structure ──────────────────────────────────────────────────────────────

def check_structure(pack, fts) -> list[dict]:
    iss = []
    with tempfile.TemporaryDirectory() as td:
        try:
            packdb.write_db(pack, f"{td}/a.db", fts)
            back = packdb.read_db(f"{td}/a.db")
            js = json.loads(json.dumps(packdb.normalize_pack(pack)))
            packdb.write_db(js, f"{td}/b.db", fts)
            if packdb.read_db(f"{td}/b.db") != back or back != packdb.normalize_pack(pack):
                iss.append(_issue("pack.json ⇄ pack.db round trip differs"))
        except sqlite3.Error as e:
            iss.append(_issue(f"DDL / foreign key: {e}"))
    dur = pack["asset"]["duration_s"]
    for t in packdb.tables():
        if t in ("pack_meta", "asset", "embeddings"):
            continue
        for r in pack[t]:
            for c, v in r.items():
                if (c.endswith("_s") and c not in ("offset_s",) or c == "t_s") and isinstance(v, (int, float)) \
                        and not (0 <= v <= dur + 1e-6):
                    key = STATEFUL.get(t)
                    iss.append(_issue(f"{t}.{c}={v} outside [0, {dur}]", ITEM_TABLE_REV.get(t), r.get(key) if key else None))
    for c in ("story_start_s", "story_end_s", "credits_start_s"):
        v = pack["asset"][c]
        if v is not None and not 0 <= v <= dur:
            iss.append(_issue(f"asset.{c}={v} outside [0, {dur}]"))
    segs = sorted(pack["segments"], key=lambda s: s["start_s"])
    if not segs or abs(segs[0]["start_s"]) > 0.5 or abs(segs[-1]["end_s"] - dur) > 0.5:
        iss.append(_issue("segments do not cover [0, duration] (±0.5 s)"))
    for a, b in zip(segs, segs[1:]):
        if abs(b["start_s"] - a["end_s"]) > 0.5:
            iss.append(_issue(f"segments {a['segment_id']}→{b['segment_id']} not contiguous"))
    for x in pack["item_characters"] + pack["item_cues"]:
        if x["item_type"] in ITEM_TABLE and _row(pack, x["item_type"], x["item_id"]) is None:
            iss.append(_issue(f"dangling {x['item_type']} {x['item_id']} in link table"))
    for x in pack["item_characters"]:
        if x["item_type"] not in ("scene", "event", "cue", "character_fact", "character_look", "object"):
            iss.append(_issue(f"item_characters.item_type {x['item_type']} not allowed"))
    for x in pack["item_cues"]:
        if x["item_type"] not in ("event", "character_fact", "scene"):
            iss.append(_issue(f"item_cues.item_type {x['item_type']} not allowed"))
    return iss


ITEM_TABLE_REV = {v[0]: k for k, v in ITEM_TABLE.items()}


# ── 2 opaque ids ─────────────────────────────────────────────────────────

def check_ids(pack) -> list[dict]:
    toks = set()
    for c in pack["characters"]:
        if c["name_source"] is not None or c["name_known_at_s"] is not None:
            for n in (c["canonical_name"], c["name_zh"]):
                toks.update(tokens(n or ""))
    for a in pack["character_aliases"]:
        if not a["generic"]:
            toks.update(tokens(a["alias"]))
    for k in pack["canaries"]:
        toks.update(tokens(k["term"]))
    iss = []
    for t, cols in ID_COLS.items():
        for r in pack.get(t, []):
            for c in cols:
                v = normalize(str(r[c] or ""))
                hit = next((w for w in toks if w and w in v), None)
                if hit:
                    key = STATEFUL.get(t)
                    iss.append(_issue(f"{t}.{c} '{r[c]}' contains name/canary token", ITEM_TABLE_REV.get(t),
                                      r.get(key) if key else None))
    return iss


# ── 3 name visibility ────────────────────────────────────────────────────────

def name_terms(pack) -> list[tuple[str, str, float, int]]:
    """(character_id, term, usable-from time K, case_sensitive): non-public names and non-generic aliases."""
    from ingest.derive import name_case_sensitive
    out = []
    dur = pack["asset"]["duration_s"]
    for c in pack["characters"]:
        named = c["name_source"] is not None or c["name_known_at_s"] is not None
        if named and not c["name_public"]:
            k = c["name_known_at_s"] if c["name_known_at_s"] is not None else dur + 1
            for n in (c["canonical_name"], c["name_zh"]):
                if n:
                    out.append((c["character_id"], n, k, name_case_sensitive(n)))
    for a in pack["character_aliases"]:
        if not a["generic"]:
            out.append((a["character_id"], a["alias"], a["alias_known_at_s"], name_case_sensitive(a["alias"])))
    return out


def check_names(pack) -> list[dict]:
    iss = []
    terms = name_terms(pack)
    for it, iid, v, text, owner in timed_texts(pack):
        for cid, term, k, cs in terms:
            if v is not None and v < k and term_matches(term, text, "word", cs):
                if it == "character" and normalize(text) == normalize(term):
                    continue  # the alias row itself
                iss.append(_issue(f"{it} {iid} (visible {v:.2f}s) uses '{term}' of {cid} before {k:.2f}s", it, iid))
    return iss


# ── 4 supporting cues / 5 mention order ─────────────────────────────────────────────

def check_cues(pack) -> list[dict]:
    iss = []
    cue_end = {c["cue_id"]: c["end_s"] for c in pack["cues"]}
    for it, rows, key, cond in (("event", pack["events"], "event_id", lambda r: r["kind"] == "dialogue"),
                                ("character_fact", pack["character_facts"], "fact_id", lambda r: r["basis"] == "dialogue")):
        for r in rows:
            if not cond(r) or r["review_state"] == "rejected":
                continue
            linked = [x["cue_id"] for x in pack["item_cues"] if x["item_type"] == it and x["item_id"] == r[key]]
            if not linked:
                iss.append(_issue(f"{it} {r[key]} has no supporting cue", it, r[key]))
            elif r["reveal_at_s"] < max(cue_end[c] for c in linked) - 1e-6:
                iss.append(_issue(f"{it} {r[key]} reveal_at {r['reveal_at_s']} < supporting cue end "
                                  f"{max(cue_end[c] for c in linked)}", it, r[key]))
    return iss


def check_mentions(pack) -> list[dict]:
    first = {c["character_id"]: c["first_appears_s"] for c in pack["characters"]}
    iss = []
    for x in pack["item_characters"]:
        if x["relation"] != "mentioned":
            continue
        row = _row(pack, x["item_type"], x["item_id"])
        if row is not None and row.get("review_state") == "rejected":
            continue
        v = visible_time(pack, x["item_type"], x["item_id"])
        f = first.get(x["character_id"])
        if v is not None and f is not None and v < f:
            iss.append(_issue(f"{x['item_type']} {x['item_id']} (visible {v}) mentions {x['character_id']} "
                              f"before its first appearance {f}", x["item_type"], x["item_id"]))
    return iss


# ── 6 identity isolation / 7 twist timing ─────────────────────────────────────────────

def _clue_canaries(pack, link_id):
    return [k for k in pack["canaries"] if k["kind"] == "identity_clue" and k["ref_type"] == "identity_link"
            and k["ref_id"] == link_id]


def check_identity(pack) -> list[dict]:
    iss = []
    first = {c["character_id"]: c["first_appears_s"] for c in pack["characters"]}
    for k in pack["identity_links"]:
        if k["review_state"] == "rejected":
            continue
        a, b, R = k["a_character_id"], k["b_character_id"], k["reveal_at_s"]
        fa = first.get(a)
        if fa is None:
            continue
        b_aps = [(x["start_s"], x["end_s"]) for x in pack["appearances"] if x["character_id"] == b
                 and x["review_state"] != "rejected"]

        def violates(lo, hi):
            lo2, hi2 = max(lo, fa), min(hi, R)
            if lo2 > hi2 or lo2 >= R:
                return False
            return not any(s <= hi2 and e >= lo2 for s, e in b_aps)

        for x in pack["item_characters"]:
            if x["character_id"] != b:
                continue
            row = _row(pack, x["item_type"], x["item_id"])
            if row is not None and row.get("review_state") == "rejected":
                continue
            iv = item_interval(pack, x["item_type"], x["item_id"])
            if iv and violates(*iv):
                iss.append(_issue(f"{x['item_type']} {x['item_id']} attached to {b} ({x['relation']}) inside "
                                  f"[{fa}, {R}) of link {k['link_id']}", x["item_type"], x["item_id"]))
        for f in pack["character_facts"]:
            if f["character_id"] == b and f["review_state"] != "rejected" and violates(f["reveal_at_s"], f["reveal_at_s"]):
                iss.append(_issue(f"fact {f['fact_id']} of {b} inside [{fa}, {R}) of {k['link_id']}", "character_fact", f["fact_id"]))
        for lk in pack["character_looks"]:
            if lk["character_id"] == b and lk["review_state"] != "rejected" and violates(lk["from_s"], lk["reveal_at_s"]):
                iss.append(_issue(f"look {lk['look_id']} of {b} inside [{fa}, {R}) of {k['link_id']}", "character_look", lk["look_id"]))
        for it, iid, v, text, _ in timed_texts(pack):
            if v is None or v >= R:
                continue
            for cn in _clue_canaries(pack, k["link_id"]):
                if term_matches(cn["term"], text, cn["match"], cn["case_sensitive"]):
                    iss.append(_issue(f"{it} {iid} (visible {v}) contains clue '{cn['term']}' of {k['link_id']} (R={R})", it, iid))
    return iss


def human_spoiler_record(pack, item_type, item_id) -> bool:
    return any(r["item_type"] == item_type and r["item_id"] == item_id and r["spoiler_critical"] == 1
               and r["decision"] in ("approve", "edit") and HUMAN(r) for r in pack["review_log"])


def check_reveal_timing(pack) -> list[dict]:
    from ingest.derive import name_case_sensitive
    iss = []
    chars = {c["character_id"]: c for c in pack["characters"]}
    bset = {k["b_character_id"] for k in pack["identity_links"]}

    def names_of(cid):
        c = chars[cid]
        ns = [(c["canonical_name"], name_case_sensitive(c["canonical_name"]))]
        if c["name_zh"]:
            ns.append((c["name_zh"], 0))
        ns += [(a["alias"], name_case_sensitive(a["alias"])) for a in pack["character_aliases"]
               if a["character_id"] == cid and not a["generic"]]
        return [(n, "word", cs) for n, cs in ns]

    def check(item_type, item_id, R, terms):
        C = [c for c in pack["cues"] if R - 20 <= c["start_s"] <= R + 20
             and any(term_matches(t, c["text"], m, cs) for t, m, cs in terms)]
        if C:
            first_c = min(C, key=lambda c: c["start_s"])
            if R < first_c["start_s"]:
                iss.append(_issue(f"{item_type} {item_id} reveal_at {R} earlier than confirming cue {first_c['cue_id']} "
                                  f"at {first_c['start_s']}", item_type, item_id))
        elif not human_spoiler_record(pack, item_type, item_id):
            iss.append(_issue(f"{item_type} {item_id}: no confirming cue near {R}; needs a human spoiler_critical "
                              f"confirmation in review_log", item_type, item_id, flag=False))

    for k in pack["identity_links"]:
        if k["review_state"] != "rejected":
            check("identity_link", k["link_id"], k["reveal_at_s"], names_of(k["b_character_id"]))
    for e in pack["events"]:
        if e["kind"] != "reveal" or e["review_state"] == "rejected":
            continue
        terms = [(x["term"], x["match"], x["case_sensitive"]) for x in pack["canaries"]
                 if x["ref_type"] == "event" and x["ref_id"] == e["event_id"]]
        for x in pack["item_characters"]:
            if x["item_type"] == "event" and x["item_id"] == e["event_id"] and x["character_id"] in bset:
                terms += names_of(x["character_id"])
        check("event", e["event_id"], e["reveal_at_s"], terms)
    return iss


# ── 8 review coverage ──────────────────────────────────────────────────────────

def check_coverage(pack) -> list[dict]:
    iss = []
    idchars = {c for k in pack["identity_links"] for c in (k["a_character_id"], k["b_character_id"])}
    need = []
    need += [("character", c["character_id"], c) for c in pack["characters"]]
    need += [("character_fact", f["fact_id"], f) for f in pack["character_facts"]]
    need += [("character_look", x["look_id"], x) for x in pack["character_looks"]]
    need += [("identity_link", x["link_id"], x) for x in pack["identity_links"]]
    need += [("event", x["event_id"], x) for x in pack["events"] if x["kind"] == "reveal" or x["spoiler_critical"]]
    need += [("object", x["object_id"], x) for x in pack["objects"] if x["spoiler_critical"]]
    need += [("appearance", f"{x['character_id']}@{x['start_s']}", x) for x in pack["appearances"] if x["character_id"] in idchars]
    for it, iid, r in need:
        if r["review_state"] not in ("reviewed", "rejected"):
            iss.append(_issue(f"{it} {iid} needs human review (state {r['review_state']})", it, iid, flag=False))
    # conservative reading: items still auto / flagged (any table) also count as review incomplete
    for t, key in STATEFUL.items():
        for r in pack[t]:
            if r["review_state"] in ("auto", "flagged") and not any(x[1] == r[key] for x in need if x[0] == ITEM_TABLE_REV[t]):
                iss.append(_issue(f"{ITEM_TABLE_REV[t]} {r[key]} still {r['review_state']}", ITEM_TABLE_REV[t], r[key], flag=False))
    for a in pack["appearances"]:
        if a["review_state"] in ("auto", "flagged") and a["character_id"] not in idchars:
            iss.append(_issue(f"appearance {a['character_id']}@{a['start_s']} still {a['review_state']}", "appearance",
                              f"{a['character_id']}@{a['start_s']}", flag=False))
    titled = {r["item_id"] for r in pack["review_log"] if r["item_type"] == "title_info" and HUMAN(r)
              and r["decision"] in ("approve", "edit")}
    for t in pack["title_info"]:
        if t["field"] not in titled:
            iss.append(_issue(f"title_info {t['field']} needs human review", "title_info", t["field"], flag=False))

    def langs(kind, ref_type, ref_id):
        return {k["lang"] for k in pack["canaries"] if k["kind"] == kind and k["ref_type"] == ref_type and k["ref_id"] == ref_id}

    for k in pack["identity_links"]:
        if k["review_state"] != "rejected":
            miss = {"en", "zh"} - langs("identity_clue", "identity_link", k["link_id"])
            if miss:
                iss.append(_issue(f"identity_link {k['link_id']} lacks identity_clue canaries for {sorted(miss)}",
                                  "identity_link", k["link_id"], flag=False))
    for it, rows, key in (("event", pack["events"], "event_id"), ("character_fact", pack["character_facts"], "fact_id"),
                          ("object", pack["objects"], "object_id"), ("character_look", pack["character_looks"], "look_id")):
        for r in rows:
            if r["spoiler_critical"] and r["review_state"] != "rejected":
                miss = {"en", "zh"} - {k["lang"] for k in pack["canaries"] if k["kind"] in ("event", "other")
                                       and k["ref_type"] == it and k["ref_id"] == r[key]}
                if miss:
                    iss.append(_issue(f"spoiler_critical {it} {r[key]} lacks event canaries for {sorted(miss)}", it, r[key], flag=False))
    for k in pack["canaries"]:
        if k["kind"] in ("name", "alias") and k["lang"] == "en":
            if not any(x["kind"] in ("name", "alias") and x["lang"] == "zh" and x["ref_type"] == k["ref_type"]
                       and x["ref_id"] == k["ref_id"] for x in pack["canaries"]):
                iss.append(_issue(f"{k['kind']} canary '{k['term']}' ({k['ref_id']}) has no zh counterpart", "character",
                                  k["ref_id"], flag=False))
    return iss


# ── 9 real-person names / 10 character cards ──────────────────────────────────────────────

def check_blocklist(pack) -> list[dict]:
    iss = []
    texts = [(it, iid, text) for it, iid, _, text, _ in timed_texts(pack)]
    texts += [("character", c["character_id"], c[f]) for c in pack["characters"] for f in ("canonical_name", "name_zh") if c[f]]
    texts += [("cue", c["cue_id"], c["text"]) for c in pack["cues"]]
    for it, iid, text in texts:
        for b in pack["blocklist"]:
            if term_matches(b["term"], text, "word", False):
                iss.append(_issue(f"{it} {iid} contains blocklisted real-person term ({b['person_ref']})", it, iid))
    return iss


def check_cards(pack) -> list[dict]:
    iss = []
    for c in pack["characters"]:
        if c["role"] == "mentioned_only" or c["review_state"] == "rejected":
            continue
        cid = c["character_id"]
        miss = []
        if c["first_appears_s"] is None:
            miss.append("first_appears_s")
        if not any(a["character_id"] == cid and a["review_state"] != "rejected" for a in pack["appearances"]):
            miss.append("appearances")
        if not any(lk["character_id"] == cid and lk["review_state"] != "rejected" for lk in pack["character_looks"]):
            miss.append("character_looks")
        if miss:
            iss.append(_issue(f"character {cid} card incomplete: {', '.join(miss)}", "character", cid))
    return iss


# ── 11 canary scan ───────────────────────────────────────────────────────

def _item_of(it, iid):
    if it == "character_alias":
        return "character", iid.split("/")[0]
    return it, iid


def check_canary_scan(pack) -> list[dict]:
    m = margins(pack)
    pts = {round(k["active_until_s"] - 0.01, 3) for k in pack["canaries"]}
    for s in pack["scenes"]:
        pts.add(round(s["reveal_at_s"] + (m["gate_margin_scene_snapped_s"] if s["snapped"] else m["gate_margin_scene_unsnapped_s"]), 3))
    for rows, mk in ((pack["events"], "gate_margin_event_s"), (pack["objects"], "gate_margin_object_s"),
                     (pack["character_facts"], "gate_margin_reviewed_reveal_s"),
                     (pack["character_looks"], "gate_margin_reviewed_reveal_s"),
                     (pack["identity_links"], "gate_margin_reviewed_reveal_s")):
        for r in rows:
            pts.add(round(r["reveal_at_s"] + m[mk], 3))
    pts = sorted(p for p in pts if p >= 0)
    iss, seen = [], set()
    for assume in (False, True):
        g = Gate(pack, assume_reviewed=assume)
        for P in pts:
            active = [k for k in pack["canaries"] if P < k["active_until_s"]]
            if not active:
                continue
            for it, iid, field, text in g.texts(P):
                for k in active:
                    if term_matches(k["term"], text, k["match"], k["case_sensitive"]):
                        key = (it, iid, k["term"], assume)
                        if key in seen:
                            continue
                        seen.add(key)
                        t, i = _item_of(it, iid)
                        mode = "if approved" if assume else "as gated"
                        iss.append(_issue(f"P={P}: {it} {iid}.{field} hits {k['kind']} canary '{k['term']}' "
                                          f"(active until {k['active_until_s']}; {mode})", t if t != "cue" else None, i))
    return iss


def run_checks(pack: dict, fts: list[dict]) -> dict:
    rep = Report()
    rep.add(1, "structure", check_structure(pack, fts))
    rep.add(2, "opaque_ids", check_ids(pack))
    rep.add(3, "name_visibility", check_names(pack))
    rep.add(4, "supporting_cues", check_cues(pack))
    rep.add(5, "mention_order", check_mentions(pack))
    rep.add(6, "identity_isolation", check_identity(pack))
    rep.add(7, "reveal_timing", check_reveal_timing(pack))
    rep.add(8, "review_coverage", check_coverage(pack))
    rep.add(9, "real_person_names", check_blocklist(pack))
    rep.add(10, "character_cards", check_cards(pack))
    rep.add(11, "canary_scan", check_canary_scan(pack))
    flags = sorted({(i["item_type"], i["item_id"]) for c in rep.checks for i in c["issues"] if i["flag"]},
                   key=lambda x: (x[0], str(x[1])))
    return {"checks": rep.checks, "ok": all(c["ok"] for c in rep.checks), "flag_items": [list(x) for x in flags]}
