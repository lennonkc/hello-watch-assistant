"""Shot-level understanding (Shot-by-Shot) + in-film character roster (AutoAD-II exemplars).

Produces `stages/vision_shots.json`: {"exemplars": [...], "shots": [...]}; `ingest/assemble_shots.py` assembles it into
`character_exemplars` / `shot_captions` / `shot_entities` rows (DDL v1.1).

1. Roster: for each look of each character, sample 1 fps thumbnails evenly within its appearances as candidates; one call judges
   visibility / clarity and gives boxes; keep ≤3 per look.
2. Shots: cut-based shots (< 1 s merged into a neighbour) are packed per segment; each call = segment video + a keyframe thumbnail per
   shot + reference crops of only the characters likely present (AutoAD-II: sending everyone is worse; keep other). Boxes are on the
   keyframe (bbox_t_s = keyframe second).
Character and look ids come from an assembler fed the same inputs as build (`base_assembler`); prompts get only ids and labels, never names.
"""
from __future__ import annotations

import io
import json
import math
from concurrent.futures import ThreadPoolExecutor

from ingest import gemini, prompts_shots as P
from ingest.ledger import cost_generate
from ingest.stages import Ctx, stage
from ingest.util import fingerprint, mmss, read_json, write_json

SHOT_FPS = 2.0
MIN_SHOT_S = 1.0
EXEMPLAR_CANDIDATES = 12      # max candidates sent per character
EXEMPLARS_PER_LOOK = 3
EXEMPLAR_MIN_CLARITY = 0.45
EXEMPLAR_MIN_GAP_S = 5        # min spacing between exemplars of the same look
CROP_MAX_SIDE = 320
WORKERS = 4
PROMPT_VERSION = "shots-v1"
DEPS = ("shots", "segments", "subtitles", "align", "thumbs", "vision_full", "vision_seg")
POSITIONS = ("left", "center", "right", "background")


# ── assembler with the same inputs as build (final character / look / appearance / object ids) ────────────

def base_assembler(ctx: Ctx):
    from ingest.assemble import Assembler
    st = {n: ctx.load(n) for n in ("register",) + DEPS if ctx.stage_path(n).exists()}
    st["asset_mismatch"] = None
    asm = Assembler(st, ctx.title, ctx.catalog_item)
    for step in (asm.asset_rows, asm.subtitle_rows, asm.structure_rows, asm.character_rows, asm.seg_characters,
                 asm.scene_rows, asm.event_rows, asm.object_rows, asm.appearance_rows):
        step()
    return asm


def roster(asm) -> list[dict]:
    """Characters (by id) + their looks and appearance intervals; only id / label / kind, no names."""
    looks: dict[str, list] = {}
    for lk in asm.pack["character_looks"]:
        looks.setdefault(lk["character_id"], []).append(lk)
    aps: dict[str, list] = {}
    for a in asm.pack["appearances"]:
        aps.setdefault(a["character_id"], []).append((a["start_s"], a["end_s"]))
    out = []
    for c in asm.pack["characters"]:
        if c["first_appears_s"] is None:
            continue
        cid = c["character_id"]
        out.append({"ref": cid, "label": c["display_label_pre_name"], "kind": c["kind"], "first": c["first_appears_s"],
                    "looks": sorted(looks.get(cid, []), key=lambda x: x["from_s"]), "aps": sorted(aps.get(cid, []))})
    return out


def story_end_of(asm) -> float | None:
    a = asm.pack["asset"]
    return a["story_end_s"] if a.get("credits_start_s") is not None else None


# ── pure functions ────────────────────────────────────────────────────────────

def parse_box(v) -> list[int] | None:
    """Gemini box [ymin, xmin, ymax, xmax] (0–1000) → normalized ints; invalid / degenerate (side < 10) → None."""
    if not isinstance(v, (list, tuple)) or len(v) != 4:
        return None
    try:
        y0, x0, y1, x1 = (min(1000, max(0, int(round(float(x))))) for x in v)
    except (TypeError, ValueError):
        return None
    y0, y1 = sorted((y0, y1))
    x0, x1 = sorted((x0, x1))
    if y1 - y0 < 10 or x1 - x0 < 10:
        return None
    return [y0, x0, y1, x1]


def box_area(b: list[int]) -> float:
    return (b[2] - b[0]) * (b[3] - b[1]) / 1e6


