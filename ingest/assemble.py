"""Stage outputs → auto rows (spec table schema, review_state = auto).

The full-film pass defines the character set; segment passes only add recall (new characters, appearances, events in gaps, new objects).
Model times are truncated to whole seconds: reveal-type times always get +0.999 (the conservative direction); first appearances do not (the margin covers them).
"""
from __future__ import annotations

import bisect
import urllib.parse
import difflib
import re

from ingest import assemble_shots, assemble_speakers
from ingest.derive import name_case_sensitive
from ingest.textnorm import has_cjk, is_common, normalize, term_matches
from ingest.util import parse_ts, r3

SNAP_TOL = 1.5
TRUNC = 0.999
SEG_EVENT_GAP_S = 15.0
NAME_SOURCE = {"spoken_dialogue": "dialogue", "on_screen_story_text": "on_screen_text", "catalog_synopsis": "catalog",
               "descriptive_label": None}
PRESENT_TOL_S = 1.0       # tolerance between appearance intervals and item times (appearances come from 1 fps / 2 fps vision, whole seconds)
ROLE = {"protagonist": "protagonist", "supporting": "supporting", "major": "supporting", "minor": "minor",
        "background": "minor"}
KIND = {"person": "person", "human": "person", "creature": "creature", "animal": "creature", "robot": "robot"}


def _t(s) -> float | None:
    return parse_ts(s) if s not in (None, "") else None


def _later(*xs) -> float:
    return max(x for x in xs if x is not None)


# ── basics: cut snapping, cue matching, Chinese name renderings ──────────────────────────────────

def nearest_cut(cuts: list[float], t: float, lo: float, hi: float) -> float | None:
    cand = [c for c in cuts[max(0, bisect.bisect_left(cuts, lo) - 1): bisect.bisect_right(cuts, hi) + 1] if lo <= c <= hi]
    return min(cand, key=lambda c: abs(c - t)) if cand else None


def _words(s: str) -> list[str]:
    """Tokens for quote matching: Latin by word, CJK per character."""
    s = s.lower()
    return re.findall(r"[a-z0-9']+|[\u3400-\u9fff\uf900-\ufaff]", s)


def match_quote(quote: str, cues: list[dict], near: float | None, window: float = 25.0) -> list[dict]:
    """Split the quote into sentences; for each, find the textually closest cue within `near ± window` (ratio ≥0.6)."""
    if not quote:
        return []
    out = []
    for sent in [s for s in re.split(r"(?<=[.?!…])\s+", quote.strip()) if _words(s)]:
        sw = _words(sent)
        best = None
        for c in cues:
            if near is not None and abs(c["start_s"] - near) > window:
                continue
            r = difflib.SequenceMatcher(None, sw, _words(c["text"])).ratio()
            if near is not None:
                r -= 0.002 * abs(c["start_s"] - near)
            if best is None or r > best[0]:
                best = (r, c)
        if best and best[0] >= 0.6 and best[1] not in out:
            out.append(best[1])
    return sorted(out, key=lambda c: c["start_s"])


def _overlap(a0, a1, b0, b1) -> float:
    return max(0.0, min(a1, b1) - max(a0, b0))


def zh_renderings(name: str, ref_cues: list[dict], zh_cue_sets: list[list[dict]]) -> list[str]:
    """How a name is written in each Chinese track / name source: the longest common CJK substring of the Chinese cues aligned with English cues containing the name (≥2 chars, ≥2 supporting cues)."""
    hits = [c for c in ref_cues if term_matches(name, c["text"], "word", True)]
    out = []
    if len(hits) < 2:
        return out
    for cues in zh_cue_sets:
        texts = []
        for h in hits:
            best = max(cues, key=lambda c: _overlap(h["start_s"], h["end_s"], c["start_s"], c["end_s"]), default=None)
            if best and _overlap(h["start_s"], h["end_s"], best["start_s"], best["end_s"]) > 0:
                texts.append("".join(re.findall(r"[㐀-鿿]", best["text"])))
        if len(texts) < 2 or not all(texts):
            continue
        base = min(texts, key=len)
        found = None
        for n in range(len(base), 1, -1):
            for i in range(0, len(base) - n + 1):
                sub = base[i:i + n]
                if all(sub in t for t in texts):
                    found = sub
                    break
            if found:
                break
        if found and found not in out:
            out.append(found)
    return out


