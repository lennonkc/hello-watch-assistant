"""Leak and safety fixes: Latin terms inside Chinese text, identity co-occurrence, guarded tool arguments, text written
before a tool call, the deadline, the client address behind Google's front end, long-film timestamps, box freshness
across a cut, and the cost of the per-question build on a long film."""
import json
import time

import pytest

from server import context as C
from server import guard, main
from server.knowledge import Moment, secs, ts
from server.tests.fakes import oracle_leaks, request_text

TAP_WOLF = {"x": 0.2, "y": 0.3}          # inside the wolf's box [100, 50, 600, 400] (keyframe 60 s)


def data(events, name):
    return [d for e, d in events if e == name]


def answer(events):
    return data(events, "answer")[0]


def sent(events) -> str:
    return "".join(d["text"] for d in data(events, "delta"))


def kinds_of(hits):
    return [h["kind"] for h in hits]


# ── 1.2 Latin terms inside Chinese text ──────────────────────────────

def test_latin_terms_inside_chinese_text(title):
    m = Moment(title, 8.0, "zh")                                # Bob's name is visible from 10 + 1 s
    assert [h["term"] for h in guard.violations(m.pack, "那是Bob。", 8.0)] == ["Bob"]
    assert kinds_of(guard.violations(m.pack, "由Jane Realperson配音。", 8.0)) == ["real_name"]
    assert guard.violations(m.pack, "Bobsled和Bob_1不是名字。", 8.0) == []          # glued to Latin letters / _
    assert m.sanitize("那是Bob。") == "那是男孩。"                 # zh label inside Chinese text
    assert m.sanitize("Then Bob waved.") == "Then the boy waved."
    assert C.mentions("那是Alice。", ["Alice"]) and not C.mentions("那是Alicea。", ["Alice"])
    k = next(c for c in m.pack["canaries"] if c["term"] == "howl")
    assert kinds_of(guard.violations(m.pack, "它会howl的。", k["active_until_s"] - 1)) == ["canary:identity_clue"]


def test_oracle_catches_what_the_old_matcher_missed(title):
    assert "Bob" in oracle_leaks(title.pack, "那是Bob。", 8.0)


# ── 1.1 identity co-occurrence ───────────────────────────────────────

def test_cooccurrence_per_sentence(title):
    m = Moment(title, 59.0)                                     # the wolf is Bob: revealed at 60.5 (+1), pending
    links = guard.identity_links(m)
    assert [(lk.a, lk.b) for lk in links] == [("c03", "c02")] and not links[0].focus
    check = lambda t: kinds_of(guard.violations(m.pack, t, 59.0, links))       # noqa: E731
    assert check("The wolf is Bob.") == ["identity_cooccurrence"]
    assert check("That's the wolf, and Bob is far away.") == ["identity_cooccurrence"]
    assert check("狼就是鲍勃。") == ["identity_cooccurrence"]
    assert check("That is the wolf. Bob is at home.") == []                   # different sentences, no focus
    after = Moment(title, 70.0)                                               # revealed: nothing pending
    assert guard.identity_links(after) == []


def test_cooccurrence_when_the_answer_is_about_the_a_side(title):
    m = Moment(title, 61.2)                                     # the wolf's box (keyframe 60 s) is usable; still pending
    tapped = guard.identity_links(m, C.focus(m, m.on_screen(TAP_WOLF, refs=True)))
    asked = guard.identity_links(m, question="Is the wolf Bob?")
    on_screen = guard.identity_links(m, C.focus(m, m.on_screen(None, refs=True)))
    assert tapped[0].focus and asked[0].focus
    assert on_screen[0].focus                     # the wolf is on screen and Bob (the b-side) is not
    both = {"entities": [{"ref": ("character", "c03")}, {"ref": ("character", "c02")}]}
    assert C.focus(m, both) == frozenset()        # both sides on screen: only the per-sentence rule
    for links in (tapped, asked):
        assert kinds_of(guard.violations(m.pack, "That is the wolf. Bob is at home.", 61.2, links)) == [
            "identity_cooccurrence"]
        assert kinds_of(guard.violations(m.pack, "The film hasn't said whether it is Bob.", 61.2, links)) == [
            "identity_cooccurrence"]
        assert guard.violations(m.pack, "That is the wolf, grey and watchful.", 61.2, links) == []


