"""JSON schemas and prompt assembly for vision understanding / end credits.

Prompt texts are not in the code: `promptbook` loads them at runtime (Secret Manager / local directory). Changes alter the stage fingerprint (trigger a re-run).
"""
from __future__ import annotations

from ingest import promptbook as pb

TS = {"type": "string", "description": "MM:SS in the ORIGINAL video timeline"}
STR = {"type": "string"}
STRS = {"type": "array", "items": STR}
BOOL = {"type": "boolean"}


def _obj(props: dict, required: list[str] | None = None) -> dict:
    return {"type": "object", "properties": props, "required": required or list(props)}


LOOK = _obj({"from": {**TS, "description": "first MM:SS at which this appearance is visible"},
             "description": {"type": "string", "description": "ONLY attributes visible on screen at `from`; no plot, no later changes"},
             "reveal_at": {**TS, "description": "MM:SS by which every attribute in the description has been visible (>= from)"}})
FACT = _obj({"text": STR, "reveal_at": TS,
             "basis": {"type": "string", "enum": ["shown", "dialogue", "inferred"]},
             "quote": {"type": "string", "description": "exact supporting words if basis=dialogue, else empty"},
             "spoiler_critical": {**BOOL, "description": "true for twists, identity reveals, deaths, transformations"}})
CHARACTER = _obj({
    "canonical_name": {"type": "string", "description": "name the story gives (dialogue / on-screen story text), else a short lower-case descriptive label"},
    "display_label": {"type": "string", "description": "short lower-case descriptive label to use BEFORE the name is known, e.g. 'the young woman'"},
    "display_label_zh": {"type": "string", "description": "Chinese (Simplified) translation of display_label"},
    "name_zh": {"type": "string", "description": "Chinese (Simplified) rendering of the story name; empty for descriptive labels"},
    "name_source": {"type": "string", "enum": ["spoken_dialogue", "on_screen_story_text", "descriptive_label"]},
    "name_known_at": {**TS, "description": "first MM:SS the name is spoken or shown in the story; empty if never"},
    "aliases": {"type": "array", "items": _obj({"alias": STR, "known_at": TS})},
    "kind": {"type": "string", "enum": ["person", "creature", "robot", "other"]},
    "role": {"type": "string", "enum": ["protagonist", "supporting", "minor"]},
    "first_appears": TS,
    "looks": {"type": "array", "items": LOOK},
    "facts": {"type": "array", "items": FACT},
})
EVENT = _obj({"t": TS, "reveal_at": TS,
              "kind": {"type": "string", "enum": ["action", "dialogue", "reveal", "arrival", "departure", "other"]},
              "text": STR, "quote": {"type": "string", "description": "exact spoken words if kind=dialogue, else empty"},
              "present": {**STRS, "description": "canonical_name of characters on screen"},
              "mentioned": {**STRS, "description": "canonical_name of characters referred to but not on screen"},
              "spoiler_critical": BOOL})
OBJECT = _obj({"name": STR, "description": STR, "first_seen": TS, "reveal_at": TS,
               "seen_at": {"type": "array", "items": TS}, "spoiler_critical": BOOL})

FULL_SCHEMA = _obj({
    "story_start": {**TS, "description": "MM:SS where the story proper begins (after opening title cards / logos)"},
    "story_end": {**TS, "description": "MM:SS where the story ends and end credits begin; empty if none"},
    "scenes": {"type": "array", "items": _obj({
        "start": TS, "end": TS,
        "kind": {"type": "string", "enum": ["story", "opening_titles", "end_credits", "flashback", "montage"]},
        "location": STR,
        "summary_en": {"type": "string", "description": "1-3 sentences; only what a first-time viewer knows by the END of this scene"},
        "characters": STRS, "objects": STRS}, ["start", "end", "kind", "location", "summary_en", "characters", "objects"])},
    "characters": {"type": "array", "items": CHARACTER},
    "identity_links": {"type": "array", "items": _obj({
        "a": {"type": "string", "description": "entry as the viewer knows it before the reveal"},
        "b": {"type": "string", "description": "its true identity"},
        "relation": {"type": "string", "description": "e.g. same_individual, parent_of"},
        "reveal_at": {**TS, "description": "the moment the reveal is CONFIRMED (line of dialogue or decisive shot), not the first hint"},
        "text": STR,
        "clue_words": {**STRS, "description": "words a viewer would use for the visual/verbal clue that gives the reveal away"}})},
    "appearances": {"type": "array", "items": _obj({"character": STR, "start": TS, "end": TS})},
    "objects": {"type": "array", "items": OBJECT},
    "events": {"type": "array", "items": EVENT},
})