def looks_descriptive(name: str) -> bool:
    """The model sometimes reports a label ('the dog', 'the shepherd girl') as a name: leading article or all-lowercase common words → treat as a label."""
    ws = re.findall(r"[A-Za-z]+", name or "")
    if not ws or has_cjk(name):
        return False
    return ws[0].lower() in ("the", "a", "an") or (name == name.lower() and all(is_common(w) for w in ws))


def is_generic(alias: str) -> int:
    """Generic = lowercase description made entirely of common words (e.g. 'the dragon'); a capitalized single-word alias is a proper name (e.g. 'Scar')."""
    ws = re.findall(r"[A-Za-z]+", alias)
    if has_cjk(alias):
        return 0
    if alias.strip()[:1].isupper() and normalize(ws[0] if ws else "") not in ("the", "a", "an"):
        return 0
    return int(bool(ws) and all(is_common(w) for w in ws))


def merge_intervals(iv: list[tuple[float, float, str]], gap: float = 2.0) -> list[tuple[float, float, str]]:
    out = []
    for a, b, src in sorted(iv):
        if out and a <= out[-1][1] + gap:
            pa, pb, ps = out[-1]
            out[-1] = (pa, max(pb, b), ps if ps == src else "+".join(sorted(set(ps.split("+")) | {src})))
        else:
            out.append((a, b, src))
    return out


# ── main flow ────────────────────────────────────────────────────────────