def position_from_box(b: list[int] | None) -> str | None:
    if not b:
        return None
    if box_area(b) < 0.01:
        return "background"
    cx = (b[1] + b[3]) / 2
    return "left" if cx < 350 else "right" if cx > 650 else "center"


def merge_short(shots: list[dict], min_len: float = MIN_SHOT_S) -> list[dict]:
    """Merge shots < min_len into the previous shot (the first one into the next). Input is time-sorted and contiguous."""
    out: list[dict] = []
    carry = None
    for s in shots:
        cur = {"start_s": s["start_s"], "end_s": s["end_s"], "shot_ids": [s["shot_id"]]}
        if carry:
            cur = {"start_s": carry["start_s"], "end_s": cur["end_s"], "shot_ids": carry["shot_ids"] + cur["shot_ids"]}
            carry = None
        if cur["end_s"] - cur["start_s"] < min_len:
            if out:
                out[-1]["end_s"] = cur["end_s"]
                out[-1]["shot_ids"] += cur["shot_ids"]
            else:
                carry = cur
            continue
        out.append(cur)
    if carry:
        if out:
            out[-1]["end_s"] = carry["end_s"]
            out[-1]["shot_ids"] += carry["shot_ids"]
        else:
            out.append(carry)
    return out


def keyframe(start: float, end: float, avail: set[int]) -> int | None:
    """Representative thumbnail second N within the shot (tN = last frame with pts ≤ N, so start ≤ N < end); the available one nearest the midpoint."""
    lo, hi = math.ceil(start - 1e-6), math.ceil(end - 1e-6) - 1
    if hi < lo:
        return None
    mid = (start + end) / 2
    for n in sorted(range(lo, hi + 1), key=lambda n: (abs(n - mid), n)):
        if n in avail:
            return n
    return None


def spread(xs: list[int], k: int) -> list[int]:
    """Pick k evenly spaced items from sorted ints."""
    if len(xs) <= k:
        return list(xs)
    step = len(xs) / k
    return [xs[int(step * i + step / 2)] for i in range(k)]


def exemplar_candidates(ch: dict, avail: set[int], story_end: float | None, k: int = EXEMPLAR_CANDIDATES) -> list[dict]:
    """Per look, evenly sample candidate seconds in [from, to) ∩ appearances ∩ [first, story_end); if empty, use the 10 s after the look starts."""
    looks = ch["looks"] or [{"look_id": None, "from_s": ch["first"], "to_s": None}]
    per = max(3, k // len(looks))
    out = []
    end_cap = story_end if story_end is not None else float("inf")
    for lk in looks:
        lo = max(lk["from_s"], ch["first"])
        hi = min(lk["to_s"] if lk["to_s"] is not None else float("inf"), end_cap)
        secs = sorted({n for a, b in ch["aps"] for n in range(math.ceil(max(a, lo)), math.floor(min(b, hi - 1e-6)) + 1)
                       if n in avail})
        if not secs:
            secs = [n for n in range(math.ceil(lo), math.ceil(lo) + 10) if n in avail and n < hi]
        out += [{"t_s": n, "look_id": lk["look_id"]} for n in spread(secs, per)]
    return out


def pick_exemplars(cands: list[dict], verdicts: list[dict], per_look: int = EXEMPLARS_PER_LOOK) -> list[dict]:
    """Per-image model verdicts → per look, the ≤ per_look clearest images spaced ≥ EXEMPLAR_MIN_GAP_S apart; keep at least 1 per character (if any visible)."""
    by_k = {v.get("k"): v for v in verdicts}
    scored = []
    for k, c in enumerate(cands, 1):
        v = by_k.get(k) or {}
        box = parse_box(v.get("box_2d"))
        if not v.get("visible") or not box or box_area(box) < 0.004:
            continue
        clarity = float(v.get("clarity") or 0)
        scored.append({**c, "box": box, "clarity": round(clarity, 2), "score": clarity - (0.2 if v.get("others_similar") else 0)})
    out = []
    for look in dict.fromkeys(c["look_id"] for c in cands):
        chosen: list[dict] = []
        for s in sorted((s for s in scored if s["look_id"] == look), key=lambda s: (-s["score"], s["t_s"])):
            if s["score"] < EXEMPLAR_MIN_CLARITY or len(chosen) >= per_look:
                continue
            if all(abs(s["t_s"] - x["t_s"]) >= EXEMPLAR_MIN_GAP_S for x in chosen):
                chosen.append(s)
        out += chosen
    if not out and scored:
        out = [max(scored, key=lambda s: (s["score"], -s["t_s"]))]
    return [{k: x[k] for k in ("t_s", "look_id", "box", "clarity")} for x in sorted(out, key=lambda x: x["t_s"])]


def best_exemplar(exs: list[dict], t: float) -> dict | None:
    """Reference image for time t: prefer the clearest one from the look active at t (latest look start ≤ t)."""
    if not exs:
        return None
    started = [e["look_from"] for e in exs if e["look_from"] <= t]
    pool = [e for e in exs if e["look_from"] == max(started)] if started else exs
    return max(pool, key=lambda e: (e["clarity"], -abs(e["t_s"] - t)))


# ── images ──────────────────────────────────────────────────────────────

def thumb_path(ctx: Ctx, n: int):
    return ctx.wd / "thumbs" / f"t{n:05d}.jpg"


def crop_jpeg(path, box: list[int], pad: float = 0.15, max_side: int = CROP_MAX_SIDE) -> bytes:
    from PIL import Image
    im = Image.open(path).convert("RGB")
    W, H = im.size
    y0, x0, y1, x1 = box
    h, w = (y1 - y0) / 1000 * H, (x1 - x0) / 1000 * W
    L, T = max(0, int(x0 / 1000 * W - w * pad)), max(0, int(y0 / 1000 * H - h * pad))
    R, B = min(W, int(x1 / 1000 * W + w * pad)), min(H, int(y1 / 1000 * H + h * pad))
    im = im.crop((L, T, max(R, L + 8), max(B, T + 8)))
    im.thumbnail((max_side, max_side))
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=85)
    return buf.getvalue()


