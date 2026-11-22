"""Offline tests for the benchmark: rule gate on the synthetic mini pack, aggregation, case lint, SSE client."""
from __future__ import annotations

import copy
import http.server
import json
import socket
import threading

import pytest

from bench import common as C
from bench import judge as J
from bench import run_bench as R
from ingest import packdb
from ingest.tests.conftest import SHA, mini_pack


@pytest.fixture
def packs(tmp_path, monkeypatch):
    root = tmp_path / "packs"
    packdb.write_db(copy.deepcopy(mini_pack()), root / SHA / "master" / "pack.db")
    monkeypatch.setattr(J, "PACKS", root)
    C._PACKS.clear()
    yield root
    C._PACKS.clear()


def case(**kw) -> dict:
    c = {"id": "t1", "content_id": "mini", "asset_sha256": SHA, "title": "Mini", "P": 20.0, "lang": "en",
         "question": "Who is that?", "category": "who_is_this", "expect": "answer", "reference": "Alice.",
         "_file": "unit"}
    c.update(kw)
    return c


# ── term matching ────────────────────────────────────────────────────

def test_marker_matching_folds_text_and_respects_word_boundaries():
    assert C.marker_hits("scales", "That's SCALES, the dragon.")
    assert not C.marker_hits("scale", "Scales")                                   # whole word
    assert C.marker_hits({"term": "Scales", "case_sensitive": True}, "Scales!")
    assert not C.marker_hits({"term": "Scales", "case_sensitive": True}, "fish scales")
    assert C.marker_hits("龙", "那是一条龙。")                                       # CJK: substring
    assert C.marker_hits("café", "the cafe")                                      # accents folded
    assert C.marker_hits("bell tower", "the bell-tower")                          # - _ whitespace collapsed
    assert C.marker_hits({"regex": r"^\s*yes\b", "ignore_case": True}, "Yes, it is.")
    assert C.canary_hits({"term": "mark*", "match": "word"}, "she was marked")
    assert not C.canary_hits({"term": "mark*", "match": "word"}, "Denmark")
    # the pack's boundary rule (ingest.textnorm.LATIN_WORD): CJK and other scripts are boundaries, Latin letters not
    assert C.marker_hits("Scales", "史科尔斯Scales的")
    assert C.marker_hits("Bob", "Bobа")                                            # Cyrillic а after it
    assert not C.marker_hits("Bob", "Bobø") and not C.marker_hits("Bob", "Bob2")
    # regex markers: \b on the same Latin word class (as the service's textnorm.latin_boundaries)
    assert C.marker_hits({"regex": r"\b(aged|older)\b"}, "他变older了") and not C.marker_hits({"regex": r"\bolder\b"}, "colder")
    assert C.marker_hits({"regex": r"\Bold"}, "bold") and not C.marker_hits({"regex": r"\Bold"}, "他old")
    assert C.latin_boundaries(r"[\b]x\\b") == r"[\b]x\\b" and C.latin_boundaries(r"[]\b]") == r"[]\b]"


# ── rule gate ────────────────────────────────────────────────────────

def test_hidden_name_is_a_leak_unless_the_viewer_typed_it(packs):
    early = case(P=5.0)
    r = J.rule_check(early, "That is Bob.")
    assert "hidden_name:Bob" in r["rule_leaks"]
    echo = J.rule_check(case(P=5.0, question="Is that Bob?"), "Yes, Bob.")
    assert echo["rule_leaks"] == [] and "hidden_name:Bob (echo)" in echo["suspects"]
    assert J.rule_check(case(P=20.0), "That is Bob.")["rule_leaks"] == []         # name visible from 11 s
    # an earlier turn of the same conversation also counts as the viewer's own words
    hist = case(P=5.0, question="What is he doing?", history=[{"question": "Is that Bob?", "P": 5.0}])
    assert J.rule_check(hist, "Bob is waving.")["rule_leaks"] == []


def test_canaries_real_names_and_case_terms(packs):
    r = J.rule_check(case(P=20.0, must_not_include=["wolf"], flag_if_any=["meadow"]),
                     "It turns out the wolf will howl. Jane Realperson voices her. A meadow.")
    assert r["rule_leaks"] == ["canary:howl", "canary:turns out", "must_not_include:wolf"]
    assert r["real_names"] == ["Jane Realperson"]
    assert r["suspects"] == ["flag:meadow"]
    assert J.rule_check(case(P=70.0), "It turns out the wolf is Bob.")["rule_leaks"] == []   # all revealed by 70 s


