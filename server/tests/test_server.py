"""Online service on the synthetic mini pack: gate-aware views, name visibility at P, output guards, HTTP surface."""
import json

import pytest

from ingest import packdb
from ingest.retrieval import Gate
from ingest.tests.conftest import mini_pack
from server import guard
from server.knowledge import Moment, pick_entity
from server.packs import Title


@pytest.fixture
def title(tmp_path):
    p = mini_pack()
    p["shot_captions"] = [
        {"shot_caption_id": "sc000", "start_s": 0.0, "end_s": 50.0, "reveal_at_s": 50.0, "source": "t",
         "caption_en": "A girl walks through a meadow.", "caption_zh": None, "review_state": "auto"},
        {"shot_caption_id": "sc001", "start_s": 50.0, "end_s": 100.0, "reveal_at_s": 100.0, "source": "t",
         "caption_en": "The wolf howls; it is Bob.", "caption_zh": None, "review_state": "auto"}]
    p["shot_entities"] = [
        {"shot_caption_id": "sc001", "seq": 0, "entity_type": "character", "character_id": "c03", "object_id": None,
         "label_en": "the wolf", "label_zh": None, "position": "left", "bbox": "[100, 50, 600, 400]", "bbox_t_s": 60,
         "action_en": "howling at Bob's house", "action_zh": None},
        {"shot_caption_id": "sc001", "seq": 1, "entity_type": "character", "character_id": "c01", "object_id": None,
         "label_en": "Alice", "label_zh": None, "position": "right", "bbox": "[100, 600, 900, 900]", "bbox_t_s": 60,
         "action_en": None, "action_zh": None}]
    p["recaps"] = [{"recap_id": "r01", "upto_scene_id": "s01", "upto_s": 50.0, "reveal_at_s": 50.0,
                    "text_en": "Alice met the boy in a meadow.", "text_zh": None, "source": "t", "review_state": "auto"}]
    root = tmp_path / "master"
    root.mkdir()
    packdb.write_db(packdb.normalize_pack(p), root / "pack.db")
    return Title("mini", "ab" * 32, "Mini", "", "CC-BY-3.0", "", root)


def test_labels_follow_name_visibility(title):
    assert Moment(title, 8.0).label("c02") == "the boy"            # on screen, name not spoken yet
    assert Moment(title, 10.9).label("c02") == "the boy"           # spoken at 10 s, margin 1 s
    assert Moment(title, 11.0).label("c02") == "Bob"
    assert Moment(title, 11.0).label("c02", "zh") == "鲍勃"
    assert Moment(title, 3.0).label("c01") == "Alice"              # name_public


def test_known_characters_only_after_first_appearance(title):
    assert [c["label"] for c in Moment(title, 6.0).known_characters()] == ["Alice"]   # Bob: 5 s + 2 s margin
    assert "the boy" in [c["label"] for c in Moment(title, 7.0).known_characters()]


def test_story_so_far_never_returns_future_items(title):
    for P in (5.0, 20.0, 52.9, 53.0, 61.0, 70.0):
        m = Moment(title, P)
        eligible = {it.text for it in Gate(m.pack, P, allow_auto=True).eligible()}
        out = m.story_so_far()
        for it in out.get("since_recap", []) + out.get("so_far", []):
            assert it["text"] in eligible
        assert "recap" not in out or P >= 53.0


def test_recent_dialogue_excludes_lines_still_on_screen(title):
    assert Moment(title, 12.9).recent_dialogue() == []             # cue ends at 12 s, margin 1 s
    lines = Moment(title, 13.0).recent_dialogue()
    assert [ln["text"] for ln in lines] == ["Hello, Bob."]


def test_current_shot_rule(title):
    m = Moment(title, 59.5)                                        # frame 60 not shown yet
    assert m.on_screen()["entities"] == []
    out = Moment(title, 61.2).on_screen({"x": 0.2, "y": 0.3})
    labels = [e["label"] for e in out["entities"]]
    assert labels == ["the wolf", "Alice"]
    assert out["entities"][out["tapped"]]["label"] == "the wolf"
    text = json.dumps(out)
    assert "howl" not in text and "Bob" not in text                # no caption / action of the unfinished shot


def test_credits_decline_on_screen(title):
    assert "entities" not in Moment(title, 95.0).on_screen()


def test_pick_entity_prefers_smallest_containing_box():
    ents = [{"bbox": [0, 0, 1000, 1000]}, {"bbox": [400, 400, 600, 600]}]
    assert pick_entity(ents, 500, 500) == 1
    assert pick_entity([{"bbox": [0, 0, 100, 100]}], 900, 900) is None


def test_guard(title):
    pack = title.pack
    assert guard.violations(pack, "That is Jane Realperson.", 50.0)[0]["kind"] == "real_name"
    assert {h["kind"] for h in guard.violations(pack, "That's Bob.", 8.0)} & {"canary:name", "name_not_yet_visible"}
    assert guard.violations(pack, "That's Bob.", 12.0) == []
    assert guard.violations(pack, "A wolf, nothing more.", 70.0) == []


def test_canary_prefix_match():
    assert guard.matches("scar*", "The scarred wing", "word", False)
    assert not guard.matches("scar*", "Oscar", "word", False)
    assert guard.matches("伤疤*", "有伤疤的翅膀", "word", False)


def test_gate_counts_are_counts_only(title):
    c = Moment(title, 53.0).gate_counts()
    assert c["eligible"]["scene"] == 1 and c["total"]["scene"] == 2
    assert all(isinstance(v, int) for v in c["eligible"].values())


def test_http_surface(title, monkeypatch):
    from fastapi.testclient import TestClient

    from server import main
    from server.packs import STORE
    monkeypatch.setitem(STORE.titles, "mini", title)
    c = TestClient(main.app)
    assert c.get("/api/titles/mini/gate", params={"p": 53}).json()["eligible"]["scene"] == 1
    assert c.get("/api/titles/nope/gate", params={"p": 1}).status_code == 404
    assert c.post("/api/ask", json={"title_id": "mini", "p": 5, "question": "x" * 400}).status_code == 422


def test_limiter(monkeypatch):
    from server import config, main
    monkeypatch.setattr(config, "ASK_PER_MINUTE", 2)
    lim = main.Limiter()
    assert lim.check("1.2.3.4") is None and lim.check("1.2.3.4") is None
    assert lim.check("1.2.3.4") is not None
    assert lim.check("5.6.7.8") is None
