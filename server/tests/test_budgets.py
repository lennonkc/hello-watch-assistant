"""Time budgets inside one question (the guard retry, look_closer, the paused frame), spoiler probes, and the request
id that ties an answer to its log lines."""
import json
import logging
import re
import time

from server import config, main
from server import context as C
from server.tests.fakes import request_images, request_text, tools_off


def data(events, name):
    return [d for e, d in events if e == name]


def answer(events):
    return data(events, "answer")[0]


def ask_record(caplog) -> dict:
    return json.loads(next(r.getMessage() for r in caplog.records if r.getMessage().startswith("ask "))[4:])


def test_spoiler_probes_are_recognised():
    for q in ("How does it end?", "What happens next?", "Who dies?", "Will she find the dragon?", "Is the wolf actually Bob?",
              "Does the robot escape?", "结局是什么？", "接下来会发生什么？", "她会死吗？", "谁是凶手？"):
        assert C.spoiler_probe(q), q
    for q in ("Who is this?", "What did I miss?", "What did he just say?", "Who is the man at the end of the table?",
              "What is she really doing?", "Did she find the dragon?", "这是谁？", "刚才他说了什么？"):
        assert not C.spoiler_probe(q), q


def test_a_probe_gets_the_decline_note_in_its_context(wired):
    wired.ctx.script = [{"text": "That hasn't been shown yet."}]
    wired.ask(30.0, "How does it end?")
    assert "ABOUT THIS QUESTION" in request_text(wired.ctx.requests[-1])
    wired.ctx.script = [{"text": "Alice is in the meadow."}]
    wired.ask(30.0, "What did I miss?")
    assert "ABOUT THIS QUESTION" not in request_text(wired.ctx.requests[-1])


def test_a_probe_that_trips_the_guard_is_declined_at_once(wired, caplog):
    wired.ctx.script = [{"text": "In the end the wolf turns out to be Bob."}, {"text": "never asked"}]
    with caplog.at_level(logging.INFO, logger="watch"):
        ev = wired.ask(30.0, "How does it end?")
    assert answer(ev)["text"] == main.FALLBACK["en"] and len(wired.ctx.requests) == 1        # no second draft
    rec = ask_record(caplog)
    assert rec["declined"] == "probe" and not rec["retried"] and rec["probe"]


def test_the_retry_has_no_tools_and_its_own_budget(wired, monkeypatch):
    monkeypatch.setattr(config, "RETRY_TIMEOUT_S", 0.4)
    wired.ctx.script = [{"text": "Alice waves at Bob."}, {"text": "Alice waves.", "sleep": 3}]      # Bob: from 11 s
    ev = wired.ask(8.0, "What is she doing?")
    a = answer(ev)
    assert tools_off(wired.ctx.requests[1])                                   # the retry cannot call a tool
    assert a["text"] == main.FALLBACK["en"]                                   # it ran out: the decline, not a wait
    assert a["timing"]["total_ms"] < 2500, a["timing"]


def test_no_retry_when_too_little_time_is_left(wired, monkeypatch, caplog):
    monkeypatch.setattr(config, "ASK_DEADLINE_S", 2.0)
    monkeypatch.setattr(config, "RETRY_MIN_S", 1.5)
    wired.ctx.script = [{"text": "Alice waves at Bob.", "sleep": 1.0}, {"text": "never asked"}]
    with caplog.at_level(logging.INFO, logger="watch"):
        ev = wired.ask(8.0, "What is she doing?")
    assert answer(ev)["text"] == main.FALLBACK["en"] and len(wired.ctx.requests) == 1
    assert ask_record(caplog)["declined"] == "no_time"


def test_look_closer_does_not_start_without_time_for_the_answer(wired, monkeypatch):
    from server import agent as A
    calls = []

    async def vision(parts):
        calls.append(parts)
    monkeypatch.setattr(A, "vision_generate", vision)
    monkeypatch.setattr(config, "ANSWER_RESERVE_S", 9.5)                      # of a 10 s question
    wired.ctx.script = [{"call": ("look_closer", {"question": "what moved"})}, {"text": "The wolf moved."}]
    ev = wired.ask(61.2, "What moved?")
    assert not calls and answer(ev)["text"] == "The wolf moved."
    assert data(ev, "tool_result")[-1]["summary"] == "No time left to look closer"


def test_slow_stills_shrink_look_closer_instead_of_stretching_it(wired, monkeypatch):
    from server import agent as A

    class Seen:
        text, usage_metadata = "A grey wolf.", None
    seen = []

    async def vision(parts):
        seen.append(sum(1 for p in parts if p.inline_data))
        return Seen()
    real = A.read_thumbs

    def slow(title, ts_):
        if len(ts_) > 1:                                                      # the stills, not the paused frame
            time.sleep(3)
        return real(title, ts_)
    monkeypatch.setattr(A, "vision_generate", vision)
    monkeypatch.setattr(A, "read_thumbs", slow)
    monkeypatch.setattr(config, "LOOK_TIMEOUT_S", 2.0)
    wired.ctx.script = [{"call": ("look_closer", {"question": "what moved"})}, {"text": "The wolf moved."}]
    ev = wired.ask(61.2, "What moved?")
    # server time (the test client's per-request event loop waits for the abandoned thread when it closes)
    assert answer(ev)["timing"]["total_ms"] < 2500 and seen == [1]            # the paused frame from the context
    assert answer(ev)["text"] == "The wolf moved."


def test_a_slow_paused_frame_costs_the_image_not_the_answer(wired, monkeypatch, caplog):
    from server import agent as A
    real = A.read_thumb

    def slow(title, t):
        time.sleep(2)
        return real(title, t)
    monkeypatch.setattr(A, "read_thumb", slow)
    monkeypatch.setattr(config, "FRAME_WAIT_S", 0.3)
    wired.ctx.script = [{"text": "Alice is in the meadow."}]
    with caplog.at_level(logging.INFO, logger="watch"):
        ev = wired.ask(30.0, "What is going on?")
    assert answer(ev)["timing"]["total_ms"] < 1200
    assert request_images(wired.ctx.requests[-1]) == [] and answer(ev)["text"] == "Alice is in the meadow."
    assert ask_record(caplog)["frame"].startswith("skipped")


def test_request_id_in_the_answer_and_in_every_log_line(wired, caplog):
    wired.ctx.script = [{"text": "Alice waves at Bob."}, {"text": "Alice waves."}]
    with caplog.at_level(logging.INFO, logger="watch"):
        ev = wired.ask(8.0, "What is she doing?")
    rid = answer(ev)["request_id"]
    assert re.fullmatch(r"[0-9a-f]{8}", rid)
    lines = [r.getMessage() for r in caplog.records if r.name == "watch"]
    assert ask_record(caplog)["rid"] == rid
    others = [ln for ln in lines if not ln.startswith("ask ")]
    assert any("guard dropped" in ln for ln in others) and any("dropped draft sentence" in ln for ln in others)
    assert all(ln.endswith(f"[rid={rid}]") for ln in others), others
    wired.ctx.script = [{"text": "Alice waves."}]
    assert answer(wired.ask(8.0, "And now?"))["request_id"] != rid