SEG_SCHEMA = _obj({
    "new_characters": {"type": "array", "items": _obj({
        "label": {"type": "string", "description": "short lower-case descriptive label"},
        "label_zh": STR,
        "kind": {"type": "string", "enum": ["person", "creature", "robot", "other"]},
        "first_appears": TS, "look": LOOK, "facts": {"type": "array", "items": FACT}})},
    "appearances": {"type": "array", "items": _obj({"ref": {"type": "string", "description": "roster ref like R3, or new:<label>"},
                                                     "start": TS, "end": TS})},
    "events": {"type": "array", "items": _obj({
        "t": TS, "reveal_at": TS,
        "kind": {"type": "string", "enum": ["action", "dialogue", "reveal", "arrival", "departure", "other"]},
        "text": STR, "quote": STR, "present": STRS, "mentioned": STRS, "spoiler_critical": BOOL})},
    "objects": {"type": "array", "items": OBJECT},
})


def seg_rules(start: str, end: str, roster: list[dict]) -> str:
    lines = "\n".join(f"- {r['ref']}: {r['label']} ({r['kind']}; first seen {r['first_appears']}) — {r['look']}" for r in roster)
    return pb.text("common_rules") + pb.render("seg_task", start=start, end=end, roster=lines)


CREDITS_SCHEMA = _obj({"people": {"type": "array", "items": _obj({
    "person_name": STR,
    "credit_role": {"type": "string", "description": "role text next to the name (job title, or a CHARACTER name for cast)"},
    "is_cast": {"type": "boolean", "description": "true if credited as performer / voice of a character"},
    "is_real_person": {"type": "boolean", "description": "false for companies, studios, funds, software, places"},
    "t": TS})}})


# ── v2: names from the catalog synopsis, lyrics spans, characters present with objects, trailer rules; already-ingested assets such as Sintel stay on v1 (title config vision_prompt) ──

CHARACTER_V2 = {**CHARACTER, "properties": {**CHARACTER["properties"], "name_source": {
    "type": "string", "enum": ["spoken_dialogue", "on_screen_story_text", "catalog_synopsis", "descriptive_label"]}}}
OBJECT_V2 = _obj({**OBJECT["properties"],
                  "present": {**STRS, "description": "canonical_name of characters on screen when the object is first seen"}})
EVENT_V2 = _obj({**EVENT["properties"],
                 "present": {**STRS, "description": "canonical_name of EVERY character visible on screen at t"}})
LYRICS_SPANS = {"type": "array", "description": "intervals in which sung song lyrics (not dialogue) are heard",
                "items": _obj({"start": TS, "end": TS})}
FULL_SCHEMA_V2 = _obj({**FULL_SCHEMA["properties"],
                       "scenes": {"type": "array", "items": _obj({
                           **FULL_SCHEMA["properties"]["scenes"]["items"]["properties"],
                           "characters": {**STRS, "description": "canonical_name of EVERY character visible in the scene"}},
                           ["start", "end", "kind", "location", "summary_en", "characters", "objects"])},
                       "characters": {"type": "array", "items": CHARACTER_V2},
                       "objects": {"type": "array", "items": OBJECT_V2},
                       "events": {"type": "array", "items": EVENT_V2},
                       "lyrics_spans": LYRICS_SPANS})