class Assembler:
    def __init__(self, st: dict, title: dict, catalog_item: dict):
        self.st = st
        self.title = title
        self.cat = catalog_item
        self.reg = st["register"]
        self.dur = self.reg["duration_s"]
        self.cuts = st["shots"]["cuts"]
        self.full = st["vision_full"]["doc"]
        self.src_full = f"vision:full_fps{st['vision_full']['fps']:g}@{st['vision_full']['model']}"
        self.src_seg = f"vision:seg_fps{st['vision_seg']['fps']:g}@{st['vision_seg']['model']}" if st.get("vision_seg") else None
        self.pack = {"pack_meta": {}, "asset": {}}
        self.mismatch = st.get("asset_mismatch")
        self.aux = {"unmapped_refs": [], "identity_clue_words": {}, "name_renderings": {}, "seg_added": {}}
        self.name2id: dict[str, str] = {}
        self.ref2id: dict[str, str] = {}

    # character name / label / alias / segment ref → id
    def cid(self, name: str | None) -> str | None:
        if not name:
            return None
        n = name.strip()
        if n in self.ref2id:
            return self.ref2id[n]
        k = normalize(n)
        k2 = re.sub(r"^(the|a|an)\s+", "", k)
        hit = self.name2id.get(k) or self.name2id.get(k2)
        if hit is None and n not in self.aux["unmapped_refs"]:
            self.aux["unmapped_refs"].append(n)
        return hit

    def _register_name(self, name: str, cid: str) -> None:
        k = normalize(name)
        self.name2id.setdefault(k, cid)
        self.name2id.setdefault(re.sub(r"^(the|a|an)\s+", "", k), cid)

    def build(self) -> tuple[dict, dict]:
        self.asset_rows()
        self.subtitle_rows()
        self.structure_rows()
        self.character_rows()
        self.seg_characters()
        self.scene_rows()
        self.event_rows()
        self.object_rows()
        self.appearance_rows()
        assemble_shots.rows(self)
        assemble_speakers.rows(self)
        self.present_links()
        self.mention_links()
        self.blocklist_rows()
        p = self.pack
        for t in ("canaries", "review_log", "embeddings"):
            p.setdefault(t, [])
        return p, self.aux

    def asset_rows(self) -> None:
        f = self.full
        s0 = _t(f.get("story_start")) or 0.0
        s1 = _t(f.get("story_end"))
        story_start = 0.0 if s0 <= 0 else (nearest_cut(self.cuts, s0, s0 - SNAP_TOL, s0 + SNAP_TOL) or s0)
        story_end = self.dur if s1 is None else (nearest_cut(self.cuts, s1, s1 - SNAP_TOL, s1 + SNAP_TOL) or s1)
        self.pack["asset"] = {
            "asset_sha256": self.reg["asset_sha256"], "content_id": self.title["catalog_id"], "title": self.title["title"],
            "asset_kind": self.title["asset_kind"], "duration_s": self.dur, "video_fps": self.reg["video_fps"],
            "width": self.reg["width"], "height": self.reg["height"], "audio_tracks": self.reg["audio_tracks"],
            "has_dialogue": 0, "dialogue_lang": self.title.get("dialogue_lang"), "license": self.title["license"],
            "derivatives_ok": self.title["derivatives_ok"], "story_start_s": r3(story_start), "story_end_s": r3(story_end),
            "credits_start_s": r3(story_end) if s1 is not None else None}
        ti = [{"field": "title", "text_en": self.title["title"], "text_zh": self.cat.get("title_zh"), "source": "catalog"}]
        if self.cat.get("year"):
            ti.append({"field": "year", "text_en": str(self.cat["year"]), "text_zh": str(self.cat["year"]), "source": "catalog"})
        if self.cat.get("genres"):
            ti.append({"field": "genre", "text_en": ", ".join(self.cat["genres"]), "text_zh": None, "source": "catalog"})
        self.pack["title_info"] = ti

    def subtitle_rows(self) -> None:
        subs = self.st["subtitles"]
        al = self.st.get("align") or {"corrections": {}, "generated_track": None}
        tracks = list(subs["tracks"]) + ([al["generated_track"]] if al.get("generated_track") else [])
        trows, crows = [], []
        for t in tracks:
            corr = al["corrections"].get(t["track"], {"offset_s": 0.0, "drift_s_per_hour": 0.0, "note": t.get("note")})
            off, drift = corr["offset_s"], corr["drift_s_per_hour"]
            trows.append({"track": t["track"], "lang": t["lang"], "source": t["source"],
                          "priority": {"official": 0, "human": 1, "generated": 2}[t["source"]],
                          "source_url": t.get("source_url"), "license": t.get("license"), "sha256": t.get("sha256"),
                          "offset_s": off, "drift_s_per_hour": drift, "alignment_note": corr.get("note")})
            for i, c in enumerate(t["cues"], 1):
                fix = lambda x: r3(x + off + drift * x / 3600)  # noqa: E731
                kind = c["kind"]
                if t["source"] == "generated" and kind == "dialogue" and self.in_lyrics(c["start_s"], c["end_s"]):
                    kind = "lyrics"
                crows.append({"cue_id": f"{t['track']}:{i:03d}", "track": t["track"], "start_s": fix(c["start_s"]),
                              "end_s": fix(c["end_s"]), "text": c["text"], "kind": kind, "speaker_raw": c.get("speaker")})
                if c.get("interpolated"):
                    self.aux.setdefault("interpolated_cues", []).append(f"{t['track']}:{i:03d}")
        self.pack["subtitle_tracks"], self.pack["cues"] = trows, crows
        lang = self.title.get("dialogue_lang", "en")
        ref = next((t for t in trows if t["lang"] == lang and t["source"] == "official"), None) or \
            next((t for t in trows if t["lang"] == lang), None)
        self.ref_cues = [c for c in crows if ref and c["track"] == ref["track"] and c["kind"] == "dialogue"]
        self.pack["asset"]["has_dialogue"] = int(bool(self.ref_cues))
        self.zh_sets = [[c for c in crows if c["track"] == t["track"]] for t in trows if t["lang"].startswith("zh")]
        self.zh_sets += [ns["cues"] for ns in subs.get("name_sources", []) if ns["lang"].startswith("zh")]

    def in_lyrics(self, a: float, b: float) -> bool:
        """A generated-track cue inside a lyrics span marked by the vision pass (over half overlapping) → kind = lyrics (not output by default)."""
        spans = [(_t(x["start"]), _t(x["end"]) + TRUNC) for x in self.full.get("lyrics_spans", []) or []]
        return any(_overlap(a, max(b, a + 0.01), s0, s1) >= 0.5 * max(b - a, 0.01) for s0, s1 in spans
                   if s0 is not None and s1 is not None)

    def structure_rows(self) -> None:
        self.pack["shots"] = [dict(s) for s in self.st["shots"]["shots"]]
        self.pack["segments"] = [{k: s[k] for k in ("segment_id", "start_s", "end_s", "file_sha256")}
                                 for s in self.st["segments"]["segments"]]
        self.pack["thumbnails"] = [{k: r[k] for k in ("t_s", "path", "width", "height", "bytes")}
                                   for r in self.st["thumbs"]["thumbnails"]]

    def publicly_known(self, name: str) -> bool:
        """spec §6: title characters and names in the catalog synopsis may be used early (name_public = 1 still needs human confirmation)."""
        if normalize(name) == normalize(self.title["title"]):
            return True
        if getattr(self, "mismatch", None):        # catalog metadata does not match the asset: the synopsis cannot justify a name as public
            return False
        texts = [self.title["title"], self.cat.get("title") or "", self.cat.get("synopsis_full") or "",
                 self.cat.get("synopsis_short_zh") or ""]
        return any(term_matches(name, t, "word", name_case_sensitive(name)) for t in texts)

    def _first_cue_with(self, name: str) -> dict | None:
        return next((c for c in sorted(self.ref_cues, key=lambda c: c["start_s"])
                     if term_matches(name, c["text"], "word", True)), None)

    def character_rows(self) -> None:
        chars = sorted(self.full["characters"], key=lambda c: (_t(c["first_appears"]) or 0, c["canonical_name"]))
        rows, aliases, looks, facts = [], [], [], []
        self.char_first: dict[str, float] = {}
        for i, c in enumerate(chars, 1):
            cid = f"c{i:02d}"
            for n in [c["canonical_name"], c.get("display_label")] + [a["alias"] for a in c.get("aliases", [])]:
                if n:
                    self._register_name(n, cid)
        for r in self.st.get("vision_seg", {}).get("roster", []):
            self.ref2id[r["ref"]] = self.name2id.get(normalize(r["canonical_name"]))
        for i, c in enumerate(chars, 1):
            cid = f"c{i:02d}"
            named = c.get("name_source") != "descriptive_label" and not looks_descriptive(c["canonical_name"])
            first = _t(c["first_appears"])
            self.char_first[cid] = first
            known = _t(c.get("name_known_at")) if named else None
            name_source = NAME_SOURCE.get(c.get("name_source"))
            if named:
                cue = self._first_cue_with(c["canonical_name"])
                if cue:
                    known, name_source = cue["start_s"], "dialogue"
            label = c.get("display_label") or c["canonical_name"]
            public = int(named and (self.publicly_known(c["canonical_name"])
                                    or bool(c.get("name_zh")) and self.publicly_known(c["name_zh"])))
            rows.append({"character_id": cid, "canonical_name": c["canonical_name"],
                         "name_zh": (c.get("name_zh") or None) if named else None,
                         "display_label_pre_name": label, "display_label_pre_name_zh": c.get("display_label_zh") or label,
                         "name_known_at_s": r3(known), "name_public": public, "name_source": name_source,
                         "kind": KIND.get(c.get("kind"), "other"), "role": ROLE.get(c.get("role"), "minor"),
                         "first_appears_s": r3(first), "review_state": "auto", "source": self.src_full})
            seen = {normalize(c["canonical_name"])}
            for a in c.get("aliases", []):
                if not a.get("alias") or normalize(a["alias"]) in seen:
                    continue
                seen.add(normalize(a["alias"]))
                aliases.append({"character_id": cid, "alias": a["alias"], "lang": "zh" if has_cjk(a["alias"]) else "en",
                                "alias_known_at_s": r3(_t(a.get("known_at")) or known or first),
                                "generic": is_generic(a["alias"])})
            if named and known is not None:
                rend = zh_renderings(c["canonical_name"], self.ref_cues, self.zh_sets)
                self.aux["name_renderings"][cid] = rend
                for z in rend:
                    if normalize(z) in seen or (c.get("name_zh") and normalize(z) == normalize(c["name_zh"])):
                        continue
                    seen.add(normalize(z))
                    aliases.append({"character_id": cid, "alias": z, "lang": "zh", "alias_known_at_s": r3(known),
                                    "generic": 0})
            cl = sorted(c.get("looks", []), key=lambda x: _t(x["from"]) or 0)
            for j, lk in enumerate(cl):
                fr = _t(lk["from"]) or first
                nxt = _t(cl[j + 1]["from"]) if j + 1 < len(cl) else None
                looks.append({"look_id": None, "character_id": cid, "from_s": r3(fr), "to_s": r3(nxt),
                              "description_en": lk["description"], "description_zh": None,
                              "reveal_at_s": r3(_later(fr, _t(lk.get("reveal_at"))) + TRUNC),
                              "spoiler_critical": 0, "review_state": "auto"})
            for f in c.get("facts", []):
                facts.append(self._fact(cid, f, self.src_full))
        for i, lk in enumerate(sorted(looks, key=lambda x: (x["character_id"], x["from_s"])), 1):
            lk["look_id"] = f"l{i:03d}"
        self.pack.update(characters=rows, character_aliases=aliases, character_looks=looks)
        self._facts = facts
        self.pack["identity_links"] = []
        for i, l in enumerate(self.full.get("identity_links", []), 1):
            a, b = self.cid(l["a"]), self.cid(l["b"])
            if not a or not b or a == b:
                continue
            lid = f"k{i:02d}"
            self.pack["identity_links"].append({
                "link_id": lid, "a_character_id": a, "b_character_id": b, "relation": l.get("relation") or "same_individual",
                "reveal_at_s": r3(_t(l["reveal_at"]) + TRUNC), "text_en": l.get("text") or "", "text_zh": None,
                "review_state": "auto"})
            self.aux["identity_clue_words"][lid] = l.get("clue_words", [])

    def _fact(self, cid: str, f: dict, source: str) -> dict:
        rev = _t(f["reveal_at"]) + TRUNC
        cues = match_quote(f.get("quote", ""), self.ref_cues, _t(f["reveal_at"])) if f.get("basis") == "dialogue" else []
        if cues:
            rev = max(rev, max(c["end_s"] for c in cues))
        return {"row": {"fact_id": None, "character_id": cid, "text_en": f["text"], "text_zh": None, "reveal_at_s": r3(rev),
                        "basis": f.get("basis", "shown"),
                        "evidence_t_s": r3(_t(f["reveal_at"])) if f.get("basis") == "shown" else None,
                        "spoiler_critical": int(bool(f.get("spoiler_critical"))), "review_state": "auto", "source": source},
                "cues": [c["cue_id"] for c in cues]}

    def seg_characters(self) -> None:
        seg = self.st.get("vision_seg")
        if not seg:
            self._finish_facts()
            return
        n = len(self.pack["characters"])
        new_by_label: dict[str, str] = {}
        added = []
        for part in seg["segments"]:
            for nc in part["doc"].get("new_characters", []):
                key = normalize(nc["label"])
                fa = _t(nc["first_appears"])
                # same kind as an existing character and first appearance within ±3 s → duplicate, don't add
                dup = next((c["character_id"] for c in self.pack["characters"] if c["kind"] == KIND.get(nc["kind"], "other")
                            and c["first_appears_s"] is not None and abs(c["first_appears_s"] - fa) <= 3), None)
                if key in new_by_label or dup:
                    self.ref2id[f"new:{nc['label']}"] = new_by_label.get(key) or dup
                    continue
                n += 1
                cid = f"c{n:02d}"
                new_by_label[key] = cid
                self.ref2id[f"new:{nc['label']}"] = cid
                self._register_name(nc["label"], cid)
                self.char_first[cid] = fa
                self.pack["characters"].append({
                    "character_id": cid, "canonical_name": nc["label"], "name_zh": None, "display_label_pre_name": nc["label"],
                    "display_label_pre_name_zh": nc.get("label_zh") or nc["label"], "name_known_at_s": None, "name_public": 0,
                    "name_source": None, "kind": KIND.get(nc["kind"], "other"), "role": "minor", "first_appears_s": r3(fa),
                    "review_state": "auto", "source": self.src_seg})
                lk = nc.get("look") or {}
                if lk.get("description"):
                    fr = _t(lk.get("from")) or fa
                    self.pack["character_looks"].append({
                        "look_id": f"l{len(self.pack['character_looks']) + 1:03d}", "character_id": cid, "from_s": r3(fr),
                        "to_s": None, "description_en": lk["description"], "description_zh": None,
                        "reveal_at_s": r3(_later(fr, _t(lk.get("reveal_at"))) + TRUNC), "spoiler_critical": 0,
                        "review_state": "auto"})
                for f in nc.get("facts", []):
                    self._facts.append(self._fact(cid, f, self.src_seg))
                added.append(cid)
        self.aux["seg_added"]["characters"] = added
        self._finish_facts()

    def _finish_facts(self) -> None:
        facts = sorted(self._facts, key=lambda x: (x["row"]["reveal_at_s"], x["row"]["character_id"]))
        self.pack.setdefault("item_cues", [])
        out = []
        for i, f in enumerate(facts, 1):
            f["row"]["fact_id"] = f"f{i:03d}"
            out.append(f["row"])
            for cue in f["cues"]:
                self.pack["item_cues"].append({"item_type": "character_fact", "item_id": f["row"]["fact_id"], "cue_id": cue})
        self.pack["character_facts"] = out

    def scene_rows(self) -> None:
        scs = sorted(self.full["scenes"], key=lambda s: _t(s["start"]) or 0)
        rows, ic = [], self.pack.setdefault("item_characters", [])
        for i, s in enumerate(scs):
            S, E = _t(s["start"]), _t(s["end"])
            if i == 0 and S <= 1:
                start, ss = 0.0, True
            else:
                c = nearest_cut(self.cuts, S, S - SNAP_TOL, S + SNAP_TOL)
                start, ss = (c, True) if c is not None else (S, False)
            if i == len(scs) - 1 and E >= self.dur - 2:
                end, es = self.dur, True
            else:
                c = nearest_cut(self.cuts, E + 0.5, E, E + SNAP_TOL)
                end, es = (c, True) if c is not None else (min(self.dur, E + TRUNC), False)
            if end <= start:
                end, es = min(self.dur, max(start + 0.5, E + TRUNC)), False
            sid = f"s{i + 1:02d}"
            rows.append({"scene_id": sid, "start_s": r3(start), "end_s": r3(end), "start_s_model": S, "end_s_model": E,
                         "snapped": int(ss and es), "reveal_at_s": r3(end), "location_en": s.get("location") or None,
                         "summary_en": s["summary_en"], "summary_zh": None, "review_state": "auto", "source": self.src_full})
            for n in s.get("characters", []):
                cid = self.cid(n)
                if cid:
                    ic.append({"item_type": "scene", "item_id": sid, "character_id": cid, "relation": "present"})
        self.pack["scenes"] = rows

    def _event(self, e: dict, source: str) -> tuple[dict, list[str], list[tuple[str, str]]]:
        t = _t(e["t"])
        rev = _later(t, _t(e.get("reveal_at"))) + TRUNC
        cues = match_quote(e.get("quote", ""), self.ref_cues, t) if e.get("kind") == "dialogue" else []
        if cues:
            t = min(t, cues[0]["start_s"])
            rev = max(rev, max(c["end_s"] for c in cues))
        rels = [(n, "present") for n in e.get("present", [])] + [(n, "mentioned") for n in e.get("mentioned", [])]
        kind = e.get("kind") if e.get("kind") in ("action", "dialogue", "reveal", "arrival", "departure") else "other"
        row = {"event_id": None, "t_s": r3(t), "reveal_at_s": r3(rev), "kind": kind, "text_en": e["text"], "text_zh": None,
               "spoiler_critical": int(bool(e.get("spoiler_critical"))), "review_state": "auto", "source": source}
        return row, [c["cue_id"] for c in cues], rels

    def event_rows(self) -> None:
        evs = [self._event(e, self.src_full) for e in self.full.get("events", [])]
        full_ts = [r["t_s"] for r, _, _ in evs]
        added = 0
        for part in (self.st.get("vision_seg") or {}).get("segments", []):
            for e in part["doc"].get("events", []):
                t = _t(e["t"])
                if all(abs(t - x) > SEG_EVENT_GAP_S for x in full_ts):
                    evs.append(self._event(e, self.src_seg))
                    full_ts.append(t)
                    added += 1
        self.aux["seg_added"]["events"] = added
        evs.sort(key=lambda x: (x[0]["t_s"], x[0]["text_en"]))
        ic, icue = self.pack["item_characters"], self.pack["item_cues"]
        for i, (row, cues, rels) in enumerate(evs, 1):
            row["event_id"] = f"e{i:03d}"
            for cue in cues:
                icue.append({"item_type": "event", "item_id": row["event_id"], "cue_id": cue})
            for n, rel in rels:
                cid = self.cid(n)
                if cid and not any(x["item_type"] == "event" and x["item_id"] == row["event_id"] and x["character_id"] == cid
                                   and x["relation"] == rel for x in ic):
                    ic.append({"item_type": "event", "item_id": row["event_id"], "character_id": cid, "relation": rel})
        self.pack["events"] = [r for r, _, _ in evs]

    def object_rows(self) -> None:
        objs = [(o, self.src_full) for o in self.full.get("objects", [])]
        have = {normalize(o["name"]) for o, _ in objs}
        added = 0
        for part in (self.st.get("vision_seg") or {}).get("segments", []):
            for o in part["doc"].get("objects", []):
                k = normalize(o["name"])
                if k not in have and re.sub(r"^(the|a|an)\s+", "", k) not in {re.sub(r"^(the|a|an)\s+", "", h) for h in have}:
                    have.add(k)
                    objs.append((o, self.src_seg))
                    added += 1
        self.aux["seg_added"]["objects"] = added
        objs.sort(key=lambda x: (_t(x[0]["first_seen"]) or 0, x[0]["name"]))
        rows, sights = [], []
        for i, (o, src) in enumerate(objs, 1):
            oid = f"o{i:02d}"
            fs = _t(o["first_seen"])
            rows.append({"object_id": oid, "name_en": o["name"], "name_zh": None, "description_en": o["description"],
                         "description_zh": None, "first_seen_s": r3(fs), "reveal_at_s": r3(_later(fs, _t(o.get("reveal_at"))) + TRUNC),
                         "spoiler_critical": int(bool(o.get("spoiler_critical"))), "review_state": "auto", "source": src})
            for t in sorted({_t(x) for x in [o["first_seen"]] + o.get("seen_at", []) if _t(x) is not None}):
                sights.append({"object_id": oid, "t_s": t})
            for n in o.get("present", []) or []:
                cid = self.cid(n)
                if cid:
                    self.pack["item_characters"].append({"item_type": "object", "item_id": oid, "character_id": cid,
                                                         "relation": "present"})
        self.pack["objects"], self.pack["object_sightings"] = rows, sights

    def appearance_rows(self) -> None:
        iv: dict[str, list] = {}
        for a in self.full.get("appearances", []):
            cid = self.cid(a["character"])
            if cid:
                iv.setdefault(cid, []).append((_t(a["start"]), _t(a["end"]) + TRUNC, "full"))
        for part in (self.st.get("vision_seg") or {}).get("segments", []):
            for a in part["doc"].get("appearances", []):
                cid = self.cid(a["ref"])
                if not cid:
                    continue
                s, e = _t(a["start"]), _t(a["end"]) + TRUNC
                s = max(s, self.char_first.get(cid) or s)   # segment passes must not move the first appearance earlier
                if e > s:
                    iv.setdefault(cid, []).append((s, e, "seg"))
        for c in self.pack["characters"]:
            if c["character_id"] not in iv and c["first_appears_s"] is not None:
                iv[c["character_id"]] = [(c["first_appears_s"], c["first_appears_s"] + 2, "first_appears")]
        rows = []
        for cid, lst in iv.items():
            for a, b, src in merge_intervals(lst):
                rows.append({"character_id": cid, "start_s": r3(a), "end_s": r3(min(b, self.dur)),
                             "source": {"full": self.src_full, "seg": self.src_seg, "first_appears": "builder:first_appears"}
                             .get(src, f"{self.src_full}+seg"), "review_state": "auto"})
        self.pack["appearances"] = rows

    def present_links(self) -> None:
        new = present_rows(self.pack)
        self.pack["item_characters"] += new
        self.aux["present_links_derived"] = len(new)

    def mention_links(self) -> None:
        """Other characters named in the text → item_characters (mentioned)."""
        ic = self.pack["item_characters"]
        have = {(x["item_type"], x["item_id"], x["character_id"]) for x in ic}
        terms = []
        for c in self.pack["characters"]:
            named = c["name_source"] is not None
            ts = [(c["canonical_name"], name_case_sensitive(c["canonical_name"]) if named else 0),
                  (c["display_label_pre_name"], 0)]
            ts += [(a["alias"], name_case_sensitive(a["alias"])) for a in self.pack["character_aliases"]
                   if a["character_id"] == c["character_id"] and a["lang"] == "en" and not a["generic"]]
            terms.append((c["character_id"], [t for t in ts if t[0]]))
        def scan(item_type, item_id, text, owner=None):
            for cid, ts in terms:
                if cid == owner or (item_type, item_id, cid) in have:
                    continue
                if any(term_matches(t, text, "word", cs) for t, cs in ts):
                    ic.append({"item_type": item_type, "item_id": item_id, "character_id": cid, "relation": "mentioned"})
                    have.add((item_type, item_id, cid))
        for s in self.pack["scenes"]:
            scan("scene", s["scene_id"], s["summary_en"])
        for e in self.pack["events"]:
            scan("event", e["event_id"], e["text_en"])
        for f in self.pack["character_facts"]:
            scan("character_fact", f["fact_id"], f["text_en"], f["character_id"])
        for lk in self.pack["character_looks"]:
            scan("character_look", lk["look_id"], lk["description_en"], lk["character_id"])
        for o in self.pack["objects"]:
            scan("object", o["object_id"], o["description_en"])

    def people(self) -> list[tuple[str, str]]:
        """(real-person name, source): end credits, trailer cards / narration (identify), catalog cast / director (`.cache/sources/<id>/cast.json`)."""
        out = []
        for stage_name in ("credits", "identify"):
            st = self.st.get(stage_name) or {}
            people = st.get("people") if stage_name == "credits" else (st.get("doc") or {}).get("people")
            for p in people or []:
                if p.get("is_real_person", True) and (p.get("person_name") or "").strip():
                    out.append((" ".join(urllib.parse.unquote(p["person_name"]).split()), "credits"))
        for n in (self.st.get("cast") or {}).get("names", []):
            out.append((" ".join(n.split()), "catalog"))
        return out

    def blocklist_rows(self) -> None:
        rows, seen, refs = [], set(), {}
        chars = {normalize(c["canonical_name"]) for c in self.pack["characters"]}
        skip = chars | {normalize(self.title["title"])}
        for full, source in self.people():
            if normalize(full) in skip or has_cjk(full) and normalize(full) in skip:
                continue
            pk = re.sub(r"[^\w]", "", normalize(full))       # "X, JR." and "X Jr." are the same person
            ref = refs.get(pk)
            if ref is None:
                ref = refs[pk] = f"p{len(refs) + 1:02d}"
            for term in blocklist_variants(full):
                lang = "zh" if has_cjk(term) else "en"
                k = (normalize(term), lang)
                if k in seen:
                    continue
                seen.add(k)
                rows.append({"term": term, "lang": lang, "person_ref": ref, "source": source})
        self.pack["blocklist"] = rows


