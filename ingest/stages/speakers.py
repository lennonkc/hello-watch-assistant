"""Speaker attribution (AutoAD who-is-speaking / HAVEN): transcription diarization + visual verification against the character roster.

Produces `stages/speakers.json`; `ingest/assemble_speakers.py` assembles it into `cue_speakers` rows (DDL v1.1).

1. Diarization: gemini-3.5-transcribe word-level + `diarization_mode = speaker` (once per film, labels consistent film-wide); each
   reference-track cue takes the label with the most time overlap.
2. Visual judgment: lines are grouped into windows by gaps ≤6 s; window video + reference crops of present characters + each line's
   diarization label and on-screen characters per shot; one call judges many lines.
3. Vote: within one diarization label, confidence-weighted vote (share ≥0.6 and ≥2 lines) gives that voice's character; merged per line with the visual verdict.
Cues of other language tracks inherit the reference track's result by time overlap.
"""
from __future__ import annotations

from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor

from ingest import gemini, prompts_shots as P
from ingest.ledger import cost_interaction
from ingest.stages import Ctx, stage
from ingest.stages import vision_shots as V
from ingest.util import fingerprint, mmss, read_json, write_json

SPEAKER_FPS = 2.0
WINDOW_GAP_S = 6.0
WINDOW_PAD_S = 1.0
MAX_LINES_PER_CALL = 40
MAX_VIDEO_S_PER_CALL = 150.0
VOTE_SHARE = 0.6
PLAUSIBLE_WINDOW_S = 20.0     # the cluster's character must appear within ± this long of the line
VISUAL_STRONG = 0.7
DIAR_MODE = {"type": "verbatim", "timestamp_granularities": ["word"], "diarization_mode": "speaker"}
PROMPT_VERSION = "speakers-v1"
DEPS = V.DEPS + ("vision_shots",)


# ── pure functions ────────────────────────────────────────────────────────────

def _sec(x) -> float | None:
    try:
        return float(str(x).rstrip("s"))
    except (TypeError, ValueError):
        return None


def parse_words(raw: dict) -> list[dict]:
    """Interactions output → [{s, e, text, spk}] (text order). Times are occasionally garbled (e.g. 107.3 s reported as 7.3 s): a start
    more than 5 s before the previous word's is replaced by that word's start; end is never before start."""
    ws = [a for st in raw.get("steps", []) for c in st.get("content", []) or [] for a in c.get("annotations", []) or []
          if a.get("type") == "word_info"]
    ws.sort(key=lambda a: a.get("start_index", 0))
    out, prev = [], 0.0
    for w in ws:
        s, e = _sec(w.get("start_offset")), _sec(w.get("end_offset"))
        if s is None:
            continue
        if s < prev - 5:
            s = prev
        e = e if e is not None and e >= s else s + 0.2
        out.append({"s": s, "e": e, "text": w.get("text", ""), "spk": w.get("speaker")})
        prev = s
    return out


def overlap(a0, a1, b0, b1) -> float:
    return max(0.0, min(a1, b1) - max(a0, b0))


def assign_diar(cues: list[dict], words: list[dict], pad: float = 0.2) -> dict[str, str | None]:
    """cue → the speaker label with the most time overlap."""
    out = {}
    for c in cues:
        acc: dict[str, float] = defaultdict(float)
        for w in words:
            if w["spk"] and w["e"] >= c["start_s"] - pad and w["s"] <= c["end_s"] + pad:
                acc[w["spk"]] += max(overlap(w["s"], w["e"], c["start_s"] - pad, c["end_s"] + pad), 0.05)
        out[c["cue_id"]] = max(sorted(acc), key=lambda k: acc[k]) if acc else None
    return out


