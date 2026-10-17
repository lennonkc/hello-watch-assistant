"""`vision_shots` stage output → character_exemplars / shot_captions / shot_entities rows.

Characters appear in model text only as `{cNN}`; here they are replaced with "the name or label usable at that time" per the shot's
visible time (spec §6, same rule as check 3 / name canaries), followed by three safeguards: non-public names / aliases become labels;
sentences hitting reviewer canaries (identity clues, spoiler events) are removed, and if every sentence hits, reveal_at_s is delayed
until the canary expires; text hitting the real-person blocklist is dropped.
"""
from __future__ import annotations

import json
import re

from ingest.derive import MARGINS, item_reveal, ref_margin
from ingest.textnorm import has_cjk, term_matches
from ingest.util import r3, read_json, work_dir

M_REV = MARGINS["gate_margin_reviewed_reveal_s"]
TOKEN = re.compile(r"\{\s*(c\d{2,3})\s*\}|\b(c\d{2})\b")
SENT_SPLIT = re.compile(r"(?<=[.!?。!?])\s*")
FIRST_APPEAR_TOL_S = 2.0


# ── names / labels ──────────────────────────────────────────────────────

def name_visible(c: dict, t: float) -> bool:
    if c["name_source"] is None and c["name_known_at_s"] is None:
        return False          # label-only character
    return bool(c["name_public"]) or (c["name_known_at_s"] is not None and t >= c["name_known_at_s"] + M_REV)


def label_for(c: dict, t: float, lang: str, zh_renderings: list[str] | None = None) -> str:
    if name_visible(c, t):
        if lang == "en":
            return c["canonical_name"]
        return c["name_zh"] or (zh_renderings or [None])[0] or c["canonical_name"]
    return c["display_label_pre_name"] if lang == "en" else (c["display_label_pre_name_zh"] or c["display_label_pre_name"])


def fill_tokens(text: str | None, label_of) -> str | None:
    """{cNN} → label_of(cid); capitalize at sentence start."""
    if not text:
        return None

    def rep(m):
        cid = m.group(1) or m.group(2)
        lab = label_of(cid)
        at_start = m.start() == 0 or bool(re.search(r"[.!?]\s*$", text[:m.start()]))
        return lab[:1].upper() + lab[1:] if at_start and lab else lab
    out = TOKEN.sub(rep, text).strip()
    out = re.sub(r"\s{2,}", " ", out)
    if out and out[0].islower():
        out = out[0].upper() + out[1:]
    return out or None


def cap_first(text: str | None) -> str | None:
    return text[:1].upper() + text[1:] if text else text


def replace_term(text: str, term: str, repl: str, cs: int) -> str:
    if has_cjk(term):
        return text.replace(term, repl)
    return re.sub(rf"(?<![\w]){re.escape(term)}(?![\w])", repl, text, flags=0 if cs else re.IGNORECASE)


class Guard:
    """Text safeguards: name visibility, reviewer canaries, real-person names. All judged on the effective view (auto rows + patches)."""

    def __init__(self, asm):
        from ingest.assemble import blocklist_variants
        from ingest.checks import name_terms
        view = effective(asm)
        self.chars = {c["character_id"]: c for c in view["characters"]}
        self.names = name_terms(view)              # (cid, term, K, cs): must not appear before K
        self.canaries = reviewer_canaries(asm)
        self.block = sorted({v for full, _ in asm.people() for v in blocklist_variants(full)})
        self.rend = asm.aux.get("name_renderings", {})

    def names_ok(self, text: str | None, t: float, lang: str) -> str | None:
        if not text:
            return text
        for cid, term, k, cs in self.names:
            if t < k + M_REV and term_matches(term, text, "word", cs):
                text = replace_term(text, term, label_for(self.chars[cid], t, lang, self.rend.get(cid)), cs)
                if term_matches(term, text, "word", cs):
                    return None
        return text

    def canary_hits(self, text: str, t: float) -> list[float]:
        return [k["active_until_s"] for k in self.canaries
                if t < k["active_until_s"] and term_matches(k["term"], text, k["match"], k["case_sensitive"])]

    def redact(self, text: str | None, t: float) -> tuple[str | None, float]:
        """Remove sentences hitting active canaries; return (remaining text, earliest time the full original may be shown)."""
        if not text:
            return text, t
        keep, need = [], t
        for s in [x for x in SENT_SPLIT.split(text) if x.strip()]:
            hits = self.canary_hits(s, t)
            if hits:
                need = max(need, *hits)
            else:
                keep.append(s)
        sep = "" if has_cjk(text) else " "
        return (sep.join(keep).strip() or None), need

    def blocked(self, text: str | None) -> bool:
        return bool(text) and any(term_matches(b, text, "word", False) for b in self.block)

    def clean(self, text: str | None, t: float, lang: str) -> str | None:
        """Short text (action / label / object name): still hits a canary or the blocklist after name replacement → drop."""
        text = self.names_ok(text, t, lang)
        if not text or self.blocked(text) or self.canary_hits(text, t):
            return None
        return text


