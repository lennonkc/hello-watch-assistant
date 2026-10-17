"""Subtitles: official track parsing (multilingual), generated track (only when the language has no official track), alignment check (transcription word timestamps as independent reference)."""
from __future__ import annotations

import difflib
import re
import statistics as st

from ingest.stages import Ctx, stage
from ingest.util import sha256_bytes

PRIORITY = {"official": 0, "human": 1, "generated": 2}
ALIGN_TOL_S = 0.3        # no correction when offset and total film-wide drift are both within this


def decode(raw: bytes, enc: str | None) -> str:
    if raw.startswith(b"\xff\xfe") or raw.startswith(b"\xfe\xff"):
        return raw.decode("utf-16")
    if raw.startswith(b"\xef\xbb\xbf"):
        return raw[3:].decode("utf-8")
    for e in [enc, "utf-8"]:
        if not e:
            continue
        try:
            return raw.decode(e)
        except UnicodeDecodeError:
            continue
    raise ValueError("cannot decode subtitle file")


def _ts(s: str) -> float:
    h, m, sec = s.strip().replace(",", ".").split(":")
    return int(h) * 3600 + int(m) * 60 + float(sec)


def parse_srt(text: str) -> list[dict]:
    text = text.replace("\r\n", "\n").replace("\r", "\n").lstrip("﻿")
    cues = []
    for blk in re.split(r"\n\s*\n", text.strip()):
        lines = [ln.strip() for ln in blk.strip().split("\n")]
        k = next((i for i, ln in enumerate(lines) if "-->" in ln), None)
        if k is None:
            continue
        a, b = lines[k].split("-->")
        body = " ".join(ln for ln in lines[k + 1:] if ln)
        if not body:
            continue
        kind = "sfx" if re.fullmatch(r"[\[(（【].*[\])）】]", body) else ("lyrics" if body.startswith("♪") else "dialogue")
        cues.append({"start_s": round(_ts(a), 3), "end_s": round(_ts(b.split()[0]), 3), "text": body, "kind": kind})
    return cues


def load_tracks(ctx: Ctx) -> tuple[list[dict], list[dict]]:
    tracks, name_sources = [], []
    for t in ctx.title.get("subtitles", []):
        raw = (ctx.subs_dir / t["file"]).read_bytes()
        sha = sha256_bytes(raw)
        if t.get("sha256") and sha != t["sha256"]:
            raise RuntimeError(f"subtitle {t['file']} sha256 mismatch")
        tracks.append({"track": t["track"], "lang": t["lang"], "source": t["track"].split(".", 1)[1],
                       "source_url": t.get("source_url"), "license": ctx.title.get("subtitle_license"),
                       "sha256": sha, "cues": parse_srt(decode(raw, t.get("encoding")))})
    for t in ctx.title.get("name_sources", []):
        raw = (ctx.subs_dir / t["file"]).read_bytes()
        name_sources.append({"lang": t["lang"], "file": t["file"], "sha256": sha256_bytes(raw),
                             "cues": parse_srt(decode(raw, t.get("encoding")))})
    return tracks, name_sources


@stage("subtitles", deps=("register",),
       inputs=lambda ctx: {"title_subs": ctx.title.get("subtitles", []), "names": ctx.title.get("name_sources", [])})
def run(ctx: Ctx) -> dict:
    tracks, name_sources = load_tracks(ctx)
    return {"tracks": tracks, "name_sources": name_sources}


# ── alignment check ────────────────────────────────────────────────────────────

def _norm_words(s: str) -> list[str]:
    return re.sub(r"[^a-z' ]", " ", s.lower()).split()


