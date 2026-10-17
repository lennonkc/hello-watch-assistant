"""Paid stages: upload (reusing registered files), transcription (alignment reference), full-film vision, per-segment recall top-up, end-credits extraction."""
from __future__ import annotations

import json
import subprocess

from ingest import gemini, prompts
from ingest.ledger import cost_generate, cost_interaction
from ingest.stages import Ctx, stage
from ingest.util import fingerprint, mmss, parse_ts, read_json, sha256_file, write_json

FULL_FPS = 1.0
SEG_FPS = 2.0
CREDITS_FPS = 2.0


def registry_path(ctx: Ctx):
    return ctx.wd / "registry.json"


def ensure_asset_file(ctx: Ctx, c) -> dict:
    """spec §4 sidecar `registry.json`: full-film registration; re-upload when expired / missing. The enterprise backend uses the GCS asset directly, bypassing the Files API."""
    reg = read_json(registry_path(ctx)) if registry_path(ctx).exists() else {"asset_sha256": ctx.sha, "segments": []}
    if gemini.backend() == "enterprise":
        rec = gemini.gcs_asset_record(ctx.sha)
        reg["asset_file"] = {**(reg.get("asset_file") or {}), "storage_uri": rec["uri"]}
        write_json(registry_path(ctx), reg)
        return rec
    known = (reg.get("asset_file") or {}).get("gemini_file")
    f, how = gemini.ensure_file(c, ctx.asset, ctx.sha, known, f"asset_{ctx.sha[:12]}.mp4")
    rec = gemini.file_record(f, how)
    reg["asset_file"] = {"storage_uri": None, **{k: rec[k] for k in ("gemini_file", "expires_at")}}
    write_json(registry_path(ctx), reg)
    return rec


def ensure_audio_file(ctx: Ctx, c) -> dict:
    flac = ctx.wd / "audio_16k_mono.flac"
    if not flac.exists():
        subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(ctx.asset), "-vn", "-ac", "1",
                        "-ar", "16000", "-map_metadata", "-1", "-fflags", "+bitexact", str(flac)], check=True)
    sha = sha256_file(flac)
    side = ctx.wd / "audio_file.json"
    known = read_json(side)["gemini_file"] if side.exists() else None
    f, how = gemini.ensure_file(c, flac, sha, known, f"audio_{ctx.sha[:12]}.flac")
    rec = {**gemini.file_record(f, how), "sha256": sha}
    write_json(side, rec)
    return rec


@stage("upload", deps=("register",), paid=True, inputs=lambda ctx: {"sha": ctx.sha})
def run_upload(ctx: Ctx) -> dict:
    c = gemini.client()
    a = ensure_asset_file(ctx, c)
    out = {"asset_file": {k: a[k] for k in ("gemini_file", "expires_at", "how")}}
    if ctx.load("register")["audio_tracks"] > 0:
        b = ensure_audio_file(ctx, gemini.client("developer"))      # transcription always uses developer
        out["audio_file"] = {k: b[k] for k in ("gemini_file", "expires_at", "how", "sha256")}
    return out


def _has_audio(ctx: Ctx) -> bool:
    return ctx.load("register")["audio_tracks"] > 0


def _lang(ctx: Ctx) -> str:
    return ctx.title.get("dialogue_lang", "en")


def _transcribe(ctx: Ctx, granularity: list[str] | None) -> dict:
    c = gemini.client("developer")          # enterprise lacks 3.5-transcribe on the Interactions API (see docs/cloud-deploy.md)
    audio = ensure_audio_file(ctx, c)
    mode = {"type": "verbatim", **({"timestamp_granularities": granularity} if granularity else {})}
    cfg = {"transcription_config": {"mode": mode, "language_codes": [_lang(ctx)]}}
    inter = c.interactions.create(model=gemini.TRANSCRIBE_MODEL,
                                  input=[{"type": "audio", "uri": audio["uri"], "mime_type": audio["mime_type"]}],
                                  generation_config=cfg)
    raw = inter.model_dump(mode="json", exclude_none=True)
    usage = raw.get("usage", {})
    return {"model": gemini.TRANSCRIBE_MODEL, "raw": {"steps": raw.get("steps", [])}, "_usage": usage,
            "_cost_usd": cost_interaction(gemini.TRANSCRIBE_MODEL, usage)}


@stage("transcribe", deps=("register",), paid=True,
       inputs=lambda ctx: {"model": gemini.TRANSCRIBE_MODEL, "lang": ctx.title.get("dialogue_lang", "en"), "mode": "word"})