def present_rows(pack: dict) -> list[dict]:
    """Presence completion (required by kernel check rule 5c): any character whose appearances cover an item's time is recorded as present; returns the missing rows.

    Scene = overlap with the scene interval ≥ min(0.9 s, scene length / 2); event = covers [t − 1, t + 1]; object = covers any sighting ±1 s.
    Uses only non-rejected appearances and items. Appearances are attached to "the character as the viewer knows them at that time" (to a before the reveal), so derived rows also respect identity isolation.
    """
    have = {(x["item_type"], x["item_id"], x["character_id"], x["relation"]) for x in pack["item_characters"]}
    live = lambda r: r.get("review_state") != "rejected"  # noqa: E731
    aps: dict[str, list[tuple[float, float]]] = {}
    for a in pack["appearances"]:
        if live(a):
            aps.setdefault(a["character_id"], []).append((a["start_s"], a["end_s"]))
    out = []

    def add(item_type, item_id, cid):
        k = (item_type, item_id, cid, "present")
        if k not in have:
            have.add(k)
            out.append({"item_type": item_type, "item_id": item_id, "character_id": cid, "relation": "present"})

    def covers(lst, t):
        return any(a <= t + PRESENT_TOL_S and b >= t - PRESENT_TOL_S for a, b in lst)

    for sc in filter(live, pack["scenes"]):
        need = min(0.9, (sc["end_s"] - sc["start_s"]) / 2)   # a one-second appearance = [t, t + 0.999]
        for cid, lst in aps.items():
            if any(_overlap(sc["start_s"], sc["end_s"], a, b) >= need for a, b in lst):
                add("scene", sc["scene_id"], cid)
    for e in filter(live, pack["events"]):
        for cid, lst in aps.items():
            if covers(lst, e["t_s"]):
                add("event", e["event_id"], cid)
    sights: dict[str, list[float]] = {}
    for x in pack["object_sightings"]:
        sights.setdefault(x["object_id"], []).append(x["t_s"])
    for o in filter(live, pack["objects"]):
        for cid, lst in aps.items():
            if any(covers(lst, t) for t in sights.get(o["object_id"], [o["first_seen_s"]])):
                add("object", o["object_id"], cid)
    return out


def blocklist_variants(full: str) -> list[str]:
    """spec §7.1: full name, accent-folded form; a lone first / last name only if ≥5 chars and not a common word."""
    out = [full]
    folded = normalize(full, case_sensitive=True)
    if folded != full:
        out.append(folded)
    for tok in re.findall(r"[^\s.,()\-]+", full):
        if len(tok) >= 5 and not is_common(normalize(tok)) and tok not in out:
            out.append(tok)
    return out