def align_official(cues: list[dict], words: list[dict]) -> dict:
    """For each official cue, find the textually closest word window within ±15 s; fit start delta = offset + slope·t."""
    rows = []
    for c in cues:
        ow = _norm_words(c["text"])
        if not ow:
            continue
        best = None
        for i, w in enumerate(words):
            if abs(w["s"] - c["start_s"]) > 15:
                continue
            for j in range(i, min(len(words), i + len(ow) + 3)):
                ww = [x for y in words[i:j + 1] for x in _norm_words(y["text"])]
                r = difflib.SequenceMatcher(None, ow, ww).ratio()
                score = r - 0.02 * abs(w["s"] - c["start_s"])
                if best is None or score > best[0]:
                    best = (score, r, i)
        if best and best[1] >= 0.5:
            rows.append({"start_s": c["start_s"], "d_start": round(words[best[2]]["s"] - c["start_s"], 3)})
    # one-to-one: keep only the nearest cue per start word
    if len(rows) < 3:
        return {"matched": len(rows), "of": len(cues), "offset_s": None, "slope": None}
    xs = [r["start_s"] for r in rows]
    ys = [r["d_start"] for r in rows]
    mx, my = st.mean(xs), st.mean(ys)
    den = sum((x - mx) ** 2 for x in xs) or 1.0
    b = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / den
    a = my - b * mx
    ads = sorted(abs(y) for y in ys)
    return {"matched": len(rows), "of": len(cues), "offset_s": round(a, 3), "slope": round(b, 7),
            "abs_d_start_median": round(st.median(ads), 3), "abs_d_start_p90": ads[int(0.9 * (len(ads) - 1))]}


def words_from_transcript(raw: dict) -> list[dict]:
    ws = [a for s in raw.get("steps", []) for c in s.get("content", []) or [] for a in c.get("annotations", []) or []
          if a.get("type") == "word_info"]
    return [{"s": float(w["start_offset"].rstrip("s")), "e": float(w["end_offset"].rstrip("s")), "text": w["text"],
             "speaker": w.get("speaker")} for w in ws]


CJK_CHAR = re.compile(r"[\u3400-\u9fff\uf900-\ufaff]")
PLAIN_MIN_GAP_S = 0.3     # words recovered by the plain pass need at least this much gap to be inserted


def _is_cjk(s: str) -> bool:
    return bool(CJK_CHAR.search(s or ""))


def _join(a: str, b: str) -> str:
    return a + b if (_is_cjk(a[-1:]) or _is_cjk(b[:1])) else a + " " + b


def generated_cues(words: list[dict]) -> list[dict]:
    out, cur = [], None
    for w in words:
        brk = (cur is None or w["s"] - cur["end_s"] > 0.6 or re.search(r"[.?!。？！]$", cur["text"])
               or w["e"] - cur["start_s"] > 6)
        if brk:
            cur = {"start_s": w["s"], "end_s": w["e"], "text": w["text"], "kind": "dialogue"}
            out.append(cur)
        else:
            cur["end_s"] = w["e"]
            cur["text"] = _join(cur["text"], w["text"])
    return out


def plain_text(raw: dict) -> str:
    return " ".join(c.get("text", "") for s in raw.get("steps", []) for c in s.get("content", []) or []
                    if c.get("type") == "text").strip()


def surface_tokens(text: str) -> list[tuple[str, str]]:
    """(normalized key, surface): Latin split on whitespace (key lowercased, punctuation stripped), CJK per character."""
    out = []
    for chunk in text.split():
        buf = ""
        for ch in chunk:
            if _is_cjk(ch):
                if buf:
                    out.append(buf)
                    buf = ""
                out.append(ch)
            elif re.match(r"[\w']", ch) or not out or buf:
                buf += ch
            else:
                buf += ch
        if buf:
            out.append(buf)
    return [(re.sub(r"[^\w']", "", t.lower()), t) for t in out if re.sub(r"[^\w']", "", t.lower())]