def circled_jpeg(path, box: list[int], width: int = 4) -> bytes:
    """Draw a red ellipse around the box (visual prompting)."""
    from PIL import Image, ImageDraw
    im = Image.open(path).convert("RGB")
    W, H = im.size
    y0, x0, y1, x1 = box
    pad_x, pad_y = (x1 - x0) / 1000 * W * 0.12, (y1 - y0) / 1000 * H * 0.12
    ImageDraw.Draw(im).ellipse((x0 / 1000 * W - pad_x, y0 / 1000 * H - pad_y, x1 / 1000 * W + pad_x, y1 / 1000 * H + pad_y),
                               outline=(255, 0, 0), width=width)
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=85)
    return buf.getvalue()


def verified(picks: list[dict], verdicts: list[dict], min_conf: float = 0.5) -> list[dict]:
    """Circle verification: keep only if the circle is mostly the target with no other character taking a large part; if none pass, keep the clearest one where the circled person is the target (confidence ≥0.8)."""
    by_k = {v.get("k"): v for v in verdicts}
    out, fallback = [], []
    for k, x in enumerate(picks, 1):
        v = by_k.get(k) or {}
        conf = float(v.get("confidence") or 0)
        if v.get("circled_is_target") and not v.get("other_inside") and conf >= min_conf:
            out.append(x)
        elif v.get("circled_is_target") and conf >= 0.8:
            fallback.append(x)
    return out or sorted(fallback, key=lambda x: -x["clarity"])[:1]


def image_part(data: bytes):
    from google.genai import types
    return types.Part.from_bytes(data=data, mime_type="image/jpeg")


def exemplar_parts(ctx: Ctx, cand: list[dict], ex_by_char: dict, t: float) -> tuple[list, list]:
    """Reference-crop parts for candidate characters (sets has_image on cand in place) and the exemplars used (part of the cache key)."""
    parts, used = [], []
    for c in cand:
        e = best_exemplar(ex_by_char.get(c["ref"], []), t)
        c["has_image"] = bool(e)
        if e:
            used.append([c["ref"], e["t_s"], e["box"]])
            parts += [gemini.text_part(f"Reference crop of {c['ref']} ({c['label']}):"),
                      image_part(crop_jpeg(thumb_path(ctx, e["t_s"]), e["box"]))]
    return parts, used


def group_exemplars(exemplars: list[dict]) -> dict[str, list]:
    out: dict[str, list] = {}
    for e in exemplars:
        out.setdefault(e["character_id"], []).append(e)
    return out


