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


def links_for(m, tap=None, question=""):
    tapped, screen = C.focus(m.on_screen(tap, refs=True))
    return guard.identity_links(m, tapped, question, screen)


def test_cooccurrence_when_the_answer_is_about_the_a_side(title):
    m = Moment(title, 61.2)                                     # the wolf's box (keyframe 60 s) is usable; still pending
    tapped = links_for(m, TAP_WOLF, "Who is that?")
    asked = links_for(m, None, "Who is the wolf?")
    pointed = links_for(m, None, "Who is this?")            # the wolf is on screen, Bob is not, the question points
    assert tapped[0].focus and asked[0].focus and pointed[0].focus
    missed = links_for(m, None, "What did I miss?")         # on screen but not asked about: per passage only
    assert not missed[0].focus and missed[0].present
    both = guard.identity_links(m, (), "Who is this?", frozenset({"c03", "c02"}))
    assert not both[0].focus                                # both sides on screen: only the per-passage rule
    for links in (tapped, asked, pointed):
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
    assert a["text"] == main.TIMEOUT_FALLBACK["en"] and a["guard"]["rewritten"]     # nothing was held back: neutral
    assert "Let me check" not in json.dumps(ev)                                       # held, never sent
    line = json.loads(next(r.getMessage() for r in caplog.records if r.getMessage().startswith("ask "))[4:])
    assert line["held_preamble"] == 1 and line["leaked_preamble"] == 0
    wired.ctx.script = [{"text": "Ok."}]
    wired.ask(31.0, "And now?", session_id="pre")
    assert "Let me check" not in request_text(wired.ctx.requests[-1])          # not in the history either


def test_preamble_is_left_out_of_the_final_text(wired):
    wired.ctx.script = [{"pre": "Here is what I know. Let me check the rest.", "call": ("search_moments", {"query": "x"})},
                        {"text": "Alice met the boy in a meadow."}]
    ev = wired.ask(70.0, "What happened?")
    assert sent(ev).startswith("Here is what I know.")                       # it was already on screen …
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

def test_client_ip_is_the_hop_google_appends(monkeypatch):
    from starlette.datastructures import Headers

    from server import config

    class R:
        def __init__(self, *fwd):
            self.headers = Headers(raw=[(b"x-forwarded-for", f.encode()) for f in fwd])
            self.client = type("C", (), {"host": "10.0.0.1"})()
    assert main.client_ip(R("6.6.6.6, 203.0.113.9")) == "203.0.113.9"         # the first hop is client-written
    assert main.client_ip(R("203.0.113.9")) == "203.0.113.9"
    assert main.client_ip(R("6.6.6.6", "203.0.113.9")) == "203.0.113.9"      # two header lines are one list
    assert main.client_ip(R()) == "10.0.0.1"
    monkeypatch.setattr(config, "TRUSTED_PROXY_HOPS", 2)                      # a load balancer in front
    assert main.client_ip(R("6.6.6.6, 203.0.113.9, 10.9.9.9")) == "203.0.113.9"


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


# ── review items ─────────────────────────────────────────────────────

def test_sanitize_changes_only_the_name(title):
    m = Moment(title, 8.0)
    assert m.sanitize("Then Bob ate at the café.") == "Then the boy ate at the café."     # accents kept
    assert m.sanitize("Bob在臺北。") == "男孩在臺北。"                                        # Traditional kept
    assert m.sanitize("BOB waves.") == "BOB waves."                     # Bob is case-sensitive (a common noun)
    pack = title.pack
    mary = {**pack["characters"][0], "character_id": "c09", "canonical_name": "Mary Bob", "name_zh": None,
            "name_public": 1, "display_label_pre_name": "the woman", "display_label_pre_name_zh": "女人"}
    pack["characters"].append(mary)
    try:
        m2 = Moment(title, 8.5)
        assert m2.sanitize("Mary Bob waves at Bob.") == "Mary Bob waves at the boy."     # the longer visible name wins
    finally:
        pack["characters"].remove(mary)


