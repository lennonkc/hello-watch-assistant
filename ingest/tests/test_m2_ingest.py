"""Ingest changes: catalog registration and mismatch detection, plain + word merge, CJK quotes, presence backfill, public-name checks, unchanged v1 prompt fingerprints."""
import hashlib
import pathlib

import pytest

from ingest import library, prompts, promptbook
from ingest.assemble import Assembler, match_quote, present_rows
from ingest.ledger import Ledger
from ingest.stages import Ctx
from ingest.stages.gemini_stages import _full_inputs, _seg_inputs
from ingest.stages.subtitles import generated_cues, merge_plain, plain_text, surface_tokens
from ingest.tests.conftest import mini_pack

ITEMS = [{"id": "m1", "type": "movie", "title": "Sonic the Hedgehog 2", "title_zh": "刺猬索尼克2", "duration_s": 130},
         {"id": "m2", "type": "movie", "title": "WOLF WARRIOR II", "title_zh": "战狼2", "duration_s": 110},
         {"id": "m3", "type": "movie", "title": "Mufasa: The Lion King", "title_zh": "木法沙:狮子王", "duration_s": 143}]


# ── Catalog registration ────────────────────────────────────────────────

def test_match_catalog_by_zh_then_en():
    assert library.match_catalog("刺猬索尼克2 Sonic the Hedgehog 2.mp4", ITEMS)["id"] == "m1"
    assert library.match_catalog("战狼2 Wolf Warrior 2.mp4", ITEMS)["id"] == "m2"
    assert library.match_catalog("xx Mufasa.mp4", ITEMS)["id"] == "m3"      # English prefix
    assert library.match_catalog("未知 Unknown.mp4", ITEMS) is None


def test_series_trailer_filed_as_movie_is_a_content_mismatch():
    ident = {"doc": {"asset_form": "episode_trailer", "title_on_screen": "SONIC PRIME", "series_marker": "A Netflix Series",
                     "matches_catalog": False, "mismatch_reason": "series trailer"}}
    ms = library.content_mismatches(ITEMS[0], {"asset_kind": "trailer"}, ident)
    kinds = [m["kind"] for m in ms]
    assert kinds.count("content") == 3          # on-screen title mismatch + series marker + model verdict
    assert "kind" not in kinds                  # a series trailer is still a trailer


def test_matching_trailer_has_no_mismatch():
    ident = {"doc": {"asset_form": "trailer", "title_on_screen": "Sonic the Hedgehog 2", "series_marker": "",
                     "matches_catalog": True, "mismatch_reason": ""}}
    assert library.content_mismatches(ITEMS[0], {"asset_kind": "trailer"}, ident) == []
    ident["doc"]["title_on_screen"] = "战狼2"
    assert library.content_mismatches(ITEMS[1], {"asset_kind": "trailer"}, ident) == []


def test_series_marker_alone_is_not_a_mismatch():
    ident = {"doc": {"asset_form": "short_film", "title_on_screen": "Sonic", "series_marker": "chapter 1: first cycle",
                     "matches_catalog": True}}
    assert library.content_mismatches(ITEMS[0], {"asset_kind": "short"}, ident) == []


def test_kind_mismatch():
    ident = {"doc": {"asset_form": "short_film", "title_on_screen": "", "series_marker": "", "matches_catalog": True}}
    assert [m["kind"] for m in library.content_mismatches(ITEMS[0], {"asset_kind": "trailer"}, ident)] == ["kind"]


def test_mismatch_note_only_for_real_mismatches():
    assert library.asset_mismatch_note({"mismatches": []}) is None
    note = library.asset_mismatch_note({"mismatches": [{"kind": "duration", "detail": "x"}, {"kind": "content", "detail": "@someone"},
                                                        {"kind": "sha256", "detail": "x"}]})
    assert note == "content;duration"          # categories only, no raw reason text


# ── Generated subtitles: plain + word passes ────────────────────────────

W = [{"s": 1.0, "e": 1.5, "text": "This"}, {"s": 1.5, "e": 2.0, "text": "blade."},
     {"s": 5.0, "e": 5.5, "text": "Thank"}, {"s": 5.5, "e": 6.0, "text": "you."}]