# ── calls (with per-part cache for resuming) ─────────────────────────────────

def parts_dir(ctx: Ctx):
    d = ctx.wd / "stages" / "vision_shots_parts"
    d.mkdir(parents=True, exist_ok=True)
    return d


RETRIES = 4


def with_retry(fn, *a, **kw):
    """Retry transient network failures (SSL EOF / disconnects / 5xx over a proxy) with exponential backoff; re-raise other errors."""
    import time
    for i in range(RETRIES + 1):
        try:
            return fn(*a, **kw)
        except Exception as e:
            transient = type(e).__name__ in ("ConnectError", "RemoteProtocolError", "ReadTimeout", "ReadError", "ConnectTimeout",
                                             "ServerError") or any(x in str(e) for x in ("503", "500", "502", "504", "429", "UNAVAILABLE"))
            if not transient or i == RETRIES:
                raise
            time.sleep(min(60, 4 * 2 ** i))


def cached_call(ctx: Ctx, c, part_file, key, ledger_name: str, est: float, parts: list, schema: dict,
                thinking: str | None = None) -> dict:
    if part_file.exists():
        old = read_json(part_file)
        if old.get("key") == key:
            return old
    ctx.ledger.guard(est)
    text, usage, lat, finish = with_retry(gemini.generate_json, c, gemini.VISION_MODEL, parts, schema, thinking=thinking)
    cost = cost_generate(gemini.VISION_MODEL, usage)
    ctx.ledger.log(ledger_name, "ran", lat, cost, usage=usage)
    r = {"key": key, "latency_s": lat, "finish": finish, "cost_usd": round(cost, 5), "doc": json.loads(text)}
    write_json(part_file, r)
    return r


def run_exemplars(ctx: Ctx, c, ros: list[dict], avail: set[int], story_end) -> list[dict]:
    pd = parts_dir(ctx)

    def one(ch):
        cands = exemplar_candidates(ch, avail, story_end)
        if not cands:
            return []
        looks = [{"from": mmss(lk["from_s"])[:5], "description": lk["description_en"]} for lk in ch["looks"]]
        others = [{"ref": o["ref"], "label": o["label"], "kind": o["kind"]} for o in ros if o["ref"] != ch["ref"]]
        prompt = P.exemplar_prompt({"ref": ch["ref"], "label": ch["label"], "kind": ch["kind"]}, looks, others)
        key = fingerprint([PROMPT_VERSION, gemini.VISION_MODEL, prompt, P.EXEMPLAR_SCHEMA, cands])
        parts = []
        for k, cd in enumerate(cands, 1):
            parts += [gemini.text_part(f"Candidate {k} (t={cd['t_s']}s):"), image_part(thumb_path(ctx, cd["t_s"]).read_bytes())]
        parts.append(gemini.text_part(prompt))
        r = cached_call(ctx, c, pd / f"ex_{ch['ref']}.json", key, f"vision_shots:ex_{ch['ref']}", 0.02, parts,
                        P.EXEMPLAR_SCHEMA, thinking="LOW")
        picks = pick_exemplars(cands, r["doc"].get("candidates", []))
        if picks:
            vprompt = P.verify_prompt({"label": ch["label"], "kind": ch["kind"]}, looks,
                                      [{"label": o["label"], "kind": o["kind"]} for o in others])
            vparts = []
            for k, x in enumerate(picks, 1):
                vparts += [gemini.text_part(f"Image {k}:"), image_part(circled_jpeg(thumb_path(ctx, x["t_s"]), x["box"]))]
            vkey = fingerprint([PROMPT_VERSION, gemini.VISION_MODEL, vprompt, P.VERIFY_SCHEMA, picks])
            vr = cached_call(ctx, c, pd / f"exv_{ch['ref']}.json", vkey, f"vision_shots:exv_{ch['ref']}", 0.01,
                             vparts + [gemini.text_part(vprompt)], P.VERIFY_SCHEMA, thinking="LOW")
            picks = verified(picks, vr["doc"].get("images", []))
        froms = {lk["look_id"]: lk["from_s"] for lk in ch["looks"]}
        return [{"character_id": ch["ref"], **x, "look_from": froms.get(x["look_id"], ch["first"])} for x in picks]

    with ThreadPoolExecutor(WORKERS) as ex:
        return [x for lst in ex.map(one, ros) for x in lst]