def test_canary_that_also_describes_eligible_text_is_only_a_suspect(packs, monkeypatch):
    monkeypatch.setattr(J, "seen_items", lambda c, P: [("event@00:10", "Alice turns out the lights.")])
    r = J.rule_check(case(P=20.0), "It turns out fine.")
    assert "canary:turns out (already eligible: event@00:10)" in r["suspects"] and r["rule_leaks"] == []


def test_already_eligible_means_one_item_the_viewer_saw(packs):
    """Not bare presence in a joined text: a phrase canary split over two items, or a word that only occurs in the
    list of names, does not make it "already eligible"."""
    pv = C.pack_for(SHA, packs)
    items = pv.seen_items(70.0)
    assert ("event@00:10", "Alice greets Bob.") in items and all("\n" not in t for _l, t in items)
    assert not any(C.canary_hits({"term": "turns out", "match": "phrase"}, t) for _l, t in pv.seen_items(20.0))


@pytest.fixture
def zh_packs(tmp_path, monkeypatch):
    """The mini pack with a Chinese text on an early event that uses the canary word 原来 innocently."""
    p = copy.deepcopy(mini_pack())
    p["events"][0]["text_zh"] = "爱丽丝向鲍勃打招呼,原来他们早就认识。"
    root = tmp_path / "packs"
    packdb.write_db(packdb.normalize_pack(p), root / SHA / "master" / "pack.db")
    monkeypatch.setattr(J, "PACKS", root)
    C._PACKS.clear()
    yield root
    C._PACKS.clear()


def test_zh_canary_in_eligible_chinese_text_is_not_a_leak(zh_packs):
    """Regression: the "already eligible" test used the English texts only, so a zh canary word inside an eligible
    item's Chinese text (sent back as evidence with a zh answer) was reported as a leak."""
    c = case(P=20.0, lang="zh", question="这是谁?")
    evidence = ["爱丽丝向鲍勃打招呼,原来他们早就认识。"]
    r = J.rule_check(c, "这是爱丽丝（Alice）。", evidence)
    assert r["rule_leaks"] == [] and r["suspects"] == ["canary:原来 (already eligible: event@00:10) @evidence"]
    assert r["rule_hits"] == [{"rule": "canary:原来 (already eligible: event@00:10) @evidence", "where": "evidence",
                               "match": "爱丽丝向鲍勃打招呼,[原来]他们早就认识。"}]


def test_rule_hits_say_where_and_what_matched(packs):
    c = case(P=20.0, lang="zh", question="这是谁?", must_not_include=["森林"])
    r = J.rule_check(c, "这是爱丽丝。", ["后来狼在嚎叫"], streamed="她走进森林")
    assert r["rule_leaks"] == ["canary:嚎 @evidence", "must_not_include:森林 @streamed"]
    assert {h["where"] for h in r["rule_hits"]} == {"evidence", "streamed"}
    assert [h["match"] for h in r["rule_hits"]] == ["后来狼在[嚎]叫", "她走进[森林]"]
    assert C.snippet(C.find_span("Scales", "史科尔斯（Scales）是一条龙", True)) == "史科尔斯([Scales])是一条龙"


def test_future_and_known_facts(packs):
    fut = J.future_facts(case(P=20.0))
    assert any("The wolf turns out to be Bob." in f for f in fut)
    assert any("identity reveal" in f and "'the wolf' is actually 'Bob'" in f for f in fut)
    known = J.known_facts(case(P=20.0))
    assert any("Alice greets Bob." in f for f in known)
    assert not any("wolf" in f.lower() for f in known)
    prompt = J.render_prompt(case(P=20.0, tap={"x": 0.5, "y": 0.25}, history=[{"question": "Who?", "P": 10.0}]),
                             "Alice.", known, fut, history=[{"answer": "The girl."}])
    assert "x=0.50, y=0.25" in prompt and "Assistant: The girl." in prompt and "{" not in prompt.split("ASSISTANT")[0][-50:]


# ── aggregation ──────────────────────────────────────────────────────

def row(i, verdict="correct", leak=False, rule=(), cat="who_is_this", expect="answer", total=1000, first=None,
        tools=(), ids=(), status="ok", **kw):
    r = {"id": f"r{i}", "file": "unit", "set": kw.pop("set", "base"), "category": cat, "expect": expect,
         "status": status, "error": None if status == "ok" else "boom", "t_total_ms": total, "t_first_text_ms": first,
         "t_first_tool_ms": None, "server_latency_ms": None, "streamed": first is not None, "tools": list(tools),
         "server_tools": [t for t, x in zip(tools, ids) if x.startswith("ctx-")],
         "model_tools": [t for t, x in zip(tools, ids) if not x.startswith("ctx-")], "guard_rewritten": False,
         "usage": None, "answer": "a", "answer_sha": "x", "include_hits": None, "rule_leaks": list(rule),
         "real_names": [], "suspects": [], "llm": {"verdict": verdict, "appropriate": True, "leak": leak}}
    r.update(kw)
    return r


