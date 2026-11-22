"""The answer path end to end with scripted models: context injection, streaming through the per-sentence guard,
retry / fallback, tool budget, deadlines, history across seeks, highlight truthfulness, the tools flow."""
import asyncio
import json
import logging
import time

import pytest

from ingest.retrieval import Gate, OnlineEmbedder
from server import config, guard, main
from server.answer import GuardedAnswer, split_ready, split_sentences
from server.knowledge import Moment
from server.tests.fakes import request_images, request_text, tools_off

TAP_WOLF = {"x": 0.2, "y": 0.3}          # inside the wolf's box [100, 50, 600, 400] (keyframe 60 s)


def kinds(events):
    return [e for e, _ in events]


def data(events, name):
    return [d for e, d in events if e == name]


def answer(events):
    return data(events, "answer")[0]


def sent_text(events) -> str:
    return "".join(d["text"] for d in data(events, "delta"))


# ── sentence splitting and the per-sentence guard ───────────────────

def test_split_english_and_chinese():
    assert split_ready("That is Sintel. She is 3.5 m tall! Dr. Lee says e.g. this") == (
        ["That is Sintel.", " She is 3.5 m tall!"], " Dr. Lee says e.g. this")
    assert split_ready("她是辛特尔。她在找“鳞片”。然后") == (["她是辛特尔。", "她在找“鳞片”。"], "然后")
    assert split_ready("Wait.") == ([], "Wait.")                        # unconfirmed until more text or the end
    assert split_sentences("One.\nTwo") == ["One.", "Two"]


def test_guarded_answer_drops_only_the_bad_sentence_and_joins_exactly():
    g = GuardedAnswer(lambda t: [{"kind": "x", "term": "Bob"}] if "Bob" in t else [])
    out = []
    for ch in ["That is Al", "ice. Bob is", " here. Nice", " coat!"]:
        out += g.feed(ch)
    out += g.flush()
    assert out == ["That is Alice.", " Nice coat!"] and g.text == "".join(out) and len(g.dropped) == 1
    zh = GuardedAnswer(lambda t: [])
    assert zh.feed("她是爱丽丝。她穿着红外套。") + zh.flush() == ["她是爱丽丝。", "她穿着红外套。"]


def test_guard_sees_terms_across_sentences():
    g = GuardedAnswer(lambda t: [{"kind": "canary", "term": "turns out"}] if "turns out" in t.lower() else [])
    assert g.feed("It turns. Out of nowhere, a wolf. ") == ["It turns.", " Out of nowhere, a wolf."]
    g2 = GuardedAnswer(lambda t: [{"kind": "canary", "term": "a. b"}] if "a. b" in t else [])
    assert g2.feed("This is a. b is next. ") == ["This is a."]          # the joined text is what is checked


# ── context flow ─────────────────────────────────────────────────────

def test_everyday_question_needs_one_model_call(wired):
    wired.ctx.script = [{"text": ["That is the wo", "lf, grey and ", "watchful. It came out of the forest."]}]
    ev = wired.ask(61.2, "Who is that?", tap=TAP_WOLF)
    assert kinds(ev)[0] == "gate" and kinds(ev)[-1] == "done"
    assert [d["id"] for d in data(ev, "tool")] == ["ctx-1", "ctx-2", "ctx-3"]
    assert [d["name"] for d in data(ev, "tool_result")] == ["on_screen", "recent_dialogue", "story_so_far"]
    assert data(ev, "tool_result")[0]["summary"] == "Tapped: the wolf"
    assert [d["text"] for d in data(ev, "delta")] == ["That is the wolf, grey and watchful.", " It came out of the forest."]
    a = answer(ev)
    assert a["text"] == sent_text(ev) and a["flow"] == "context"
    assert a["usage"] == {"input_tokens": 1000, "output_tokens": 25, "thought_tokens": 5, "model_calls": 1}
    assert a["timing"]["first_text_ms"] <= a["timing"]["total_ms"]
    # everything was in the first (and only) request: context, the paused frame with the tap ring, the question
    req = wired.ctx.requests[0]
    assert len(wired.ctx.requests) == 1 and req["stream"]
    text = request_text(req)
    assert "ON SCREEN" in text and "the wolf" in text and "TAP: " in text and "LAST LINES SPOKEN" in text
    assert "STORY SO FAR" in text and text.rstrip().endswith("Who is that?")
    assert len(request_images(req)) == 1


