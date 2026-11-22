"""Shared fixtures: the mini pack as a servable title, and /api/ask wired to scripted models."""
import pytest
from fastapi.testclient import TestClient
from google.adk.runners import Runner

from ingest import packdb
from ingest.tests.conftest import mini_pack
from server.packs import Title
from server.tests.fakes import FakeLlm


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
    # a row that names Bob before his name is visible (11 s): eligible at 10 s, must reach the model as "the boy"
    p["events"].append({"event_id": "e003", "t_s": 6.0, "reveal_at_s": 7.0, "kind": "action",
                        "text_en": "Alice waves at Bob.", "text_zh": None, "spoiler_critical": 0, "review_state": "reviewed",
                        "source": "test"})
    p["item_characters"] += [{"item_type": "event", "item_id": "e003", "character_id": c, "relation": "present"}
                             for c in ("c01", "c02")]
    p["character_aliases"] = [{"character_id": "c02", "alias": "Bobby", "lang": "en", "alias_known_at_s": 30.0, "generic": 0}]
    p["events"].append({"event_id": "e004", "t_s": 20.0, "reveal_at_s": 21.0, "kind": "action",
                        "text_en": "Bobby climbs a tree in the meadow.", "text_zh": None, "spoiler_critical": 0,
                        "review_state": "reviewed", "source": "test"})
    # a second subtitle track repeating the lines, and a longer line on both
    p["subtitle_tracks"].append({**p["subtitle_tracks"][0], "track": "zh.official", "lang": "zh", "priority": 1})
    p["cues"].append({"cue_id": "en.official:003", "track": "en.official", "start_s": 20.0, "end_s": 23.0,
                      "text": "Look at the meadow from up here!", "kind": "dialogue", "speaker_raw": None})
    # spoken by Bob while the wolf is on screen and before "the wolf is Bob" is revealed (60.5 s)
    p["cues"].append({"cue_id": "en.official:004", "track": "en.official", "start_s": 57.0, "end_s": 58.0,
                      "text": "Who is out there in the dark?", "kind": "dialogue", "speaker_raw": None})
    p["cues"] += [{**c, "cue_id": c["cue_id"].replace("en.", "zh."), "track": "zh.official"} for c in list(p["cues"])]
    p["cue_speakers"] = [{"cue_id": f"en.official:00{i}", "character_id": "c02", "diar_label": "S1", "confidence": 0.9,
                          "method": "t", "review_state": "auto"} for i in (2, 3, 4)]
    root = tmp_path / "master"
    root.mkdir()
    packdb.write_db(packdb.normalize_pack(p), root / "pack.db")
    return Title("mini", "ab" * 32, "Mini", "", "CC-BY-3.0", "", root)


@pytest.fixture
def framed_title(title):
    """The title with one thumbnail per second (a flat grey frame), as the ingest writes them."""
    from PIL import Image
    d = title.root / "thumbs"
    d.mkdir()
    img = Image.new("RGB", (64, 36), (128, 128, 128))
    for t in range(0, 101):
        img.save(d / f"t{t:05d}.jpg", "JPEG")
    return title


class Wired:
    def __init__(self, client, ctx: FakeLlm):
        self.client, self.ctx = client, ctx

    def ask(self, p, question="Who is that?", **kw):
        from server.tests.fakes import sse_events
        r = self.client.post("/api/ask", json={"title_id": "mini", "p": p, "question": question, **kw})
        assert r.status_code == 200, r.text
        return sse_events(r.text)


def wire(monkeypatch, title) -> Wired:
    from server import agent as A
    from server import config, main
    from server.packs import STORE
    monkeypatch.setitem(STORE.titles, "mini", title)
    monkeypatch.setenv("CEDAR_EMBED", "0")                       # no query embeddings: full-text ranking
    monkeypatch.setattr(config, "ASK_PER_MINUTE", 10_000)
    monkeypatch.setattr(main, "LIMITER", main.Limiter())
    monkeypatch.setattr(main, "HISTORY", main.History())
    A._moment.cache_clear()
    ctx = FakeLlm()
    monkeypatch.setitem(main.RUNNERS, "context", Runner(app_name=main.APP_NAME, agent=A.build_context_agent(model=ctx),
                                                        session_service=main.session_service))
    return Wired(TestClient(main.app), ctx)


@pytest.fixture
def wired(monkeypatch, framed_title):
    yield wire(monkeypatch, framed_title)
    from server import agent as A
    A._moment.cache_clear()