def segment_shots(ctx: Ctx, story_end, avail: set[int]) -> list[tuple[dict, list[dict]]]:
    """Merged shots per segment (shots after the end credits start are not described: the frames are the credits)."""
    shots = ctx.load("shots")["shots"]
    out = []
    for sg in ctx.load("segments")["segments"]:
        inside = [s for s in shots if sg["start_s"] - 1e-3 <= s["start_s"] < sg["end_s"] - 1e-3
                  and (story_end is None or s["start_s"] < story_end)]
        merged = merge_short(inside)
        for m in merged:
            m["keyframe_t"] = keyframe(m["start_s"], m["end_s"], avail)
        if merged:
            out.append((sg, merged))
    return out


def _txt(x) -> str | None:
    return (x or "").strip() or None


def clean_shot(item: dict, refs: set[str], obj_ids: set[str]) -> dict:
    chars = []
    for x in item.get("characters") or []:
        box = parse_box(x.get("box_2d"))
        chars.append({"ref": x.get("ref") if x.get("ref") in refs else "other",
                      "descriptor_en": _txt(x.get("descriptor_en")), "descriptor_zh": _txt(x.get("descriptor_zh")),
                      "position": x.get("position") if x.get("position") in POSITIONS else position_from_box(box),
                      "box": box, "action_en": _txt(x.get("action_en")), "action_zh": _txt(x.get("action_zh"))})
    objs = []
    for x in (item.get("objects") or [])[:3]:
        if not _txt(x.get("name_en")):
            continue
        box = parse_box(x.get("box_2d"))
        objs.append({"ref": x.get("ref") if x.get("ref") in obj_ids else None, "name_en": x["name_en"].strip(),
                     "name_zh": _txt(x.get("name_zh")),
                     "position": x.get("position") if x.get("position") in POSITIONS else position_from_box(box), "box": box})
    return {"caption_en": (item.get("caption_en") or "").strip(), "caption_zh": _txt(item.get("caption_zh")),
            "characters": chars, "objects": objs}


def segment_cast(ros: list[dict], s0: float, s1: float) -> tuple[list[dict], list[dict]]:
    """(likely present: characters whose appearances overlap the segment, other already-introduced characters). Characters not yet introduced stay out of the prompt."""
    cand = [{"ref": ch["ref"], "label": ch["label"], "kind": ch["kind"],
             "look": next((lk["description_en"][:160] for lk in reversed(ch["looks"]) if lk["from_s"] <= s1), None)}
            for ch in ros if ch["first"] <= s1 and any(a <= s1 + 1 and b >= s0 - 1 for a, b in ch["aps"])]
    cref = {x["ref"] for x in cand}
    others = [{"ref": ch["ref"], "label": ch["label"], "kind": ch["kind"]} for ch in ros
              if ch["first"] <= s1 and ch["ref"] not in cref]
    return cand, others


def run_shots(ctx: Ctx, c, asm, ros: list[dict], exemplars: list[dict], avail: set[int], story_end) -> list[dict]:
    rec = gemini_rec(ctx, c)
    pd = parts_dir(ctx)
    ex_by_char = group_exemplars(exemplars)

    def one(arg):
        sg, merged = arg
        s0, s1 = sg["start_s"], sg["end_s"]
        cand, others = segment_cast(ros, s0, s1)
        objs = [{"ref": o["object_id"], "name": o["name_en"]} for o in asm.pack["objects"] if o["first_seen_s"] <= s1]
        parts = [gemini.video_part(rec, SHOT_FPS, s0, s1)]
        ex_parts, used = exemplar_parts(ctx, cand, ex_by_char, (s0 + s1) / 2)
        parts += ex_parts
        table = []
        for k, m in enumerate(merged, 1):
            kf = m["keyframe_t"]
            table.append(f"Shot {k} [{mmss(m['start_s'])}–{mmss(m['end_s'])}]" + (f" keyframe t={kf}s" if kf is not None else ""))
            if kf is not None:
                parts += [gemini.text_part(f"Shot {k} keyframe (t={kf}s):"), image_part(thumb_path(ctx, kf).read_bytes())]
        prompt = P.shot_prompt(f"{mmss(s0)}–{mmss(s1)}", "\n".join(table), cand, others, objs)
        parts.append(gemini.text_part(prompt))
        key = fingerprint([PROMPT_VERSION, gemini.VISION_MODEL, SHOT_FPS, s0, s1, prompt, P.SHOT_SCHEMA, used])
        r = cached_call(ctx, c, pd / f"shots_{sg['segment_id']}.json", key, f"vision_shots:{sg['segment_id']}", 0.08,
                        parts, P.SHOT_SCHEMA)
        by_k = {x.get("k"): x for x in r["doc"].get("shots", [])}
        refs = {ch["ref"] for ch in ros if ch["first"] <= s1}
        out = []
        for k, m in enumerate(merged, 1):
            item = by_k.get(k)
            if item and (item.get("caption_en") or "").strip():
                out.append({"segment_id": sg["segment_id"], "k": k, **m, **clean_shot(item, refs, {o["ref"] for o in objs})})
        return out

    with ThreadPoolExecutor(WORKERS) as ex:
        return [x for lst in ex.map(one, segment_shots(ctx, story_end, avail)) for x in lst]