def test_throwaway_sessions_are_deleted(wired):
    wired.ctx.script = [{"text": "Fine."}]
    wired.ask(30.0, session_id="s-del")
    assert not main.session_service.sessions.get(main.APP_NAME, {}).get("s-del:mini")


def test_violating_sentence_never_leaves_the_server(wired):
    # "turns out" is a canary for the reveal at 62 s (+3)
    wired.ctx.script = [{"text": ["That is the wolf. It turns out to be ", "friendly. Keep watching."]}]
    ev = wired.ask(61.2, "Who is that?", tap=TAP_WOLF)
    assert [d["text"] for d in data(ev, "delta")] == ["That is the wolf.", " Keep watching."]
    assert "turns out" not in json.dumps(ev)
    a = answer(ev)
    assert a["guard"] == {"checked": True, "rewritten": True, "dropped": 1} and a["text"] == sent_text(ev)


def test_nothing_survives_then_one_retry(wired):
    wired.ctx.script = [{"text": "That's Bob."},                          # Bob's name is only visible from 11 s
                        lambda req: {"text": "That's the boy, waving." if "Bob" in request_text({"contents": req.contents})
                                     else "no note"}]
    ev = wired.ask(8.0, "Who is he?")
    assert sent_text(ev) == "That's the boy, waving." and "Bob" not in json.dumps(ev)
    assert not wired.ctx.requests[1]["stream"]                            # the retry is not streamed
    assert answer(ev)["guard"]["rewritten"]


def test_retry_fails_too_gives_the_fixed_fallback(wired):
    wired.ctx.script = [{"text": "That's Bob."}, {"text": "Still Bob."}]
    ev = wired.ask(8.0, "Who is he?")
    assert [d["text"] for d in data(ev, "delta")] == [main.FALLBACK["en"]]
    assert answer(ev)["text"] == main.FALLBACK["en"] and "Bob" not in json.dumps(ev)


def test_tool_rounds_are_capped(wired):
    def step(req):
        return {"text": "Done looking."} if tools_off(req) else {"call": ("search_moments", {"query": "meadow"})}
    wired.ctx.script = [step, step, step, step]
    ev = wired.ask(70.0, "What happened in the meadow?")
    assert [d["name"] for d in data(ev, "tool")] == ["on_screen", "recent_dialogue", "story_so_far",
                                                     "search_moments", "search_moments"]
    assert [tools_off(r) for r in wired.ctx.requests] == [False, False, True]
    assert answer(ev)["usage"]["model_calls"] == 3 and sent_text(ev) == "Done looking."
    assert "matches" in data(ev, "tool_result")[-1]["summary"]


def test_text_before_a_tool_call_is_held_back(wired, caplog):
    wired.ctx.script = [{"pre": "Let me check.", "call": ("search_moments", {"query": "tree"})},
                        {"text": "Bob climbed a tree in the meadow."}]
    with caplog.at_level(logging.INFO, logger="watch"):
        ev = wired.ask(25.0, "What did he climb?")
    assert sent_text(ev) == "Bob climbed a tree in the meadow."
    line = json.loads(next(r.getMessage() for r in caplog.records if r.getMessage().startswith("ask "))[4:])
    assert line["held_preamble"] == 1 and line["leaked_preamble"] == 0
    assert line["tools"] == ["ctx:on_screen", "ctx:recent_dialogue", "ctx:story_so_far", "search_moments"]
    assert line["search_mode"] == "fts" and line["usage"]["model_calls"] == 2


def test_deadline_gives_a_timely_fallback(wired, monkeypatch):
    monkeypatch.setattr(config, "STEP_TIMEOUT_S", 0.3)
    wired.ctx.script = [{"text": "Too slow.", "sleep": 3}]
    t0 = time.monotonic()
    ev = wired.ask(30.0)
    assert time.monotonic() - t0 < 2.0
    assert sent_text(ev) == main.TIMEOUT_FALLBACK["en"] and kinds(ev)[-2:] == ["answer", "done"]


def test_stall_mid_answer_keeps_complete_sentences(wired, monkeypatch):
    monkeypatch.setattr(config, "STEP_TIMEOUT_S", 0.3)
    wired.ctx.script = [{"text": ["That is the wolf. It", " is grey."], "gap": 3}]
    ev = wired.ask(61.2, tap=TAP_WOLF)
    assert sent_text(ev) == "That is the wolf." and answer(ev)["text"] == "That is the wolf."