def test_merge_plain_fills_gap_conservatively():
    extra, rep = merge_plain(W, "This blade. Scales! Thank you.")
    assert [(c["start_s"], c["end_s"], c["text"]) for c in extra] == [(2.0, 5.0, "Scales!")]
    assert extra[0]["interpolated"] and rep["inserted_cues"] == 1


def test_merge_plain_drops_insert_without_gap():
    extra, rep = merge_plain(W, "This um blade. Thank you.")      # "um" sits between two adjacent words, no gap
    assert extra == [] and rep["dropped_no_gap"] == 1


def test_merge_plain_identical_adds_nothing():
    assert merge_plain(W, "This blade. Thank you.")[0] == []


def test_plain_text_from_interaction_steps():
    raw = {"steps": [{"type": "model_output", "content": [{"type": "text", "text": "Hello there."}]}]}
    assert plain_text(raw) == "Hello there."


def test_cjk_tokens_and_cues():
    assert [k for k, _ in surface_tokens("你好, Scales!")] == ["你", "好", "scales"]
    cues = generated_cues([{"s": 1.0, "e": 1.2, "text": "你"}, {"s": 1.2, "e": 1.4, "text": "好。"},
                           {"s": 1.5, "e": 1.7, "text": "走"}])
    assert [c["text"] for c in cues] == ["你好。", "走"]


def test_match_quote_cjk():
    cues = [{"cue_id": "zh.generated:001", "start_s": 10.0, "end_s": 12.0, "text": "犯我中华者,虽远必诛"},
            {"cue_id": "zh.generated:002", "start_s": 20.0, "end_s": 21.0, "text": "走吧"}]
    assert [c["cue_id"] for c in match_quote("犯我中华者虽远必诛", cues, 11.0)] == ["zh.generated:001"]


# ── Presence backfill ───────────────────────────────────────────────────

def test_present_rows_from_appearances():
    p = mini_pack()
    p["item_characters"] = []
    p["scenes"] = [dict(p["scenes"][0], scene_id="s01", start_s=0.0, end_s=30.0)]
    p["events"] = [dict(p["events"][0], event_id="e01", t_s=12.0, reveal_at_s=13.0)]
    p["objects"] = [dict(p["objects"][0], object_id="o01", first_seen_s=40.0, reveal_at_s=41.0)] if p["objects"] else []
    p["object_sightings"] = [{"object_id": "o01", "t_s": 40.0}] if p["objects"] else []
    p["appearances"] = [{"character_id": "c01", "start_s": 10.0, "end_s": 20.0, "source": "t", "review_state": "reviewed"},
                        {"character_id": "c02", "start_s": 39.5, "end_s": 45.0, "source": "t", "review_state": "reviewed"},
                        {"character_id": "c02", "start_s": 0.0, "end_s": 0.4, "source": "t", "review_state": "rejected"}]
    rows = {(r["item_type"], r["item_id"], r["character_id"]) for r in present_rows(p)}
    assert ("scene", "s01", "c01") in rows and ("event", "e01", "c01") in rows
    assert ("scene", "s01", "c02") not in rows                    # rejected appearance spans do not count
    if p["objects"]:
        assert ("object", "o01", "c02") in rows and ("object", "o01", "c01") not in rows
    p["item_characters"] = present_rows(p)
    assert present_rows(p) == []                                  # idempotent


# ── Public-name checks ──────────────────────────────────────────────────

class _A(Assembler):
    def __init__(self, title, cat):  # noqa: D107 - only tests publicly_known
        self.title, self.cat = title, cat


def test_publicly_known_title_and_synopsis():
    a = _A({"title": "Mufasa: The Lion King"}, {"title": "Mufasa: The Lion King",
                                               "synopsis_full": "Mufasa, a cub lost and alone, meets a lion named Taka.",
                                               "synopsis_short_zh": "孤儿幼狮木法沙"})
    assert a.publicly_known("Mufasa") and a.publicly_known("Taka") and a.publicly_known("木法沙")
    assert not a.publicly_known("Scar")


# ── Stage fingerprints of already-ingested (v1) assets stay unchanged ───

PROD_COMMON_RULES_SHA256 = "10581b9447625d8f5de84900229ec98bef11a84700beff6020447f1a5ea52c17"


