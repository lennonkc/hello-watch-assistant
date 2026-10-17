"""Progressive recaps (MovieChat breakpoint mode): at the end of each scene, generate a "story so far" summary into `recaps`.

Spoiler-free by construction: checkpoint k is fed only scenes / events / subtitles with reveal time ≤ upto_s, and names follow
spec §6 visibility at upto_s. Checkpoints are generated independently (no chaining off the previous recap, so errors don't
accumulate) and can run in parallel. Output is re-checked against name visibility and the blocklist; on a hit we retry once
with feedback, and if it still hits the checkpoint is dropped (online falls back to the previous eligible recap).
"""
from __future__ import annotations

import concurrent.futures as cf
import json

from ingest import gemini
from ingest import promptbook as pb
from ingest.checks import name_terms
from ingest.derive import margins
from ingest.ledger import cost_generate
from ingest.textnorm import term_matches
from ingest.util import fingerprint, mmss, read_json, write_json

WORKERS = 6
RECAP_SCHEMA = {"type": "object", "properties": {"recap_en": {"type": "string"}, "recap_zh": {"type": "string"}},
                "required": ["recap_en", "recap_zh"]}


def checkpoints(pack: dict) -> list[dict]:
    """One checkpoint at the end of each story scene (sorted by reveal time; one per distinct time)."""
    a = pack["asset"]
    out, seen = [], set()
    for s in sorted(pack["scenes"], key=lambda s: (s["reveal_at_s"], s["scene_id"])):
        if s["review_state"] == "rejected" or s["start_s"] >= a["story_end_s"] or s["reveal_at_s"] < a["story_start_s"]:
            continue
        if s["reveal_at_s"] in seen:
            continue
        seen.add(s["reveal_at_s"])
        # Unsnapped scenes have a 2 s larger gate margin than snapped ones; recaps are gated with the snapped margin, so shift the reveal time to compensate
        extra = 0.0 if s["snapped"] else margins(pack)["gate_margin_scene_unsnapped_s"] - margins(pack)["gate_margin_scene_snapped_s"]
        out.append({"scene_id": s["scene_id"], "upto_s": s["reveal_at_s"], "reveal_at_s": round(s["reveal_at_s"] + extra, 3)})
    return out


def _cue_track(pack: dict) -> str | None:
    tracks = sorted(pack["subtitle_tracks"], key=lambda t: (not t["lang"].startswith("en"), t["priority"]))
    return tracks[0]["track"] if tracks else None


def character_refs(pack: dict, t: float) -> list[dict]:
    """Usable references for characters already on screen by t (§6: name only once visible, otherwise the label)."""
    m = margins(pack)
    out = []
    for c in sorted(pack["characters"], key=lambda c: (c["first_appears_s"] is None, c["first_appears_s"] or 0)):
        if c["review_state"] == "rejected" or c["first_appears_s"] is None or c["first_appears_s"] > t:
            continue
        named = c["name_public"] or (c["name_known_at_s"] is not None
                                     and c["name_known_at_s"] + m["gate_margin_reviewed_reveal_s"] <= t)
        en = c["canonical_name"] if named else c["display_label_pre_name"]
        zh = (c["name_zh"] or c["canonical_name"]) if named else c["display_label_pre_name_zh"]
        out.append({"en": en, "zh": zh})
    return out


def inputs_at(pack: dict, t: float) -> dict:
    """Scenes / events / subtitles with reveal time ≤ t (time-ordered); rejected rows excluded."""
    live = lambda rows: [r for r in rows if r["review_state"] != "rejected"]  # noqa: E731
    scenes = sorted((s for s in live(pack["scenes"]) if s["reveal_at_s"] <= t), key=lambda s: s["start_s"])
    events = sorted((e for e in live(pack["events"]) if e["reveal_at_s"] <= t), key=lambda e: e["t_s"])
    track = _cue_track(pack)
    cues = sorted((c for c in pack["cues"] if c["track"] == track and c["kind"] == "dialogue" and c["end_s"] <= t),
                  key=lambda c: c["start_s"])
    return {"scenes": [(s["start_s"], s["summary_en"]) for s in scenes],
            "events": [(e["t_s"], e["text_en"]) for e in events],
            "dialogue": [(c["start_s"], c["text"]) for c in cues]}


