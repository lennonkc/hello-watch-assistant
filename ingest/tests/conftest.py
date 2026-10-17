"""Synthetic minimal knowledge pack: everything human-reviewed, passes all §10 checks (review_complete = 1). Each test builds a counterexample on a copy."""
import copy

import pytest

from ingest import packdb, promptbook
from ingest.derive import MARGINS, derive_canaries

SHA = "ab" * 32
HUMAN = "reviewer-1"


def _log(i, item_type, item_id, sc=0):
    return {"log_id": i, "item_type": item_type, "item_id": item_id, "field": None, "old_value": None, "new_value": None,
            "decision": "approve", "spoiler_critical": sc, "reviewer": HUMAN, "ts": "2026-10-14T00:00:00Z",
            "seconds_spent": 5.0, "why": None}


def mini_pack() -> dict:
    R = "reviewed"
    p = packdb.empty_pack()
    p["pack_meta"] = {**{k: str(v) for k, v in MARGINS.items()}, "schema_version": "1", "pack_version": "1",
                      "asset_sha256": SHA, "built_at": "2026-10-14T00:00:00Z", "generator": "test", "review_complete": "0",
                      "fixture": "0", "distribution": "master", "wordlists_version": "1"}
    p["asset"] = {"asset_sha256": SHA, "content_id": "mini", "title": "Mini", "asset_kind": "short", "duration_s": 100.0,
                  "video_fps": 24.0, "width": 640, "height": 360, "audio_tracks": 1, "has_dialogue": 1, "dialogue_lang": "en",
                  "license": "CC-BY-3.0", "derivatives_ok": 1, "story_start_s": 0.0, "story_end_s": 90.0, "credits_start_s": 90.0}
    p["title_info"] = [{"field": "title", "text_en": "Mini", "text_zh": "迷你", "source": "catalog"}]
    p["shots"] = [{"shot_id": "sh000", "start_s": 0.0, "end_s": 50.0}, {"shot_id": "sh001", "start_s": 50.0, "end_s": 100.0}]
    p["segments"] = [{"segment_id": "seg00", "start_s": 0.0, "end_s": 100.0, "file_sha256": "00" * 32}]
    p["subtitle_tracks"] = [{"track": "en.official", "lang": "en", "source": "official", "priority": 0, "source_url": None,
                             "license": None, "sha256": None, "offset_s": 0.0, "drift_s_per_hour": 0.0, "alignment_note": None}]
    p["cues"] = [{"cue_id": "en.official:001", "track": "en.official", "start_s": 10.0, "end_s": 12.0, "text": "Hello, Bob.",
                  "kind": "dialogue", "speaker_raw": None},
                 {"cue_id": "en.official:002", "track": "en.official", "start_s": 60.0, "end_s": 62.0, "text": "Bob?",
                  "kind": "dialogue", "speaker_raw": None}]
    ch = lambda cid, name, zh, lab, labzh, known, public, src, kind, first: {  # noqa: E731
        "character_id": cid, "canonical_name": name, "name_zh": zh, "display_label_pre_name": lab,
        "display_label_pre_name_zh": labzh, "name_known_at_s": known, "name_public": public, "name_source": src,
        "kind": kind, "role": "supporting", "first_appears_s": first, "review_state": R, "source": "test"}
    p["characters"] = [ch("c01", "Alice", "爱丽丝", "the girl", "女孩", 1.0, 1, "title", "person", 1.0),
                       ch("c02", "Bob", "鲍勃", "the boy", "男孩", 10.0, 0, "dialogue", "person", 5.0),
                       ch("c03", "the wolf", None, "the wolf", "狼", None, 0, None, "creature", 55.0)]
    p["character_looks"] = [
        {"look_id": "l001", "character_id": "c01", "from_s": 1.0, "to_s": None, "description_en": "A girl in a red coat.",
         "description_zh": None, "reveal_at_s": 2.0, "spoiler_critical": 0, "review_state": R},
        {"look_id": "l002", "character_id": "c02", "from_s": 5.0, "to_s": None, "description_en": "A boy with a cap.",
         "description_zh": None, "reveal_at_s": 6.0, "spoiler_critical": 0, "review_state": R},
        {"look_id": "l003", "character_id": "c03", "from_s": 55.0, "to_s": None, "description_en": "A grey wolf.",
         "description_zh": None, "reveal_at_s": 56.0, "spoiler_critical": 0, "review_state": R}]
    p["character_facts"] = [{"fact_id": "f001", "character_id": "c02", "text_en": "He is greeted by name.", "text_zh": None,
                             "reveal_at_s": 12.0, "basis": "dialogue", "evidence_t_s": None, "spoiler_critical": 0,
                             "review_state": R, "source": "test"}]
    p["identity_links"] = [{"link_id": "k01", "a_character_id": "c03", "b_character_id": "c02", "relation": "same_individual",
                            "reveal_at_s": 60.5, "text_en": "The wolf is Bob.", "text_zh": None, "review_state": R}]
    p["appearances"] = [{"character_id": c, "start_s": a, "end_s": b, "source": "test", "review_state": R}
                        for c, a, b in (("c01", 1.0, 90.0), ("c02", 5.0, 30.0), ("c03", 55.0, 80.0))]
    p["scenes"] = [
        {"scene_id": "s01", "start_s": 0.0, "end_s": 50.0, "start_s_model": 0.0, "end_s_model": 50.0, "snapped": 1,
         "reveal_at_s": 50.0, "location_en": "A meadow", "summary_en": "Alice meets Bob.", "summary_zh": None,
         "review_state": R, "source": "test"},
        {"scene_id": "s02", "start_s": 50.0, "end_s": 100.0, "start_s_model": 50.0, "end_s_model": 100.0, "snapped": 1,
         "reveal_at_s": 100.0, "location_en": "A forest", "summary_en": "A wolf appears; it is Bob.", "summary_zh": None,
         "review_state": R, "source": "test"}]
    p["events"] = [
        {"event_id": "e001", "t_s": 10.0, "reveal_at_s": 12.0, "kind": "dialogue", "text_en": "Alice greets Bob.",
         "text_zh": None, "spoiler_critical": 0, "review_state": R, "source": "test"},
        {"event_id": "e002", "t_s": 60.0, "reveal_at_s": 62.0, "kind": "reveal", "text_en": "The wolf turns out to be Bob.",
         "text_zh": None, "spoiler_critical": 1, "review_state": R, "source": "test"}]
    p["item_characters"] = [{"item_type": t, "item_id": i, "character_id": c, "relation": r} for t, i, c, r in (
        ("scene", "s01", "c01", "present"), ("scene", "s01", "c02", "present"), ("scene", "s02", "c03", "present"),
        ("event", "e001", "c01", "present"), ("event", "e001", "c02", "mentioned"), ("event", "e002", "c02", "mentioned"))]
    p["item_cues"] = [{"item_type": "event", "item_id": "e001", "cue_id": "en.official:001"},
                      {"item_type": "character_fact", "item_id": "f001", "cue_id": "en.official:001"}]
    p["blocklist"] = [{"term": "Jane Realperson", "lang": "en", "person_ref": "p01", "source": "credits"},
                      {"term": "珍·真人", "lang": "zh", "person_ref": "p01", "source": "review"}]
    can = lambda term, lang, match, kind, rt, rid: {"term": term, "lang": lang, "match": match, "case_sensitive": 0,  # noqa: E731
                                                    "active_until_s": 0.0, "kind": kind, "ref_type": rt, "ref_id": rid}
    p["canaries"] = [can("howl", "en", "word", "identity_clue", "identity_link", "k01"),
                     can("嚎", "zh", "word", "identity_clue", "identity_link", "k01"),
                     can("turns out", "en", "phrase", "event", "event", "e002"),
                     can("原来", "zh", "word", "event", "event", "e002")]
    p["review_log"] = [_log(1, "title_info", "title"), _log(2, "event", "e002", sc=1)]
    p["canaries"] = derive_canaries(p)
    return packdb.normalize_pack(p)


@pytest.fixture
def pack():
    return copy.deepcopy(mini_pack())


@pytest.fixture(autouse=True)
def prompt_texts(request):
    """Inject placeholder prompts by default (tests do not depend on real prompts); tests marked `real_prompts` use the real loader and skip if unavailable."""
    if request.node.get_closest_marker("real_prompts"):
        promptbook.use(None)
        try:
            promptbook.source()
        except promptbook.PromptsUnavailable as e:
            pytest.skip(str(e))
    else:
        promptbook.use(promptbook.placeholder_bundle())
    yield
    promptbook.use(None)
