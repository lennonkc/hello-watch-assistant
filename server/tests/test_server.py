"""Online service on the synthetic mini pack: gate-aware views, name visibility at P, output guards, HTTP surface."""
import json

import pytest

from ingest.retrieval import Gate
from server import guard
from server.knowledge import Moment, pick_entity
from server.tests.fakes import oracle_leaks


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
    assert guard.matches("mark*", "The marked door", "word", False)
    assert not guard.matches("mark*", "Denmark", "word", False)
    assert guard.matches("印记*", "门上有印记", "word", False)


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


def test_sanitize_replaces_hidden_names_with_the_label_at_p(title):
    assert Moment(title, 10.5).sanitize("Alice waves at Bob.") == "Alice waves at the boy."
    assert Moment(title, 10.5).sanitize("Bob's cap. Bob waves.") == "The boy's cap. The boy waves."
    assert Moment(title, 10.5).sanitize("A bob of the head.") == "A bob of the head."  # common noun: case-sensitive
    assert Moment(title, 11.0).sanitize("Alice waves at Bob.") == "Alice waves at Bob."      # visible from 11 s
    assert Moment(title, 10.5, "zh").sanitize("爱丽丝和鲍勃") == "爱丽丝和男孩"
    assert Moment(title, 20.0).sanitize("Bobby is up the tree.") == "Bob is up the tree."    # alias: 30 s, name: 11 s
    assert Moment(title, 31.0).sanitize("Bobby is up the tree.") == "Bobby is up the tree."
    assert Moment(title, 10.5).sanitize("Bobsleigh") == "Bobsleigh"                          # whole words only
    story = json.dumps(Moment(title, 10.5).story_so_far())
    assert "Alice waves at the boy." in story and "Bob" not in story


def test_search_uses_one_track_and_skips_short_lines(title):
    hits = Moment(title, 30.0).search("meadow")                     # no vectors in this pack: full-text ranking
    lines = [h for h in hits if h.item_type == "cue"]
    assert [h.item_id for h in lines] == ["en.official:003"]       # not once per subtitle track
    assert not [h for h in Moment(title, 70.0).search("Bob") if h.text == "Bob?"]
    assert "Bob climbs a tree" in " ".join(h.text for h in Moment(title, 25.0).search("tree"))   # alias → name at 25 s


def test_recent_dialogue_falls_back_to_the_last_lines(title):
    m = Moment(title, 50.0)
    assert m.recent_dialogue(10) == []
    assert [ln["at"] for ln in m.recent_dialogue(10, at_least=2)] == ["00:10", "00:20"]


def test_speaker_withheld_while_identity_is_isolated(title):
    line = lambda P: next(ln for ln in Moment(title, P).recent_dialogue(30) if ln["at"] == "00:57")  # noqa: E731
    assert line(59.5)["speaker"] == "unknown speaker"              # attributing it to Bob would reveal the wolf
    assert line(62.0)["speaker"] == "Bob"


def test_evidence_dedup():
    from server.evidence import Candidate, select
    cands = [Candidate("search", "shot", 1.0, 2.0, "A girl walks through the meadow."),
             Candidate("search", "shot", 1.0, 2.0, "A girl walks through the meadow."),
             Candidate("fact", "character", 5.0, 5.0, "Alice: walks through the meadow, fact one"),
             Candidate("fact", "character", 5.0, 5.0, "Alice: walks through the meadow, fact two")]
    kept = select(cands, "A girl walks through the meadow.", lambda _s: [])
    assert [c.text for c in kept] == ["A girl walks through the meadow.", "Alice: walks through the meadow, fact one",
                                      "Alice: walks through the meadow, fact two"]


def _views(m: Moment) -> tuple[list, list]:
    """Everything the agent can read at P as (texts, search hits)."""
    texts = [json.dumps(m.story_so_far(), ensure_ascii=False), json.dumps(m.recent_dialogue(60, at_least=4)),
             json.dumps(m.on_screen({"x": 0.2, "y": 0.3}), ensure_ascii=False), json.dumps(m.known_characters())]
    hits = [h for q in ("Bob", "wolf", "meadow", "Hello Bob", "tree", "Bobby") for h in m.search(q)]
    return texts + [h.text or "" for h in hits], hits


@pytest.mark.parametrize("lang", ["en", "zh"])
def test_sweep_views_never_leak(title, lang):
    for i in range(0, 200):
        P = i / 2
        m = Moment(title, P, lang)
        texts, hits = _views(m)
        for text in texts:
            assert oracle_leaks(m.pack, text, P, prose=False) == [], (P, text)
        ok = {(it.item_type, it.item_id) for it in Gate(m.pack, P, allow_auto=True).eligible()}
        assert all((h.item_type, h.item_id) in ok for h in hits), P


def test_limiter(monkeypatch):
    from server import config, main
    monkeypatch.setattr(config, "ASK_PER_MINUTE", 2)
    lim = main.Limiter()
    assert lim.check("1.2.3.4") is None and lim.check("1.2.3.4") is None
    hit = lim.check("1.2.3.4")
    assert hit["limit"] == "per_minute" and hit["scope"] == "ip" and 1 <= hit["retry_after_s"] <= 60
    assert lim.check("5.6.7.8") is None
    monkeypatch.setattr(config, "ASK_PER_MINUTE", 100)
    monkeypatch.setattr(config, "ASK_PER_DAY_TOTAL", 3)
    hit = lim.check("9.9.9.9")
    assert hit["limit"] == "daily" and hit["scope"] == "total"


def test_429_names_the_limit_and_bench_token_skips_it(title, monkeypatch):
    from server import config, main
    from server.tests.conftest import wire
    w = wire(monkeypatch, title)
    w.ctx.script = [{"text": "Alice, in her red coat."}]
    monkeypatch.setattr(config, "ASK_PER_MINUTE", 0)
    c = w.client
    body = {"title_id": "mini", "p": 30, "question": "Who is that?"}
    r = c.post("/api/ask", json=body)
    assert r.status_code == 429 and r.json()["limit"] == "per_minute" and r.headers["retry-after"]
    assert c.post("/api/ask", json=body, headers={main.BENCH_HEADER: "anything"}).status_code == 429   # no token set
    monkeypatch.setattr(config, "BENCH_TOKEN", "s3cret")
    assert c.post("/api/ask", json=body, headers={main.BENCH_HEADER: "wrong"}).status_code == 429
    r = c.post("/api/ask", json=body, headers={main.BENCH_HEADER: "s3cret"})
    assert r.status_code == 200 and "event: answer" in r.text
    # bench requests may pick the flow
    w.tools.script = [{"text": "Alice, in her red coat."}]
    r = c.post("/api/ask", json=body, headers={main.BENCH_HEADER: "s3cret", "x-watch-flow": "tools"})
    assert '"flow": "tools"' in r.text and not w.tools.script