PROXY_WIDTH = 640


def gemini_rec(ctx: Ctx, c) -> dict:
    """Cloud file for video parts: reuse the full-film registration if still valid; otherwise upload a 640-wide proxy (same timeline, ~1/10 size).
    Gemini samples by fps and downscales to ~768 px, so the proxy is near-lossless for shot / speaker judgments, while re-uploading the full film over a proxied connection takes tens of minutes."""
    from ingest.stages.gemini_stages import registry_path
    reg = read_json(registry_path(ctx)) if registry_path(ctx).exists() else {}
    known = (reg.get("asset_file") or {}).get("gemini_file")
    if known:
        try:
            f = c.files.get(name=known)
            if gemini._usable(f, ctx.sha):
                return gemini.file_record(f, "reused")
        except Exception:
            pass
    return proxy_rec(ctx, c)


def proxy_rec(ctx: Ctx, c) -> dict:
    import subprocess
    from ingest.util import sha256_file
    mp4 = ctx.wd / f"proxy_{PROXY_WIDTH}.mp4"
    if not mp4.exists():
        tmp = mp4.with_suffix(".tmp.mp4")
        subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(ctx.asset), "-map", "0:v:0", "-map", "0:a:0?",
                        "-vf", f"scale={PROXY_WIDTH}:-2", "-c:v", "libx264", "-preset", "veryfast", "-crf", "28",
                        "-fps_mode", "passthrough", "-c:a", "aac", "-ac", "1", "-b:a", "64k", "-map_metadata", "-1", str(tmp)],
                       check=True)
        tmp.rename(mp4)
    side = ctx.wd / "proxy_file.json"
    sha = sha256_file(mp4)
    known = read_json(side)["gemini_file"] if side.exists() else None
    f, how = with_retry(gemini.ensure_file, c, mp4, sha, known, f"proxy_{ctx.sha[:12]}.mp4")
    rec = {**gemini.file_record(f, how), "sha256": sha}
    write_json(side, rec)
    return rec


def available_thumbs(ctx: Ctx) -> set[int]:
    return {r["t_s"] for r in ctx.load("thumbs")["thumbnails"]}


@stage("vision_shots", deps=DEPS, paid=True,
       inputs=lambda ctx: {"impl": PROMPT_VERSION, "model": gemini.VISION_MODEL, "fps": SHOT_FPS, "min_shot_s": MIN_SHOT_S,
                           "prompts": fingerprint([P.exemplar_prompt({"ref": "", "label": "", "kind": ""}, [], []),
                                                   P.EXEMPLAR_SCHEMA, P.shot_prompt("", "", [], [], []), P.SHOT_SCHEMA,
                                                   P.verify_prompt({"label": "", "kind": ""}, [], []), P.VERIFY_SCHEMA])})
def run_vision_shots(ctx: Ctx) -> dict:
    asm = base_assembler(ctx)
    ros = roster(asm)
    avail = available_thumbs(ctx)
    story_end = story_end_of(asm)
    c = gemini.client()
    exemplars = run_exemplars(ctx, c, ros, avail, story_end)
    shots = run_shots(ctx, c, asm, ros, exemplars, avail, story_end)
    return {"model": gemini.VISION_MODEL, "fps": SHOT_FPS, "prompt_version": PROMPT_VERSION,
            "exemplars": exemplars, "shots": shots}   # per-call costs already logged individually
