"""What the viewer is allowed to know at the locked position P: gate-first views over one master pack.

Everything the agent can read goes through `Moment`, which is built from `ingest.retrieval.Gate` (spec §5) with
`allow_auto=True` (the v1.1 rows in the demo packs are all `auto`; time conditions are unchanged). Nothing here returns
an item before it is eligible, and character references always use the label that is valid at P (spec §6).

Defence in depth: every text a view returns goes through `Moment.sanitize`, which replaces any character name or
non-generic alias that is not yet visible at P with that character's current label. The builder checks (§10) should
make this a no-op; it is here so that one mislabelled row cannot put a name in front of the model or the viewer.
"""
from __future__ import annotations

import bisect
import collections
import dataclasses
import functools
import json
import math
import re

from ingest import retrieval
from ingest.checks import name_terms
from ingest.retrieval import EPS
from ingest.textnorm import has_cjk, normalize, term_matches, word_pattern
from server.packs import Title

BAD = {"flagged", "rejected"}
EVIDENCE_TYPES = {"scene", "event", "recap", "shot_caption", "character_fact", "object"}
TYPE_LABEL = {"scene": "scene", "event": "event", "recap": "recap", "shot_caption": "shot", "character_fact": "character",
              "object": "object", "cue": "line"}
CUE_K = 3                       # dialogue lines per search, ranked separately from the other evidence
MIN_CUE_WORDS = 3               # shorter lines ("A dragon!") hijack the ranking (docs/online-qa.md §2)
MIN_CUE_CJK = 4
_SENTENCE_START = re.compile(r"(?:^|[.!?。！？]\s*)$")


def ts(t: float) -> str:
    """Film time for people and prompts: mm:ss, or h:mm:ss from one hour on (whole seconds, rounded down)."""
    s = max(0, int(math.floor(float(t) + EPS)))
    h, rest = divmod(s, 3600)
    return f"{h}:{rest // 60:02d}:{rest % 60:02d}" if h else f"{rest // 60:02d}:{rest % 60:02d}"


def secs(stamp: str) -> float:
    """Inverse of `ts`: "mm:ss" or "h:mm:ss" → seconds."""
    out = 0.0
    for part in stamp.split(":"):
        out = out * 60 + float(part)
    return out


def name_usable(known_at: float | None, P: float, m: dict) -> bool:
    """The one name-visibility rule (spec §6): usable once `known_at` plus the reviewed-reveal margin is behind P."""
    return known_at is not None and known_at + m["gate_margin_reviewed_reveal_s"] <= P + EPS


def long_enough(text: str | None) -> bool:
    if not text:
        return False
    if has_cjk(text):
        return len(re.findall(r"[㐀-鿿]", text)) >= MIN_CUE_CJK
    return len(text.split()) >= MIN_CUE_WORDS


def _index(pack: dict) -> dict:
    """P-independent lookups for one pack, built once (`retrieval.per_pack`): a 169-minute film has ~10,000 cues and
    ~3,000 shots, and a question must not re-sort them."""
    tracks = sorted(pack["subtitle_tracks"], key=lambda t: (not t["lang"].startswith("en"), t["priority"]))
    track = tracks[0]["track"] if tracks else None
    shots = sorted(pack.get("shot_captions", []), key=lambda s: s["start_s"])
    ents: dict = {}
    for e in pack.get("shot_entities", []):
        ents.setdefault(e["shot_caption_id"], []).append(e)
    aliases: dict = {}
    for a in pack.get("character_aliases", []):
        aliases.setdefault(a["character_id"], []).append(a)
    return {
        "track": track,
        "cues": {c["cue_id"]: c for c in pack["cues"]},
        "lines": sorted((c for c in pack["cues"] if c["track"] == track and c["kind"] == "dialogue"
                         and (c["text"] or "").strip()), key=lambda c: c["start_s"]),
        "speakers": {s["cue_id"]: s for s in pack.get("cue_speakers", [])
                     if s["review_state"] not in BAD and s.get("character_id")},
        "shots": shots, "shot_starts": [s["start_s"] for s in shots], "entities": ents,
        "fact_owner": {f["fact_id"]: f["character_id"] for f in pack["character_facts"]},
        "objects": {o["object_id"]: o for o in pack.get("objects", [])},
        "recaps": {r["recap_id"]: r for r in pack.get("recaps", [])},
        "aliases": aliases,
    }