def test_summarize_accuracy_leaks_and_latency():
    rows = [row(1, "correct", total=100), row(2, "partial", total=200), row(3, "incorrect", total=300),
            row(4, "declined", cat="spoiler_probe", expect="either", total=400, leak=True),
            row(5, "correct", rule=["canary:x"], total=500, tools=("a", "b"), ids=("ctx-1", "m-1"))]
    s = J.summarize(rows)
    assert s["accuracy_pct"] == round(100 * (1 + 0.5 + 0 + 1) / 4, 1)            # probe excluded; partial = 0.5
    assert s["correct_pct"] == 50.0
    assert s["leak_pct"] == 40.0 and s["rule_leak_pct"] == 20.0 and s["llm_leak_pct"] == 20.0
    assert s["latency_total_ms"]["p50"] == 300 and s["latency_total_ms"]["p95"] is None     # n < 20: not shown
    assert s["latency_total_ms"]["p90"] == 460 and s["latency_total_ms"]["n"] == 5
    assert s["tools_per_answer"] == 0.4 and s["server_tools_per_answer"] == 0.2 and s["model_tools_per_answer"] == 0.2
    assert s["tool_usage"] == {"a": 1, "b": 1}


def test_quantile_interpolates():
    xs = list(range(1, 101))
    assert (J.quantile(xs, 0.5), J.quantile(xs, 0.9), J.quantile(xs, 0.95)) == (50.5, 90.1, 95.0)
    assert J.quantile([1, 2, 3, 4], 0.5) == 2.5                                  # not the lower middle
    assert J.quantile([7], 0.95) == 7 and J.quantile([], 0.5) is None
    assert J._lat(range(19))["p95"] is None and J._lat(range(20))["p95"] is not None


def test_usage_summary_contract_and_cost():
    u = J.usage_summary({"input_tokens": 1_000_000, "output_tokens": 200_000, "thought_tokens": 100_000,
                         "model_calls": 2}, "gemini-3.8-flash")
    assert (u["input"], u["output"], u["thoughts"], u["model_calls"]) == (1_000_000, 200_000, 100_000, 2)
    assert u["cost_usd"] == pytest.approx(0.75 + 0.2 * 3.75)                 # output already includes thinking
    g = J.usage_summary({"prompt_token_count": 10, "candidates_token_count": 5, "thoughts_token_count": 3})
    assert (g["input"], g["output"], g["model_calls"]) == (10, 8, None)         # Gemini style: thoughts added
    assert J.usage_summary({"input_tokens": 5, "cost_usd": 0.01})["cost_usd"] == 0.01
    assert J.usage_summary({"input_tokens": 5, "model": "unknown-model"})["cost_usd"] is None
    assert J.usage_summary(None) is None and J.usage_summary([]) is None


def test_summarize_tokens_and_server_first_text():
    rows = [row(i, total=1000 + i, usage=J.usage_summary({"input_tokens": 1000 * i, "output_tokens": 10 * i,
                                                          "thought_tokens": 0, "model_calls": 1 + i % 2}),
                server_first_text_ms=100 * i, flow="tools" if i > 3 else None) for i in range(1, 6)]   # old rows
    s = J.summarize(rows)
    assert (s["tokens_in_p50"], s["tokens_in_p90"], s["tokens_out_p50"]) == (3000, 4600, 30)
    assert s["model_calls_per_answer"] == 1.6 and "flow_mix" not in s
    assert s["latency_server_first_text_ms"]["p50"] == 300


def test_report_side_by_side(tmp_path):
    a = [row(1, total=100), row(2, "partial", total=300), row(3, set="new", total=200)]
    b = [row(1, total=50, first=20), row(2, total=150, first=30, rule=["must_not_include:x"])]
    md = J.report({"A": a, "B": b}, tmp_path, {"A": {"base": "http://a"}, "B": {"base": "http://b"}})
    card = json.loads((tmp_path / "scorecard.json").read_text())
    assert card["groups"]["all"]["A"]["n"] == 2                                 # only shared cases are compared
    assert "| Δ |" in md and "Only the 2 cases" in md and "`r2`" in md
    assert (tmp_path / "scorecard.md").read_text() == md
    assert len(md.splitlines()) < 300
    summary = (tmp_path / "summary.md").read_text()
    assert "## Failing cases" in summary and "leak (rule gate) (1): r2" in summary and "## Tool usage" not in summary
    assert len(summary.splitlines()) < 120