def run_transcribe(ctx: Ctx) -> dict:
    if not _has_audio(ctx):
        return {"model": gemini.TRANSCRIBE_MODEL, "raw": {"steps": []}, "skipped": "no audio track"}
    ctx.ledger.guard(0.1)
    return _transcribe(ctx, ["word"])


def needs_plain(ctx: Ctx) -> bool:
    """Run the plain pass only when there is no official track in that language (early experiments: the word-level pass misses single-word shouts / whispers, the plain pass recovers them)."""
    lang = _lang(ctx)
    return _has_audio(ctx) and not any(t["lang"] == lang and t["track"].endswith(".official")
                                       for t in ctx.title.get("subtitles", []))


@stage("transcribe_plain", deps=("register",), paid=True,
       inputs=lambda ctx: {"model": gemini.TRANSCRIBE_MODEL, "lang": ctx.title.get("dialogue_lang", "en"), "mode": "plain",
                           "needed": needs_plain(ctx)})
def run_transcribe_plain(ctx: Ctx) -> dict:
    if not needs_plain(ctx):
        return {"model": gemini.TRANSCRIBE_MODEL, "raw": {"steps": []}, "skipped": "official track or no audio"}
    ctx.ledger.guard(0.1)
    return _transcribe(ctx, None)


def _parse(text: str) -> dict:
    return json.loads(text)


def prompt_version(ctx: Ctx) -> str:
    return ctx.title.get("vision_prompt", "v2")


def is_trailer(ctx: Ctx) -> bool:
    return ctx.title.get("asset_kind") == "trailer"


def full_prompt(ctx: Ctx) -> tuple[str, dict]:
    if prompt_version(ctx) == "v1":
        return prompts.FULL_RULES, prompts.FULL_SCHEMA
    return (prompts.full_rules_v2(ctx.catalog_item.get("synopsis_full", ""), is_trailer(ctx), _lang(ctx)),
            prompts.FULL_SCHEMA_V2)


def _full_inputs(ctx: Ctx) -> dict:
    return {"model": gemini.VISION_MODEL, "fps": FULL_FPS, "prompt": fingerprint(list(full_prompt(ctx)))}


@stage("vision_full", deps=("register",), paid=True, inputs=_full_inputs)
def run_vision_full(ctx: Ctx) -> dict:
    ctx.ledger.guard(0.3)
    c = gemini.client()
    rec = ensure_asset_file(ctx, c)
    rules, schema = full_prompt(ctx)
    text, usage, lat, finish = gemini.generate_json(c, gemini.VISION_MODEL, [gemini.video_part(rec, FULL_FPS),
                                                    gemini.text_part(rules)], schema)
    return {"model": gemini.VISION_MODEL, "fps": FULL_FPS, "latency_s": lat, "finish": finish,
            "prompt_sha256": fingerprint([rules, schema]), "prompt_version": prompt_version(ctx),
            "doc": _parse(text), "_usage": usage, "_cost_usd": cost_generate(gemini.VISION_MODEL, usage)}


def roster_from_full(doc: dict) -> list[dict]:
    chars = sorted(doc["characters"], key=lambda ch: parse_ts(ch["first_appears"]) or 0)
    out = []
    for i, ch in enumerate(chars, 1):
        look = (ch.get("looks") or [{}])[0].get("description", "")
        out.append({"ref": f"R{i}", "canonical_name": ch["canonical_name"], "label": ch.get("display_label") or ch["canonical_name"],
                    "kind": ch["kind"], "first_appears": ch["first_appears"], "look": look[:160]})
    return out


def seg_prompt(ctx: Ctx, start: str, end: str, roster: list[dict]) -> tuple[str, dict]:
    if prompt_version(ctx) == "v1":
        return prompts.seg_rules(start, end, roster), prompts.SEG_SCHEMA
    return prompts.seg_rules_v2(start, end, roster, is_trailer(ctx)), prompts.SEG_SCHEMA_V2


def _seg_inputs(ctx: Ctx) -> dict:
    if prompt_version(ctx) == "v1":
        return {"model": gemini.VISION_MODEL, "fps": SEG_FPS,
                "prompt": fingerprint([prompts.COMMON_RULES, prompts.seg_rules("A", "B", []), prompts.SEG_SCHEMA])}
    return {"model": gemini.VISION_MODEL, "fps": SEG_FPS, "prompt": fingerprint(list(seg_prompt(ctx, "A", "B", [])))}