# ── effective view: auto rows + existing patches (automated pre-review may change names / labels / first appearances, add transliterated aliases and canaries) ───────────

VIEW_TABLES = {"characters": "character_id", "character_looks": "look_id", "character_facts": "fact_id",
               "identity_links": "link_id", "events": "event_id", "objects": "object_id"}


def patch_ops(asm) -> list[dict]:
    pd = work_dir(asm.reg["asset_sha256"]) / "patches"
    return [op for p in (sorted(pd.glob("*.json")) if pd.exists() else []) for op in read_json(p).get("ops", [])]


def effective(asm) -> dict:
    """The "final" characters / aliases / reveal times as seen at assembly: replay patch edits and alias add / delete on copies of the relevant tables; canary adds kept separately.
    shot / speaker rows themselves are auto rows and replay happens at export; this only keeps the safeguards consistent with post-export checks."""
    if getattr(asm, "_eff", None) is not None:
        return asm._eff
    view = {t: [dict(r) for r in asm.pack.get(t, [])] for t in VIEW_TABLES}
    view["character_aliases"] = [dict(a) for a in asm.pack.get("character_aliases", [])]
    view["appearances"] = asm.pack.get("appearances", [])
    view["asset"] = asm.pack["asset"]
    view["canaries"] = []
    idx = {t: {r[k]: r for r in view[t]} for t, k in VIEW_TABLES.items()}
    for op in patch_ops(asm):
        t, kind = op.get("table"), op.get("op")
        if kind == "edit" and t in VIEW_TABLES:
            r = idx[t].get((op.get("key") or {}).get(VIEW_TABLES[t]))
            if r is not None:
                r.update(op.get("set") or {})
        elif kind == "add" and t == "character_aliases":
            view["character_aliases"].append(dict(op["row"]))
        elif kind == "delete" and t == "character_aliases":
            k = op.get("key") or {}
            view["character_aliases"] = [a for a in view["character_aliases"]
                                         if not all(a.get(f) == v for f, v in k.items())]
        elif kind == "add" and t == "canaries":
            view["canaries"].append(dict(op["row"]))
    asm._eff = view
    return view


def reviewer_canaries(asm) -> list[dict]:
    """Reviewer-added canaries in patches (identity clues / spoiler events); active_until derived from the referenced item's reveal time + margin (as in derive)."""
    view = effective(asm)
    out = []
    for r in view["canaries"]:
        if r.get("kind") not in ("identity_clue", "event", "other"):
            continue
        until = r.get("active_until_s") or 0.0
        if r["kind"] != "other":
            rev = item_reveal(view, r["ref_type"], r["ref_id"])
            if rev is None:
                continue
            until = rev + ref_margin(MARGINS, r["ref_type"])
        out.append({"term": r["term"], "match": r.get("match", "word"), "case_sensitive": r.get("case_sensitive", 0),
                    "active_until_s": until})
    return out


# ── time constraints on character attribution ────────────────────────────────────────────────

def attachable(asm, cid: str, s0: float, s1: float) -> bool:
    """Can shot / line [s0, s1] attach to character cid: not before first appearance (2 s tolerance); before an identity link reveal, not to b unless b's appearances cover it (spec §5.3)."""
    view = effective(asm)
    c = next((x for x in view["characters"] if x["character_id"] == cid), None)
    if c is None or c["first_appears_s"] is None or s1 < c["first_appears_s"] - FIRST_APPEAR_TOL_S:
        return False
    for k in view["identity_links"]:
        if k["b_character_id"] == cid and s0 < k["reveal_at_s"]:
            if not any(a["character_id"] == cid and a["start_s"] <= s1 and a["end_s"] >= s0 for a in view["appearances"]):
                return False
    return True


# ── assembly ─────────────────────────────────────────────────────────────

def exemplar_rows(asm, vs: dict) -> list[dict]:
    chars = {c["character_id"]: c for c in effective(asm)["characters"]}
    looks = {lk["look_id"]: lk for lk in asm.pack["character_looks"]}
    src = f"vision:exemplar@{vs.get('model')}"
    out = []
    for e in sorted(vs.get("exemplars", []), key=lambda e: (e["character_id"], e["t_s"])):
        c = chars.get(e["character_id"])
        if c is None or c["first_appears_s"] is None or e["t_s"] < c["first_appears_s"] or not e.get("box"):
            continue
        lk = looks.get(e.get("look_id"))
        out.append({"exemplar_id": f"x{len(out) + 1:03d}", "character_id": e["character_id"], "t_s": int(e["t_s"]),
                    "bbox": json.dumps(e["box"]), "look_id": lk["look_id"] if lk and lk["character_id"] == c["character_id"] else None,
                    "source": src, "review_state": "auto"})
    return out