def test_judge_run_offline_uses_rules_and_caches(packs, tmp_path):
    run = tmp_path / "run1"
    run.mkdir()
    rows = [{"id": "t1", "status": "ok", "answer": "That is Bob.", "evidence_text": [], "t_total_ms": 900,
             "tools": ["look"], "server_tools": [], "model_tools": ["look"]},
            {"id": "t2", "status": "error", "error": "HTTP 500", "answer": "", "evidence_text": [], "t_total_ms": 50}]
    C.write_jsonl(run / "results.jsonl", rows)
    cases = {"t1": case(id="t1", P=5.0), "t2": case(id="t2")}
    judged = J.judge_run(run, cases, use_llm=False, use_rules=True, workers=2)
    assert judged[0]["rule_leaks"] == ["canary:Bob", "hidden_name:Bob"] and judged[1]["llm"]["verdict"] == "nonanswer"
    again = J.judge_run(run, cases, use_llm=False, use_rules=False, workers=1)     # offline: cached rule results
    assert again[0]["rule_leaks"] == judged[0]["rule_leaks"]


def test_legacy_rows_are_accepted():
    r = J._legacy({"id": "x", "status": "ok", "latency_ms": 1234, "first_tool_ms": 400,
                   "raw": {"events": [{"event": "tool", "data": {"id": "ctx-1", "name": "context"}},
                                      {"event": "tool", "data": {"id": "adk-2", "name": "search"}}]}})
    assert r["t_total_ms"] == 1234 and r["t_first_tool_ms"] == 400 and r["t_first_text_ms"] == 1234
    assert r["server_tools"] == ["context"] and r["model_tools"] == ["search"]


# ── cases and lint ───────────────────────────────────────────────────

def test_shipped_cases_lint_clean(capsys):
    cases = C.read_cases()
    assert len(cases) >= 69
    assert J.lint(cases, with_packs=False) == 0, capsys.readouterr().out


def test_lint_catches_bad_cases(capsys):
    real = C.read_cases()[0]
    bad = [dict(real, id="new:x1", set="new", notes="出题依据"),                  # notes must be English
           dict(real, id="new:x2", set="new", asset_sha256="00" * 32),            # wrong asset
           dict(real, id="new:x3", set="new", source="see " + C.FORBIDDEN[2] + "x"),             # leak-scanner string
           dict(real, id="new:x4", set="new", reference="Scales", must_not_include=["Scales"]),
           dict(real, id="new:x5", set="new", history=[{"question": "q", "P": real["P"] + 10}])]
    assert J.lint(bad, with_packs=False) == 1
    out = capsys.readouterr().out
    for cid in ("new:x1", "new:x2", "new:x3", "new:x4", "new:x5"):
        assert f"ERROR {cid}:" in out


def test_validate_schema():
    assert C.validate(case()) == []
    assert any("tap" in e for e in C.validate(case(tap={"x": 1.5, "y": 0})))
    assert any("category" in e for e in C.validate(case(category="nope")))
    assert any("regex" in e for e in C.validate(case(must_not_include=[{"regex": "("}])))


# ── SSE client against a tiny fake server ────────────────────────────

def test_iter_sse_framing():
    raw = [b": comment\n", b"event: gate\n", b'data: {"p": 1}\n', b"\n", b"event: delta\n", b'data: {"text": "a"}\n',
           b"\n", b"data: line1\n", b"data: line2\n", b"\n", b"event: done\n", b"data: {}\n"]
    assert list(R.iter_sse(raw)) == [("gate", '{"p": 1}'), ("delta", '{"text": "a"}'), ("message", "line1\nline2"),
                                     ("done", "{}")]


