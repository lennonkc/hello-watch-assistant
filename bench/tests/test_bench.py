"""Offline tests for the benchmark: rule gate on the synthetic mini pack, aggregation, case lint, SSE client."""
from __future__ import annotations

import copy
import http.server
import json
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
    monkeypatch.setattr(J, "eligible_text", lambda c: "Alice turns out the lights.")
    r = J.rule_check(case(P=20.0), "It turns out fine.")
    assert "canary:turns out" in r["suspects"] and "canary:turns out" not in r["rule_leaks"]


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
    assert s["latency_total_ms"]["p50"] == 300 and s["latency_total_ms"]["p95"] == 500
    assert s["tools_per_answer"] == 0.4 and s["server_tools_per_answer"] == 0.2 and s["model_tools_per_answer"] == 0.2
    assert s["tool_usage"] == {"a": 1, "b": 1}


def test_quantile_nearest_rank():
    xs = list(range(1, 101))
    assert (J.quantile(xs, 0.5), J.quantile(xs, 0.9), J.quantile(xs, 0.95)) == (50, 90, 95)
    assert J.quantile([7], 0.95) == 7 and J.quantile([], 0.5) is None


def test_usage_summary_shapes_and_cost():
    u = J.usage_summary({"prompt_token_count": 1_000_000, "candidates_token_count": 100_000,
                         "thoughts_token_count": 100_000}, "gemini-3.8-flash")
    assert u["input"] == 1_000_000 and u["output"] == 100_000 and u["thoughts"] == 100_000
    assert u["cost_usd"] == pytest.approx(0.75 + 0.2 * 3.75)
    two = J.usage_summary([{"input_tokens": 10, "output_tokens": 5}, {"input_tokens": 20, "output_tokens": 5}])
    assert two["input"] == 30 and two["output"] == 10
    assert J.usage_summary({"calls": [{"prompt_token_count": 5}], "cost_usd": 0.01})["cost_usd"] == 0.01
    assert J.usage_summary({"prompt_token_count": 5, "model": "unknown-model"})["cost_usd"] is None
    assert J.usage_summary(None) is None


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
    assert "## Failing cases" in summary and "leak (1): r2" in summary and "## Tool usage" not in summary
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

    def log_message(self, *a):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        FakeServer.calls.append({"body": body, "token": self.headers.get("X-Watch-Bench-Token")})
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
        ev("tool", {"id": "ctx-1", "name": "context"})
        ev("tool", {"id": "fc-1", "name": "search_moments"})
        ev("tool_result", {"id": "fc-1", "name": "search_moments", "summary": "2 hits"})
        if body["question"] != "no stream":
            ev("delta", {"text": "Hello "})
            ev("delta", {"text": "there."})
        ev("answer", {"text": "Hello there.", "evidence": [{"text": "cue 1"}], "guard": {"rewritten": False},
                      "latency_ms": 42, "usage": {"prompt_token_count": 100, "candidates_token_count": 10},
                      "timing": {"model_ms": 30}})
        ev("done", {})


@pytest.fixture
def fake():
    FakeServer.calls = []
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeServer)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def test_client_records_timings_tools_and_usage(fake):
    cl = R.Client(fake, token="t0ken")
    r = R.run_case(cl, case(tap={"x": 0.4, "y": 0.6}))
    assert r["status"] == "ok" and r["answer"] == "Hello there." and r["evidence_text"] == ["cue 1"]
    assert r["server_tools"] == ["context"] and r["model_tools"] == ["search_moments"]
    assert r["streamed"] and r["n_delta"] == 2 and r["streamed_text"] is None             # same as the final text
    assert r["t_first_tool_ms"] <= r["t_first_text_ms"] <= r["t_answer_ms"] <= r["t_total_ms"]
    assert r["server_latency_ms"] == 42 and r["usage"]["prompt_token_count"] == 100 and r["timing"] == {"model_ms": 30}
    assert r["guard_rewritten"] is False and r["gate"]["eligible_total"] == 3
    call = FakeServer.calls[-1]
    assert call["token"] == "t0ken" and call["body"]["tap"] == {"x": 0.4, "y": 0.6}
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