def windows(cues: list[dict], dur: float, gap: float = WINDOW_GAP_S, pad: float = WINDOW_PAD_S) -> list[dict]:
    """Group lines with gaps ≤ gap into video windows [first start − pad, last end + pad]."""
    out = []
    for c in sorted(cues, key=lambda c: c["start_s"]):
        if out and c["start_s"] - out[-1]["last_end"] <= gap:
            out[-1]["cues"].append(c)
            out[-1]["last_end"] = max(out[-1]["last_end"], c["end_s"])
        else:
            out.append({"cues": [c], "first_start": c["start_s"], "last_end": c["end_s"]})
    return [{"start_s": round(max(0.0, w["first_start"] - pad), 3), "end_s": round(min(dur, w["last_end"] + pad), 3),
             "cues": w["cues"]} for w in out]


def batches(wins: list[dict], max_lines: int = MAX_LINES_PER_CALL, max_s: float = MAX_VIDEO_S_PER_CALL) -> list[list[dict]]:
    """Pack windows into calls (order preserved): lines ≤ max_lines and total video ≤ max_s; an oversized window gets its own batch."""
    out, cur, n, s = [], [], 0, 0.0
    for w in wins:
        wl, ws = len(w["cues"]), w["end_s"] - w["start_s"]
        if cur and (n + wl > max_lines or s + ws > max_s):
            out.append(cur)
            cur, n, s = [], 0, 0.0
        cur.append(w)
        n, s = n + wl, s + ws
    if cur:
        out.append(cur)
    return out


def vote(visual: dict[str, tuple[str | None, float]], diar: dict[str, str | None],
         share_min: float = VOTE_SHARE) -> dict[str, tuple[str, float, int]]:
    """Diarization label → (character, weighted share, votes); labels below threshold are omitted. unknown / off-screen lines count toward the denominator by their confidence."""
    acc: dict[str, dict[str | None, float]] = defaultdict(lambda: defaultdict(float))
    cnt: dict[str, dict[str | None, int]] = defaultdict(lambda: defaultdict(int))
    for cue, lab in diar.items():
        if lab is None or cue not in visual:
            continue
        cid, conf = visual[cue]
        acc[lab][cid] += max(conf, 0.05)
        cnt[lab][cid] += 1
    out = {}
    for lab, w in acc.items():
        total = sum(w.values())
        named = {k: v for k, v in w.items() if k}
        if not named or total <= 0:
            continue
        top = max(sorted(named), key=lambda k: named[k])
        share = named[top] / total
        if share >= share_min and cnt[lab][top] >= 2:
            out[lab] = (top, round(share, 3), cnt[lab][top])
    return out


def decide(v_cid: str | None, v_conf: float, cluster: tuple[str, float, int] | None,
           plausible: bool = True) -> tuple[str | None, float, str]:
    """Per-line visual verdict + its diarization label's vote → (character, confidence, method).

    Diarization can merge short lines from different people into one cluster: if the cluster's character does not appear near the line (plausible = False), the cluster verdict does not override the visual one."""
    if cluster and not plausible and v_cid != cluster[0]:
        cluster = None
    if cluster and v_cid == cluster[0]:
        return v_cid, round(min(1.0, max(v_conf, cluster[1]) + 0.1), 3), "diarization+visual"
    if v_cid and v_conf >= VISUAL_STRONG:
        return v_cid, round(v_conf * (0.8 if cluster else 1.0), 3), "visual"
    if cluster:
        return cluster[0], round(0.4 + 0.4 * cluster[1], 3), "diarization-vote"
    if v_cid:
        return v_cid, round(v_conf, 3), "visual"
    return None, round(v_conf, 3), "visual"


def inherit(other: list[dict], ref: list[dict], decided: dict[str, dict]) -> dict[str, dict]:
    """A non-reference-track cue inherits the verdict of the reference cue overlapping it by ≥ half the shorter one."""
    out = {}
    for c in other:
        best, bo = None, 0.0
        for r in ref:
            o = overlap(c["start_s"], c["end_s"], r["start_s"], r["end_s"])
            if o > bo:
                best, bo = r, o
        if best and best["cue_id"] in decided and bo >= 0.5 * min(c["end_s"] - c["start_s"], best["end_s"] - best["start_s"]):
            d = decided[best["cue_id"]]
            out[c["cue_id"]] = {**d, "cue_id": c["cue_id"], "method": d["method"] + "+track_align"}
    return out