@stage("vision_seg", deps=("segments", "vision_full"), paid=True, inputs=_seg_inputs)
def run_vision_seg(ctx: Ctx) -> dict:
    segs = ctx.load("segments")["segments"]
    roster = roster_from_full(ctx.load("vision_full")["doc"])
    c = gemini.client()
    rec = ensure_asset_file(ctx, c)
    part_dir = ctx.wd / "stages" / "vision_seg_parts"
    part_dir.mkdir(parents=True, exist_ok=True)
    results = []
    for sg in segs:
        pf = part_dir / f"{sg['segment_id']}.json"
        _, schema = seg_prompt(ctx, "A", "B", [])
        key = fingerprint([sg, roster, schema, prompts.COMMON_RULES] if prompt_version(ctx) == "v1"
                          else [sg, roster, seg_prompt(ctx, "A", "B", [])])
        if pf.exists() and read_json(pf).get("key") == key:
            results.append(read_json(pf))
            continue
        ctx.ledger.guard(0.06)
        prompt, schema = seg_prompt(ctx, mmss(sg["start_s"])[:5], mmss(sg["end_s"])[:5], roster)
        text, usage, lat, finish = gemini.generate_json(
            c, gemini.VISION_MODEL, [gemini.video_part(rec, SEG_FPS, sg["start_s"], sg["end_s"]), gemini.text_part(prompt)],
            schema)
        part_cost = cost_generate(gemini.VISION_MODEL, usage)
        ctx.ledger.log(f"vision_seg:{sg['segment_id']}", "ran", lat, part_cost, usage=usage)
        r = {"key": key, "segment_id": sg["segment_id"], "start_s": sg["start_s"], "end_s": sg["end_s"],
             "latency_s": lat, "finish": finish, "doc": _parse(text)}
        write_json(pf, r)
        results.append(r)
    return {"model": gemini.VISION_MODEL, "fps": SEG_FPS, "roster": roster, "segments": results}  # per-segment costs already logged individually


@stage("credits", deps=("register", "vision_full"), paid=True,
       inputs=lambda ctx: {"model": gemini.VISION_MODEL, "fps": CREDITS_FPS,
                           "prompt": fingerprint([prompts.CREDITS_PROMPT, prompts.CREDITS_SCHEMA])})
def run_credits(ctx: Ctx) -> dict:
    dur = ctx.load("register")["duration_s"]
    story_end = parse_ts(ctx.load("vision_full")["doc"].get("story_end"))
    if story_end is None:
        return {"people": [], "range": None, "note": "no end credits detected"}
    ctx.ledger.guard(0.05)
    c = gemini.client()
    rec = ensure_asset_file(ctx, c)
    start = max(0.0, story_end - 2)
    text, usage, lat, finish = gemini.generate_json(
        c, gemini.VISION_MODEL, [gemini.video_part(rec, CREDITS_FPS, start, dur), gemini.text_part(prompts.CREDITS_PROMPT)],
        prompts.CREDITS_SCHEMA, thinking="LOW")
    return {"model": gemini.VISION_MODEL, "range": [start, dur], "latency_s": lat, "finish": finish,
            "people": _parse(text)["people"], "_usage": usage, "_cost_usd": cost_generate(gemini.VISION_MODEL, usage)}


IDENTIFY_FPS_TRAILER = 2.0   # trailer cast cards often stay on screen only 1–2 s
IDENTIFY_FPS_FILM = 1.0


def identify_fps(ctx: Ctx) -> float:
    return IDENTIFY_FPS_TRAILER if ctx.load("register")["duration_s"] <= 300 else IDENTIFY_FPS_FILM


@stage("identify", deps=("register",), paid=True,
       inputs=lambda ctx: {"model": gemini.VISION_MODEL, "fps": identify_fps(ctx),
                           "prompt": fingerprint([prompts.identify_prompt(ctx.catalog_item), prompts.IDENTIFY_SCHEMA])})
def run_identify(ctx: Ctx) -> dict:
    """Asset identification: on-screen title cards, title / episode markers, match against the catalog entry, dialogue language; real-person names go to the refusal list."""
    ctx.ledger.guard(0.1)
    c = gemini.client()
    rec = ensure_asset_file(ctx, c)
    fps = identify_fps(ctx)
    text, usage, lat, finish = gemini.generate_json(
        c, gemini.VISION_MODEL, [gemini.video_part(rec, fps), gemini.text_part(prompts.identify_prompt(ctx.catalog_item))],
        prompts.IDENTIFY_SCHEMA)
    return {"model": gemini.VISION_MODEL, "fps": fps, "latency_s": lat, "finish": finish, "doc": _parse(text),
            "_usage": usage, "_cost_usd": cost_generate(gemini.VISION_MODEL, usage)}