def prompt_for(pack: dict, cp: dict) -> str:
    t = cp["upto_s"]
    inp = inputs_at(pack, t)
    story = t - pack["asset"]["story_start_s"]
    words = int(min(180, max(40, 40 + story / 60 * 15)))
    lines = [pb.render("recap_rules", at=mmss(t)[:5], words=words), "CHARACTERS (en / zh):"]
    lines += [f"- {r['en']} / {r['zh']}" for r in character_refs(pack, t)] or ["- (none yet)"]
    lines.append("SCENES:")
    lines += [f"[{mmss(s)[:5]}] {txt}" for s, txt in inp["scenes"]]
    lines.append("EVENTS:")
    lines += [f"[{mmss(s)[:5]}] {txt}" for s, txt in inp["events"]]
    if inp["dialogue"]:
        lines.append("DIALOGUE (subtitles):")
        lines += [f"[{mmss(s)[:5]}] {txt}" for s, txt in inp["dialogue"][-60:]]
    return "\n".join(lines)


def violations(pack: dict, text: str, t: float) -> list[str]:
    """Names / non-generic aliases that appear before they are visible at t, or hits on the real-person blocklist."""
    bad = [term for _, term, k, cs in name_terms(pack) if t < k and term_matches(term, text, "word", cs)]
    bad += [b["term"] for b in pack["blocklist"] if term_matches(b["term"], text, "word", False)]
    return sorted(set(bad))


def _generate(c, prompt: str) -> tuple[dict, dict, float]:
    text, usage, lat, _ = gemini.generate_json(c, gemini.VISION_MODEL, [gemini.text_part(prompt)], RECAP_SCHEMA,
                                               thinking="LOW", max_out=4096)
    return json.loads(text), usage, lat


def _one(ctx, c, pack: dict, cp: dict, cache_dir) -> dict | None:
    prompt = prompt_for(pack, cp)
    key = fingerprint([gemini.VISION_MODEL, prompt, RECAP_SCHEMA])
    pf = cache_dir / f"{cp['scene_id']}.json"
    if pf.exists() and read_json(pf).get("key") == key:
        return read_json(pf)["result"]
    result, tries = None, []
    p = prompt
    for _ in range(2):
        ctx.ledger.guard(0.01)
        doc, usage, lat = _generate(c, p)
        ctx.ledger.log(f"recaps:{cp['scene_id']}", "ran", lat, cost_generate(gemini.VISION_MODEL, usage), usage=usage)
        bad = violations(pack, doc["recap_en"] + "\n" + doc["recap_zh"], cp["reveal_at_s"])
        tries.append({"doc": doc, "violations": bad})
        if not bad:
            result = doc
            break
        p = prompt + pb.render("recap_retry", terms=", ".join(bad))
    write_json(pf, {"key": key, "result": result, "tries": tries})
    return result


def add_recaps(ctx, pack: dict) -> None:
    cps = checkpoints(pack)
    pack["recaps"] = []
    if not cps:
        return
    cache_dir = ctx.wd / "post" / "recaps"
    cache_dir.mkdir(parents=True, exist_ok=True)
    c = gemini.client()
    with cf.ThreadPoolExecutor(WORKERS) as ex:
        docs = list(ex.map(lambda cp: _one(ctx, c, pack, cp, cache_dir), cps))
    for i, (cp, doc) in enumerate(zip(cps, docs), 1):
        if doc is None:
            continue
        pack["recaps"].append({"recap_id": f"rc{i:03d}", "upto_scene_id": cp["scene_id"], "upto_s": cp["upto_s"],
                               "reveal_at_s": cp["reveal_at_s"], "text_en": doc["recap_en"].strip(),
                               "text_zh": doc["recap_zh"].strip() or None, "source": f"recap@{gemini.VISION_MODEL}",
                               "review_state": "auto"})