def test_model_error_before_any_text_is_an_error_event(wired):
    def boom(_req):
        raise RuntimeError("quota")
    wired.ctx.script = [boom]
    ev = wired.ask(30.0)
    assert kinds(ev)[-2:] == ["error", "done"] and "quota" not in json.dumps(ev)


def test_history_follows_seeks(wired):
    wired.ctx.script = [{"text": "Answer one."}, {"text": "Answer two."}, {"text": "Answer three."},
                        {"text": "Answer four."}]
    wired.ask(30.0, "First question?", session_id="s1")
    wired.ask(40.0, "Second question?", session_id="s1")
    assert "[asked at 00:30] First question?" in request_text(wired.ctx.requests[1])
    assert "Answer one." in request_text(wired.ctx.requests[1])
    wired.ask(20.0, "After a seek back?", session_id="s1")               # turns asked after 20 s are hidden
    assert "First question" not in request_text(wired.ctx.requests[2])
    wired.ask(35.0, "Caught up?", session_id="s1")
    t = request_text(wired.ctx.requests[3])
    assert "First question?" in t and "Second question" not in t and "After a seek back?" in t


def test_history_holds_what_was_sent_not_the_draft(wired):
    wired.ctx.script = [{"text": "That is the wolf. It turns out to be friendly."}, {"text": "Ok."}]
    wired.ask(61.2, "Who?", session_id="s2", tap=TAP_WOLF)
    wired.ask(62.0, "And now?", session_id="s2")
    t = request_text(wired.ctx.requests[1])
    assert "That is the wolf." in t and "turns out" not in t


# ── highlight ────────────────────────────────────────────────────────

def test_highlight_early_and_in_answer_when_fresh_and_mentioned(wired):
    wired.ctx.script = [{"text": "That is the wolf."}]
    ev = wired.ask(61.2, tap=TAP_WOLF)                                    # floor(P) = 61, box measured at 60
    hl = data(ev, "highlight")
    assert hl and hl[0]["label"] == "the wolf" and hl[0]["frame_t"] == 60
    assert kinds(ev).index("highlight") < kinds(ev).index("delta")
    assert answer(ev)["highlight"]["label"] == "the wolf"


def test_no_early_highlight_for_a_stale_box(wired):
    wired.ctx.script = [{"text": "That is the wolf."}]
    ev = wired.ask(63.5, tap=TAP_WOLF)                                    # box 3 s older than the paused frame
    assert not data(ev, "highlight") and answer(ev)["highlight"]["label"] == "the wolf"
    assert "measured 3 s before the pause" in request_text(wired.ctx.requests[0])


def test_no_highlight_when_the_answer_is_about_someone_else(wired):
    wired.ctx.script = [{"text": "That is Alice, in her red coat."}]
    ev = wired.ask(61.2, tap=TAP_WOLF)
    assert answer(ev)["highlight"] is None


def test_zh_labels_in_context_and_highlight(wired):
    wired.ctx.script = [{"text": "那是狼。"}]
    ev = wired.ask(61.2, "那是谁?", tap=TAP_WOLF, lang="zh")
    assert data(ev, "highlight")[0]["label"] == "狼" and answer(ev)["highlight"]["label"] == "狼"
    assert "1. 狼 (character" in request_text(wired.ctx.requests[0])


# ── tools flow (kept selectable for the benchmark) ───────────────────

def test_tools_flow_still_works(wired, monkeypatch):
    monkeypatch.setattr(config, "FLOW", "tools")
    wired.tools.script = [{"call": ("on_screen", {})}, {"text": "That is the wolf."}]
    ev = wired.ask(61.2, tap=TAP_WOLF)
    assert [d["name"] for d in data(ev, "tool")] == ["on_screen"]
    assert data(ev, "delta") == [{"text": "That is the wolf."}]
    a = answer(ev)
    assert a["flow"] == "tools" and a["highlight"]["label"] == "the wolf" and a["usage"]["model_calls"] == 2


def test_tools_flow_guard_retry(wired, monkeypatch):
    monkeypatch.setattr(config, "FLOW", "tools")
    wired.tools.script = [{"text": "That's Bob."}, {"text": "That's the boy."}]
    ev = wired.ask(8.0)
    assert sent_text(ev) == "That's the boy." and answer(ev)["guard"]["rewritten"]


# ── the sweep: nothing ineligible anywhere, at any P ─────────────────

