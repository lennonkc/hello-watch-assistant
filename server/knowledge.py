"""What the viewer is allowed to know at the locked position P: gate-first views over one master pack.

Everything the agent can read goes through `Moment`, which is built from `ingest.retrieval.Gate` (spec §5) with
`allow_auto=True` (the v1.1 rows in the demo packs are all `auto`; time conditions are unchanged). Nothing here returns
an item before it is eligible, and character references always use the label that is valid at P (spec §6).
"""
from __future__ import annotations

import functools
import json
import math

from ingest import retrieval
from ingest.retrieval import EPS
from ingest.util import mmss
from server.packs import Title

BAD = {"flagged", "rejected"}
EVIDENCE_TYPES = {"scene", "event", "recap", "shot_caption", "character_fact", "object"}
TYPE_LABEL = {"scene": "scene", "event": "event", "recap": "recap", "shot_caption": "shot", "character_fact": "character",
              "object": "object", "cue": "line"}


def ts(t: float) -> str:
    return mmss(t)[:5]


class Moment:
    def __init__(self, title: Title, P: float, lang: str = "en"):
        self.title, self.P, self.lang = title, float(P), lang
        self.fp = int(math.floor(self.P + EPS))
        self.pack = title.pack
        self.gate = retrieval.Gate(self.pack, self.P, allow_auto=True)
        self.m = self.gate.m
        self.asset = self.pack["asset"]
        self.chars = self.gate.chars

    # ── basic state ─────────────────────────────────────────────────

    @property
    def phase(self) -> str:
        if self.P + EPS < self.asset["story_start_s"]:
            return "before_story"
        if self.P >= self.asset["story_end_s"]:
            return "credits"
        return "story"

    @functools.cached_property
    def eligible(self) -> list[retrieval.Item]:
        return self.gate.eligible()

    def char_ok(self, cid: str | None) -> bool:
        return bool(cid) and self.gate.char_from(cid) <= self.P + EPS

    def name_visible(self, c: dict) -> bool:
        k = c.get("name_known_at_s")
        return bool(c["name_public"]) or (k is not None and k + self.m["gate_margin_reviewed_reveal_s"] <= self.P + EPS)

    def label(self, cid: str, lang: str | None = None) -> str:
        """§6 at P: the name once it is visible, otherwise the pre-name label."""
        c, zh = self.chars[cid], (lang or self.lang) == "zh"
        if self.name_visible(c):
            return (c["name_zh"] or c["canonical_name"]) if zh else c["canonical_name"]
        return c["display_label_pre_name_zh"] if zh else c["display_label_pre_name"]

    def known_characters(self) -> list[dict]:
        out = []
        for cid, c in sorted(self.chars.items(), key=lambda kv: kv[1].get("first_appears_s") or 0):
            if self.char_ok(cid):
                out.append({"label": self.label(cid, "en"), "label_zh": self.label(cid, "zh"), "kind": c["kind"],
                            "first_seen": ts(c["first_appears_s"]), "name_known": self.name_visible(c)})
        return out

    # ── long-range memory: latest recap + what happened after it ────

    def story_so_far(self) -> dict:
        recaps = [it for it in self.eligible if it.item_type == "recap"]
        latest = max(recaps, key=lambda it: (it.start_s, it.item_id), default=None)
        since = latest.start_s if latest else -1.0
        after = sorted((it for it in self.eligible
                        if it.item_type in ("event", "scene") and it.start_s >= since - EPS),
                       key=lambda it: (it.start_s, it.item_type))
        out = {"position": ts(self.P), "phase": self.phase}
        if latest:
            row = next(r for r in self.pack["recaps"] if r["recap_id"] == latest.item_id)
            out["recap"] = {"up_to": ts(latest.start_s), "text": row["text_zh"] if self.lang == "zh" and row.get("text_zh")
                            else row["text_en"]}
        out["since_recap" if latest else "so_far"] = [
            {"at": ts(it.start_s), "type": it.item_type, "text": it.text} for it in after[-24:]]
        return out

    # ── semantic search, gate first ─────────────────────────────────

    def search(self, query: str, k: int = 6) -> list[retrieval.Hit]:
        """Vector search over eligible evidence; dialogue lines are ranked separately so very short lines can't crowd
        out scenes and events (known issue in docs/online-qa.md §2)."""
        db = self.title.db_path
        hits = retrieval.search(db, self.P, query, k=k, mode="vector", allow_auto=True, types=EVIDENCE_TYPES)
        lines = retrieval.search(db, self.P, query, k=3, mode="vector", allow_auto=True, types={"cue"})
        return hits + [h for h in lines if h.text and len(h.text.split()) >= 3]

    # ── near-window dialogue with speakers ──────────────────────────

    def _cue_track(self) -> str | None:
        tracks = sorted(self.pack["subtitle_tracks"], key=lambda t: (not t["lang"].startswith("en"), t["priority"]))
        return tracks[0]["track"] if tracks else None

    def recent_dialogue(self, seconds: float = 90.0) -> list[dict]:
        track, m = self._cue_track(), self.m["gate_margin_cue_s"]
        speakers = {s["cue_id"]: s for s in self.pack.get("cue_speakers", []) if s["review_state"] not in BAD}
        out = []
        for c in sorted(self.pack["cues"], key=lambda c: c["start_s"]):
            if c["track"] != track or c["kind"] != "dialogue":
                continue
            if c["end_s"] + m > self.P + EPS or c["start_s"] < self.P - seconds:
                continue
            sp = speakers.get(c["cue_id"])
            who = self.label(sp["character_id"], "en") if sp and self.char_ok(sp["character_id"]) else "unknown speaker"
            out.append({"at": ts(c["start_s"]), "t": c["start_s"], "speaker": who, "text": c["text"]})
        return out

    # ── current shot: who / what is on screen (spec §5.2 current-shot rule) ──

    def _facts(self, cid: str) -> list[str]:
        owner = {f["fact_id"]: f["character_id"] for f in self.pack["character_facts"]}
        return [it.text for it in self.eligible if it.item_type == "character_fact" and owner.get(it.item_id) == cid]

    def _look(self, cid: str) -> str | None:
        mr = self.m["gate_margin_reviewed_reveal_s"]
        looks = [lk for lk in self.pack["character_looks"]
                 if lk["character_id"] == cid and self.gate.reviewed(lk["review_state"])
                 and lk["from_s"] <= self.P + EPS and lk["reveal_at_s"] + mr <= self.P + EPS]
        best = max(looks, key=lambda lk: lk["from_s"], default=None)
        return best["description_en"] if best else None

    def exemplars(self, cid: str, limit: int = 2) -> list[int]:
        rows = [e for e in self.pack.get("character_exemplars", [])
                if e["character_id"] == cid and e["review_state"] not in BAD and e["t_s"] <= self.fp]
        return [e["t_s"] for e in sorted(rows, key=lambda e: -e["t_s"])[:limit]]

    def _object_ok(self, oid: str | None) -> bool:
        return bool(oid) and any(it.item_type == "object" and it.item_id == oid for it in self.eligible)

    def on_screen(self, tap: dict | None = None) -> dict:
        if self.phase == "credits":
            return {"note": "The end credits are rolling; there is nobody to identify on screen."}
        shots = sorted(self.pack.get("shot_captions", []), key=lambda s: s["start_s"])
        cur = [i for i, s in enumerate(shots) if s["start_s"] <= self.P + EPS < s["end_s"]]
        ents_by_shot: dict = {}
        for e in self.pack.get("shot_entities", []):
            ents_by_shot.setdefault(e["shot_caption_id"], []).append(e)
        # current shot first; if it has no usable frame yet (it only just started), the shot before it
        candidates = []
        if cur:
            i = cur[0]
            candidates = [shots[i]] + ([shots[i - 1]] if i > 0 else [])
        entities, used_shot = [], None
        for s in candidates:
            if s["review_state"] in BAD:
                continue
            rows = [e for e in ents_by_shot.get(s["shot_caption_id"], [])
                    if e.get("bbox_t_s") is not None and e["bbox_t_s"] <= self.fp and e.get("bbox")]
            if rows:
                entities, used_shot = rows, s
                break
        out_entities = []
        for e in sorted(entities, key=lambda e: e["seq"]):
            ent = {"position": e["position"], "bbox": json.loads(e["bbox"]), "frame_t": e["bbox_t_s"]}
            if e["entity_type"] == "character":
                cid = e.get("character_id")
                if self.char_ok(cid):
                    c = self.chars[cid]
                    ent.update(kind="character", label=self.label(cid, "en"), name_known=self.name_visible(c),
                               look=self._look(cid), facts=self._facts(cid)[-6:], reference_frames=self.exemplars(cid))
                else:
                    ent.update(kind="character", label="a character who has only just appeared (nothing known yet)")
            else:
                oid = e.get("object_id")
                if self._object_ok(oid):
                    o = next(o for o in self.pack["objects"] if o["object_id"] == oid)
                    ent.update(kind="object", label=o["name_en"], description=o["description_en"])
                else:
                    ent.update(kind="object", label="an object (not described yet)")
            out_entities.append(ent)
        out = {"position": ts(self.P), "entities": out_entities}
        if not out_entities:
            out["note"] = "No on-screen entities are known for this frame; use look_closer to inspect it."
        if tap and out_entities:
            ty, tx = float(tap["y"]) * 1000, float(tap["x"]) * 1000
            out["tap"] = {"x": round(tx), "y": round(ty)}
            hit = pick_entity(out_entities, tx, ty)
            if hit is not None:
                out["tapped"] = hit
            else:
                out["tap_note"] = "The tap is not on any known entity."
        if used_shot is not None:
            out["shot_started"] = ts(used_shot["start_s"])
        return out

    # ── media ───────────────────────────────────────────────────────

    def near_window(self, seconds: float) -> tuple[float, float] | None:
        """Whole-film clip that ends strictly before P (spec §5.2 media rule: endOffset ≤ floor(P) − 1)."""
        start = max(self.asset["story_start_s"], self.P - seconds)
        end = self.fp - 1
        return (start, float(end)) if end - start >= 2 else None

    def evidence_frame(self, start_s: float, end_s: float) -> int | None:
        t = min(int((start_s + end_s) / 2), self.fp)
        for cand in (t, int(start_s), self.fp):
            if 0 <= cand <= self.fp and self.title.thumb(cand).exists():
                return cand
        return None

    def gate_counts(self) -> dict:
        everything = self.gate.candidates()
        total: dict = {}
        for it in everything:
            if it.item_type != "video_window":
                total[it.item_type] = total.get(it.item_type, 0) + 1
        elig = {k: 0 for k in total}
        for it in self.eligible:
            if it.item_type in elig:
                elig[it.item_type] += 1
        chars = [cid for cid, c in self.chars.items() if c["review_state"] not in BAD]
        return {"p": round(self.P, 2), "eligible": elig, "total": total,
                "characters_known": sum(self.char_ok(c) for c in chars), "characters_total": len(chars)}


def pick_entity(entities: list[dict], tx: float, ty: float) -> int | None:
    """Index of the tapped entity: smallest box containing the tap, else the nearest box centre within 25% of the frame."""
    inside = [(i, (e["bbox"][2] - e["bbox"][0]) * (e["bbox"][3] - e["bbox"][1]))
              for i, e in enumerate(entities)
              if e["bbox"][0] <= ty <= e["bbox"][2] and e["bbox"][1] <= tx <= e["bbox"][3]]
    if inside:
        return min(inside, key=lambda x: x[1])[0]
    best = None
    for i, e in enumerate(entities):
        cy, cx = (e["bbox"][0] + e["bbox"][2]) / 2, (e["bbox"][1] + e["bbox"][3]) / 2
        d = math.hypot(cx - tx, cy - ty)
        if d <= 250 and (best is None or d < best[1]):
            best = (i, d)
    return best[0] if best else None
