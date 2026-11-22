"""spec §10 builder checks: the synthetic pack passes all; one counterexample per check."""
from ingest.checks import run_checks
from ingest.derive import derive_canaries, fts_rows


def run(pack):
    pack["canaries"] = derive_canaries(pack)
    rep = run_checks(pack, fts_rows(pack))
    return rep, {c["id"]: c for c in rep["checks"]}


def failed(pack):
    _, by = run(pack)
    return {i for i, c in by.items() if not c["ok"]}


def flagged(pack):
    rep, _ = run(pack)
    return {tuple(x) for x in rep["flag_items"]}


def test_positive_all_pass(pack):
    rep, by = run(pack)
    assert rep["ok"], [i["msg"] for c in rep["checks"] for i in c["issues"]]


def test_1_structure_segments_gap(pack):
    pack["segments"][0]["end_s"] = 90.0
    assert 1 in failed(pack)


def test_1_structure_time_out_of_range(pack):
    pack["events"][0]["reveal_at_s"] = 101.0
    assert 1 in failed(pack)
    assert ("event", "e001") in flagged(pack)


def test_4_generated_cue_blank_or_runaway(pack):
    pack["subtitle_tracks"].append(dict(pack["subtitle_tracks"][0], track="en.generated", source="generated", priority=2))
    pack["cues"].append(dict(pack["cues"][0], cue_id="en.generated:001", track="en.generated", text=" "))
    pack["cues"].append(dict(pack["cues"][0], cue_id="en.generated:002", track="en.generated", end_s=30.0))
    _, by = run(pack)
    msgs = [i["msg"] for i in by[4]["issues"]]
    assert any("en.generated:001 is blank" in m for m in msgs) and any("en.generated:002 lasts 20.00s" in m for m in msgs)


def test_2_id_contains_name(pack):
    pack["character_looks"][1]["look_id"] = "l-bob"
    assert 2 in failed(pack)


def test_3_name_before_known(pack):
    pack["character_looks"][1]["description_en"] = "Bob wears a cap."    # visible at 6 s, name only known at 10 s
    assert 3 in failed(pack)
    assert ("character_look", "l002") in flagged(pack)


def test_3_zh_name_before_known(pack):
    pack["character_looks"][1]["description_zh"] = "鲍勃戴着帽子。"
    assert 3 in failed(pack)


def test_3_public_name_ok(pack):
    pack["character_looks"][0]["description_en"] = "Alice wears a red coat."   # name_public = 1
    assert 3 not in failed(pack)


def test_3_case_sensitive_name(pack):
    pack["characters"][1]["canonical_name"] = "Scales"                     # same spelling as a common noun → case-sensitive
    pack["characters"][1]["name_zh"] = "史科尔斯"
    pack["cues"][0]["text"] = "Hello, Scales."
    pack["cues"][1]["text"] = "Scales?"
    pack["identity_links"][0]["text_en"] = "The wolf is Scales."
    pack["scenes"][0]["summary_en"] = "Alice meets Scales."
    pack["events"][0]["text_en"] = "Alice greets Scales."
    pack["events"][1]["text_en"] = "The wolf turns out to be Scales."
    pack["scenes"][1]["summary_en"] = "A wolf appears; it is Scales."
    pack["character_looks"][1]["description_en"] = "A boy covered in tan scales."
    assert 3 not in failed(pack)
    pack["character_looks"][1]["description_en"] = "Scales wears a cap."
    assert 3 in failed(pack)


def test_4_dialogue_event_without_cue(pack):
    pack["item_cues"] = [x for x in pack["item_cues"] if x["item_type"] != "event"]
    assert 4 in failed(pack)
    assert ("event", "e001") in flagged(pack)


def test_4_reveal_before_cue_end(pack):
    pack["character_facts"][0]["reveal_at_s"] = 11.0
    assert 4 in failed(pack)


def test_5_mention_before_first_appearance(pack):
    pack["item_characters"].append({"item_type": "event", "item_id": "e001", "character_id": "c03", "relation": "mentioned"})
    assert 5 in failed(pack)


def test_6_attached_to_b_inside_window(pack):
    pack["events"].append({"event_id": "e003", "t_s": 57.0, "reveal_at_s": 58.0, "kind": "action", "text_en": "A howl.",
                           "text_zh": None, "spoiler_critical": 0, "review_state": "reviewed", "source": "t"})
    pack["item_characters"].append({"item_type": "event", "item_id": "e003", "character_id": "c02", "relation": "present"})
    assert 6 in failed(pack)
    assert ("event", "e003") in flagged(pack)


def test_6_b_present_as_itself_ok(pack):
    pack["appearances"].append({"character_id": "c02", "start_s": 56.0, "end_s": 59.0, "source": "t", "review_state": "reviewed"})
    pack["events"].append({"event_id": "e003", "t_s": 57.0, "reveal_at_s": 58.0, "kind": "action", "text_en": "The boy waves.",
                           "text_zh": None, "spoiler_critical": 0, "review_state": "reviewed", "source": "t"})
    pack["item_characters"].append({"item_type": "event", "item_id": "e003", "character_id": "c02", "relation": "present"})
    assert 6 not in failed(pack)


