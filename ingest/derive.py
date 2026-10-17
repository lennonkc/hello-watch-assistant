"""Parts derived from content tables: margin defaults, name / alias canaries, active_until of reviewer canaries, text_fts."""
from __future__ import annotations

import re

from ingest.textnorm import has_cjk, is_common, normalize

MARGINS = {
    "gate_margin_cue_s": 1.0,
    "gate_margin_scene_snapped_s": 3.0,
    "gate_margin_scene_unsnapped_s": 5.0,
    "gate_margin_event_s": 3.0,
    "gate_margin_object_s": 2.0,
    "gate_margin_first_appears_s": 2.0,
    "gate_margin_reviewed_reveal_s": 1.0,
}
REVIEWER_KINDS = ("identity_clue", "event", "other")


def margins(pack: dict) -> dict:
    m = dict(MARGINS)
    for k in MARGINS:
        if k in pack.get("pack_meta", {}):
            m[k] = float(pack["pack_meta"][k])
    return m


def name_case_sensitive(term: str) -> int:
    """Names identical to common nouns (e.g. Scales) are case-sensitive."""
    if has_cjk(term):
        return 0
    ws = re.findall(r"[A-Za-z]+", term)
    return int(bool(ws) and all(is_common(w) for w in ws))


def item_reveal(pack: dict, ref_type: str, ref_id: str) -> float | None:
    key = {"identity_link": ("identity_links", "link_id"), "event": ("events", "event_id"),
           "character_fact": ("character_facts", "fact_id"), "object": ("objects", "object_id"),
           "character_look": ("character_looks", "look_id")}.get(ref_type)
    if not key:
        return None
    row = next((r for r in pack[key[0]] if r[key[1]] == ref_id), None)
    return None if row is None else row["reveal_at_s"]


def ref_margin(m: dict, ref_type: str) -> float:
    return {"event": m["gate_margin_event_s"], "object": m["gate_margin_object_s"]}.get(ref_type, m["gate_margin_reviewed_reveal_s"])


def derive_canaries(pack: dict) -> list[dict]:
    m = margins(pack)
    m_rev = m["gate_margin_reviewed_reveal_s"]
    dur = pack["asset"]["duration_s"]
    out: dict[tuple, dict] = {}

    def add(term, lang, match, cs, until, kind, ref_type, ref_id):
        k = (term, lang, ref_type, ref_id)
        if k not in out:
            out[k] = {"term": term, "lang": lang, "match": match, "case_sensitive": cs, "active_until_s": round(until, 3),
                      "kind": kind, "ref_type": ref_type, "ref_id": ref_id}

    for c in pack["characters"]:
        is_label = c["name_source"] is None and c["name_known_at_s"] is None
        cid = c["character_id"]
        name_terms = set()
        if not is_label and not c["name_public"]:
            until = (c["name_known_at_s"] if c["name_known_at_s"] is not None else dur) + m_rev
            for term, lang in ((c["canonical_name"], "en"), (c["name_zh"], "zh")):
                if term:
                    add(term, lang, "word", name_case_sensitive(term), until, "name", "character", cid)
                    name_terms.add(normalize(term))
        for a in pack["character_aliases"]:
            if a["character_id"] != cid or a["generic"] or normalize(a["alias"]) in name_terms:
                continue
            lang = "zh" if has_cjk(a["alias"]) else "en"
            add(a["alias"], lang, "word", name_case_sensitive(a["alias"]), a["alias_known_at_s"] + m_rev, "alias", "character", cid)
    for r in pack["canaries"]:
        if r["kind"] not in REVIEWER_KINDS:
            continue
        r = dict(r)
        rev = item_reveal(pack, r["ref_type"], r["ref_id"])
        if r["kind"] != "other" and rev is not None:
            r["active_until_s"] = round(rev + ref_margin(m, r["ref_type"]), 3)
        out[(r["term"], r["lang"], r["ref_type"], r["ref_id"])] = r
    return list(out.values())


def fts_rows(pack: dict) -> list[dict]:
    rows = []
    lang_of = {t["track"]: t["lang"] for t in pack["subtitle_tracks"]}

    def add(item_type, item_id, t_s, lang, text):
        if text:
            rows.append({"item_type": item_type, "item_id": item_id, "t_s": t_s, "lang": lang, "text": text})

    for c in pack["cues"]:
        add("cue", c["cue_id"], c["end_s"], lang_of.get(c["track"], "und"), c["text"])
    for s in pack["scenes"]:
        add("scene", s["scene_id"], s["reveal_at_s"], "en", s["summary_en"])
        add("scene", s["scene_id"], s["reveal_at_s"], "zh", s["summary_zh"])
    for e in pack["events"]:
        add("event", e["event_id"], e["reveal_at_s"], "en", e["text_en"])
        add("event", e["event_id"], e["reveal_at_s"], "zh", e["text_zh"])
    for f in pack["character_facts"]:
        add("character_fact", f["fact_id"], f["reveal_at_s"], "en", f["text_en"])
        add("character_fact", f["fact_id"], f["reveal_at_s"], "zh", f["text_zh"])
    for lk in pack["character_looks"]:
        add("character_look", lk["look_id"], lk["reveal_at_s"], "en", lk["description_en"])
        add("character_look", lk["look_id"], lk["reveal_at_s"], "zh", lk["description_zh"])
    for o in pack["objects"]:
        add("object", o["object_id"], o["reveal_at_s"], "en", f"{o['name_en']}: {o['description_en']}")
        if o["name_zh"] or o["description_zh"]:
            add("object", o["object_id"], o["reveal_at_s"], "zh", f"{o['name_zh'] or ''}:{o['description_zh'] or ''}")
    for k in pack["identity_links"]:
        add("identity_link", k["link_id"], k["reveal_at_s"], "en", k["text_en"])
        add("identity_link", k["link_id"], k["reveal_at_s"], "zh", k["text_zh"])
    for t in pack["title_info"]:
        add("title_info", t["field"], pack["asset"]["story_start_s"], "en", t["text_en"])
        add("title_info", t["field"], pack["asset"]["story_start_s"], "zh", t["text_zh"])
    for s in pack.get("shot_captions", []):
        add("shot_caption", s["shot_caption_id"], s["reveal_at_s"], "en", s["caption_en"])
        add("shot_caption", s["shot_caption_id"], s["reveal_at_s"], "zh", s["caption_zh"])
    for r in pack.get("recaps", []):
        add("recap", r["recap_id"], r["reveal_at_s"], "en", r["text_en"])
        add("recap", r["recap_id"], r["reveal_at_s"], "zh", r["text_zh"])
    rows.sort(key=lambda r: (r["item_type"], str(r["item_id"]), r["lang"]))
    return rows