def _norm_map(text: str) -> tuple[str, list[int]]:
    """The case-preserving normalised text, character by character, with the index in `text` of every normalised
    character (accent marks normalise to nothing; a ligature to several characters)."""
    parts, where = [], []
    for i, ch in enumerate(text):
        n = normalize(ch, True)
        parts.append(n)
        where += [i] * len(n)
    return "".join(parts), where


class Moment:
    def __init__(self, title: Title, P: float, lang: str = "en"):
        self.title, self.P, self.lang = title, float(P), lang
        self.fp = int(math.floor(self.P + EPS))
        self.pack = title.pack
        self.gate = retrieval.Gate(self.pack, self.P, allow_auto=True, cached=True)
        self.ix = retrieval.per_pack(self.pack, "moment", _index)
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

    @property
    def has_subtitles(self) -> bool:
        return self.ix["track"] is not None

    @property
    def phase_words(self) -> str:
        return {"story": "during the story", "before_story": "before the story has started",
                "credits": "the end credits are rolling; the story is over"}[self.phase]

    @functools.cached_property
    def eligible(self) -> list[retrieval.Item]:
        return self.gate.eligible()

    def char_ok(self, cid: str | None) -> bool:
        return bool(cid) and self.gate.char_from(cid) <= self.P + EPS

    def visible(self, known_at: float | None) -> bool:
        """§6: a name or alias known from `known_at` may be used at P (after the reviewed-reveal margin)."""
        return name_usable(known_at, self.P, self.m)

    def name_visible(self, c: dict) -> bool:
        return bool(c["name_public"]) or self.visible(c.get("name_known_at_s"))

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

    # ── name sanitising (defence in depth, spec §6) ─────────────────

    @functools.cached_property
    def _hidden_names(self) -> list[tuple[str, bool, re.Pattern, str, str]]:
        """(term, case_sensitive, pattern on case-preserving normalised text, label en, label zh) for every name /
        alias that is not visible at P; longest first so "Bob Smith" is replaced before "Bob"."""
        out = []
        for cid, term, known_at, cs in name_terms(self.pack):
            if cid not in self.chars or self.visible(known_at):
                continue
            en, zh = self.label(cid, "en"), self.label(cid, "zh")
            t = normalize(term, True)
            if not t or normalize(t) in (normalize(en), normalize(zh)):
                continue
            out.append((term, bool(cs), re.compile(word_pattern(t), 0 if cs else re.IGNORECASE), en, zh))
        return sorted(out, key=lambda x: -len(x[0]))

    @functools.cached_property
    def _visible_names(self) -> list[re.Pattern]:
        """Patterns for every name / non-generic alias visible at P: a hidden name inside a longer visible one ("Bob"
        in "Mary Bob") is left alone."""
        out = []
        for c in self.chars.values():
            if self.name_visible(c):
                out += [n for n in (c["canonical_name"], c.get("name_zh")) if n]
        out += [a["alias"] for als in self.ix["aliases"].values() for a in als
                if not a["generic"] and self.visible(a["alias_known_at_s"])]
        from ingest.derive import name_case_sensitive
        return [re.compile(word_pattern(normalize(n, True)), 0 if name_case_sensitive(n) else re.IGNORECASE) for n in out]

    def sanitize(self, text: str | None) -> str | None:
        """Replace not-yet-visible names / aliases with the character's label at P (whole words; CJK as substrings),
        in the label's language of the surrounding text. Only the matched spans change: the rest of the text is
        returned exactly as given (accents, Traditional characters); the longest name wins."""
        if not text:
            return text
        hidden = [h for h in self._hidden_names if term_matches(h[0], text, "word", h[1])]
        if not hidden:
            return text
        zh = has_cjk(text)
        norm, where = _norm_map(text)
        keep = [mo.span() for pat in self._visible_names for mo in pat.finditer(norm)]
        spans: list[tuple[int, int, str]] = []
        for _term, _cs, pat, en, zh_label in hidden:                  # longest first
            for mo in pat.finditer(norm):
                s, e = mo.span()
                if e <= s or any(vs <= s and e <= ve and ve - vs > e - s for vs, ve in keep):
                    continue
                if not any(s < pe and ps < e for ps, pe, _ in spans):
                    spans.append((s, e, zh_label if zh else en))
        out = text
        for s, e, label in sorted(spans, reverse=True):
            a, b = where[s], where[e - 1] + 1
            if _SENTENCE_START.search(text[:a]) and label[:1].isalpha():
                label = label[:1].upper() + label[1:]
            out = out[:a] + label + out[b:]
        return out

    # ── long-range memory: latest recap + what happened after it ────

    def story_so_far(self, limit: int = 24) -> dict:
        recaps = [it for it in self.eligible if it.item_type == "recap"]
        latest = max(recaps, key=lambda it: (it.start_s, it.item_id), default=None)
        since = latest.start_s if latest else -1.0
        after = sorted((it for it in self.eligible
                        if it.item_type in ("event", "scene") and it.start_s >= since - EPS),
                       key=lambda it: (it.start_s, it.item_type))
        out = {"position": ts(self.P), "phase": self.phase}
        if latest:
            row = self.ix["recaps"][latest.item_id]
            out["recap"] = {"up_to": ts(latest.start_s), "text": self.sanitize(
                row["text_zh"] if self.lang == "zh" and row.get("text_zh") else row["text_en"])}
        out["since_recap" if latest else "so_far"] = [
            {"at": ts(it.start_s), "type": it.item_type, "text": self.sanitize(it.text)} for it in after[-limit:]]
        return out

    # ── semantic search, gate first ─────────────────────────────────

    def _searchable_line(self, it: retrieval.Item) -> bool:
        c = self.ix["cues"].get(it.item_id)
        return bool(c) and c["track"] == self.ix["track"] and c["kind"] == "dialogue" and long_enough(c["text"])

    def search(self, query: str, qvec=None, k: int = 6) -> list[retrieval.Hit]:
        """Gate-first search over eligible evidence: cosine ranking when a query vector is given, full text otherwise
        (or when the pack has no vectors). Dialogue lines are ranked separately, from one subtitle track and without
        very short lines, so a line cannot fill the top 3 once per language and "A dragon!" cannot crowd out scenes
        and events (docs/online-qa.md §2)."""
        db = self.title.db_path

        def run(mode: str) -> list[retrieval.Hit]:
            hits = retrieval.search(db, self.P, query, k=k, mode=mode, allow_auto=True, types=EVIDENCE_TYPES, qvec=qvec)
            lines = retrieval.search(db, self.P, query, k=CUE_K, mode=mode, allow_auto=True, types={"cue"}, qvec=qvec,
                                     keep=self._searchable_line)
            return hits + lines

        hits = run("vector") if qvec is not None else []
        if not hits:
            hits = run("fts")
        return [dataclasses.replace(h, text=self.sanitize(h.text)) for h in hits]

    # ── near-window dialogue with speakers ──────────────────────────

    def speaker(self, cue: dict) -> str:
        """Speaker label at P, or "unknown speaker" if the attribution is not usable yet (character not eligible, or
        attaching the line to them would break identity isolation)."""
        sp = self.ix["speakers"].get(cue["cue_id"])
        cid = sp["character_id"] if sp else None
        if not cid or not self.char_ok(cid) or self.gate.char_isolated(cid, cue["start_s"], cue["end_s"]):
            return "unknown speaker"
        return self.label(cid)

    def _lines(self) -> list[dict]:
        """Eligible dialogue cues on the chosen track, oldest first."""
        ok = {it.item_id for it in self.eligible if it.item_type == "cue"}
        return [c for c in self.ix["lines"] if c["cue_id"] in ok]

    def recent_dialogue(self, seconds: float = 90.0, at_least: int = 0) -> list[dict]:
        """Lines that started in the last `seconds` before P (a line still on screen is excluded, §5.2); if fewer than
        `at_least`, the last `at_least` lines before P instead, whenever they were."""
        lines = self._lines()
        out = [c for c in lines if c["start_s"] >= self.P - seconds]
        if len(out) < at_least:
            out = lines[-at_least:]
        return [{"at": ts(c["start_s"]), "t": c["start_s"], "speaker": self.speaker(c), "text": self.sanitize(c["text"])}
                for c in out]

    # ── current shot: who / what is on screen (spec §5.2 current-shot rule) ──

    def _facts(self, cid: str) -> list[str]:
        owner = self.ix["fact_owner"]
        return [self.sanitize(it.text) for it in self.eligible
                if it.item_type == "character_fact" and owner.get(it.item_id) == cid]

    def _look(self, cid: str) -> str | None:
        mr = self.m["gate_margin_reviewed_reveal_s"]
        looks = []
        for lk in self.pack["character_looks"]:
            vis = max(lk["reveal_at_s"], lk["from_s"])
            if (lk["character_id"] == cid and self.gate.reviewed(lk["review_state"]) and lk["from_s"] <= self.P + EPS
                    and vis + mr <= self.P + EPS
                    and not self.gate.char_isolated(cid, min(lk["from_s"], lk["reveal_at_s"]), vis)):
                looks.append(lk)
        best = max(looks, key=lambda lk: (lk["from_s"], lk["reveal_at_s"], lk["look_id"]), default=None)
        return self.sanitize(best["description_en"]) if best else None

    def exemplars(self, cid: str, limit: int = 2) -> list[int]:
        rows = [e for e in self.pack.get("character_exemplars", [])
                if e["character_id"] == cid and self.gate.reviewed(e["review_state"]) and 0 <= e["t_s"] <= self.fp
                and not self.gate.char_isolated(cid, e["t_s"], e["t_s"])]
        return [e["t_s"] for e in sorted(rows, key=lambda e: -e["t_s"])[:limit]]

    def _object_ok(self, oid: str | None) -> bool:
        return bool(oid) and any(it.item_type == "object" and it.item_id == oid for it in self.eligible)

    def on_screen(self, tap: dict | None = None, refs: bool = False) -> dict:
        """Entities of the current shot (§5.2 current-shot rule). `refs=True` adds `ref` = (kind, id) to each entity
        for server-side use (`mention_terms`); never send that to the model."""
        if self.phase == "credits":
            return {"note": "The end credits are rolling; there is nobody to identify on screen."}
        shots, ents_by_shot = self.ix["shots"], self.ix["entities"]
        i = bisect.bisect_right(self.ix["shot_starts"], self.P + EPS) - 1
        cur = [i] if i >= 0 and self.P + EPS < shots[i]["end_s"] else []
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
            ref = None
            if e["entity_type"] == "character":
                cid = e.get("character_id")
                if self.char_ok(cid) and not self.gate.char_isolated(cid, e["bbox_t_s"], e["bbox_t_s"]):
                    c, ref = self.chars[cid], ("character", cid)
                    ent.update(kind="character", label=self.label(cid), name_known=self.name_visible(c),
                               look=self._look(cid), facts=self._facts(cid)[-6:], reference_frames=self.exemplars(cid))
                else:
                    ent.update(kind="character", label="a character who has only just appeared (nothing known yet)")
            else:
                oid = e.get("object_id")
                if self._object_ok(oid):
                    o, ref = self.ix["objects"][oid], ("object", oid)
                    ent.update(kind="object", label=self.sanitize(o["name_en"]), description=self.sanitize(o["description_en"]))
                else:
                    ent.update(kind="object", label="an object (not described yet)")
            if refs:
                ent["ref"] = ref
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
        if refs:
            out["current_shot"] = used_shot is not None and used_shot is candidates[0]
        return out

    def mention_terms(self, ref: tuple[str, str] | None) -> list[str]:
        """Words by which an answer at P can refer to an on-screen entity: its labels in both languages (without a
        leading article), its visible names and visible non-generic aliases. Used to decide whether an answer is
        about the tapped entity."""
        if not ref:
            return []
        kind, rid = ref
        if kind == "character":
            c = self.chars.get(rid)
            if not c:
                return []
            terms = [self.label(rid, "en"), self.label(rid, "zh")]
            if self.name_visible(c):
                terms += [c["canonical_name"], c.get("name_zh")]
            terms += [a["alias"] for a in self.ix["aliases"].get(rid, [])
                      if not a["generic"] and self.visible(a["alias_known_at_s"])]
        else:
            o = next((o for o in self.pack.get("objects", []) if o["object_id"] == rid), None)
            terms = [self.sanitize(o["name_en"]), o.get("name_zh")] if o else []
        out = []
        for t in terms:
            t = re.sub(r"^(the|a|an)\s+", "", (t or "").strip(), flags=re.IGNORECASE)
            if t and t not in out:
                out.append(t)
        return out

    # ── media ───────────────────────────────────────────────────────

    def near_window(self, seconds: float) -> tuple[float, float] | None:
        """Whole-film clip that ends strictly before P (spec §5.2 media rule: endOffset ≤ floor(P) − 1)."""
        start = max(self.asset["story_start_s"], self.P - seconds)
        end = self.fp - 1
        return (start, float(end)) if end - start >= 2 else None

    def evidence_frame(self, start_s: float, end_s: float) -> int | None:
        t = min(int((start_s + end_s) / 2), self.fp)
        prefs = [c for c in (t, int(start_s), self.fp) if 0 <= c <= self.fp]
        have = set(self.title.prefetch(prefs))                   # one concurrent fetch for on-demand titles
        return next((c for c in prefs if c in have), None)

    def gate_counts(self) -> dict:
        total = retrieval.per_pack(self.pack, "gate_totals", lambda _p: dict(collections.Counter(
            it.item_type for it in self.gate.candidates() if it.item_type != "video_window")))
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