@pytest.mark.real_prompts
def test_v1_prompt_fingerprints_unchanged(tmp_path):
    if hashlib.sha256(promptbook.text("common_rules").encode()).hexdigest() != PROD_COMMON_RULES_SHA256:
        pytest.skip("fingerprint pins only hold for the production prompts")
    ctx = Ctx(sha="ab" * 32, asset=pathlib.Path("x.mp4"), catalog_id="c", title={"vision_prompt": "v1"},
              catalog_item={}, subs_dir=tmp_path, ledger=Ledger("ab" * 32))
    assert _full_inputs(ctx)["prompt"] == "966a42180b989d229c6c5cc1e7e4915dfe07f326fb386a1e197727ad6735857e"
    assert _seg_inputs(ctx)["prompt"] == "1ddc82d5ebb8b754dc04e3a6ec19a0ebcc6653f5f2a472dfeaea70beb6502996"
    ctx.title = {"asset_kind": "trailer"}
    ctx.catalog_item = {"synopsis_full": "x"}
    assert _full_inputs(ctx)["prompt"] != "966a42180b989d229c6c5cc1e7e4915dfe07f326fb386a1e197727ad6735857e"
    trailer = promptbook.text("trailer_rule")
    assert trailer in prompts.full_rules_v2("x", True, "en") and trailer not in prompts.full_rules_v2("x", False, "en")


def test_needs_human_lists_prereview_doubts():
    from ingest.build import DOUBT_TAG, needs_human
    p = mini_pack()
    p["review_log"] = [{"log_id": 1, "item_type": "appearance", "item_id": "c01/10.0", "field": "review_state", "old_value": None,
                        "new_value": None, "decision": "approve", "spoiler_critical": 0, "reviewer": "claude-prereview",
                        "ts": "t", "seconds_spent": None, "why": f"{DOUBT_TAG} 区间偏宽"},
                       {"log_id": 2, "item_type": "scene", "item_id": "s01", "field": None, "old_value": None, "new_value": None,
                        "decision": "approve", "spoiler_critical": 0, "reviewer": "reviewer-1", "ts": "t", "seconds_spent": None,
                        "why": f"{DOUBT_TAG} 人工写的不算"}]
    nh = needs_human(p, {"checks": [], "flag_items": []})
    assert nh["counts"]["prereview_doubts"] == 1 and nh["prereview_doubts"][0]["item_id"] == "c01/10.0"


def test_is_generic_capitalised_single_word_is_a_name():
    from ingest.assemble import is_generic
    assert is_generic("the dragon") == 1
    assert is_generic("Scar") == 0
    assert is_generic("The Dragon") == 1          # leading article is still a description
    assert is_generic("小鳞") == 0


def test_rejected_rows_are_not_scanned_for_names_or_mentions():
    from ingest.checks import check_mentions, check_names
    p = mini_pack()
    named = next(c for c in p["characters"] if c["name_known_at_s"] is not None and not c["name_public"])
    f = dict(p["character_facts"][0], fact_id="fx1", text_en=f"About {named['canonical_name']}.", reveal_at_s=0.5,
             review_state="rejected")
    p["character_facts"].append(f)
    p["item_characters"].append({"item_type": "character_fact", "item_id": "fx1", "character_id": named["character_id"],
                                 "relation": "mentioned"})
    assert not [i for i in check_names(p) if i["item_id"] == "fx1"]
    assert not [i for i in check_mentions(p) if i["item_id"] == "fx1"]
    f["review_state"] = "prereviewed"
    assert [i for i in check_names(p) if i["item_id"] == "fx1"]


def test_descriptive_labels_are_not_names():
    from ingest.assemble import looks_descriptive
    assert looks_descriptive("the dog") and looks_descriptive("the shepherd girl")
    assert not looks_descriptive("Gru") and not looks_descriptive("唐仁")


def test_item_cues_item_type_validated():
    from ingest.checks import check_structure
    from ingest.derive import fts_rows
    p = mini_pack()
    p["item_cues"].append({"item_type": "events", "item_id": "e01", "cue_id": p["cues"][0]["cue_id"]})
    assert any("item_cues.item_type events" in i["msg"] for i in check_structure(p, fts_rows(p)))