def near_appearance(ros: list[dict], cid: str, cue: dict, w: float = PLAUSIBLE_WINDOW_S) -> bool:
    ch = next((r for r in ros if r["ref"] == cid), None)
    return bool(ch) and any(a <= cue["end_s"] + w and b >= cue["start_s"] - w for a, b in ch["aps"])


# ── calls ─────────────────────────────────────────────────────────────

def parts_dir(ctx: Ctx):
    d = ctx.wd / "stages" / "speakers_parts"
    d.mkdir(parents=True, exist_ok=True)
    return d


def diarize(ctx: Ctx, c) -> list[dict]:
    if ctx.load("register")["audio_tracks"] == 0:
        return []
    from ingest.stages.gemini_stages import ensure_audio_file
    audio = V.with_retry(ensure_audio_file, ctx, c)
    lang = ctx.title.get("dialogue_lang", "en")
    key = fingerprint([gemini.TRANSCRIBE_MODEL, audio["sha256"], DIAR_MODE, lang])
    pf = parts_dir(ctx) / "diar.json"
    if pf.exists() and read_json(pf).get("key") == key:
        return read_json(pf)["words"]
    ctx.ledger.guard(0.1)
    import time
    t0 = time.time()
    inter = V.with_retry(c.interactions.create, model=gemini.TRANSCRIBE_MODEL,
                         input=[{"type": "audio", "uri": audio["uri"], "mime_type": audio["mime_type"]}],
                         generation_config={"transcription_config": {"mode": DIAR_MODE, "language_codes": [lang]}})
    raw = inter.model_dump(mode="json", exclude_none=True)
    usage = raw.get("usage", {})
    ctx.ledger.log("speakers:diarize", "ran", time.time() - t0, cost_interaction(gemini.TRANSCRIBE_MODEL, usage), usage=usage)
    words = parse_words(raw)
    write_json(pf, {"key": key, "model": gemini.TRANSCRIBE_MODEL, "words": words})
    return words


def on_screen(shots: list[dict], s0: float, s1: float) -> list[tuple[str, str | None]]:
    seen = {}
    for sh in shots:
        if sh["start_s"] < s1 and sh["end_s"] > s0:
            for x in sh["characters"]:
                if x["ref"] != "other":
                    seen.setdefault(x["ref"], x.get("position"))
    return list(seen.items())