def test_retry_note_names_what_was_written():
    hits = [{"kind": "canary:identity_clue", "term": r"\b(aged|older|grown up)\b", "match": "older"},
            {"kind": "name_not_yet_visible", "term": "Bob"}]
    note = main._retry_note(hits)
    assert "older" in note and "Bob" in note and "aged" not in note


def test_regex_canary_hit_carries_the_match(title):
    pack = {**title.pack, "canaries": [{"term": r"\b(aged|older)\b", "lang": "en", "match": "regex", "case_sensitive": 0,
                                        "active_until_s": 50.0, "kind": "identity_clue", "ref_type": "identity_link",
                                        "ref_id": "k01"}]}
    assert guard.violations(pack, "He looks older now.", 10.0)[0]["match"] == "older"


def test_no_subtitle_track_is_said_plainly(wired):
    pack = main.STORE.get("mini").pack
    tracks = list(pack["subtitle_tracks"])
    pack["subtitle_tracks"].clear()
    try:
        wired.ctx.script = [{"text": "There are no subtitles for this film."}]
        wired.ask(45.0, "What did she just say?")
        assert "no subtitle track" in request_text(wired.ctx.requests[0])
    finally:
        pack["subtitle_tracks"].extend(tracks)


def test_questions_without_a_session_keep_nothing(wired):
    wired.ctx.script = [{"text": "A meadow."}]
    wired.ask(30.0, "Where are we?")                                         # no session_id
    assert dict(main.HISTORY.map) == {}
    assert not any(main.session_service.sessions.get(main.APP_NAME, {}).values())


def test_answer_cut_by_the_deadline_says_so(wired, monkeypatch):
    from server import config
    monkeypatch.setattr(config, "ASK_DEADLINE_S", 0.5)
    wired.ctx.script = [{"text": ["That is the wolf. It", " is grey."], "gap": 2}]
    ev = wired.ask(61.2, tap=TAP_WOLF)
    assert answer(ev)["text"] == "That is the wolf." and answer(ev)["cut_off"] is True
    wired.ctx.script = [{"text": "That is the wolf."}]
    assert answer(wired.ask(61.2, tap=TAP_WOLF))["cut_off"] is False


# ── second pass: references, passages, both directions ──────────────

TAP_ALICE = {"x": 0.75, "y": 0.5}        # inside Alice's box [100, 600, 900, 900], next to the wolf


def test_a_side_with_any_determiner_adjective_or_pronoun(title):
    m = Moment(title, 59.0)
    links = guard.identity_links(m)
    for leak in ("That wolf is Bob.", "The grey wolf is Bob.", "Bob is a wolf now.",
                 "A grey wolf has appeared. It is Bob.", "A grey wolf has appeared. Some say it is Bob.",
                 "一只灰狼出现了。它就是鲍勃。"):
        assert kinds_of(guard.violations(m.pack, leak, 59.0, links)) == ["identity_cooccurrence"], leak
    for fine in ("That is the wolf. Bob is at home.", "Alice met Bob in the meadow. Bob climbed a tree.",
                 "A grey wolf has appeared. Alice waves at Bob."):
        assert guard.violations(m.pack, fine, 59.0, links) == [], fine
    assert links_for(Moment(title, 61.2), None, "Who is that wolf?")[0].focus
    assert links_for(Moment(title, 61.2), None, "那只狼是谁？")[0].focus


def test_a_tap_on_someone_else_keeps_the_on_screen_a_side(wired):
    m = Moment(main.STORE.get("mini"), 61.2)
    links = links_for(m, TAP_ALICE, "Who is this?")
    assert links[0].present and not links[0].focus
    leak = "That is Alice. The grey wolf next to her watches quietly. It is really Bob."
    assert kinds_of(guard.violations(m.pack, leak, 61.2, links)) == ["identity_cooccurrence"]
    wired.ctx.script = [{"text": leak}]
    ev = wired.ask(61.2, "Who is this?", tap=TAP_ALICE)
    assert "Bob" not in json.dumps(ev) and answer(ev)["text"].startswith("That is Alice.")