def merge_plain(words: list[dict], plain: str) -> tuple[list[dict], dict]:
    """The plain pass (no timestamps) fills lines missed by the word-level pass: sequence matching finds runs only in plain and places them in gaps between word-timed neighbours.

    Inserted cue time = [end of previous aligned word, start of next aligned word] (conservative: end_s is the latest possible time); gaps < 0.3 s are skipped.
    """
    wt = []      # word-level pass per token: (key, start, end)
    for w in words:
        for k, _ in surface_tokens(w["text"]):
            wt.append((k, w["s"], w["e"]))
    pt = surface_tokens(plain)
    sm = difflib.SequenceMatcher(None, [k for k, _, _ in wt], [k for k, _ in pt], autojunk=False)
    extra, dropped = [], 0
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag != "insert":
            continue
        lo = wt[i1 - 1][2] if i1 > 0 else 0.0
        hi = wt[i1][1] if i1 < len(wt) else (wt[-1][2] + 3.0 if wt else None)
        text = ""
        for _, sf in pt[j1:j2]:
            text = _join(text, sf) if text else sf
        if hi is None or hi - lo < PLAIN_MIN_GAP_S:
            dropped += 1
            continue
        extra.append({"start_s": round(lo, 3), "end_s": round(hi, 3), "text": text, "kind": "dialogue",
                      "interpolated": True})
    return extra, {"plain_tokens": len(pt), "word_tokens": len(wt), "inserted_cues": len(extra),
                   "dropped_no_gap": dropped, "matched_ratio": round(sm.ratio(), 3)}


@stage("align", deps=("subtitles", "transcribe"),
       inputs=lambda ctx: {"tol": ALIGN_TOL_S, "v": 2,
                           **({"plain": ctx.load("transcribe_plain")["_fingerprint"], "v3": 1}
                              if ctx.stage_path("transcribe_plain").exists() else {})})
def run_align(ctx: Ctx) -> dict:
    subs = ctx.load("subtitles")
    words = words_from_transcript(ctx.load("transcribe")["raw"])
    lang = ctx.title.get("dialogue_lang", "en")
    dur = ctx.load("register")["duration_s"]
    ref = next((t for t in subs["tracks"] if t["lang"] == lang and t["source"] == "official"), None)
    corrections = {}
    report = {}
    if ref:
        fit = align_official(ref["cues"], words)
        report[ref["track"]] = fit
        off = fit["offset_s"] or 0.0
        drift_h = (fit["slope"] or 0.0) * 3600
        apply = abs(off) > ALIGN_TOL_S or abs((fit["slope"] or 0.0) * dur) > ALIGN_TOL_S
        note = (f"transcribe word timestamps ({fit['matched']}/{fit['of']} cues matched): offset {off:+.2f} s, "
                f"drift {(fit['slope'] or 0) * dur:+.2f} s over {dur:.0f} s; "
                + ("correction applied" if apply else "within tolerance, no correction"))
        # other language tracks: reuse the same correction when timecodes match the reference (same count, start delta ≤1 s)
        ref_starts = [c["start_s"] for c in ref["cues"]]
        for t in subs["tracks"]:
            if t["track"] == ref["track"]:
                corrections[t["track"]] = {"offset_s": -off if apply else 0.0,
                                           "drift_s_per_hour": -drift_h if apply else 0.0, "note": note}
                continue
            ds = [min(abs(c["start_s"] - r) for r in ref_starts) for c in t["cues"]]
            same = sum(d <= 1.0 for d in ds)
            n2 = (f"timing vs {ref['track']}: {same}/{len(ds)} cue starts within 1 s (max {max(ds):.2f} s); "
                  + ("same correction as reference" if apply else "no correction"))
            corrections[t["track"]] = {"offset_s": -off if apply else 0.0,
                                       "drift_s_per_hour": -drift_h if apply else 0.0, "note": n2}
    gen = None
    if not ref and words:
        cues = generated_cues(words)
        note = f"gemini transcribe, word timestamps: {len(cues)} cues"
        if ctx.stage_path("transcribe_plain").exists():
            extra, rep = merge_plain(words, plain_text(ctx.load("transcribe_plain")["raw"]))
            report["plain_merge"] = rep
            cues = sorted(cues + extra, key=lambda c: (c["start_s"], c["end_s"]))
            note += (f"; plain pass added {len(extra)} cue(s) without own timestamps (time = gap between word-timed "
                     f"neighbours, review)")
        gen = {"track": f"{lang}.generated", "lang": lang, "source": "generated", "source_url": None,
               "license": None, "sha256": None, "cues": cues, "note": note}
    return {"report": report, "corrections": corrections, "generated_track": gen}