SEG_SCHEMA_V2 = _obj({**SEG_SCHEMA["properties"],
                      "events": {"type": "array", "items": _obj({
                          **SEG_SCHEMA["properties"]["events"]["items"]["properties"],
                          "present": {**STRS, "description": "refs of EVERY character visible on screen at t"}})},
                      "objects": {"type": "array", "items": OBJECT_V2}})


def public_names_rule(synopsis: str) -> str:
    return pb.render("public_names_rule", synopsis=synopsis)


def full_rules_v2(synopsis: str, trailer: bool, lang: str | None) -> str:
    lang_rule = "" if not lang or lang.startswith("en") else pb.render("lang_rule", lang=lang)
    trailer_rule = pb.text("trailer_rule") if trailer else ""
    return (pb.text("common_rules") + public_names_rule(synopsis) + pb.text("present_rule") + trailer_rule + lang_rule
            + pb.text("full_rules_tail_v2"))


def seg_rules_v2(start: str, end: str, roster: list[dict], trailer: bool) -> str:
    common = pb.text("common_rules")
    return seg_rules(start, end, roster).replace(common, common + pb.text("present_rule") + (pb.text("trailer_rule") if trailer else ""))


# ── identify: asset identification (checked against the catalog entry) + real-person names on on-screen text cards ──

IDENTIFY_SCHEMA = _obj({
    "asset_form": {"type": "string", "enum": ["trailer", "teaser", "episode_trailer", "short_film", "feature_excerpt",
                                               "episode", "other"],
                   "description": "episode_trailer = trailer for a TV / streaming SERIES (season, episodes)"},
    "title_on_screen": {"type": "string", "description": "the main work title exactly as shown in the title card / logo; empty if none"},
    "series_marker": {"type": "string", "description": "verbatim on-screen or spoken text indicating a TV / streaming series, "
                                                       "season or episode (e.g. 'Season 2', 'A Netflix Series', 'all episodes'); empty if none"},
    "matches_catalog": {"type": "boolean", "description": "true if the on-screen evidence agrees with the catalog entry "
                                                          "(same work, same form); false if it is a different work or a series"},
    "mismatch_reason": {"type": "string", "description": "evidence for a mismatch (verbatim text, timestamps); empty if matches"},
    "has_speech": BOOL,
    "dialogue_language": {"type": "string", "description": "BCP-47 code of the main spoken language; empty if no speech"},
    "license_notice": {"type": "string", "description": "verbatim licence text (e.g. Creative Commons) if shown; else empty"},
    "text_cards": {"type": "array", "items": _obj({
        "t": TS, "text": STR,
        "kind": {"type": "string", "enum": ["studio_logo", "rating_card", "title", "tagline", "release_date", "cast_card",
                                            "director_credit", "billing_block", "license", "story_text", "other"]}})},
    "people": {"type": "array", "items": _obj({
        "person_name": STR,
        "credit_role": {"type": "string", "description": "role text next to the name, or how the name is presented"},
        "is_cast": {"type": "boolean", "description": "true if credited as performer / voice"},
        "is_real_person": {"type": "boolean", "description": "false for companies, studios, software, places"},
        "seen_or_heard": {"type": "string", "enum": ["on_screen", "spoken"]},
        "t": TS})},
})


def identify_prompt(item: dict) -> str:
    return pb.render("identify", title=item["title"], title_zh=item.get("title_zh", ""), year=item.get("year"),
                     type=item.get("type", "movie"), synopsis=item.get("synopsis_full", ""))


_TEXTS = {"COMMON_RULES": lambda: pb.text("common_rules"),
          "FULL_RULES": lambda: pb.text("common_rules") + pb.text("full_rules_tail"),
          "CREDITS_PROMPT": lambda: pb.text("credits"),
          "PRESENT_RULE": lambda: pb.text("present_rule"),
          "TRAILER_RULE": lambda: pb.text("trailer_rule")}


def __getattr__(name: str) -> str:
    if name in _TEXTS:
        return _TEXTS[name]()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