def test_b_side_label_and_the_reverse_direction(title):
    m = Moment(title, 59.0)
    links = guard.identity_links(m)
    for leak in ("The wolf is the boy.", "狼就是男孩。"):                 # before (or besides) b's name: its label
        assert kinds_of(guard.violations(m.pack, leak, 59.0, links)) == ["identity_cooccurrence"], leak
    m20 = Moment(title, 20.0)                                         # the wolf has not appeared; still pending
    about_bob = guard.identity_links(m20, frozenset({"c02"}), "Who is this?", frozenset({"c02"}))
    assert about_bob[0].focus_b and not about_bob[0].focus
    for leak in ("That is Bob. He will become the wolf.", "That is Bob. Later the wolf comes."):
        assert kinds_of(guard.violations(m.pack, leak, 20.0, about_bob)) == ["identity_cooccurrence"], leak
    assert guard.violations(m.pack, "That is Bob, climbing a tree.", 20.0, about_bob) == []
    assert links_for(m20, None, "What is Bob doing?")[0].focus_b


def test_on_screen_a_side_does_not_bar_b_from_other_answers(wired):
    m = Moment(main.STORE.get("mini"), 61.2)
    missed = links_for(m, None, "What did I miss?")
    assert not missed[0].focus
    wired.ctx.script = [{"text": "Alice waved at Bob in the meadow. Bob climbed a tree there."}]
    ev = wired.ask(61.2, "What did I miss?")
    a = answer(ev)
    assert a["text"] == "Alice waved at Bob in the meadow. Bob climbed a tree there." and not a["guard"]["dropped"]
    # … but a question pointing at the screen is about the wolf
    wired.ctx.script = [{"text": "That is the wolf. The film hasn't said whether it is Bob."}]
    ev = wired.ask(61.2, "Who is this?")
    assert answer(ev)["text"] == "That is the wolf." and "Bob" not in json.dumps(ev)


def test_rejected_link_never_constrains(title):
    link = title.pack["identity_links"][0]
    link["review_state"] = "rejected"
    try:
        m = Moment(title, 59.0)
        assert m.gate.pending == [] and guard.identity_links(m) == []
        assert guard.violations(m.pack, "The wolf watches Bob.", 59.0, guard.identity_links(m)) == []
    finally:
        link["review_state"] = "reviewed"


def test_sources_use_the_same_references(title):
    from server import evidence as E
    m = Moment(title, 59.0)
    links = guard.identity_links(m)
    said = "That grey wolf came out of the meadow and climbed a tree."
    check = lambda s: guard.source_violations(m.pack, s, 59.0, links, said)       # noqa: E731
    cands = [E.Candidate("story", "event", 20, 20, "Bob climbs a tree in the meadow."),
             E.Candidate("story", "event", 6, 6, "Alice waves at Bob in the meadow."),
             E.Candidate("story", "event", 30, 30, "A tree stands in the meadow.")]
    assert [c.text for c in E.select(cands, said, check)] == ["A tree stands in the meadow."]


# ── refusals, Chinese answers ────────────────────────────────────────

REFUSALS = ("We don't know yet why she left.", "The film hasn't said why.", "That hasn't been revealed yet.",
            "It isn't clear who he is.", "It's not clear yet.", "I can't say without spoiling the film.",
            "Nobody has said his name so far.", "His name is still unknown.", "We'll have to wait and see.",
            "The film doesn't tell us where he went.", "That remains to be seen.",
            "我们还不知道她为什么离开。", "影片还没有交代。", "目前还不清楚他是谁。", "这一点尚未揭晓。", "暂时无法确定。")


def test_refusals_are_decided_per_sentence():
    from server import evidence as E
    for r in REFUSALS:
        assert E.is_refusal(r), r
    assert not E.is_refusal("Alice met Bob in a meadow.") and not E.is_refusal("爱丽丝在草地上遇到了鲍勃。")
    mixed = "Alice met Bob in a meadow and Bob climbed a tree. The film hasn't said why."
    cands = [E.Candidate("story", "event", 20, 20, "Bob climbs a tree in the meadow."),
             E.Candidate("story", "event", 25, 25, "Why did he leave the house?")]
    assert [c.text for c in E.select(cands, mixed, lambda _s: [])] == ["Bob climbs a tree in the meadow."]
    assert E.select(cands, "We don't know yet why Bob climbs a tree in the meadow.", lambda _s: []) == []


