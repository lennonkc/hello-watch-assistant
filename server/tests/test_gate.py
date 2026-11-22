"""Direct tests for the gate rules in `ingest/retrieval.py::Gate` on counterexamples built from the mini pack."""
import copy

import pytest

from ingest.retrieval import Gate
from ingest.tests.conftest import mini_pack

R = "reviewed"


@pytest.fixture
def pack():
    return copy.deepcopy(mini_pack())


def ids(pack, P, types=None):
    return {it.item_id for it in Gate(pack, P).eligible(types)}


def ev(eid, t, reveal, text="Something happens.", kind="action", sc=0):
    return {"event_id": eid, "t_s": t, "reveal_at_s": reveal, "kind": kind, "text_en": text, "text_zh": None,
            "spoiler_critical": sc, "review_state": R, "source": "test"}


def test_scene_visible_only_after_it_ends(pack):
    pack["scenes"][0].update(reveal_at_s=40.0, end_s=50.0)             # revealed early, still running until 50
    assert "s01" not in ids(pack, 43.0)                                 # old rule: 40 + 3
    assert "s01" not in ids(pack, 52.9) and "s01" in ids(pack, 53.0)    # max(reveal, end) + 3


def test_event_visible_at_max_of_time_and_reveal(pack):
    pack["events"].append(ev("e010", 30.0, 25.0))                       # reveal stamped before the event itself
    assert "e010" not in ids(pack, 28.0)
    assert "e010" not in ids(pack, 32.9) and "e010" in ids(pack, 33.0)


def test_nothing_from_the_credits(pack):
    # story_end_s = 90: an event that starts in the credits, and one that only reveals there
    pack["events"] += [ev("e011", 91.0, 91.5), ev("e012", 80.0, 91.0)]
    pack["scenes"].append({**pack["scenes"][0], "scene_id": "s09", "start_s": 92.0, "end_s": 95.0, "reveal_at_s": 95.0})
    pack["recaps"] = [{"recap_id": "r09", "upto_scene_id": "s09", "upto_s": 95.0, "reveal_at_s": 95.0, "text_en": "x",
                       "text_zh": None, "source": "t", "review_state": R}]
    for P in (99.0, 100.0, 1e6):
        assert not {"e011", "e012", "s09", "r09"} & ids(pack, P)
    assert {"e001", "e002", "s01"} <= ids(pack, 99.0)


def test_items_tied_to_a_character_not_yet_on_screen(pack):
    # the wolf (c03) first appears at 55 s (+2 s margin); an earlier event that mentions it must wait for it
    pack["events"].append(ev("e013", 20.0, 21.0, "Alice hears something in the forest."))
    pack["item_characters"].append({"item_type": "event", "item_id": "e013", "character_id": "c03", "relation": "mentioned"})
    assert "e013" not in ids(pack, 30.0) and "e013" not in ids(pack, 56.9)
    assert "e013" in ids(pack, 57.0)


def test_items_tied_to_an_unknown_character_never_pass(pack):
    pack["events"].append(ev("e014", 20.0, 21.0))
    pack["item_characters"].append({"item_type": "event", "item_id": "e014", "character_id": "c99", "relation": "present"})
    assert "e014" not in ids(pack, 89.0)


def test_supporting_cues_must_be_eligible(pack):
    pack["events"].append(ev("e015", 40.0, 41.0, "Alice calls out.", kind="dialogue"))
    pack["item_cues"].append({"item_type": "event", "item_id": "e015", "cue_id": "en.official:002"})   # 60–62 s
    assert "e015" not in ids(pack, 50.0) and "e015" not in ids(pack, 62.9)
    assert "e015" in ids(pack, 63.0)
    pack["item_cues"].append({"item_type": "event", "item_id": "e015", "cue_id": "en.official:404"})
    assert "e015" not in ids(pack, 89.0)                                 # a dangling cue link never passes


def test_isolated_is_judged_over_the_whole_interval(pack):
    # link k01: the wolf (c03, first appears 55) is Bob (c02), revealed at 60.5 (+1); Bob is on screen 5–30 s
    g = Gate(pack, 58.0)
    assert g.isolated("scene", "s01", 50.0, 50.0) is False              # s01 is tied to Bob but ends before 55
    assert g.isolated("event", "e002", 56.0, 58.0) is True              # tied to Bob inside [55, 60.5)
    assert g.isolated("event", "e002", 50.0, 56.0) is True              # starts before the window, overlaps it
    assert g.isolated("event", "e002", 60.5, 62.0) is False             # at / after the reveal
    assert g.isolated("event", "e001", 56.0, 58.0) is True              # e001 mentions Bob as well
    assert g.isolated("event", "nope", 56.0, 58.0) is False             # nothing linked, nothing isolated
    assert g.char_isolated("c03", 56.0, 58.0) is False                  # a itself is never isolated
    assert Gate(pack, 62.0).isolated("event", "e002", 56.0, 58.0) is False   # link eligible at 61.5


def test_isolated_unless_b_is_on_screen_for_the_whole_interval(pack):
    pack["appearances"].append({"character_id": "c02", "start_s": 56.0, "end_s": 57.0, "source": "t", "review_state": R})
    g = Gate(pack, 58.0)
    assert g.isolated("event", "e002", 56.0, 57.0) is False             # Bob himself is on screen throughout
    assert g.isolated("event", "e002", 56.0, 58.0) is True              # partly covered is not enough
    pack["appearances"][-1]["review_state"] = "prereviewed"             # linked characters need reviewed appearances
    assert Gate(pack, 58.0).isolated("event", "e002", 56.0, 57.0) is True


def test_interval_isolation_reaches_items_that_start_before_the_window(pack):
    pack["events"].append(ev("e016", 50.0, 56.0, "The boy's cap lies in the grass."))
    pack["item_characters"].append({"item_type": "event", "item_id": "e016", "character_id": "c02", "relation": "mentioned"})
    assert "e016" not in ids(pack, 59.0)                                # a point check at t_s = 50 would let it through
    assert "e016" in ids(pack, 61.5)


def test_link_pending_until_both_characters_are_eligible(pack):
    pack["identity_links"][0]["reveal_at_s"] = 50.0                    # revealed before the wolf (55 + 2) is eligible
    g = Gate(pack, 56.0)
    assert any(lk["b"] == "c02" for lk in g.pending)
    assert not any(lk["b"] == "c02" for lk in Gate(pack, 57.0).pending)


def test_link_window_starts_at_zero_without_a_first_appearance(pack):
    pack["characters"][2]["first_appears_s"] = None
    g = Gate(pack, 20.0)
    assert g.pending[0]["window_start"] == 0.0
    assert not g.isolated("event", "e001", 10.0, 12.0)                  # Bob's own appearance (5–30 s) covers it
    pack["appearances"] = [a for a in pack["appearances"] if a["character_id"] != "c02"]
    assert Gate(pack, 20.0).isolated("event", "e001", 10.0, 12.0)


def test_eligibility_is_monotone_in_p(pack):
    pack["events"] += [ev("e013", 20.0, 21.0), ev("e016", 50.0, 56.0)]
    pack["item_characters"] += [{"item_type": "event", "item_id": "e013", "character_id": "c03", "relation": "present"},
                                {"item_type": "event", "item_id": "e016", "character_id": "c02", "relation": "present"}]
    prev = set()
    for i in range(0, 1000):
        cur = {(it.item_type, it.item_id) for it in Gate(pack, i / 10).eligible()}
        assert prev <= cur, i / 10
        prev = cur
