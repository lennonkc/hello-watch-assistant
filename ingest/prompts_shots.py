"""v1.1 vision prompts and response schemas: in-film character bank frame picking (AutoAD-II exemplar), shot-level captions (Shot-by-Shot), speaker attribution (HAVEN).

Prompts give the model only character ids and labels (display_label), never names: name visibility is substituted by the assembler by time (spec §6).
Prompt texts are loaded at runtime by `promptbook` (Secret Manager / local directory); this module only assembles lists and schemas.
"""
from __future__ import annotations

from ingest import promptbook as pb

BOX = {"type": "array", "items": {"type": "integer"}, "minItems": 4, "maxItems": 4,
       "description": "[ymin, xmin, ymax, xmax] normalized 0-1000 on the referenced image"}
POSITION = {"type": "string", "enum": ["left", "center", "right", "background"]}


# ── Character bank: pick clear frames of one character from candidate thumbnails, with boxes ──

EXEMPLAR_SCHEMA = {
    "type": "object",
    "properties": {"candidates": {"type": "array", "items": {
        "type": "object",
        "properties": {
            "k": {"type": "integer", "description": "candidate number"},
            "visible": {"type": "boolean", "description": "the target character is in this image"},
            "clarity": {"type": "number", "description": "0-1: how clearly the target can be recognized (size, face/body visible, not occluded, not blurred)"},
            "box_2d": BOX,
            "others_similar": {"type": "boolean", "description": "another character that could be confused with the target is also in the image"},
        },
        "required": ["k", "visible", "clarity"]}}},
    "required": ["candidates"],
}


def exemplar_prompt(target: dict, looks: list[dict], others: list[dict]) -> str:
    lk = "\n".join(f"- look L{i} (from {x['from']}): {x['description']}" for i, x in enumerate(looks)) or "- (no look notes)"
    ot = "\n".join(f"- {o['ref']}: {o['label']} ({o['kind']})" for o in others) or "- (none)"
    return pb.render("exemplar", ref=target["ref"], label=target["label"], kind=target["kind"], looks=lk, others=ot,
                     no_people=pb.text("no_people"))


# ── Shot-level captions: one call = several shots within one segment ──

SHOT_SCHEMA = {
    "type": "object",
    "properties": {"shots": {"type": "array", "items": {
        "type": "object",
        "properties": {
            "k": {"type": "integer", "description": "shot number from the shot table"},
            "caption_en": {"type": "string", "description": "1-2 sentences: what is visible / happens in this shot. Characters only as {id} tokens"},
            "caption_zh": {"type": "string", "description": "the same in Simplified Chinese, same {id} tokens"},
            "characters": {"type": "array", "items": {
                "type": "object",
                "properties": {
                    "ref": {"type": "string", "description": "roster id like c03, or 'other' for anyone not on the roster"},
                    "descriptor_en": {"type": "string", "description": "short visual descriptor, e.g. 'girl in a brown hooded coat' (no names)"},
                    "descriptor_zh": {"type": "string"},
                    "position": POSITION,
                    "box_2d": {**BOX, "description": "[ymin, xmin, ymax, xmax] 0-1000 on THIS shot's keyframe image; omit if not in the keyframe"},
                    "action_en": {"type": "string", "description": "what this character does in the shot (no names; use {id} tokens)"},
                    "action_zh": {"type": "string"},
                },
                "required": ["ref", "descriptor_en", "position"]}},
            "objects": {"type": "array", "items": {
                "type": "object",
                "properties": {
                    "ref": {"type": "string", "description": "known object id like o02, or 'new'"},
                    "name_en": {"type": "string"},
                    "name_zh": {"type": "string"},
                    "position": POSITION,
                    "box_2d": {**BOX, "description": "on THIS shot's keyframe image; omit if not in the keyframe"},
                },
                "required": ["ref", "name_en", "position"]}},
        },
        "required": ["k", "caption_en", "caption_zh", "characters", "objects"]}}},
    "required": ["shots"],
}


def shot_prompt(seg_range: str, shot_table: str, cand: list[dict], others: list[dict], objects: list[dict]) -> str:
    cl = "\n".join(f"- {c['ref']}: {c['label']} ({c['kind']}){' — ' + c['look'] if c.get('look') else ''}"
                   f"{' [reference image above]' if c.get('has_image') else ''}" for c in cand) or "- (none)"
    ol = "\n".join(f"- {o['ref']}: {o['label']} ({o['kind']})" for o in others) or "- (none)"
    ob = "\n".join(f"- {o['ref']}: {o['name']}" for o in objects) or "- (none)"
    return pb.render("shot", seg_range=seg_range, characters=cl, others=ol, objects=ob, shot_table=shot_table,
                     no_people=pb.text("no_people"))


# ── Speaker attribution ──────────────────────────────────────────────

SPEAKER_SCHEMA = {
    "type": "object",
    "properties": {"lines": {"type": "array", "items": {
        "type": "object",
        "properties": {
            "line": {"type": "string", "description": "line id like L07"},
            "speaker": {"type": "string", "description": "roster id like c03, 'offscreen' (a voice whose speaker is not visible and cannot be inferred), or 'unknown'"},
            "confidence": {"type": "number", "description": "0-1"},
            "evidence": {"type": "string", "enum": ["lips_moving", "only_candidate_on_screen", "voice_match", "addressed_or_context", "guess"]},
        },
        "required": ["line", "speaker", "confidence", "evidence"]}}},
    "required": ["lines"],
}


def speaker_prompt(windows: str, cand: list[dict], lines: str) -> str:
    cl = "\n".join(f"- {c['ref']}: {c['label']} ({c['kind']}){' [reference image above]' if c.get('has_image') else ''}"
                   for c in cand) or "- (none)"
    return pb.render("speaker", characters=cl, lines=lines, no_people=pb.text("no_people"))


# ── Character bank verification: circled-subject check (AutoAD-Zero visual prompting) ──

VERIFY_SCHEMA = {
    "type": "object",
    "properties": {"images": {"type": "array", "items": {
        "type": "object",
        "properties": {
            "k": {"type": "integer"},
            "circled_is_target": {"type": "boolean", "description": "the subject inside the red circle is mainly the TARGET"},
            "other_inside": {"type": "boolean", "description": "a different character takes up a large part of the circle"},
            "confidence": {"type": "number"},
        },
        "required": ["k", "circled_is_target", "other_inside", "confidence"]}}},
    "required": ["images"],
}


def verify_prompt(target: dict, looks: list[dict], others: list[dict]) -> str:
    lk = "\n".join(f"- (from {x['from']}): {x['description']}" for x in looks) or "- (no look notes)"
    ot = "\n".join(f"- {o['label']} ({o['kind']})" for o in others) or "- (none)"
    return pb.render("verify", label=target["label"], kind=target["kind"], looks=lk, others=ot, no_people=pb.text("no_people"))


def __getattr__(name: str) -> str:
    if name == "NO_PEOPLE":
        return pb.text("no_people")
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