class FakeServer(http.server.BaseHTTPRequestHandler):
    calls: list = []
    fail_first_with_429 = False

    healthy = True
    revision = "rev-1"

    def log_message(self, *a):
        pass

    def do_GET(self):
        data = json.dumps({"ok": FakeServer.healthy, "titles": 3, "revision": FakeServer.revision}).encode()
        self.send_response(200 if FakeServer.healthy else 503)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        FakeServer.calls.append({"body": body, "token": self.headers.get("X-Watch-Bench-Token"),
                                 "flow_header": self.headers.get("X-Watch-Flow")})
        if FakeServer.fail_first_with_429:
            FakeServer.fail_first_with_429 = False
            data = json.dumps({"message": "slow down"}).encode()
            self.send_response(429)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        ev = lambda e, d: self.wfile.write(f"event: {e}\ndata: {json.dumps(d)}\n\n".encode()) or self.wfile.flush()  # noqa: E731
        ev("gate", {"p": body["p"], "eligible_total": 3, "locked_total": 4})
        if body["question"] == "error event":
            ev("error", {"message": "The assistant hit a problem answering this."})
            ev("done", {})
            return
        if body["question"] == "cut short":                   # the stream ends without answer / done
            ev("tool", {"id": "ctx-1", "name": "context"})
            return
        if body["question"] == "reset":                       # a chunked body that stops mid-way
            self.wfile.write(b"event: tool\ndata: {\"id\": \"ctx-1\"")
            self.wfile.flush()
            self.connection.shutdown(socket.SHUT_RDWR)
            return
        ev("tool", {"id": "ctx-1", "name": "context"})
        ev("tool", {"id": "fc-1", "name": "search_moments"})
        ev("tool_result", {"id": "fc-1", "name": "search_moments", "summary": "2 hits"})
        if body["question"] != "no stream":
            ev("delta", {"text": "Hello "})
            ev("delta", {"text": "there."})
        ev("answer", {"text": "Hello there.", "evidence": [{"text": "cue 1"}],
                      "guard": {"checked": True, "rewritten": False, "dropped": 0}, "latency_ms": 42,
                      "usage": {"input_tokens": 100, "output_tokens": 10, "thought_tokens": 4, "model_calls": 1},
                      "timing": {"first_text_ms": 30, "total_ms": 40}, "flow": "context"})
        ev("done", {})


@pytest.fixture
def fake():
    FakeServer.calls, FakeServer.healthy, FakeServer.fail_first_with_429 = [], True, False
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeServer)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def test_client_records_how_a_failed_stream_ended(fake):
    cl = R.Client(fake)
    e = cl.turn("mini", 5.0, "error event", "en", "s1")
    assert e["status"] == "error" and e["stream_end"] == "done" and e["http_status"] == 200
    assert e["sse_error"] == "The assistant hit a problem answering this."
    assert e["error"] == "error event: The assistant hit a problem answering this."
    cut = cl.turn("mini", 5.0, "cut short", "en", "s1")
    assert cut["status"] == "error" and cut["stream_end"] == "eof" and cut["events_seen"] == ["gate", "tool"]
    assert cut["error"].startswith("no answer event (stream closed without done; 2 events + 0 deltas, last event 'tool'")
    reset = cl.turn("mini", 5.0, "reset", "en", "s1")
    assert reset["status"] == "error" and reset["stream_end"] in ("eof", "exception")
    assert reset["error"] and reset["error"] != "no answer event"


def test_client_records_timings_tools_and_usage(fake):
    cl = R.Client(fake, token="t0ken")
    r = R.run_case(cl, case(tap={"x": 0.4, "y": 0.6}))
    assert r["status"] == "ok" and r["answer"] == "Hello there." and r["evidence_text"] == ["cue 1"]
    assert r["server_tools"] == ["context"] and r["model_tools"] == ["search_moments"]
    assert r["streamed"] and r["n_delta"] == 2 and r["streamed_text"] is None             # same as the final text
    assert r["t_first_tool_ms"] <= r["t_first_text_ms"] <= r["t_answer_ms"] <= r["t_total_ms"]
    assert r["server_latency_ms"] == 42 and r["usage"]["input_tokens"] == 100 and r["timing"]["first_text_ms"] == 30
    assert "flow" not in r and r["guard_dropped"] == 0
    assert r["guard_rewritten"] is False and r["gate"]["eligible_total"] == 3
    call = FakeServer.calls[-1]
    assert call["token"] == "t0ken" and call["body"]["tap"] == {"x": 0.4, "y": 0.6}
    assert call["flow_header"] is None                                        # no answer flow is asked for
    assert call["body"]["title_id"] == "mini" and call["body"]["session_id"].startswith("bench-")


def test_client_first_text_falls_back_to_answer(fake):
    r = R.run_case(R.Client(fake), case(question="no stream"))
    assert not r["streamed"] and r["t_first_text_ms"] == r["t_answer_ms"]
    assert FakeServer.calls[-1]["token"] is None


def test_followup_keeps_one_session(fake):
    c = case(category="followup", question="And what is he holding?", history=[{"question": "Who is that?", "P": 15.0}])
    r = R.run_case(R.Client(fake), c)
    assert r["status"] == "ok" and len(r["history"]) == 1 and r["history"][0]["answer"] == "Hello there."
    first, second = FakeServer.calls[-2:]
    assert first["body"]["session_id"] == second["body"]["session_id"] == r["session_id"]
    assert (first["body"]["p"], second["body"]["p"]) == (15.0, 20.0)