def run_visual(ctx: Ctx, c, ros: list[dict], vs: dict, cues: list[dict], diar: dict[str, str | None],
               dur: float) -> dict[str, tuple[str | None, float, str]]:
    rec = V.gemini_rec(ctx, c)
    ex_by_char = V.group_exemplars(vs.get("exemplars", []))
    shots = vs.get("shots", [])
    pd = parts_dir(ctx)
    labels = {lab: f"S{i}" for i, lab in enumerate(sorted({x for x in diar.values() if x}), 1)}

    def one(arg):
        bi, batch = arg
        b0, b1 = batch[0]["start_s"], batch[-1]["end_s"]
        parts, lines, ids = [], [], {}
        for wi, w in enumerate(batch, 1):
            parts += [gemini.text_part(f"Window {wi} [{mmss(w['start_s'])}–{mmss(w['end_s'])}]:"),
                      gemini.video_part(rec, SPEAKER_FPS, w["start_s"], w["end_s"])]
            for cu in w["cues"]:
                lid = f"L{len(ids) + 1:02d}"
                ids[lid] = cu["cue_id"]
                os_ = on_screen(shots, cu["start_s"], cu["end_s"])
                lines.append(f"{lid} (window {wi}) [{mmss(cu['start_s'])}–{mmss(cu['end_s'])}] "
                             f"{labels.get(diar.get(cu['cue_id']), 'S?')}: \"{cu['text']}\""
                             + (" — on screen: " + ", ".join(f"{r} ({p or '?'})" for r, p in os_) if os_ else " — on screen: nobody identified"))
        cand = [{"ref": ch["ref"], "label": ch["label"], "kind": ch["kind"]} for ch in ros
                if ch["first"] <= b1 + 2 and any(a <= w["end_s"] + 1 and b >= w["start_s"] - 1 for w in batch for a, b in ch["aps"])]
        ex_parts, used = V.exemplar_parts(ctx, cand, ex_by_char, (b0 + b1) / 2)
        prompt = P.speaker_prompt("", cand, "\n".join(lines))
        key = fingerprint([PROMPT_VERSION, gemini.VISION_MODEL, SPEAKER_FPS, [(w["start_s"], w["end_s"]) for w in batch],
                           prompt, P.SPEAKER_SCHEMA, used])
        r = V.cached_call(ctx, c, pd / f"vis_{bi:02d}.json", key, f"speakers:vis_{bi:02d}", 0.06,
                          parts + ex_parts + [gemini.text_part(prompt)], P.SPEAKER_SCHEMA)
        refs = {x["ref"] for x in cand} | {ch["ref"] for ch in ros if ch["first"] <= b1 + 2}
        out = {}
        for x in r["doc"].get("lines", []):
            cue = ids.get(x.get("line"))
            if not cue:
                continue
            spk = x.get("speaker") if x.get("speaker") in refs else None
            conf = min(1.0, max(0.0, float(x.get("confidence") or 0)))
            out[cue] = (spk, conf, x.get("evidence") or "")
        return out

    res = {}
    with ThreadPoolExecutor(V.WORKERS) as ex:
        for d in ex.map(one, enumerate(batches(windows(cues, dur)))):
            res.update(d)
    return res


@stage("speakers", deps=DEPS, paid=True,
       inputs=lambda ctx: {"impl": PROMPT_VERSION, "model": gemini.VISION_MODEL, "diar": DIAR_MODE, "fps": SPEAKER_FPS,
                           "prompt": fingerprint([P.speaker_prompt("", [], ""), P.SPEAKER_SCHEMA])})
def run_speakers(ctx: Ctx) -> dict:
    asm = V.base_assembler(ctx)
    ros = V.roster(asm)
    vs = ctx.load("vision_shots")
    story_end = V.story_end_of(asm)
    ref = [cu for cu in asm.ref_cues if story_end is None or cu["start_s"] < story_end]
    if not ref:
        return {"cues": [], "skipped": "no dialogue cues"}
    c = gemini.client()
    words = diarize(ctx, c)
    diar = assign_diar(ref, words)
    visual = run_visual(ctx, c, ros, vs, ref, diar, asm.dur)
    clusters = vote({k: (v[0], v[1]) for k, v in visual.items()}, diar)
    decided = {}
    for cu in ref:
        v_cid, v_conf, ev = visual.get(cu["cue_id"], (None, 0.0, ""))
        cl = clusters.get(diar.get(cu["cue_id"]))
        cid, conf, method = decide(v_cid, v_conf, cl, plausible=bool(cl) and near_appearance(ros, cl[0], cu))
        decided[cu["cue_id"]] = {"cue_id": cu["cue_id"], "character_id": cid, "diar_label": diar.get(cu["cue_id"]),
                                 "confidence": conf, "method": method, "evidence": ev, "visual": v_cid}
    ref_ids = {cu["cue_id"] for cu in ref}
    other = [cu for cu in asm.pack["cues"] if cu["kind"] == "dialogue" and cu["cue_id"] not in ref_ids
             and (story_end is None or cu["start_s"] < story_end)]
    inherited = inherit(other, ref, decided)
    return {"model": gemini.VISION_MODEL, "diar_model": gemini.TRANSCRIBE_MODEL, "prompt_version": PROMPT_VERSION,
            "n_words": len(words), "n_diar_labels": len({w["spk"] for w in words if w["spk"]}),
            "clusters": {k: list(v) for k, v in clusters.items()},
            "cues": sorted(list(decided.values()) + list(inherited.values()), key=lambda x: x["cue_id"])}