def test_6_clue_word_before_reveal(pack):
    pack["character_looks"][2]["description_en"] = "A grey wolf that likes to howl."   # visible at 56 < R 60.5
    assert 6 in failed(pack)


def test_7_reveal_earlier_than_confirming_cue(pack):
    pack["identity_links"][0]["reveal_at_s"] = 58.0
    assert 7 in failed(pack)
    assert ("identity_link", "k01") in flagged(pack)


def test_7_silent_reveal_needs_human_record(pack):
    pack["cues"][1]["text"] = "Huh?"           # no confirming line
    pack["review_log"] = [r for r in pack["review_log"] if r["item_type"] != "event"]
    assert 7 in failed(pack)
    rep, by = run(pack)
    assert not any(i["flag"] for i in by[7]["issues"])     # awaiting human review, not flagged


def test_7_claude_record_not_enough(pack):
    pack["cues"][1]["text"] = "Huh?"
    for r in pack["review_log"]:
        if r["item_type"] == "event":
            r["reviewer"] = "claude-prereview"
    assert 7 in failed(pack)


def test_8_prereviewed_is_not_reviewed(pack):
    pack["character_facts"][0]["review_state"] = "prereviewed"
    assert 8 in failed(pack)


def test_8_missing_zh_name_canary(pack):
    pack["characters"][1]["name_zh"] = None
    assert 8 in failed(pack)


def test_8_missing_identity_clue_lang(pack):
    pack["canaries"] = [k for k in pack["canaries"] if not (k["kind"] == "identity_clue" and k["lang"] == "zh")]
    assert 8 in failed(pack)


def test_9_blocklist_hit(pack):
    pack["scenes"][0]["summary_en"] = "Alice meets Bob, voiced by Jane Realperson."
    assert 9 in failed(pack)
    assert ("scene", "s01") in flagged(pack)


def test_9_blocklist_zh_hit(pack):
    pack["title_info"][0]["text_zh"] = "迷你(珍·真人 主演)"
    assert 9 in failed(pack)


def test_10_card_without_look(pack):
    pack["character_looks"] = [x for x in pack["character_looks"] if x["character_id"] != "c03"]
    assert 10 in failed(pack)
    assert ("character", "c03") in flagged(pack)


def test_11_event_canary_leaks_through_earlier_item(pack):
    # fact becomes eligible before the reveal event and contains the event's canary term
    pack["character_facts"].append({"fact_id": "f002", "character_id": "c03", "text_en": "It turns out to be the boy.",
                                    "text_zh": None, "reveal_at_s": 61.0, "basis": "shown", "evidence_t_s": 61.0,
                                    "spoiler_critical": 0, "review_state": "reviewed", "source": "t"})
    assert 11 in failed(pack)
    assert ("character_fact", "f002") in flagged(pack)


def test_11_prereviewed_leak_caught_in_if_approved_mode(pack):
    pack["character_facts"].append({"fact_id": "f002", "character_id": "c03", "text_en": "It turns out to be the boy.",
                                    "text_zh": None, "reveal_at_s": 61.0, "basis": "shown", "evidence_t_s": 61.0,
                                    "spoiler_critical": 0, "review_state": "prereviewed", "source": "t"})
    _, by = run(pack)
    assert any("if approved" in i["msg"] for i in by[11]["issues"])


def test_11_shot_caption_canary_leak(pack):
    pack["shot_captions"].append({"shot_caption_id": "sc001", "start_s": 10.0, "end_s": 11.0, "reveal_at_s": 11.0,
                                  "caption_en": "A howl.", "caption_zh": None, "source": "t", "review_state": "reviewed"})
    assert 11 in failed(pack)
    assert ("shot_caption", "sc001") in flagged(pack)


def test_11_shot_entity_hit_flags_its_caption(pack):
    pack["shot_captions"].append({"shot_caption_id": "sc001", "start_s": 10.0, "end_s": 11.0, "reveal_at_s": 11.0,
                                  "caption_en": "Two figures in a meadow.", "caption_zh": None, "source": "t",
                                  "review_state": "reviewed"})
    pack["shot_entities"].append({"shot_caption_id": "sc001", "seq": 1, "entity_type": "character", "character_id": None,
                                  "object_id": None, "label_en": "a boy", "label_zh": None, "position": "left",
                                  "bbox": None, "bbox_t_s": None, "action_en": "Lets out a howl", "action_zh": None})
    _, by = run(pack)
    assert any("shot_entity sc001/1.action_en" in i["msg"] for i in by[11]["issues"])
    assert ("shot_caption", "sc001") in flagged(pack)


def test_gate_skips_blank_cues_and_orders_cues_by_time(pack):
    from ingest.gate import Gate
    pack["cues"].append(dict(pack["cues"][0], cue_id="en.official:003", start_s=5.0, end_s=6.0, text="  "))
    pack["cues"].append(dict(pack["cues"][0], cue_id="en.official:004", start_s=3.0, end_s=4.0, text="Early."))
    assert [c["cue_id"] for c in Gate(pack).eligible(20.0)["cues"]] == ["en.official:004", "en.official:001"]