def test_client_retries_429_then_reports_http_errors(fake):
    FakeServer.fail_first_with_429 = True
    r = R.run_case(R.Client(fake, retry_429=1, wait_429=0), case())
    assert r["status"] == "ok" and len(FakeServer.calls) == 2
    FakeServer.fail_first_with_429 = True
    r = R.run_case(R.Client(fake, retry_429=0, wait_429=0), case())
    assert r["status"] == "error" and r["http_status"] == 429 and "slow down" in r["error"]


def test_run_bench_main_resumes(fake, tmp_path, monkeypatch):
    monkeypatch.setattr(R, "RUNS_DIR", tmp_path)
    cases = tmp_path / "cases.jsonl"
    rows = [{k: v for k, v in case(id=f"t{i}").items() if k != "_file"} for i in range(3)]
    cases.write_text("".join(json.dumps(r) + "\n" for r in rows))
    assert R.main(["--base", fake, "--cases", str(cases), "--run-id", "x", "--limit", "2"]) == 0
    assert len(C.read_jsonl(tmp_path / "x" / "results.jsonl")) == 2
    n = len(FakeServer.calls)
    assert R.main(["--base", fake, "--cases", str(cases), "--run-id", "x"]) == 0
    assert len(FakeServer.calls) == n + 1                                         # only the missing case ran
    meta = json.loads((tmp_path / "x" / "run.json").read_text())
    assert meta["base"] == fake and meta["token"] is False


def test_run_id_and_meta_carry_no_flow(fake, tmp_path, monkeypatch):
    monkeypatch.setattr(R, "RUNS_DIR", tmp_path)
    cases = tmp_path / "cases.jsonl"
    cases.write_text(json.dumps({k: v for k, v in case(id="t1").items() if k != "_file"}) + "\n")
    assert R.main(["--base", fake, "--cases", str(cases), "--token", "tk"]) == 0
    (run,) = [p for p in tmp_path.iterdir() if p.is_dir()]
    assert "flow" not in json.loads((run / "run.json").read_text()) and FakeServer.calls[-1]["flow_header"] is None
    with pytest.raises(SystemExit):
        R.main(["--base", fake, "--cases", str(cases), "--flow", "tools"])         # the option is gone