def test_cooccurrence_end_to_end(wired):
    wired.ctx.script = [{"text": "That is the wolf, grey and watchful. Bob would be scared of it."}]
    ev = wired.ask(61.2, "Who is that?", tap=TAP_WOLF)
    assert sent(ev) == "That is the wolf, grey and watchful." and "Bob" not in json.dumps(ev)
    assert answer(ev)["guard"]["dropped"] == 1


# ── 1.3 tool arguments are model-written ─────────────────────────────

def test_tool_args_are_sanitised_and_guarded(wired):
    wired.ctx.script = [{"call": ("search_moments", {"query": "Bob in the meadow"})}, {"text": "He was in the meadow."}]
    ev = wired.ask(8.0, "Where was he?")
    assert data(ev, "tool")[-1]["args"] == {"query": "The boy in the meadow"}
    wired.ctx.script = [{"call": ("search_moments", {"query": "Jane Realperson"})}, {"text": "I only talk about the film."}]
    ev = wired.ask(8.0, "Who voices her?")
    assert data(ev, "tool")[-1]["args"] == {} and "Realperson" not in json.dumps(ev)


# ── 1.4 text before a tool call never counts as the answer ──────────

def test_preamble_alone_is_never_the_answer(wired, caplog):
    import logging
    wired.ctx.script = [{"pre": "Let me check that. One moment please.", "call": ("search_moments", {"query": "x"})},
                        {"text": ""}, {"text": ""}]
    with caplog.at_level(logging.INFO, logger="watch"):
        ev = wired.ask(30.0, "What happened?", session_id="pre")
    a = answer(ev)
    assert a["text"] == main.FALLBACK["en"] and a["guard"]["rewritten"]
    assert "Let me check" not in a["text"]
    line = json.loads(next(r.getMessage() for r in caplog.records if r.getMessage().startswith("ask "))[4:])
    assert line["preamble_only"] and line["leaked_preamble"] == 1
    wired.ctx.script = [{"text": "Ok."}]
    wired.ask(31.0, "And now?", session_id="pre")
    assert "Let me check" not in request_text(wired.ctx.requests[-1])          # not in the history either


def test_preamble_is_left_out_of_the_final_text(wired):
    wired.ctx.script = [{"pre": "Let me check that. One moment please.", "call": ("search_moments", {"query": "x"})},
                        {"text": "Alice met the boy in a meadow."}]
    ev = wired.ask(70.0, "What happened?")
    assert sent(ev).startswith("Let me check that.")                         # it was already on screen …
    assert answer(ev)["text"] == "Alice met the boy in a meadow."            # … the answer event replaces it


# ── 2.3 hard deadline keeps complete sentences ───────────────────────

def test_deadline_keeps_a_finished_last_sentence(wired, monkeypatch):
    from server import config
    monkeypatch.setattr(config, "ASK_DEADLINE_S", 0.5)
    wired.ctx.script = [{"text": ["That is the wolf.", " It is grey."], "gap": 2}]
    t0 = time.monotonic()
    ev = wired.ask(61.2, tap=TAP_WOLF)
    assert time.monotonic() - t0 < 1.5
    assert sent(ev) == "That is the wolf." and answer(ev)["text"] == "That is the wolf."