def test_tools_flow_fallback_and_refusal_show_no_sources(wired, monkeypatch):
    from server import config
    monkeypatch.setattr(config, "FLOW", "tools")
    leak = "Alice waves at Bob."                                   # Bob's name is visible from 11 s
    wired.tools.script = [{"call": ("story_so_far", {})}, {"text": leak}, {"call": ("story_so_far", {})}, {"text": leak}]
    a = answer(wired.ask(8.0, "What happened?"))
    assert a["text"] == main.FALLBACK["en"] and a["evidence"] == [] and a["frames"] == []
    wired.tools.script = [{"call": ("story_so_far", {})}, {"text": "We don't know yet why Alice met the boy in a meadow."}]
    a = answer(wired.ask(70.0, "Why did she meet him?"))
    assert a["evidence"] == [] and a["frames"] == []
    wired.tools.script = [{"call": ("story_so_far", {})}, {"text": "Alice met the boy in a meadow."}]
    a = answer(wired.ask(70.0, "What happened?"))
    assert [e["text"] for e in a["evidence"]] == ["Alice met the boy in a meadow."] and a["frames"]


def test_chinese_answer_finds_english_and_chinese_sources(wired, title):
    from server import evidence as E
    m = Moment(title, 30.0, "zh")
    cands = [E.Candidate("story", "event", 10, 10, "Alice greets Bob."),
             E.Candidate("story", "event", 6, 6, "Alice waves at the girl.", text_zh="爱丽丝在草地上挥手。")]
    kept = E.select(cands, "爱丽丝和鲍勃打了招呼。", lambda _s: [], lang="zh", names=m.zh_names())
    assert [c.text for c in kept] == ["Alice greets Bob."]                       # through the names
    kept = E.select(cands, "爱丽丝在草地上挥手。", lambda _s: [], lang="zh", names=m.zh_names())
    assert [c.row("zh")["text"] for c in kept] == ["爱丽丝在草地上挥手。"]           # through its own Chinese text
    wired.ctx.script = [{"text": "爱丽丝和鲍勃打了招呼。"}]
    a = answer(wired.ask(30.0, "发生了什么？", lang="zh"))
    assert "Alice greets Bob." in [e["text"] for e in a["evidence"]]


# ── cut answers, the hold rule, the sources budget ───────────────────

def test_cut_answer_is_flagged_and_not_stored(wired, monkeypatch):
    from server import config
    monkeypatch.setattr(config, "ASK_DEADLINE_S", 0.5)
    wired.ctx.script = [{"text": ["That is the wolf. It", " is grey."], "gap": 2}]
    a = answer(wired.ask(61.2, tap=TAP_WOLF, session_id="cut"))
    assert a["cut_off"] is True and not any(main.HISTORY.map.get(k) for k in main.HISTORY.map)


def test_stall_before_a_tool_call_does_not_send_the_preamble(wired, monkeypatch):
    from server import config
    monkeypatch.setattr(config, "ASK_DEADLINE_S", 1.5)
    wired.ctx.script = [{"text": ["Let me look at the last scene.", ""], "gap": 4}]
    ev = wired.ask(61.2, "What just happened?")
    assert "Let me" not in sent(ev) and answer(ev)["text"] == main.TIMEOUT_FALLBACK["en"]
    wired.ctx.script = [{"text": ["That is the wolf in the forest.", ""], "gap": 4}]       # a real sentence stands
    assert answer(wired.ask(61.2, "What just happened?"))["text"] == "That is the wolf in the forest."


def test_a_preamble_first_sentence_waits_for_the_end_of_the_step(wired):
    wired.ctx.script = [{"text": ["Let me explain. ", "Alice met the boy in a meadow."]}]
    ev = wired.ask(70.0, "What happened?")
    assert answer(ev)["text"] == "Let me explain. Alice met the boy in a meadow."   # no tool call: it is the answer