def shot_rows(asm, vs: dict, g: Guard) -> tuple[list[dict], list[dict]]:
    chars = g.chars
    objs = {o["object_id"]: o for o in asm.pack["objects"]}
    src = f"vision:shots_fps{vs.get('fps', 2):g}@{vs.get('model')}"
    caps, ents = [], []
    stats = {"redacted": 0, "delayed": 0, "detached": 0, "dropped_text": 0}
    for sh in sorted(vs.get("shots", []), key=lambda s: s["start_s"]):
        s0, s1 = sh["start_s"], sh["end_s"]
        if s1 <= s0:
            continue
        ok = {x["ref"] for x in sh["characters"] if x["ref"] in chars and attachable(asm, x["ref"], s0, s1)}
        desc = {x["ref"]: x for x in sh["characters"]}

        def text(lang, t):
            def label_of(cid):
                if cid in ok or (cid in chars and attachable(asm, cid, s0, s1)):
                    return label_for(chars[cid], t, lang, g.rend.get(cid))
                d = desc.get(cid, {}).get(f"descriptor_{lang}")
                return d or ("someone" if lang == "en" else "某人")
            return label_of

        reveal = s1
        en = cap_first(g.names_ok(fill_tokens(sh["caption_en"], text("en", reveal)), reveal, "en"))
        zh = g.names_ok(fill_tokens(sh.get("caption_zh"), text("zh", reveal)), reveal, "zh")
        if not en or g.blocked(en):
            stats["dropped_text"] += 1
            continue
        if g.blocked(zh):
            zh = None
        en2, need_en = g.redact(en, reveal)
        zh2, need_zh = g.redact(zh, reveal)
        if en2 is None:                      # entirely spoiler: keep original text, delay until canaries expire
            reveal = max(need_en, need_zh)
            stats["delayed"] += 1
        else:
            stats["redacted"] += int(en2 != en or zh2 != zh)
            en, zh = en2, zh2
        scid = f"sc{len(caps) + 1:03d}"
        caps.append({"shot_caption_id": scid, "start_s": r3(s0), "end_s": r3(s1), "reveal_at_s": r3(reveal),
                     "caption_en": en, "caption_zh": zh, "source": src, "review_state": "auto"})
        seq = 0
        for x in sh["characters"]:
            cid = x["ref"] if x["ref"] in ok else None
            if x["ref"] in chars and cid is None:
                stats["detached"] += 1
            if cid:
                l_en, l_zh = label_for(chars[cid], reveal, "en"), label_for(chars[cid], reveal, "zh", g.rend.get(cid))
            else:
                l_en = g.clean(x.get("descriptor_en"), reveal, "en") or "someone"
                l_zh = g.clean(x.get("descriptor_zh"), reveal, "zh")
            seq += 1
            ents.append({"shot_caption_id": scid, "seq": seq, "entity_type": "character", "character_id": cid, "object_id": None,
                         "label_en": l_en, "label_zh": l_zh, "position": x.get("position"),
                         "bbox": json.dumps(x["box"]) if x.get("box") and sh.get("keyframe_t") is not None else None,
                         "bbox_t_s": sh.get("keyframe_t") if x.get("box") else None,
                         "action_en": cap_first(g.clean(fill_tokens(x.get("action_en"), text("en", reveal)), reveal, "en")),
                         "action_zh": g.clean(fill_tokens(x.get("action_zh"), text("zh", reveal)), reveal, "zh")})
        for x in sh["objects"]:
            name_en = g.clean(x["name_en"], reveal, "en")
            if not name_en:
                continue
            oid = x.get("ref") if x.get("ref") in objs else None
            seq += 1
            ents.append({"shot_caption_id": scid, "seq": seq, "entity_type": "object", "character_id": None, "object_id": oid,
                         "label_en": name_en, "label_zh": g.clean(x.get("name_zh"), reveal, "zh"), "position": x.get("position"),
                         "bbox": json.dumps(x["box"]) if x.get("box") and sh.get("keyframe_t") is not None else None,
                         "bbox_t_s": sh.get("keyframe_t") if x.get("box") else None, "action_en": None, "action_zh": None})
    asm.aux["shots"] = stats
    return caps, ents


def rows(asm) -> None:
    vs = asm.st.get("vision_shots") or {}
    asm.pack["character_exemplars"] = exemplar_rows(asm, vs)
    if vs.get("shots"):
        asm.pack["shot_captions"], asm.pack["shot_entities"] = shot_rows(asm, vs, Guard(asm))
    else:
        asm.pack["shot_captions"], asm.pack["shot_entities"] = [], []
