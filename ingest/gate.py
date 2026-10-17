"""Reference implementation of the spec §5 eligibility rules (used by builder check 11; the canonical one lives in the online service).

`assume_reviewed=True`: treat auto / prereviewed as reviewed to simulate the eligible set after "everything passes review", catching problems early.
"""
from __future__ import annotations

from ingest.derive import margins

PRE = {"prereviewed", "reviewed"}


def human_reviewed_title_fields(pack: dict) -> set[str]:
    return {r["item_id"] for r in pack.get("review_log", []) if r["item_type"] == "title_info"
            and r["decision"] in ("approve", "edit") and not r["reviewer"].startswith("claude-")}


class Gate:
    def __init__(self, pack: dict, assume_reviewed: bool = False):
        self.p = pack
        self.m = margins(pack)
        self.assume = assume_reviewed
        self.idchars = {c for k in pack["identity_links"] for c in (k["a_character_id"], k["b_character_id"])}
        self.title_ok = human_reviewed_title_fields(pack)

    def ok_pre(self, row) -> bool:
        return row["review_state"] in PRE or (self.assume and row["review_state"] == "auto")

    def ok_rev(self, row) -> bool:
        return row["review_state"] == "reviewed" or (self.assume and row["review_state"] in ("prereviewed", "auto"))

    def eligible(self, P: float) -> dict:
        p, m = self.p, self.m
        a = p["asset"]
        out = {k: [] for k in ("cues", "scenes", "events", "objects", "appearances", "characters", "character_aliases",
                               "character_facts", "character_looks", "identity_links", "title_info")}
        out["title_info"] = [t for t in p["title_info"] if (t["field"] in self.title_ok or self.assume)
                             and (P >= a["story_start_s"] or t["field"] == "title")]
        if P < a["story_start_s"]:
            return out
        mr = m["gate_margin_reviewed_reveal_s"]
        out["cues"] = [c for c in p["cues"] if c["kind"] != "lyrics" and c["end_s"] + m["gate_margin_cue_s"] <= P]
        out["scenes"] = [s for s in p["scenes"] if self.ok_pre(s) and s["reveal_at_s"] + (
            m["gate_margin_scene_snapped_s"] if s["snapped"] else m["gate_margin_scene_unsnapped_s"]) <= P]
        out["events"] = [e for e in p["events"] if (self.ok_rev(e) if (e["kind"] == "reveal" or e["spoiler_critical"])
                                                     else self.ok_pre(e)) and e["reveal_at_s"] + m["gate_margin_event_s"] <= P]
        out["objects"] = [o for o in p["objects"] if (self.ok_rev(o) if o["spoiler_critical"] else self.ok_pre(o))
                          and o["reveal_at_s"] + m["gate_margin_object_s"] <= P]
        chars = {c["character_id"]: c for c in p["characters"] if self.ok_rev(c) and c["first_appears_s"] is not None
                 and c["first_appears_s"] + m["gate_margin_first_appears_s"] <= P}
        out["characters"] = list(chars.values())
        out["appearances"] = [dict(x, end_s=min(x["end_s"], P)) for x in p["appearances"]
                              if (self.ok_rev(x) if x["character_id"] in self.idchars else self.ok_pre(x)) and x["start_s"] <= P]
        out["character_aliases"] = [x for x in p["character_aliases"] if x["character_id"] in chars
                                    and x["alias_known_at_s"] + mr <= P]
        out["character_facts"] = [f for f in p["character_facts"] if f["character_id"] in chars and self.ok_rev(f)
                                  and f["reveal_at_s"] + mr <= P]
        best = {}
        for lk in p["character_looks"]:
            if lk["character_id"] in chars and self.ok_rev(lk) and lk["from_s"] <= P and lk["reveal_at_s"] + mr <= P:
                if lk["character_id"] not in best or lk["from_s"] > best[lk["character_id"]]["from_s"]:
                    best[lk["character_id"]] = lk
        out["character_looks"] = list(best.values())
        out["identity_links"] = [k for k in p["identity_links"] if self.ok_rev(k) and k["reveal_at_s"] + mr <= P]
        return out

    def name_usable(self, c: dict, P: float) -> bool:
        return bool(c["name_public"]) or (c["name_known_at_s"] is not None
                                          and c["name_known_at_s"] + self.m["gate_margin_reviewed_reveal_s"] <= P)

    def texts(self, P: float) -> list[tuple[str, str, str, str]]:
        """All text that may be output at time P: (item_type, item_id, field, text)."""
        e = self.eligible(P)
        out = []
        for c in e["cues"]:
            out.append(("cue", c["cue_id"], "text", c["text"]))
        for s in e["scenes"]:
            out += [("scene", s["scene_id"], f, s[f]) for f in ("summary_en", "summary_zh", "location_en") if s[f]]
        for x in e["events"]:
            out += [("event", x["event_id"], f, x[f]) for f in ("text_en", "text_zh") if x[f]]
        for o in e["objects"]:
            out += [("object", o["object_id"], f, o[f]) for f in ("name_en", "name_zh", "description_en", "description_zh") if o[f]]
        for c in e["characters"]:
            fs = ("canonical_name", "name_zh") if self.name_usable(c, P) else ("display_label_pre_name", "display_label_pre_name_zh")
            out += [("character", c["character_id"], f, c[f]) for f in fs if c[f]]
        for a in e["character_aliases"]:
            out.append(("character_alias", f"{a['character_id']}/{a['alias']}", "alias", a["alias"]))
        for f in e["character_facts"]:
            out += [("character_fact", f["fact_id"], k, f[k]) for k in ("text_en", "text_zh") if f[k]]
        for lk in e["character_looks"]:
            out += [("character_look", lk["look_id"], k, lk[k]) for k in ("description_en", "description_zh") if lk[k]]
        for k in e["identity_links"]:
            out += [("identity_link", k["link_id"], f, k[f]) for f in ("text_en", "text_zh") if k[f]]
        for t in e["title_info"]:
            out += [("title_info", t["field"], f, t[f]) for f in ("text_en", "text_zh") if t[f]]
        return out