def test_slow_or_failing_sources_never_cost_the_answer(wired, monkeypatch):
    from server import config
    from server import evidence as E
    monkeypatch.setattr(config, "SOURCES_TIMEOUT_S", 0.3)
    title = main.STORE.get("mini")
    real = type(title).prefetch

    def slow(self, ts_):
        time.sleep(1.5)
        return real(self, ts_)
    monkeypatch.setattr(type(title), "prefetch", slow)
    wired.ctx.script = [{"text": "Alice met the boy in a meadow."}]
    ev = wired.ask(70.0, "What happened?")
    a = answer(ev)
    assert a["timing"]["total_ms"] - a["timing"]["first_text_ms"] < 900      # the 1.5 s fetch is not waited for
    assert a["text"] == "Alice met the boy in a meadow." and a["frames"] == [] and a["evidence"]

    def boom(*_a, **_k):
        raise RuntimeError("no")
    monkeypatch.setattr(E, "select", boom)
    wired.ctx.script = [{"text": "Alice met the boy in a meadow."}]
    ev = wired.ask(70.0, "What happened?", session_id="s")
    assert [e for e, _d in ev][-2:] == ["answer", "done"] and not data(ev, "error")
    assert answer(ev)["evidence"] == [] and main.HISTORY.map


# ── nits ─────────────────────────────────────────────────────────────

def test_regex_canary_boundaries_work_inside_chinese(title):
    pack = {**title.pack, "canaries": [{"term": r"\b(aged|older)\b", "lang": "en", "match": "regex", "case_sensitive": 0,
                                        "active_until_s": 50.0, "kind": "identity_clue", "ref_type": "identity_link",
                                        "ref_id": "k01"}]}
    assert guard.violations(pack, "他变older了。", 10.0)[0]["match"] == "older"
    assert guard.violations(pack, "A folder and a bolder move.", 10.0) == []


def test_after_a_cut_no_highlight_and_not_on_screen(wired):
    pack = main.STORE.get("mini").pack
    shot = {"shot_caption_id": "sc002", "start_s": 61.0, "end_s": 70.0, "reveal_at_s": 70.0, "source": "t",
            "caption_en": "Trees.", "caption_zh": None, "review_state": "auto"}
    pack["shot_captions"][1]["end_s"] = 61.0
    pack["shot_captions"].append(shot)
    try:
        wired.ctx.script = [{"text": "That is the wolf."}]
        ev = wired.ask(61.4, "Who is that?", tap=TAP_WOLF)
        assert answer(ev)["highlight"] is None and not data(ev, "highlight")
        ctx = request_text(wired.ctx.requests[0])
        assert "JUST BEFORE THE CUT" in ctx and "ON SCREEN (entities identified in the current shot" not in ctx
    finally:
        pack["shot_captions"].remove(shot)
        pack["shot_captions"][1]["end_s"] = 100.0


def test_endless_tool_calls_get_the_neutral_reply(wired):
    wired.ctx.script = [{"call": ("search_moments", {"query": f"q{i}"})} for i in range(12)]
    assert answer(wired.ask(30.0, "What happened?"))["text"] == main.TIMEOUT_FALLBACK["en"]


def test_tool_args_are_guarded_whole_then_cut(wired):
    q = "x" * 117 + " Bobby climbs"
    wired.ctx.script = [{"call": ("search_moments", {"query": q})}, {"text": "He climbed."}]
    ev = wired.ask(8.0, "What did he do?")
    assert data(ev, "tool")[-1]["args"] == {"query": "x" * 117 + "…"}


def test_oracle_sees_references_not_just_labels(title):
    for leak in ("That wolf is Bob.", "The grey wolf is Bob.", "A grey wolf has appeared. It is Bob.",
                 "The wolf is the boy.", "狼就是男孩。"):
        assert oracle_leaks(title.pack, leak, 59.0), leak
    assert oracle_leaks(title.pack, "That is the wolf. Bob is at home.", 59.0) == []
