"""Grade benchmark runs and write a scorecard: rule gate (pack lookup) ∪ LLM judge (accuracy, semantic leaks).

    python bench/judge.py --lint                                  # case consistency only (offline)
    python bench/judge.py --lint --packs .cache/packs             # + checks against the master packs
    python bench/judge.py RUN --packs .cache/packs                # grade one run → runs/RUN/scorecard.{md,json}
    python bench/judge.py RUN_A RUN_B --packs …                   # side by side → runs/compare-RUN_A-vs-RUN_B/
    python bench/judge.py RUN --no-llm --no-rules                 # offline: latency / tools / cost (+ cached grades)
    python bench/judge.py RUN_A+RUN_A_retry …                     # merge a re-run into a run (later wins per id)

Rule gate (master pack, allow_auto review relaxation, at the case's P; checked text = answer + evidence text + any
streamed text that differs from the final answer):
- hidden name / alias: a character name not yet visible at P or an alias not yet known (spec §6); a term the viewer
  typed in the question counts only as a suspect (echo);
- canary: a canary still active at P (spec §7.2); a suspect only when the term also describes something already
  eligible at P;
- real name: a `blocklist` term (spec §7.1), reported separately;
- the case's own `must_not_include` (leak) and `flag_if_any` (suspect).
LLM judge: given the reference, key terms, notes and the future items after P, returns a verdict (correct / partial /
incorrect / declined / nonanswer), whether the behaviour was appropriate, and whether it leaked. Cached per
(case id, answer sha256, prompt sha) in `runs/<id>/judged.jsonl`, so re-running costs nothing.

The LLM judge needs a Gemini key (`CEDAR_GEMINI_KEY_FILE`, developer backend) or `CEDAR_GEMINI_BACKEND=enterprise`
with `CEDAR_GCP_PROJECT`; the rule gate needs the master packs (`--packs` or `BENCH_PACKS_DIR`, layout
`<asset_sha>/master/pack.db`).
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import hashlib
import json
import math
import os
import pathlib
import statistics
import sys

if __package__ in (None, ""):                                    # run as a script: python bench/judge.py
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from bench import common as C  # noqa: E402
from bench.common import (CASES_DIR, CJK_ANY, FORBIDDEN, HERE, REPO, RUNS_DIR, case_set, hits,  # noqa: E402
                          marker_hits, marker_label, mmss, pack_for, read_cases, read_jsonl, write_jsonl)

VERDICT_SCORE = {"correct": 1.0, "partial": 0.5, "incorrect": 0.0, "declined": 0.0, "nonanswer": 0.0}
SCHEMA = {"type": "object", "properties": {
    "verdict": {"type": "string", "enum": list(VERDICT_SCORE)},
    "appropriate": {"type": "boolean"},
    "leak": {"type": "boolean"},
    "leak_quote": {"type": "string"},
    "leak_fact": {"type": "string"},
    "rationale": {"type": "string"}},
    "required": ["verdict", "appropriate", "leak", "rationale"]}
PROMPT_PATH = HERE / "judge_prompt.txt"
PROMPT_SHA = hashlib.sha256(PROMPT_PATH.read_bytes()).hexdigest()[:12]     # prompt edited → cache invalid
FUTURE_TYPES = {"scene", "event", "recap", "shot_caption", "character_fact", "object"}
DEFAULT_MODEL = os.environ.get("WATCH_MODEL", "gemini-3.8-flash")          # answering model, for cost estimates
MAX_LEAK_LINES, MAX_ERROR_LINES = 40, 15

PACKS: pathlib.Path | None = None                                          # set from --packs


def _pack(c: dict):
    return pack_for(c["asset_sha256"], PACKS)


# ── pack lookups ─────────────────────────────────────────────────────

def eligible_text(c: dict) -> str:
    """Every text eligible at P (allow_auto), English and Chinese, + character looks already shown + visible names of
    characters already on screen + title info."""
    pv, P = _pack(c), c["P"]
    items = pv.gate(P).eligible()
    texts = [it.text for it in items if it.text] + [pv.zh[k] for it in items if (k := (it.item_type, it.item_id)) in pv.zh]
    looks = [lk.get(f) or "" for lk in pv.data.get("character_looks", [])
             if pv.reviewed_ok(lk.get("review_state")) and lk.get("reveal_at_s") is not None
             and lk["reveal_at_s"] + pv.m_rev <= P + 1e-9 for f in ("description_en", "description_zh")]
    names = [pv.visible_name(cid, P, lang) for cid in pv.chars if pv.appeared(cid, P) for lang in ("en", "zh")]
    titles = [t.get(f) or "" for t in pv.title_info for f in ("text_en", "text_zh")]
    return "\n".join(texts + looks + names + titles)


def future_facts(c: dict, limit: int = 30) -> list[str]:
    pv, P = _pack(c), c["P"]
    fut = sorted((it for it in pv.gate(P).candidates() if it.item_type in FUTURE_TYPES and it.text
                  and P + 1e-9 < it.eligible_from < math.inf), key=lambda it: it.eligible_from)
    out, seen = [], set()
    for it in fut:
        if it.text not in seen:
            seen.add(it.text)
            out.append(f"[{mmss(it.start_s)} {it.item_type}] {it.text}")
        if len(out) >= limit:
            break
    for term, _cs, cid, kind in pv.hidden_terms(P):
        label = pv.chars[cid]["display_label_pre_name"]
        out.append(f"[reveal] the character currently known as '{label}' is later identified as '{term}' ({kind})")
    known = pv.known_names(P)
    if known:
        out.append("[NOT future — already known at P, fine to use, incl. translations] names / aliases: " + ", ".join(known))
    for lk in pv.pending_links(P):
        a, b = pv.chars.get(lk["a_character_id"]), pv.chars.get(lk["b_character_id"])
        if a and b:
            out.append(f"[identity reveal at {mmss(lk['reveal_at_s'])}] '{a['display_label_pre_name']}' is actually "
                       f"'{b['canonical_name']}' ({b['display_label_pre_name']})")
    return out


def known_facts(c: dict, limit: int = 40) -> list[str]:
    """Facts eligible at P: every scene / event / recap (a few dozen per film) + the latest `limit` of the other types.
    limit=0 keeps only scenes / events / recaps."""
    pv = _pack(c)
    items = [it for it in pv.gate(c["P"]).eligible() if it.text and it.item_type in FUTURE_TYPES | {"cue"}]
    items.sort(key=lambda it: it.eligible_from)
    spine = [it for it in items if it.item_type in ("scene", "event", "recap")]
    rest = [it for it in items if it.item_type not in ("scene", "event", "recap")]
    keep = sorted(spine + (rest[-limit:] if limit else []), key=lambda it: it.start_s)
    return [f"[{mmss(it.start_s)} {it.item_type}] {it.text}" for it in keep]


def viewer_text(c: dict) -> str:
    """Everything the viewer typed in this conversation (echoes of it are suspects, not leaks)."""
    return "\n".join([h["question"] for h in c.get("history") or []] + [c["question"]])


def rule_check(c: dict, text: str, evidence=(), streamed: str | None = None) -> dict:
    """Rule gate over what the viewer received: the answer, the evidence texts shown with it, and streamed text that
    differs from the final answer. Each hit is listed once, with where it matched (`rule_hits`: rule, where, the
    matched part with context); a label gets ` @evidence` / ` @streamed` when it did not match in the answer."""
    pv, P, q = _pack(c), c["P"], viewer_text(c)
    sources = [("answer", text or "")] + [("evidence", e) for e in evidence or () if e] + \
        ([("streamed", streamed)] if streamed else [])
    leaks, suspects, found = [], [], []

    def first(span_of):
        for where, t in sources:
            if (sp := span_of(t)) is not None:
                return where, sp
        return None

    def record(bucket: list, label: str, hit) -> None:
        where, sp = hit
        label = label if where == "answer" else f"{label} @{where}"
        bucket.append(label)
        found.append({"rule": label, "where": where, "match": C.snippet(sp)})

    for term, cs, _cid, kind in pv.hidden_terms(P):
        m = {"term": term, "case_sensitive": cs}
        if hit := first(lambda t: C.marker_span(m, t)):
            echo = marker_hits(m, q)
            record(suspects if echo else leaks, f"hidden_{kind}:{term}" + (" (echo)" if echo else ""), hit)
    legal = None
    for k in pv.active_canaries(P):
        if (hit := first(lambda t: C.canary_span(k, t))) and not C.canary_hits(k, q):
            legal = eligible_text(c) if legal is None else legal
            # a canary word can also describe something already eligible (an earlier, similar action) → suspect
            already = C.canary_hits(k, legal)
            record(suspects if already else leaks, f"canary:{k['term']}" + (" (already eligible)" if already else ""),
                   hit)
    real = []
    for b in pv.blocklist:
        if hit := first(lambda t: C.marker_span({"term": b["term"]}, t)):
            real.append(b["term"])
            found.append({"rule": f"real:{b['term']}", "where": hit[0], "match": C.snippet(hit[1])})
    for m in c.get("must_not_include") or []:
        if hit := first(lambda t: C.marker_span(m, t)):
            record(leaks, f"must_not_include:{C.marker_label(m)}", hit)
    for m in c.get("flag_if_any") or []:
        if hit := first(lambda t: C.marker_span(m, t)):
            record(suspects, f"flag:{C.marker_label(m)}", hit)
    return {"rule_leaks": sorted(set(leaks)), "real_names": sorted(set(real)), "suspects": sorted(set(suspects)),
            "rule_hits": found}


# ── LLM judge ────────────────────────────────────────────────────────

_CLIENT = None


def _client():
    global _CLIENT
    if _CLIENT is None:
        from ingest import gemini
        _CLIENT = gemini.client(os.environ.get("BENCH_JUDGE_BACKEND") or gemini.backend())
    return _CLIENT


def judge_model() -> str:
    from ingest import gemini
    return os.environ.get("BENCH_JUDGE_MODEL") or gemini.VISION_MODEL


def _marker_list(ms) -> str:
    return ", ".join(str(marker_label(m)) for m in ms or []) or "(none)"


def render_prompt(c: dict, answer: str, known: list[str], future: list[str], history: list | None = None) -> str:
    tpl = PROMPT_PATH.read_text(encoding="utf-8")
    tap = (f"The viewer tapped the paused frame at x={c['tap']['x']:.2f}, y={c['tap']['y']:.2f} (0–1, origin top-left)."
           if c.get("tap") else "")
    hist = ""
    if c.get("history"):
        turns = []
        for i, h in enumerate(c["history"]):
            got = history[i] if history and i < len(history) else {}
            turns.append(f"  Viewer (P = {h.get('P', c['P'])}): {h['question']}\n"
                         f"  Assistant: {(got or {}).get('answer') or '(not recorded)'}")
        hist = "Earlier in this conversation (the question below is a follow-up):\n" + "\n".join(turns) + "\n"
    return tpl.format(title=c["title"], p=c["P"], p_mmss=mmss(c["P"]), lang=c["lang"], question=c["question"],
                      history=hist, tap_line=tap, expect=c["expect"],
                      reference=c.get("reference") or "(none — use key terms and notes)",
                      must_include=_marker_list(c.get("must_include_any")), notes=c.get("notes") or "",
                      future="\n".join(f"- {f}" for f in future) or "- (nothing: P is at the end)",
                      known="\n".join(f"- {f}" for f in known) or "- (not listed)", answer=answer or "(empty)")


def llm_judge(c: dict, text: str, history: list | None = None) -> dict:
    from ingest import gemini
    fut = future_facts(c)
    fin = None
    # occasional empty responses (truncation, or a long plot excerpt tripping a content filter): shrink "already
    # shown" step by step and retry
    for known_n, max_out in ((40, 2048), (40, 4096), (12, 4096), (0, 4096)):
        prompt = render_prompt(c, text, known_facts(c, known_n), fut, history)
        out, _usage, _lat, fin = gemini._retry(lambda: gemini.generate_json(
            _client(), judge_model(), [gemini.text_part(prompt)], SCHEMA, thinking="LOW", max_out=max_out))
        if out:
            return {**json.loads(out), "judge_model": judge_model(), "prompt_sha": PROMPT_SHA, "known_n": known_n}
    return {"judge_error": str(fin), "verdict": None, "appropriate": None, "leak": False, "rationale": "judge returned nothing"}


# ── usage / cost ─────────────────────────────────────────────────────

def usage_summary(usage, model: str = DEFAULT_MODEL) -> dict | None:
    """Tokens, model calls and estimated cost of one answer from `answer.usage`.

    The service sends `{"input_tokens", "output_tokens", "thought_tokens", "model_calls"}` summed over every model call
    of the question, `output_tokens` already including the thinking tokens (docs/web-api.md). A Gemini
    usage_metadata-style dict (`prompt_token_count`, `candidates_token_count` without thoughts) is accepted too.
    Cost: list prices from `ingest/ledger.py`, output incl. thinking at the output price, no cache discount; a
    `cost_usd` field wins."""
    if not isinstance(usage, dict) or not usage:
        return None

    def num(k):
        return int(usage[k]) if isinstance(usage.get(k), (int, float)) else 0
    if "input_tokens" in usage or "output_tokens" in usage:
        n_in, n_out, n_th = num("input_tokens"), num("output_tokens"), num("thought_tokens")
    else:
        n_th = num("thoughts_token_count")
        n_in, n_out = num("prompt_token_count"), num("candidates_token_count") + n_th
    from ingest.ledger import PRICES
    price = PRICES.get(usage.get("model") or model)
    cost = (n_in * price[0] + n_out * price[1]) / 1e6 if price else None
    if isinstance(usage.get("cost_usd"), (int, float)):
        cost = float(usage["cost_usd"])
    return {"input": n_in, "output": n_out, "thoughts": n_th, "model_calls": num("model_calls") or None,
            "cost_usd": round(cost, 6) if cost is not None else None}


# ── grading one run ──────────────────────────────────────────────────

def _legacy(r: dict) -> dict:
    """Accept result rows of the earlier harness (latency_ms / first_tool_ms, no ctx- split)."""
    r = dict(r)
    r.setdefault("t_total_ms", r.get("latency_ms"))
    r.setdefault("t_first_tool_ms", r.get("first_tool_ms"))
    if r.get("t_first_text_ms") is None and not r.get("streamed"):
        r["t_first_text_ms"] = r.get("t_answer_ms") or r.get("t_total_ms")       # no deltas: text arrives with answer
    if "model_tools" not in r:
        evs = [e["data"] for e in ((r.get("raw") or {}).get("events") or []) if e.get("event") == "tool"]
        r["server_tools"] = [e.get("name") for e in evs if str(e.get("id") or "").startswith("ctx-")]
        r["model_tools"] = [e.get("name") for e in evs if not str(e.get("id") or "").startswith("ctx-")]
    return r


def judge_run(run_dir: pathlib.Path, cases: dict, use_llm: bool, use_rules: bool, workers: int,
              model: str = DEFAULT_MODEL) -> list[dict]:
    results = [_legacy(r) for r in read_jsonl(run_dir / "results.jsonl")]
    jpath = run_dir / "judged.jsonl"
    llm_cache, rule_cache = {}, {}
    for r in read_jsonl(jpath):
        if r.get("llm") and r["llm"].get("prompt_sha") and not r["llm"].get("judge_error"):
            llm_cache[(r["id"], r["answer_sha"], r["llm"]["prompt_sha"])] = r["llm"]
        if r.get("rule_leaks") is not None:
            rule_cache[(r["id"], r["answer_sha"])] = {k: r.get(k) for k in ("rule_leaks", "real_names", "suspects",
                                                                            "rule_hits")}

    def one(r):
        c = cases.get(r["id"])
        if c is None:
            return None
        answer_text = "\n".join([r.get("answer") or ""] + [t for t in r.get("evidence_text") or [] if t])
        checked = answer_text + ("\n" + r["streamed_text"] if r.get("streamed_text") else "")
        sha = hashlib.sha256(checked.encode()).hexdigest()[:16]
        row = {"id": r["id"], "file": c["_file"], "set": case_set(c), "category": c["category"], "title": c["title"],
               "lang": c["lang"], "expect": c["expect"], "status": r["status"], "error": r.get("error"),
               "http_status": r.get("http_status"), "stream_end": r.get("stream_end"), "P": c["P"],
               "t_total_ms": r.get("t_total_ms"), "t_first_text_ms": r.get("t_first_text_ms"),
               "t_first_tool_ms": r.get("t_first_tool_ms"), "server_latency_ms": r.get("server_latency_ms"),
               "streamed": bool(r.get("streamed")), "tools": r.get("tools") or [],
               "server_tools": r.get("server_tools") or [], "model_tools": r.get("model_tools") or [],
               "guard_rewritten": r.get("guard_rewritten"), "usage": usage_summary(r.get("usage"), model),
               "server_first_text_ms": (r.get("timing") or {}).get("first_text_ms"), "flow": r.get("flow"),
               "answer": r.get("answer"), "answer_sha": sha,
               "include_hits": hits(c.get("must_include_any"), answer_text) if c.get("must_include_any") else None}
        if use_rules:
            row.update(rule_check(c, r.get("answer") or "", r.get("evidence_text") or [], r.get("streamed_text")))
        else:
            row.update(rule_cache.get((r["id"], sha)) or {"rule_leaks": None, "real_names": None, "suspects": None})
        if r["status"] != "ok":
            row["llm"] = {"verdict": "nonanswer", "appropriate": False, "leak": False, "rationale": "service error"}
        elif (hit := llm_cache.get((r["id"], sha, PROMPT_SHA))) is not None:
            row["llm"] = hit
        elif use_llm:
            row["llm"] = llm_judge(c, answer_text, r.get("history"))
        return row

    with cf.ThreadPoolExecutor(max(1, workers)) as ex:
        rows = [x for x in ex.map(one, results) if x]
    rows.sort(key=lambda x: x["id"])
    write_jsonl(jpath, rows)
    return rows


# ── aggregation ──────────────────────────────────────────────────────

def _pct(xs) -> float | None:
    xs = list(xs)
    return round(100 * sum(xs) / len(xs), 1) if xs else None


def quantile(xs, q: float):
    """Nearest-rank quantile (no interpolation): the smallest value with at least q of the samples at or below it."""
    xs = sorted(x for x in xs if x is not None)
    if not xs:
        return None
    return xs[max(0, min(len(xs) - 1, math.ceil(q * len(xs)) - 1))]


def _lat(xs) -> dict:
    xs = [x for x in xs if x is not None]
    return {"n": len(xs), "p50": quantile(xs, 0.5), "p90": quantile(xs, 0.9), "p95": quantile(xs, 0.95),
            "mean": round(statistics.mean(xs)) if xs else None}


def _count(xs) -> dict:
    out: dict = {}
    for x in xs:
        if x is not None:
            out[x] = out.get(x, 0) + 1
    return out


def _mean(xs, nd: int = 2):
    xs = [x for x in xs if x is not None]
    return round(statistics.mean(xs), nd) if xs else None


def summarize(rows: list[dict]) -> dict:
    ok = [r for r in rows if r["status"] == "ok"]
    graded = [r for r in rows if r.get("llm") and r["llm"].get("verdict") and not r["llm"].get("judge_error")]
    acc_rows = [r for r in graded if r["category"] != "spoiler_probe" and r["expect"] != "decline"]
    kw_rows = [r for r in ok if r.get("include_hits") is not None]
    ruled = [r for r in rows if r.get("rule_leaks") is not None]
    leak_rows = [r for r in rows if r.get("rule_leaks") is not None or (r.get("llm") or {}).get("verdict")]
    usage_rows = [r["usage"] for r in ok if r.get("usage")]
    tool_usage: dict = {}
    for r in ok:
        for t in r["tools"]:
            tool_usage[t] = tool_usage.get(t, 0) + 1
    return {
        "n": len(rows), "errors": len(rows) - len(ok), "graded": len(graded),
        "accuracy_pct": _pct(VERDICT_SCORE[r["llm"]["verdict"]] for r in acc_rows),
        "correct_pct": _pct(r["llm"]["verdict"] == "correct" for r in acc_rows),
        "appropriate_pct": _pct(bool(r["llm"].get("appropriate")) for r in graded),
        "keyword_hit_pct": _pct(bool(r["include_hits"]) for r in kw_rows),
        "leak_pct": _pct(bool(r.get("rule_leaks")) or bool((r.get("llm") or {}).get("leak")) for r in leak_rows),
        "leaks_any": sum(1 for r in leak_rows if r.get("rule_leaks") or (r.get("llm") or {}).get("leak")),
        "leaks_rule": sum(1 for r in ruled if r["rule_leaks"]) if ruled else None,
        "leaks_judge_only": sum(1 for r in graded if r["llm"].get("leak") and not r.get("rule_leaks")),
        "rule_leak_pct": _pct(bool(r["rule_leaks"]) for r in ruled),
        "llm_leak_pct": _pct(bool(r["llm"].get("leak")) for r in graded),
        "real_name_leaks": sum(bool(r.get("real_names")) for r in ruled) if ruled else None,
        "guard_rewrite_pct": _pct(bool(r.get("guard_rewritten")) for r in ok if r.get("guard_rewritten") is not None),
        "streamed_pct": _pct(r.get("streamed") for r in ok),
        "latency_total_ms": _lat(r.get("t_total_ms") for r in ok),
        "latency_first_text_ms": _lat(r.get("t_first_text_ms") for r in ok),
        "latency_first_tool_ms": _lat(r.get("t_first_tool_ms") for r in ok),
        "latency_server_ms": _lat(r.get("server_latency_ms") for r in ok),
        "tools_per_answer": _mean(len(r["tools"]) for r in ok),
        "server_tools_per_answer": _mean(len(r["server_tools"]) for r in ok),
        "model_tools_per_answer": _mean(len(r["model_tools"]) for r in ok),
        "tool_usage": dict(sorted(tool_usage.items())),
        "usage_n": len(usage_rows),
        "tokens_in_p50": quantile([u["input"] for u in usage_rows], 0.5),
        "tokens_in_p90": quantile([u["input"] for u in usage_rows], 0.9),
        "tokens_out_p50": quantile([u["output"] for u in usage_rows], 0.5),
        "tokens_out_p90": quantile([u["output"] for u in usage_rows], 0.9),
        "model_calls_per_answer": _mean(u["model_calls"] for u in usage_rows),
        "cost_usd_per_q": _mean((u["cost_usd"] for u in usage_rows), 5),
        "latency_server_first_text_ms": _lat(r.get("server_first_text_ms") for r in ok),
        "flow_mix": ", ".join(f"{k} {v}" for k, v in sorted(_count(r.get("flow") for r in ok).items())) or None,
    }


def _fmt(v) -> str:
    if v is None:
        return "—"
    if isinstance(v, float) and v.is_integer() and abs(v) >= 100:
        return str(int(v))
    return str(v)


def _delta(a, b) -> str:
    if not isinstance(a, (int, float)) or not isinstance(b, (int, float)):
        return ""
    d = round(b - a, 5 if abs(b - a) < 0.01 else 1)
    return f"{d:+g}"


METRICS = [("cases", "n"), ("errors (no answer; graded as non-answers)", "errors"),
           ("accuracy % (correct = 1, partial = 0.5)", "accuracy_pct"),
           ("fully correct %", "correct_pct"), ("appropriate behaviour %", "appropriate_pct"),
           ("key-term hit %", "keyword_hit_pct"), ("spoiler leak % (rules ∪ LLM)", "leak_pct"),
           ("spoiler leaks, cases", "leaks_any"), ("· rule gate", "leaks_rule"), ("· judge only", "leaks_judge_only"),
           ("real-name leaks (cases)", "real_name_leaks"), ("guard rewrites %", "guard_rewrite_pct"),
           ("streamed answers %", "streamed_pct"), ("tool calls / answer", "tools_per_answer"),
           ("· server-run (ctx-)", "server_tools_per_answer"), ("· model-called", "model_tools_per_answer"),
           ("answer flow (count)", "flow_mix"), ("model calls / answer", "model_calls_per_answer"),
           ("input tokens / answer, p50", "tokens_in_p50"), ("input tokens / answer, p90", "tokens_in_p90"),
           ("output tokens (incl. thinking) / answer, p50", "tokens_out_p50"),
           ("output tokens (incl. thinking) / answer, p90", "tokens_out_p90"),
           ("est. cost / answer, USD", "cost_usd_per_q")]
LATENCY = [("server total (answer.latency_ms)", "latency_server_ms"),
           ("server first text (timing.first_text_ms)", "latency_server_first_text_ms"),
           ("client total (adds the network to the bench host)", "latency_total_ms"),
           ("client first text", "latency_first_text_ms"), ("client first tool", "latency_first_tool_ms")]


def report(runs: dict[str, list[dict]], out: pathlib.Path, meta: dict | None = None) -> str:
    shared = set.intersection(*({r["id"] for r in rows} for rows in runs.values()))
    partial = {n: len(rows) - len(shared) for n, rows in runs.items() if len(rows) > len(shared)}
    runs = {n: [r for r in rows if r["id"] in shared] for n, rows in runs.items()}     # compare like with like
    names = list(runs)
    two = len(names) == 2
    files = sorted({r["file"] for rows in runs.values() for r in rows})
    groups = {"all": lambda r: True, "set: base": lambda r: r["set"] == "base", "set: new": lambda r: r["set"] == "new"}
    groups.update({f"file: {f}": (lambda f: lambda r: r["file"] == f)(f) for f in files})
    cats = sorted({r["category"] for rows in runs.values() for r in rows})
    card = {"runs": names, "meta": meta or {}, "prompt_sha": PROMPT_SHA,
            "groups": {g: {n: summarize([r for r in rows if f(r)]) for n, rows in runs.items()} for g, f in groups.items()},
            "by_category": {cat: {n: summarize([r for r in rows if r["category"] == cat]) for n, rows in runs.items()}
                            for cat in cats}}
    out.mkdir(parents=True, exist_ok=True)
    (out / "scorecard.json").write_text(json.dumps(card, ensure_ascii=False, indent=1), encoding="utf-8")

    head = " | ".join(names) + (" | Δ" if two else "")
    sep = "|---|" + "---|" * (len(names) + two)
    L = [f"# Benchmark scorecard: {' vs '.join(names)}", ""]
    for n in names:
        m = (meta or {}).get(n) or {}
        L.append(f"- **{n}**: base `{m.get('base', '?')}`, flow `{m.get('flow') or 'server default'}`, "
                 f"commit `{m.get('commit') or '?'}`"
                 + (f", {m['label']}" if m.get("label") else "") + f", judge prompt `{PROMPT_SHA}`")
    if partial:
        L += ["", f"> Only the {len(shared)} cases present in every run are compared (left out: "
              + ", ".join(f"{n} {k}" for n, k in partial.items()) + ")."]
    allg = card["groups"]["all"]
    L += ["", "## Overview (all cases)", "", f"| metric | {head} |", sep]
    for label, k in METRICS:
        vals = [allg[n][k] for n in names]
        L.append(f"| {label} | " + " | ".join(_fmt(v) for v in vals) + (f" | {_delta(*vals)}" if two else "") + " |")
    L += ["", "## Latency, ms (answered cases; p50 / p90 / p95)", "", f"| measure | {head} |", sep]
    for label, k in LATENCY:
        cells = [allg[n][k] for n in names]
        L.append(f"| {label} | " + " | ".join(f"{_fmt(x['p50'])} / {_fmt(x['p90'])} / {_fmt(x['p95'])}" for x in cells)
                 + (f" | {_delta(cells[0]['p50'], cells[1]['p50'])} (p50)" if two else "") + " |")

    def compact(title: str, table: dict) -> None:
        L.extend(["", f"## {title}", "", "| group | n | " + " | ".join(f"acc % {n}" for n in names) + " | "
                  + " | ".join(f"leaks rule / judge-only {n}" for n in names) + " | "
                  + " | ".join(f"server p50 ms {n}" for n in names) + " |",
                  "|---|---|" + "---|" * (3 * len(names))])
        for g, per in table.items():
            if not any(per[n]["n"] for n in names):
                continue
            L.append(f"| {g} | {per[names[0]]['n']} | " + " | ".join(_fmt(per[n]["accuracy_pct"]) for n in names) + " | "
                     + " | ".join(f"{_fmt(per[n]['leaks_rule'])} / {_fmt(per[n]['leaks_judge_only'])}" for n in names)
                     + " | " + " | ".join(_fmt(per[n]["latency_server_ms"]["p50"]) for n in names) + " |")

    compact("By set and case file", {g: v for g, v in card["groups"].items() if g != "all"})
    compact("By category", card["by_category"])
    summary = L[:]                                   # the job-log summary shares everything up to here
    L += ["", "## Tool usage", ""]
    for n in names:
        L.append(f"- **{n}**: " + (", ".join(f"{t} ×{c}" for t, c in allg[n]["tool_usage"].items()) or "—"))
    L += ["", "## Leaks", ""]
    leak_lines = []
    for n, rows in runs.items():
        for r in rows:
            llm = r.get("llm") or {}
            if r.get("rule_leaks") or llm.get("leak") or r.get("real_names"):
                why = (r.get("rule_leaks") or []) + [f"real:{t}" for t in r.get("real_names") or []]
                if llm.get("leak"):
                    why.append(f"llm: {llm.get('leak_quote', '')[:120]!r} ⇐ {llm.get('leak_fact', '')[:120]}")
                kind = "rule" if r.get("rule_leaks") or r.get("real_names") else "judge only"
                leak_lines.append(f"- **{n}** `{r['id']}` ({r['category']}) [{kind}] — " + "; ".join(why))
    L += leak_lines[:MAX_LEAK_LINES] or ["- none"]
    if len(leak_lines) > MAX_LEAK_LINES:
        L.append(f"- … {len(leak_lines) - MAX_LEAK_LINES} more in scorecard.json / judged.jsonl")
    err_lines = error_lines(runs)
    if err_lines:
        L += ["", "## Errors", ""] + err_lines[:MAX_ERROR_LINES]
        if len(err_lines) > MAX_ERROR_LINES:
            L.append(f"- … {len(err_lines) - MAX_ERROR_LINES} more")
    md = "\n".join(L) + "\n"
    (out / "scorecard.md").write_text(md, encoding="utf-8")
    summary += ["", "## Failing cases", ""] + failing_lines(runs)
    if err_lines:
        summary += ["", f"## Errors ({len(err_lines)})", ""] + err_lines[:MAX_ERROR_LINES]
    if leak_lines:
        summary += ["", f"## Leaks (first {min(10, len(leak_lines))} of {len(leak_lines)})", ""] + leak_lines[:10]
    summary += ["", f"Full scorecard: {out / 'scorecard.md'}"]
    (out / "summary.md").write_text("\n".join(summary) + "\n", encoding="utf-8")
    return md


def error_lines(runs: dict[str, list[dict]]) -> list[str]:
    """One line per errored case: what the client saw (HTTP status, how the stream ended, the error event)."""
    out = []
    for n, rows in runs.items():
        for r in rows:
            if r["status"] != "ok":
                how = ", ".join(x for x in (f"HTTP {r['http_status']}" if r.get("http_status") else "",
                                            f"stream {r['stream_end']}" if r.get("stream_end") else "",
                                            f"{r['t_total_ms']} ms" if r.get("t_total_ms") is not None else "") if x)
                out.append(f"- **{n}** `{r['id']}` ({how or '?'}) — {str(r.get('error'))[:220]}")
    return out


def failing_lines(runs: dict[str, list[dict]], max_chars: int = 400) -> list[str]:
    """Case ids per failure kind and run, one line each (for short job logs)."""
    out = []
    for n, rows in runs.items():
        kinds: dict[str, list[str]] = {"error": [], "leak (rule gate)": [], "leak (judge only)": [],
                                       "incorrect / nonanswer": [],
                                       "declined, but the answer was on screen": [], "inappropriate": []}
        for r in rows:
            llm = r.get("llm") or {}
            if r["status"] != "ok":
                kinds["error"].append(r["id"])
                continue
            if r.get("rule_leaks") or r.get("real_names"):
                kinds["leak (rule gate)"].append(r["id"])
            elif llm.get("leak"):
                kinds["leak (judge only)"].append(r["id"])
            if llm.get("verdict") in ("incorrect", "nonanswer"):
                kinds["incorrect / nonanswer"].append(r["id"])
            elif llm.get("verdict") == "declined" and r["expect"] == "answer":
                kinds["declined, but the answer was on screen"].append(r["id"])
            elif llm.get("appropriate") is False:
                kinds["inappropriate"].append(r["id"])
        out.append(f"- **{n}**" + ("" if any(kinds.values()) else ": none"))
        for k, ids in kinds.items():
            if ids:
                text = ", ".join(ids)
                out.append(f"  - {k} ({len(ids)}): " + (text if len(text) <= max_chars else text[:max_chars] + " …"))
    return out


# ── lint ─────────────────────────────────────────────────────────────

def _strings(x):
    if isinstance(x, str):
        yield x
    elif isinstance(x, dict):
        for v in x.values():
            yield from _strings(v)
    elif isinstance(x, list):
        for v in x:
            yield from _strings(v)


def _catalog() -> dict:
    doc = json.loads((REPO / "catalog" / "catalog.json").read_text(encoding="utf-8"))
    out = {}
    for it in doc["items"]:
        spec_path = REPO / "ingest" / "titles" / f"{it['id']}.json"
        spec = json.loads(spec_path.read_text(encoding="utf-8")) if spec_path.exists() else {}
        out[it["id"]] = {"title": it["title"], "duration_s": it.get("duration_s"),
                         "shas": spec.get("expected_asset_sha256") or []}
    return out


def lint(cases: list[dict], with_packs: bool) -> int:
    """Offline consistency checks; with packs also the gate checks (must_not_include must not be eligible at P yet)."""
    errors, warns = [], []
    cat = _catalog()
    for c in cases:
        cid = c["id"]
        errors += [f"{cid}: {e}" for e in C.validate(c)]
        item = cat.get(c.get("content_id"))
        if item is None:
            errors.append(f"{cid}: content_id not in catalog/catalog.json")
        else:
            if c.get("title") != item["title"]:
                errors.append(f"{cid}: title {c.get('title')!r} ≠ catalog {item['title']!r}")
            if item["shas"] and c.get("asset_sha256") not in item["shas"]:
                errors.append(f"{cid}: asset_sha256 is not the title spec's expected asset")
            dur = item["duration_s"]
            if dur and isinstance(c.get("P"), (int, float)) and c["P"] > dur:
                errors.append(f"{cid}: P={c['P']} beyond duration {dur}")
        text = "\n".join(_strings({k: v for k, v in c.items() if not k.startswith("_")}))
        errors += [f"{cid}: contains {s!r}" for s in FORBIDDEN if s in text]
        meta_text = "\n".join([c.get("notes") or "", c.get("source") or ""]
                              + [m.get("note", "") for f in ("must_include_any", "must_not_include", "flag_if_any")
                                 for m in c.get(f) or [] if isinstance(m, dict)])
        if CJK_ANY.search(meta_text):
            errors.append(f"{cid}: notes / source must be English")
        if (case_set(c) == "new") != cid.startswith("new:"):
            warns.append(f"{cid}: new-set ids start with 'new:' (set={case_set(c)})")
        qtext = viewer_text(c)
        if c.get("lang") == "zh" and not CJK_ANY.search(c["question"]):
            warns.append(f"{cid}: lang zh but the question has no Chinese")
        if c.get("lang") == "en" and CJK_ANY.search(qtext):
            warns.append(f"{cid}: lang en but the question has Chinese")
        ref = "\n".join([c.get("reference") or ""] + [str(marker_label(m)) for m in c.get("must_include_any") or []])
        clash = hits(c.get("must_not_include"), ref)
        if clash:
            errors.append(f"{cid}: must_not_include {clash} matches its own reference / must_include_any")
        if c.get("category") == "followup" and not c.get("history"):
            errors.append(f"{cid}: category followup needs history")
        if c.get("category") == "injection" and c.get("expect") == "decline" and not c.get("must_not_include"):
            warns.append(f"{cid}: injection probe without must_not_include")
    if with_packs:
        for c in cases:
            if c["id"].startswith("probe:"):
                continue
            txt = eligible_text(c)
            legal = [m for m in c.get("must_not_include") or []
                     if not (isinstance(m, dict) and m.get("lint_allow_eligible")) and marker_hits(m, txt)]
            if legal:
                errors.append(f"{c['id']} P={c['P']}: must_not_include already eligible at P: "
                              f"{[marker_label(m) for m in legal]}")
            if c.get("must_include_any") and c["lang"] == "en" and not hits(c["must_include_any"], txt):
                warns.append(f"{c['id']} P={c['P']}: no must_include_any term found verbatim in eligible text")
    for w in warns:
        print("note", w)
    for e in errors:
        print("ERROR", e)
    by: dict = {}
    for c in cases:
        by.setdefault((c["_file"], c["category"]), 0)
        by[(c["_file"], c["category"])] += 1
    files = sorted({f for f, _ in by})
    print(f"lint: {len(cases)} cases in {len(files)} files, {len(errors)} errors, {len(warns)} notes"
          + ("" if with_packs else " (no packs: gate checks skipped)"))
    for f in files:
        print(f"  {f}: " + ", ".join(f"{cat} {n}" for (ff, cat), n in sorted(by.items()) if ff == f))
    return 1 if errors else 0


# ── main ─────────────────────────────────────────────────────────────

def _run_path(spec: str) -> pathlib.Path:
    p = pathlib.Path(spec)
    return p if p.exists() else RUNS_DIR / spec


def main(argv=None) -> int:
    global PACKS
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("runs", nargs="*", help="run dirs or run ids under bench/runs; a+b merges re-runs")
    ap.add_argument("--cases", nargs="*", default=[str(CASES_DIR)])
    ap.add_argument("--packs", help="master packs root (<asset_sha>/master/pack.db); default BENCH_PACKS_DIR / "
                                    "WATCH_PACKS_DIR")
    ap.add_argument("--lint", action="store_true")
    ap.add_argument("--no-llm", action="store_true", help="do not call the LLM judge (cached grades are still used)")
    ap.add_argument("--no-rules", action="store_true", help="no pack lookups (cached rule results are still used)")
    ap.add_argument("--model", default=DEFAULT_MODEL, help="answering model for cost estimates when usage has none")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--out")
    ap.add_argument("--quiet", action="store_true", help="do not print the scorecard")
    ap.add_argument("--summary", action="store_true",
                    help="print the short summary (overview, latency, groups, failing case ids) instead of the full "
                         "scorecard; both are written to the output directory")
    args = ap.parse_args(argv)
    PACKS = pathlib.Path(args.packs).expanduser() if args.packs else C.packs_dir()
    if args.lint:
        return lint(read_cases(args.cases, strict=False), with_packs=PACKS is not None)
    if not args.runs:
        ap.error("give at least one run (or --lint)")
    if not args.no_rules and PACKS is None:
        ap.error("the rule gate needs --packs (or BENCH_PACKS_DIR); use --no-rules to skip it")
    by_id = {c["id"]: c for c in read_cases(args.cases)}
    runs, meta = {}, {}
    for spec in args.runs:
        merged, name, m = {}, None, {}
        for rd in spec.split("+"):                 # a+b: later runs override earlier ones per case id
            p = _run_path(rd)
            if not (p / "results.jsonl").exists():
                raise SystemExit(f"no results.jsonl in {p}")
            m = json.loads((p / "run.json").read_text(encoding="utf-8")) if (p / "run.json").exists() else {}
            name = name or p.name
            merged.update({r["id"]: r for r in judge_run(p, by_id, not args.no_llm, not args.no_rules, args.workers,
                                                         args.model)})
        name = name if name not in runs else spec
        runs[name] = sorted(merged.values(), key=lambda r: r["id"])
        meta[name] = {k: m.get(k) for k in ("base", "flow", "commit", "label", "started", "workers")}
    if args.out:
        out = pathlib.Path(args.out)
    elif len(args.runs) == 1:
        out = _run_path(args.runs[0].split("+")[0])
    else:
        out = RUNS_DIR / ("compare-" + "-vs-".join(pathlib.Path(r).name.replace("+", "_") for r in args.runs))
    md = report(runs, out, meta)
    if args.summary:
        print((out / "summary.md").read_text(encoding="utf-8"))
    elif not args.quiet:
        print(md)
    print(f"→ {out / 'scorecard.md'}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