def test_look_closer_sends_at_most_eight_stills(wired, monkeypatch):
    from server import agent as A

    class Seen:
        text, usage_metadata = "A grey wolf.", None
    seen = []

    async def vision(parts):
        seen.append(sum(1 for p in parts if p.inline_data))
        return Seen()
    monkeypatch.setattr(A, "vision_generate", vision)
    wired.ctx.script = [{"call": ("look_closer", {"question": "what moved"})}, {"text": "The wolf moved."}]
    wired.ask(61.2, "What moved?")
    assert 2 <= seen[0] <= 9                                                   # ≤ 8 stills + the paused frame


# ── 1.6 the client address ───────────────────────────────────────────

def test_client_ip_is_the_hop_google_appends():
    class R:
        def __init__(self, fwd):
            self.headers = {"x-forwarded-for": fwd} if fwd else {}
            self.client = type("C", (), {"host": "10.0.0.1"})()
    assert main.client_ip(R("6.6.6.6, 203.0.113.9")) == "203.0.113.9"         # the first hop is client-written
    assert main.client_ip(R("203.0.113.9")) == "203.0.113.9"
    assert main.client_ip(R(None)) == "10.0.0.1"


# ── 2.1 long films ───────────────────────────────────────────────────

def test_timestamps_past_one_hour():
    assert [ts(t) for t in (59.9, 3599, 3600, 6005.3, 10140)] == ["00:59", "59:59", "1:00:00", "1:40:05", "2:49:00"]
    for t in (0, 59, 3599, 3600, 6005, 10140):
        assert secs(ts(t)) == t


# ── 2.4 a box from the shot before the cut is not fresh ──────────────

def test_box_from_the_previous_shot_is_not_fresh(wired):
    pack = main.STORE.get("mini").pack
    shot = {"shot_caption_id": "sc002", "start_s": 61.0, "end_s": 70.0, "reveal_at_s": 70.0, "source": "t",
            "caption_en": "Trees.", "caption_zh": None, "review_state": "auto"}
    ent = {"shot_caption_id": "sc002", "seq": 0, "entity_type": "object", "character_id": None, "object_id": None,
           "label_en": "trees", "label_zh": None, "position": "center", "bbox": "[0, 0, 900, 900]", "bbox_t_s": 63,
           "action_en": None, "action_zh": None}
    pack["shot_captions"][1]["end_s"] = 61.0
    pack["shot_captions"].append(shot)
    pack["shot_entities"].append(ent)
    try:
        wired.ctx.script = [{"text": "That is the wolf."}]
        ev = wired.ask(61.4, tap=TAP_WOLF)                 # 0.4 s after a cut: the boxes are the previous shot's
        assert not data(ev, "highlight")
        assert "measured in the shot before this one" in request_text(wired.ctx.requests[0])
    finally:
        pack["shot_captions"].remove(shot)
        pack["shot_entities"].remove(ent)
        pack["shot_captions"][1]["end_s"] = 100.0


# ── 2.2 the per-question build on a long film ────────────────────────

def test_per_question_build_is_fast_on_a_long_film(title):
    from server import agent as A
    p = title.pack
    base, ev0 = p["cues"][0], p["events"][0]
    p["cues"] += [{**base, "cue_id": f"en.official:{i:05d}", "start_s": 100 + i, "end_s": 100.8 + i,
                   "text": f"Line {i} is spoken here."} for i in range(10_000)]
    p["shot_captions"] += [{"shot_caption_id": f"sx{i:05d}", "start_s": 100 + i * 3.3, "end_s": 103.3 + i * 3.3,
                            "reveal_at_s": 103.3 + i * 3.3, "source": "t", "caption_en": f"Shot {i}.", "caption_zh": None,
                            "review_state": "auto"} for i in range(3_000)]
    p["events"] += [{**ev0, "event_id": f"ex{i:05d}", "t_s": 100 + i * 10.0, "reveal_at_s": 101 + i * 10.0}
                    for i in range(1_000)]
    p["asset"]["duration_s"] = p["asset"]["story_end_s"] = 10_200.0
    Moment(title, 5000.0).gate_counts()                                       # the once-per-pack index
    worst = 0.0
    for P in (6005.3, 9000.7, 3600.0):
        t0 = time.perf_counter()
        m = Moment(title, P)
        m.gate_counts()
        C.build(m, {"x": 0.5, "y": 0.5}, A.Turn())
        worst = max(worst, time.perf_counter() - t0)
    assert worst < 0.15, worst
    assert "1:40:05" in C.build(Moment(title, 6005.3), None, A.Turn()).text