LEAKY = ("That is Bob. Bobby waves. It will howl. The wolf turns out to be Bob. Jane Realperson voices her. "
         "原来狼就是鲍勃。")


@pytest.mark.parametrize("lang", ["en", "zh"])
def test_sweep_context_evidence_and_deltas(wired, lang):
    title_pack = Moment(main.STORE.get("mini"), 0).pack
    for i in range(0, 34):
        P = i * 3.0
        wired.ctx.script = [{"text": LEAKY}, {"text": LEAKY}]
        n = len(wired.ctx.requests)
        ev = wired.ask(P, "What is going on?", tap=TAP_WOLF, lang=lang)
        ctx_text = request_text(wired.ctx.requests[n]) if len(wired.ctx.requests) > n else ""
        a = answer(ev)
        shown = [sent_text(ev), a["text"], json.dumps(a["evidence"], ensure_ascii=False),
                 json.dumps(data(ev, "tool_result"), ensure_ascii=False), ctx_text]
        for text in shown:
            assert guard.violations(title_pack, text, P) == [], (P, text[:300])
        # no text of an ineligible row reaches the model
        g = Gate(title_pack, P, allow_auto=True)
        ok = {it.text for it in g.eligible() if it.text}
        for it in g.candidates():
            if it.text and it.text not in ok and len(it.text) > 12:
                assert it.text not in ctx_text, (P, it.item_type, it.item_id)


# ── bounded query embedding ──────────────────────────────────────────

def test_online_embedder_times_out_fails_over_and_caches(monkeypatch):
    monkeypatch.delenv("CEDAR_EMBED", raising=False)

    async def slow(_t):
        await asyncio.sleep(5)

    async def boom(_t):
        raise PermissionError("PERMISSION_DENIED")

    calls = []

    async def ok(t):
        calls.append(t)
        return [3.0, 4.0]

    async def run():
        e = OnlineEmbedder(timeout_s=0.1, cooldown_s=60, embed=slow)
        t0 = time.monotonic()
        assert await e.vector("q") is None and time.monotonic() - t0 < 1
        e._embed = ok
        assert await e.vector("q") is None and not calls                  # cooling down: no call at all
        e2 = OnlineEmbedder(embed=boom)
        assert await e2.vector("q") is None and "PERMISSION_DENIED" in e2.last_error
        e3 = OnlineEmbedder(embed=ok)
        v = await e3.vector("q")
        assert abs(float(v[0]) - 0.6) < 1e-6 and await e3.vector("q") is v and len(calls) == 1
    asyncio.run(run())


def test_search_falls_back_to_full_text_without_vectors(title):
    hits = Moment(title, 30.0).search("meadow", qvec=None)
    assert hits and all(h.fts_rank for h in hits)


# ── prompts ──────────────────────────────────────────────────────────

def test_prompt_dir_without_a_new_prompt_falls_back(tmp_path, monkeypatch):
    from server import prompts
    for n in ("agent_instruction", "look_closer", "guard_retry"):
        (tmp_path / f"{n}.txt").write_text((prompts.DEFAULT_DIR / f"{n}.txt").read_text())
    monkeypatch.setenv(prompts.DIR_ENV, str(tmp_path))
    prompts._load.cache_clear()
    try:
        assert "{title}" not in prompts.render("agent_context", title="T", position="01:00", phase="p", lang="English",
                                               characters="- x")
    finally:
        monkeypatch.delenv(prompts.DIR_ENV)
        prompts._load.cache_clear()


def test_blank_cues_never_count(wired):
    title = main.STORE.get("mini")
    pack = title.pack
    blank = {**pack["cues"][0], "cue_id": "en.official:099", "start_s": 40.0, "end_s": 41.0, "text": "   "}
    pack["cues"].append(blank)
    try:
        assert "en.official:099" not in {it.item_id for it in Gate(pack, 80.0, allow_auto=True).candidates()}
        m = Moment(title, 45.0)
        assert all(ln["text"].strip() for ln in m.recent_dialogue(240))
        assert [ln["at"] for ln in m.recent_dialogue(10, at_least=1)] == ["00:20"]     # the last non-blank line
        wired.ctx.script = [{"text": "Quiet for now."}]
        wired.ask(45.0, "What did she just say?")
        assert '[00:40]' not in request_text(wired.ctx.requests[0])
    finally:
        pack["cues"].remove(blank)