def test_resume_refuses_another_revision_or_a_tools_flow_run(fake, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(R, "RUNS_DIR", tmp_path)
    cases = tmp_path / "cases.jsonl"
    rows = [{k: v for k, v in case(id=f"t{i}").items() if k != "_file"} for i in range(2)]
    cases.write_text("".join(json.dumps(r) + "\n" for r in rows))
    assert R.main(["--base", fake, "--cases", str(cases), "--run-id", "x", "--limit", "1"]) == 0
    monkeypatch.setattr(FakeServer, "revision", "rev-2")
    assert R.main(["--base", fake, "--cases", str(cases), "--run-id", "x"]) == 2
    assert "revision ('rev-1' → 'rev-2')" in capsys.readouterr().err
    monkeypatch.setattr(FakeServer, "revision", "rev-1")
    mpath = tmp_path / "x" / "run.json"
    assert json.loads(mpath.read_text())["revision"] == "rev-1"
    mpath.write_text(json.dumps({**json.loads(mpath.read_text()), "flow": "context"}))   # an older run: same flow
    assert R.main(["--base", fake, "--cases", str(cases), "--run-id", "x", "--limit", "0"]) == 0
    mpath.write_text(json.dumps({**json.loads(mpath.read_text()), "flow": "tools"}))     # the removed flow
    assert R.main(["--base", fake, "--cases", str(cases), "--run-id", "x"]) == 2
    assert "flow ('tools' → None)" in capsys.readouterr().err


def test_refuses_to_start_when_health_fails(fake, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(R, "RUNS_DIR", tmp_path)
    cases = tmp_path / "cases.jsonl"
    cases.write_text(json.dumps({k: v for k, v in case(id="t1").items() if k != "_file"}) + "\n")
    FakeServer.healthy = False
    assert R.main(["--base", fake, "--cases", str(cases), "--run-id", "h"]) == 2
    assert "api/health failed" in capsys.readouterr().err and FakeServer.calls == []
    assert R.main(["--base", "http://127.0.0.1:9", "--cases", str(cases), "--run-id", "h"]) == 2   # nothing listening


def test_stops_when_token_still_gets_429(fake, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(R, "RUNS_DIR", tmp_path)
    cases = tmp_path / "cases.jsonl"
    rows = [{k: v for k, v in case(id=f"t{i}").items() if k != "_file"} for i in range(3)]
    cases.write_text("".join(json.dumps(r) + "\n" for r in rows))
    FakeServer.fail_first_with_429 = True
    assert R.main(["--base", fake, "--cases", str(cases), "--run-id", "x", "--token", "bad"]) == 2
    assert "429 although a bench token was sent" in capsys.readouterr().err
    assert len(FakeServer.calls) == 1 and C.read_jsonl(tmp_path / "x" / "results.jsonl") == []


# ── honesty pins: what is graded, what is only leak-checked ─────────

def test_accuracy_denominator_and_scores_are_pinned():
    # a service error is graded as a non-answer and stays in every denominator
    s = J.summarize([row(1, "correct"), row(2, "nonanswer", status="error",
                                            llm={"verdict": "nonanswer", "appropriate": False, "leak": False})])
    assert s["accuracy_pct"] == 50.0 and s["accuracy_n"] == 2 and s["appropriate_pct"] == 50.0 and s["errors"] == 1
    assert J.summarize([row(1, "declined")])["accuracy_pct"] == 0.0                 # declining is not a correct answer
    assert J.VERDICT_SCORE == {"correct": 1.0, "partial": 0.5, "incorrect": 0.0, "declined": 0.0, "nonanswer": 0.0}
    # a judge failure is not dropped silently: it is counted on its own line
    s = J.summarize([row(1, "correct"), row(2, llm={"judge_error": "empty", "verdict": None})])
    assert s["accuracy_n"] == 1 and s["ungraded"] == 1 and s["judge_errors"] == 1


def _result(answer, **kw):
    r = {"id": "t1", "status": "ok", "answer": answer, "evidence_text": [], "t_total_ms": 900, "tools": [],
         "server_tools": [], "model_tools": []}
    r.update(kw)
    return r


def test_judge_run_grades_the_answer_alone_and_checks_every_channel(packs, tmp_path, monkeypatch):
    run = tmp_path / "run1"
    run.mkdir()
    seen = {}
    monkeypatch.setattr(J, "llm_judge", lambda c, text, history=None, shown="": seen.update(text=text, shown=shown)
                        or {"verdict": "declined", "appropriate": True, "leak": False, "prompt_sha": J.PROMPT_SHA,
                            "judge_model": "m"})
    monkeypatch.setattr(J, "judge_model", lambda: "m")
    raw = {"answer": {"evidence": [{"text": "Alice greets Bob.", "start_s": 10.0, "end_s": 30.0}],
                      "frames": [{"t": 25, "caption": "Earlier: 00:25, it will howl"}],
                      "highlight": {"label": "the wolf", "frame_t": 21}},
           "events": [{"event": "tool", "data": {"id": "fc-1", "name": "search", "args": {"q": "Bob the wolf"}}},
                      {"event": "tool_result", "data": {"id": "fc-1", "summary": "it turns out fine"}}]}
    C.write_jsonl(run / "results.jsonl", [_result("I can't tell who that is yet.", evidence_text=["Alice greets Bob."],
                                                   streamed_text="Bob…", raw=raw,
                                                   history=[{"answer": "That is Bob.", "P": 5.0}])])
    c = case(id="t1", P=20.0, must_include_any=["Alice"], history=[{"question": "Who?", "P": 5.0}])
    (j,) = J.judge_run(run, {"t1": c}, use_llm=True, use_rules=True, workers=1)
    assert seen["text"] == "I can't tell who that is yet." and j["include_hits"] == []     # evidence is not the answer
    assert "[evidence] Alice greets Bob." in seen["shown"] and "[history1, earlier turn at P = 5.0]" in seen["shown"]
    assert {"canary:howl @frame", "canary:turns out @tool_summary", "hidden_name:Bob @history1",
            "time:evidence end_s 00:30 > P", "time:frame 00:25 > P", "time:highlight 00:21 > P"} <= set(j["rule_leaks"])
    assert j["channels"] == ["evidence", "frame", "highlight", "history1", "streamed", "tool_args", "tool_summary"]


def test_evidence_and_streamed_text_reach_the_rule_gate(packs, tmp_path):
    run = tmp_path / "run2"
    run.mkdir()
    C.write_jsonl(run / "results.jsonl", [_result("A boy.", evidence_text=["it will howl"], streamed_text="turns out")])
    (j,) = J.judge_run(run, {"t1": case(id="t1", P=20.0)}, use_llm=False, use_rules=True, workers=1)
    assert j["rule_leaks"] == ["canary:howl @evidence", "canary:turns out @streamed"]


def test_echoed_canary_is_a_suspect_not_dropped_and_public_names_are_allowed(packs):
    r = J.rule_check(case(P=20.0, question="Does it howl?"), "Yes, it will howl.")
    assert r["rule_leaks"] == [] and r["suspects"] == ["canary:howl (echo)"]
    r = J.rule_check(case(P=20.0, flag_if_any=["Alice", "meadow"]), "Alice in the meadow.")
    assert r["suspects"] == ["flag:meadow"]                                          # Alice is name_public
    assert any("public names" in f and "Alice" in f for f in J.future_facts(case(P=20.0)))


def test_marker_channels_limit_where_a_term_counts(packs):
    m = {"term": "wolf", "channels": ["answer"]}
    assert J.rule_check(case(P=20.0, must_not_include=[m]), "A boy.", ["a wolf"])["rule_leaks"] == []
    assert J.rule_check(case(P=20.0, must_not_include=[m]), "A wolf.")["rule_leaks"] == ["must_not_include:wolf"]
    assert any("channels" in e for e in C.validate(case(must_not_include=[{"term": "x", "channels": ["nope"]}])))


def test_future_facts_carry_every_later_recap_and_canary(packs, monkeypatch):
    fut = J.future_facts(case(P=5.0), limit=1)
    assert any("turns out" in f and "howl" in f for f in fut if f.startswith("[words that point"))


def test_grade_cache_key_has_case_pack_and_judge_model(packs, tmp_path, monkeypatch):
    run = tmp_path / "run3"
    run.mkdir()
    C.write_jsonl(run / "results.jsonl", [_result("A boy.")])
    calls = []
    monkeypatch.setattr(J, "llm_judge", lambda c, *a, **k: calls.append(c["reference"]) or
                        {"verdict": "correct", "appropriate": True, "leak": False, "prompt_sha": J.PROMPT_SHA,
                         "judge_model": J.judge_model()})
    monkeypatch.setattr(J, "judge_model", lambda: "m1")
    J.judge_run(run, {"t1": case(id="t1")}, True, True, 1)
    J.judge_run(run, {"t1": case(id="t1")}, True, True, 1)
    assert len(calls) == 1                                                           # cached
    J.judge_run(run, {"t1": case(id="t1", reference="Bob.")}, True, True, 1)         # case content changed
    monkeypatch.setattr(J, "judge_model", lambda: "m2")
    J.judge_run(run, {"t1": case(id="t1", reference="Bob.")}, True, True, 1)         # judge model changed
    assert len(calls) == 3
    j = C.read_jsonl(run / "judged.jsonl")[0]
    assert j["cache"]["pack"].startswith(SHA[:12] + ":")


def test_fallbacks_are_their_own_line_and_listed(tmp_path):
    assert J.fallback_kind({"status": "ok", "answer": next(iter(J.fallback_texts()))}) in ("spoiler", "timeout")
    assert J.fallback_kind({"status": "ok", "answer": "x", "guard_fallback": "timeout"}) == "timeout"
    rows = [row(1, fallback="spoiler", guard_rewritten=True), row(2), row(3, guard_rewritten=True)]
    assert J.summarize(rows)["fallback_pct"] == 33.3
    J.report({"A": rows}, tmp_path)
    summary = (tmp_path / "summary.md").read_text()
    assert "`r1` (fallback: spoiler), `r3`" in summary and "fallback answers %" in summary and "(n 3)" in summary


def test_lint_public_names_and_verified_link_markers(packs, capsys):
    ok = case(id="new:l1", set="new", P=20.0, must_not_include=[{"term": "Bob", "case_sensitive": True, "link": True}])
    bad_link = case(id="new:l2", set="new", P=20.0, must_not_include=[{"term": "Alice", "link": True}])
    public = case(id="new:l3", set="new", P=20.0, must_not_include=["Alice"])
    late = case(id="new:l4", set="new", P=80.0, must_not_include=[{"term": "Bob", "link": "k01"}])   # link revealed
    mini = {"mini": {"title": "Mini", "duration_s": 100.0, "shas": [SHA]}}
    assert J.lint([ok], with_packs=True, titles=mini) == 0, capsys.readouterr().out
    capsys.readouterr()
    assert J.lint([bad_link, public, late], with_packs=True, titles=mini) == 1
    out = capsys.readouterr().out
    assert "new:l2: link marker" in out and "new:l3: must_not_include has a public name" in out and "new:l4: link" in out