# ── E1–E3: sources shown with the answer ─────────────────────────────

def test_sources_are_what_the_answer_rests_on_and_pass_the_guard(title):
    from server import evidence as E
    m = Moment(title, 61.2)
    links = guard.identity_links(m, question="Who is the wolf?")               # the answer is about the a-side
    answer_text = "The wolf watches the meadow from the trees."
    check = lambda s: guard.source_violations(m.pack, s, 61.2, links, answer_text)   # noqa: E731
    cands = [E.Candidate("line", "line", 20, 20, "Bob: The wolf watches the meadow."),
             E.Candidate("line", "line", 21, 21, "Alice: The wolf watches the meadow."),
             E.Candidate("story", "event", 30, 30, "Alice crosses a river.", frame_at=30)]
    assert [c.text for c in E.select(cands, answer_text, check)] == ["Alice: The wolf watches the meadow."]
    many = [E.Candidate("line", "line", i, i, f"Alice sees the wolf by the meadow trees {i}.") for i in range(9)]
    assert len(E.select(many, answer_text, check)) == E.CAP
    assert E.select(cands, "The film hasn't shown that yet.", check) == []
    assert E.frames(m, [], "The film hasn't shown that yet.", check) == []
    # an answer naming the b-side: no source may refer to the a-side
    about_b = "Bob climbed a tree in the meadow."
    check_b = lambda s: guard.source_violations(m.pack, s, 61.2, guard.identity_links(m), about_b)   # noqa: E731
    assert check_b("The wolf came out of the forest.") and not check_b("Alice climbed a tree in the meadow.")


def test_who_is_this_a_side_on_screen_shows_no_b_side_source(wired):
    # like c5:sintel-g03: only the a-side is identified as the subject; eligible lines name the b-side
    wired.ctx.script = [{"text": "That is the wolf, grey and watchful, watching the meadow."}]
    ev = wired.ask(61.2, "Who is this?")
    a = answer(ev)
    assert a["text"].startswith("That is the wolf") and "Bob" not in json.dumps(ev)


def test_recap_sentence_not_whole_recap_and_no_linked_name(wired):
    # like c5:tos-g04: "Who is the older man?" with a recap that names the b-side in another sentence
    pack = main.STORE.get("mini").pack
    recap = {"recap_id": "r02", "upto_scene_id": "s01", "upto_s": 58.0, "reveal_at_s": 58.0, "source": "t",
             "text_en": "Alice met Bob in the meadow. Then a grey wolf came out of the dark forest.", "text_zh": None,
             "review_state": "auto"}
    pack["recaps"].append(recap)
    try:
        wired.ctx.script = [{"text": "That is the grey wolf that came out of the dark forest."}]
        ev = wired.ask(61.2, "Who is the wolf?")
        assert "Alice met Bob" in request_text(wired.ctx.requests[0])          # the model had the whole recap …
        a = answer(ev)
        assert [e["text"] for e in a["evidence"]] == ["Then a grey wolf came out of the dark forest."]   # … not shown
        assert "Bob" not in json.dumps(ev)
    finally:
        pack["recaps"].remove(recap)


def test_a_refusal_shows_no_sources(wired):
    wired.ctx.script = [{"text": "The film hasn't shown that yet."}]
    ev = wired.ask(61.2, "What happens to Alice later?")
    assert answer(ev)["evidence"] == [] and answer(ev)["frames"] == []
