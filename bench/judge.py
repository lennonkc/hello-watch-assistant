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
import functools
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
    "leak_channel": {"type": "string", "enum": ["answer", "evidence", "frame", "highlight", "tool", "streamed",
                                                "history", "none"]},
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
    """Every text eligible at the case's P (see `PackView.eligible_text`)."""
    return _pack(c).eligible_text(c["P"])


def future_facts(c: dict, limit: int = 30) -> list[str]:
    """What the judge must treat as future at P: the next `limit` items, EVERY later recap (an ending revealed from an
    early P is in one of them), hidden names, pending identity links and every canary still active, compactly."""
    pv, P = _pack(c), c["P"]
    fut = sorted((it for it in pv.gate(P).candidates() if it.item_type in FUTURE_TYPES and it.text
                  and P + 1e-9 < it.eligible_from < math.inf), key=lambda it: it.eligible_from)
    out, seen = [], set()
    for it in fut[:limit * 3]:
        if it.text not in seen and len(seen) < limit:
            seen.add(it.text)
            out.append(f"[{mmss(it.start_s)} {it.item_type}] {it.text}")
    for it in fut:
        if it.item_type == "recap" and it.text not in seen:
            seen.add(it.text)
            out.append(f"[{mmss(it.start_s)} recap] {it.text}")
    for term, _cs, cid, kind in pv.hidden_terms(P):
        label = pv.chars[cid]["display_label_pre_name"]
        out.append(f"[reveal] the character currently known as '{label}' is later identified as '{term}' ({kind})")
    known = pv.known_names(P)
    if known:
        out.append("[NOT future — already known at P, fine to use, incl. translations] names / aliases: " + ", ".join(known))
    if public := pv.public_names():
        out.append("[NOT future — public names (title page / synopsis), allowed at ANY time, never a leak by "
                   "themselves] " + ", ".join(public))
    for lk in pv.pending_links(P):
        a, b = pv.chars.get(lk["a_character_id"]), pv.chars.get(lk["b_character_id"])
        if a and b:
            out.append(f"[identity reveal at {mmss(lk['reveal_at_s'])}] '{a['display_label_pre_name']}' is actually "
                       f"'{b['canonical_name']}' ({b['display_label_pre_name']})")
    cans = sorted(pv.active_canaries(P), key=lambda k: (k["active_until_s"], k["term"]))
    if cans:
        out.append("[words that point at later reveals; fine only where they describe something ALREADY SHOWN] "
                   + "; ".join(f"{k['term']} ({k.get('kind')}, until {mmss(k['active_until_s'])})" for k in cans))
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


def seen_items(c: dict, P: float) -> list[tuple[str, str]]:
    return _pack(c).seen_items(P)


def channels(c: dict, r: dict) -> list[tuple[str, str, float]]:
    """Everything the viewer received, as (channel, text, P): the answer (the only part that is graded), then — for
    leaks only — evidence rows, frame captions, highlight labels, tool arguments and summaries, streamed text that
    differs from the answer, and the answers of earlier turns at their own P."""
    P, raw = c["P"], r.get("raw") or {}
    ans = raw.get("answer") or {}
    out = [("answer", r.get("answer") or "", P)]
    ev = ans.get("evidence") if isinstance(ans.get("evidence"), list) else [{"text": t} for t in r.get("evidence_text") or []]
    out += [("evidence", e["text"], P) for e in ev if isinstance(e, dict) and e.get("text")]
    out += [("frame", f["caption"], P) for f in ans.get("frames") or [] if isinstance(f, dict) and f.get("caption")]
    events = raw.get("events") or []
    for h in [ans.get("highlight")] + [e.get("data") for e in events if e.get("event") == "highlight"]:
        if isinstance(h, dict) and h.get("label"):
            out.append(("highlight", str(h["label"]), P))
    for e in events:
        d = e.get("data") if isinstance(e.get("data"), dict) else {}
        if e.get("event") == "tool" and d.get("args"):
            if args := " | ".join(_strings(d["args"])):
                out.append(("tool_args", args, P))
        elif e.get("event") == "tool_result" and d.get("summary"):
            out.append(("tool_summary", str(d["summary"]), P))
    if r.get("streamed_text"):
        out.append(("streamed", r["streamed_text"], P))
    for i, (h, got) in enumerate(zip(c.get("history") or [], r.get("history") or [])):
        if (got or {}).get("answer"):
            out.append((f"history{i + 1}", got["answer"], h.get("P", P)))
    return list(dict.fromkeys(out))


def time_problems(c: dict, r: dict) -> list[dict]:
    """Evidence rows, frames and the highlight must not come from after P."""
    P = c["P"]
    ans = (r.get("raw") or {}).get("answer") or {}
    out = []
    for e in ans.get("evidence") or []:
        for k in ("start_s", "end_s"):
            if isinstance(e, dict) and isinstance(e.get(k), (int, float)) and e[k] > P + 1e-6:
                out.append({"rule": f"time:evidence {k} {mmss(e[k])} > P", "where": "evidence",
                            "match": str(e.get("text") or "")[:80]})
    for f in ans.get("frames") or []:
        if isinstance(f, dict) and isinstance(f.get("t"), (int, float)) and f["t"] > math.floor(P + 1e-9):
            out.append({"rule": f"time:frame {mmss(f['t'])} > P", "where": "frame", "match": str(f.get("caption") or "")})
    events = (r.get("raw") or {}).get("events") or []
    for h in [ans.get("highlight")] + [e.get("data") for e in events if e.get("event") == "highlight"]:
        if isinstance(h, dict) and isinstance(h.get("frame_t"), (int, float)) and h["frame_t"] > math.floor(P + 1e-9):
            out.append({"rule": f"time:highlight {mmss(h['frame_t'])} > P", "where": "highlight",
                        "match": str(h.get("label") or "")})
    return list({x["rule"]: x for x in out}.values())


def _only(src: list[tuple[str, str]], m) -> list[tuple[str, str]]:
    """The channels a marker applies to: all, or its `channels` (history1, history2, … count as "history")."""
    want = m.get("channels") if isinstance(m, dict) else None
    return src if not want else [(w, t) for w, t in src if (w.rstrip("0123456789") or w) in want]


def link_marker_ok(pv, P: float, m) -> bool:
    """A marker with `"link": true` (or a link id) names one side of an identity link that is still pending at P:
    the case's subject is the other side, so the name is barred although it may be public or already eligible."""
    if not (isinstance(m, dict) and m.get("link") and m.get("term")):
        return False
    term = C.N(m["term"])
    for lk in pv.pending_links(P):
        if isinstance(m["link"], str) and lk.get("link_id") != m["link"]:
            continue
        sides = (lk["a_character_id"], lk["b_character_id"])
        names = [t for cid in sides if cid in pv.chars for t in pv.name_variants(cid)]
        names += [a["alias"] for a in pv.aliases if a["character_id"] in sides]
        if term in {C.N(t) for t in names}:
            return True
    return False


def _is_public(m, public: set) -> bool:
    return not (isinstance(m, dict) and "regex" in m) and C.N(C.marker_label(m)) in public


def rule_check(c: dict, text: str, evidence=(), streamed: str | None = None, extra=(), times=()) -> dict:
    """Rule gate over the answer, evidence texts and streamed text (+ `extra` channels as (channel, text, P))."""
    P = c["P"]
    chans = [("answer", text or "", P)] + [("evidence", e, P) for e in evidence or () if e] + \
        ([("streamed", streamed, P)] if streamed else []) + list(extra)
    return check_channels(c, chans, times)


def check_channels(c: dict, chans: list[tuple[str, str, float]], times=()) -> dict:
    """Rule gate over what the viewer received (see `channels`), each channel at its own P. Each hit is listed once
    with where it matched (`rule_hits`: rule, where, the matched part with context); a label gets ` @<channel>` when
    it did not match in the answer.
    - hidden name / alias not yet visible: leak; a suspect ` (echo)` when the viewer typed it (echoing is fine,
      affirming it is a leak, which only the LLM judge can tell);
    - canary still active: leak; a suspect ` (echo)` when typed by the viewer, or ` (already eligible: <item>)` when
      the term occurs in one item the viewer could have seen by then (`PackView.seen_items`);
    - blocklisted real name (`real_names`); the case's must_not_include (leak) / flag_if_any (suspect);
    - `times`: evidence / frame / highlight from after P (leak)."""
    pv, q = _pack(c), viewer_text(c)
    public = {C.N(t) for t in pv.public_names()}
    leaks, suspects, real, found = [], [], [], []

    def record(bucket: list, label: str, where: str, sp) -> None:
        label = label if where == "answer" else f"{label} @{where}"
        if label not in bucket:
            bucket.append(label)
            found.append({"rule": label, "where": where, "match": C.snippet(sp)})

    by_p: dict = {}
    for where, text, P in chans:
        if text:
            by_p.setdefault(P, []).append((where, text))

    def first(src, span_of):
        for where, t in src:
            if (sp := span_of(t)) is not None:
                return where, sp
        return None

    for P, src in sorted(by_p.items()):
        for term, cs, _cid, kind in pv.hidden_terms(P):
            m = {"term": term, "case_sensitive": cs}
            if hit := first(src, lambda t: C.marker_span(m, t)):
                echo = marker_hits(m, q)
                record(suspects if echo else leaks, f"hidden_{kind}:{term}" + (" (echo)" if echo else ""), *hit)
        items = None
        for k in pv.active_canaries(P):
            if not (hit := first(src, lambda t: C.canary_span(k, t))):
                continue
            if C.canary_hits(k, q):
                record(suspects, f"canary:{k['term']} (echo)", *hit)
                continue
            items = seen_items(c, P) if items is None else items
            where_seen = next((lab for lab, t in items if C.canary_hits(k, t)), None)
            record(suspects if where_seen else leaks,
                   f"canary:{k['term']}" + (f" (already eligible: {where_seen})" if where_seen else ""), *hit)
        for b in pv.blocklist:
            if (hit := first(src, lambda t: C.marker_span({"term": b["term"]}, t))) and b["term"] not in real:
                real.append(b["term"])
                found.append({"rule": f"real:{b['term']}", "where": hit[0], "match": C.snippet(hit[1])})
        for m in c.get("must_not_include") or []:
            if hit := first(_only(src, m), lambda t: C.marker_span(m, t)):
                record(leaks, f"must_not_include:{C.marker_label(m)}", *hit)
        for m in c.get("flag_if_any") or []:
            if _is_public(m, public):
                continue                                 # a public name is allowed at any time: not even a suspect
            if hit := first(_only(src, m), lambda t: C.marker_span(m, t)):
                record(suspects, f"flag:{C.marker_label(m)}", *hit)
    for t in times:
        leaks.append(t["rule"])
        found.append(t)
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


def shown_block(chans: list[tuple[str, str, float]]) -> str:
    lines = [f"- [{w}" + (f", earlier turn at P = {p}" if w.startswith("history") else "") + f"] {t}"
             for w, t, p in chans if w != "answer" and t]
    return "\n".join(lines[:40]) or "- (nothing else)"


def render_prompt(c: dict, answer: str, known: list[str], future: list[str], history: list | None = None,
                  shown: str = "- (nothing else)") -> str:
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
                      known="\n".join(f"- {f}" for f in known) or "- (not listed)", answer=answer or "(empty)",
                      shown=shown)


def llm_judge(c: dict, text: str, history: list | None = None, shown: str = "- (nothing else)") -> dict:
    from ingest import gemini
    fut = future_facts(c)
    fin = None
    # occasional empty responses (truncation, or a long plot excerpt tripping a content filter): shrink "already
    # shown" step by step and retry
    for known_n, max_out in ((40, 2048), (40, 4096), (12, 4096), (0, 4096)):
        prompt = render_prompt(c, text, known_facts(c, known_n), fut, history, shown)
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

@functools.lru_cache(maxsize=1)
def fallback_texts() -> dict[str, str]:
    """Fixed replies of the service (text → "spoiler" / "timeout"), read from server/main.py without importing it (the
    benchmark never imports the service); used when the answer carries no `guard.fallback`."""
    import ast
    out = {}
    tree = ast.parse((REPO / "server" / "main.py").read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            kind = {"FALLBACK": "spoiler", "TIMEOUT_FALLBACK": "timeout"}.get(node.targets[0].id)
            if kind:
                try:
                    out.update({" ".join(v.split()): kind for v in ast.literal_eval(node.value).values()})
                except ValueError:
                    pass
    return out


def fallback_kind(r: dict) -> str | None:
    if r.get("status") != "ok":
        return None
    return r.get("guard_fallback") or fallback_texts().get(" ".join((r.get("answer") or "").split()))


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


def case_sha(c: dict) -> str:
    """Content of a case as graded (every field but the file it came from)."""
    return hashlib.sha256(json.dumps({k: v for k, v in c.items() if not k.startswith("_")}, sort_keys=True,
                                     ensure_ascii=False).encode()).hexdigest()[:16]


def pack_key(c: dict) -> str | None:
    """`<sha12>:<pack_version>` of the pack the rules use (None without packs)."""
    if C.pack_path(c["asset_sha256"], PACKS) is None:
        return None
    return f"{c['asset_sha256'][:12]}:{_pack(c).data['pack_meta'].get('pack_version')}"


def judge_run(run_dir: pathlib.Path, cases: dict, use_llm: bool, use_rules: bool, workers: int,
              model: str = DEFAULT_MODEL) -> list[dict]:
    """Grade every result of a run. Accuracy, key terms and "appropriate" look at the answer text only; every other
    channel (see `channels`) is checked for leaks. Grades are cached in judged.jsonl under (case id, case content,
    what was received, pack, judge prompt, judge model), so re-grading costs nothing until one of them changes."""
    results = [_legacy(r) for r in read_jsonl(run_dir / "results.jsonl")]
    jpath = run_dir / "judged.jsonl"
    llm_cache, rule_cache = {}, {}
    for r in read_jsonl(jpath):
        k = r.get("cache") or {}
        if r.get("llm") and r["llm"].get("prompt_sha") and not r["llm"].get("judge_error") and k:
            llm_cache[(r["id"], r["answer_sha"], k.get("case"), k.get("pack"), r["llm"]["prompt_sha"],
                       r["llm"].get("judge_model"))] = r["llm"]
        if r.get("rule_leaks") is not None and k:
            rule_cache[(r["id"], r["answer_sha"], k.get("case"), k.get("pack"))] = {
                x: r.get(x) for x in ("rule_leaks", "real_names", "suspects", "rule_hits")}
    jm = judge_model() if use_llm or llm_cache else None

    def one(r):
        c = cases.get(r["id"])
        if c is None:
            return None
        answer = r.get("answer") or ""
        chans = channels(c, r) if r["status"] == "ok" else [("answer", answer, c["P"])]
        sha = hashlib.sha256(json.dumps(chans, ensure_ascii=False).encode()).hexdigest()[:16]
        key = {"case": case_sha(c), "pack": pack_key(c) if use_rules or PACKS else None}
        row = {"id": r["id"], "file": c["_file"], "set": case_set(c), "private": bool(c.get("private")),
               "category": c["category"], "title": c["title"],
               "lang": c["lang"], "expect": c["expect"], "status": r["status"], "error": r.get("error"),
               "http_status": r.get("http_status"), "stream_end": r.get("stream_end"), "P": c["P"],
               "t_total_ms": r.get("t_total_ms"), "t_first_text_ms": r.get("t_first_text_ms"),
               "t_first_tool_ms": r.get("t_first_tool_ms"), "server_latency_ms": r.get("server_latency_ms"),
               "streamed": bool(r.get("streamed")), "tools": r.get("tools") or [],
               "server_tools": r.get("server_tools") or [], "model_tools": r.get("model_tools") or [],
               "guard_rewritten": r.get("guard_rewritten"), "fallback": fallback_kind(r),
               "usage": usage_summary(r.get("usage"), model),
               "server_first_text_ms": (r.get("timing") or {}).get("first_text_ms"), "flow": r.get("flow"),
               "answer": answer, "answer_sha": sha, "cache": key,
               "channels": sorted({w for w, t, _p in chans if t and w != "answer"}),
               "n_evidence": len(r.get("evidence_text") or []) if r["status"] == "ok" else None,
               "include_hits": hits(c.get("must_include_any"), answer) if c.get("must_include_any") else None}
        if use_rules:
            row.update(check_channels(c, chans, time_problems(c, r)))
        else:
            row.update(_cached(rule_cache, (r["id"], sha, key["case"], key["pack"]))
                       or {"rule_leaks": None, "real_names": None, "suspects": None})
        if r["status"] != "ok":
            row["llm"] = {"verdict": "nonanswer", "appropriate": False, "leak": False, "rationale": "service error"}
        elif (hit := _cached(llm_cache, (r["id"], sha, key["case"], key["pack"], PROMPT_SHA, jm))) is not None:
            row["llm"] = hit
        elif use_llm:
            row["llm"] = llm_judge(c, answer, r.get("history"), shown_block(chans))
        return row

    with cf.ThreadPoolExecutor(max(1, workers)) as ex:
        rows = [x for x in ex.map(one, results) if x]
    rows.sort(key=lambda x: x["id"])
    write_jsonl(jpath, rows)
    return rows


def _cached(cache: dict, key: tuple):
    """Exact key; without packs (pack None) a grade made with any pack version is accepted, other parts must match."""
    if key in cache:
        return cache[key]
    if key[3] is None:
        return next((v for k, v in cache.items() if k[:3] == key[:3] and k[4:] == key[4:]), None)
    return None


# ── aggregation ──────────────────────────────────────────────────────

def _pct(xs) -> float | None:
    xs = list(xs)
    return round(100 * sum(xs) / len(xs), 1) if xs else None


def quantile(xs, q: float):
    """Linear interpolation between the closest ranks (numpy's default): the median of [1, 2, 3, 4] is 2.5."""
    xs = sorted(x for x in xs if x is not None)
    if not xs:
        return None
    pos = q * (len(xs) - 1)
    lo, hi = math.floor(pos), math.ceil(pos)
    return round(xs[lo] + (xs[hi] - xs[lo]) * (pos - lo), 1)


MIN_N_P95 = 20                                     # below that, p95 is just the maximum: not printed


def _lat(xs) -> dict:
    xs = [x for x in xs if x is not None]
    return {"n": len(xs), "p50": quantile(xs, 0.5), "p90": quantile(xs, 0.9),
            "p95": quantile(xs, 0.95) if len(xs) >= MIN_N_P95 else None,
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
    # service errors are graded (nonanswer, not appropriate); a judge failure leaves a row ungraded (own line)
    graded = [r for r in rows if r.get("llm") and r["llm"].get("verdict") and not r["llm"].get("judge_error")]
    acc_rows = [r for r in graded if r["category"] != "spoiler_probe" and r["expect"] != "decline"]
    kw_rows = [r for r in ok if r.get("include_hits") is not None]
    ruled = [r for r in rows if r.get("rule_leaks") is not None]
    leak_rows = [r for r in rows if r.get("rule_leaks") is not None or (r.get("llm") or {}).get("verdict")]
    usage_rows = [r["usage"] for r in ok if r.get("usage")]
    ev_rows = [r for r in ok if r.get("n_evidence") is not None and r["expect"] != "decline"]
    tool_usage: dict = {}
    for r in ok:
        for t in r["tools"]:
            tool_usage[t] = tool_usage.get(t, 0) + 1
    return {
        "n": len(rows), "errors": len(rows) - len(ok), "graded": len(graded), "ungraded": len(rows) - len(graded),
        "judge_errors": sum(1 for r in rows if (r.get("llm") or {}).get("judge_error")),
        "accuracy_n": len(acc_rows), "keyword_n": len(kw_rows), "rule_n": len(ruled), "leak_n": len(leak_rows),
        "evidence_n": len(ev_rows), "evidence_en_n": sum(1 for r in ev_rows if r.get("lang") == "en"),
        "evidence_zh_n": sum(1 for r in ev_rows if r.get("lang") == "zh"),
        "suspects_any": sum(1 for r in ruled if r.get("suspects")),
        "accuracy_pct": _pct(VERDICT_SCORE[r["llm"]["verdict"]] for r in acc_rows),
        "correct_pct": _pct(r["llm"]["verdict"] == "correct" for r in acc_rows),
        "appropriate_pct": _pct(bool(r["llm"].get("appropriate")) for r in graded),
        "keyword_hit_pct": _pct(bool(r["include_hits"]) for r in kw_rows),
        "evidence_pct": _pct(r["n_evidence"] > 0 for r in ev_rows),
        "evidence_pct_en": _pct(r["n_evidence"] > 0 for r in ev_rows if r.get("lang") == "en"),
        "evidence_pct_zh": _pct(r["n_evidence"] > 0 for r in ev_rows if r.get("lang") == "zh"),
        "leak_pct": _pct(bool(r.get("rule_leaks")) or bool((r.get("llm") or {}).get("leak")) for r in leak_rows),
        "leaks_any": sum(1 for r in leak_rows if r.get("rule_leaks") or (r.get("llm") or {}).get("leak")),
        "leaks_rule": sum(1 for r in ruled if r["rule_leaks"]) if ruled else None,
        "leaks_judge_only": sum(1 for r in graded if r["llm"].get("leak") and not r.get("rule_leaks")),
        "rule_leak_pct": _pct(bool(r["rule_leaks"]) for r in ruled),
        "llm_leak_pct": _pct(bool(r["llm"].get("leak")) for r in graded),
        "real_name_leaks": sum(bool(r.get("real_names")) for r in ruled) if ruled else None,
        "guard_rewrite_pct": _pct(bool(r.get("guard_rewritten")) for r in ok if r.get("guard_rewritten") is not None),
        "fallback_pct": _pct(bool(r.get("fallback")) for r in ok), "ok_n": len(ok),
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


# (label, key, key of its denominator or None)
METRICS = [("cases", "n", None), ("errors (no answer; graded as non-answers)", "errors", None),
           ("ungraded (judge failed or not run)", "ungraded", None),
           ("accuracy % (correct = 1, partial = 0.5)", "accuracy_pct", "accuracy_n"),
           ("fully correct %", "correct_pct", "accuracy_n"), ("appropriate behaviour %", "appropriate_pct", "graded"),
           ("key-term hit % (answer text only)", "keyword_hit_pct", "keyword_n"),
           ("answers with ≥ 1 evidence line % (expect ≠ decline)", "evidence_pct", "evidence_n"),
           ("· en", "evidence_pct_en", "evidence_en_n"), ("· zh", "evidence_pct_zh", "evidence_zh_n"),
           ("spoiler leak % (rules ∪ LLM)", "leak_pct", "leak_n"),
           ("spoiler leaks, cases", "leaks_any", None), ("· rule gate", "leaks_rule", "rule_n"),
           ("· judge only", "leaks_judge_only", "graded"),
           ("rule-gate suspects, cases (not counted as leaks)", "suspects_any", "rule_n"),
           ("real-name leaks (cases)", "real_name_leaks", "rule_n"), ("guard rewrites %", "guard_rewrite_pct", "ok_n"),
           ("fallback answers % (a fixed non-answer the viewer sees)", "fallback_pct", "ok_n"),
           ("streamed answers %", "streamed_pct", "ok_n"), ("tool calls / answer", "tools_per_answer", None),
           ("· server-run (ctx-)", "server_tools_per_answer", None), ("· model-called", "model_tools_per_answer", None),
           ("answer flow (count)", "flow_mix", None), ("model calls / answer", "model_calls_per_answer", None),
           ("input tokens / answer, p50", "tokens_in_p50", None), ("input tokens / answer, p90", "tokens_in_p90", None),
           ("output tokens (incl. thinking) / answer, p50", "tokens_out_p50", None),
           ("output tokens (incl. thinking) / answer, p90", "tokens_out_p90", None),
           ("est. cost / answer, USD", "cost_usd_per_q", None)]
LATENCY = [("server total (answer.latency_ms)", "latency_server_ms"),
           ("server first text (timing.first_text_ms)", "latency_server_first_text_ms"),
           ("client total (adds the network to the bench host)", "latency_total_ms"),
           ("client first text", "latency_first_text_ms"), ("client first tool", "latency_first_tool_ms")]
MAX_SUSPECT_LINES = 30


def _with_n(v, n) -> str:
    return _fmt(v) if n is None else f"{_fmt(v)} (n {n})"


def report(runs: dict[str, list[dict]], out: pathlib.Path, meta: dict | None = None, show_private: bool = False) -> str:
    """scorecard.json / scorecard.md (everything) and summary.md (for job logs: numbers and case ids; for a private
    preview title's cases no question, answer, rule-hit or error text unless `show_private`)."""
    shared = set.intersection(*({r["id"] for r in rows} for rows in runs.values()))
    partial = {n: len(rows) - len(shared) for n, rows in runs.items() if len(rows) > len(shared)}
    runs = {n: [r for r in rows if r["id"] in shared] for n, rows in runs.items()}     # compare like with like
    names = list(runs)
    two = len(names) == 2
    files = sorted({r["file"] for rows in runs.values() for r in rows})
    groups = {"all": lambda r: True, "set: base": lambda r: r["set"] == "base", "set: new": lambda r: r["set"] == "new",
              "set: generated": lambda r: r["set"] == "generated"}
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
    for label, k, nk in METRICS:
        vals = [allg[n][k] for n in names]
        L.append(f"| {label} | " + " | ".join(_with_n(allg[n][k], allg[n][nk] if nk else None) for n in names)
                 + (f" | {_delta(*vals)}" if two else "") + " |")
    L += ["", f"## Latency, ms (answered cases; p50 / p90 / p95, interpolated; p95 only from n ≥ {MIN_N_P95})", "",
          f"| measure | {head} |", sep]
    for label, k in LATENCY:
        cells = [allg[n][k] for n in names]
        L.append(f"| {label} | " + " | ".join(f"{_fmt(x['p50'])} / {_fmt(x['p90'])} / {_fmt(x['p95'])} (n {x['n']})"
                                              for x in cells)
                 + (f" | {_delta(cells[0]['p50'], cells[1]['p50'])} (p50)" if two else "") + " |")

    def compact(title: str, table: dict) -> None:
        L.extend(["", f"## {title}", "", "| group | n | " + " | ".join(f"acc % {n}" for n in names) + " | "
                  + " | ".join(f"leaks rule / judge-only {n}" for n in names) + " | "
                  + " | ".join(f"server p50 ms {n}" for n in names) + " |",
                  "|---|---|" + "---|" * (3 * len(names))])
        for g, per in table.items():
            if not any(per[n]["n"] for n in names):
                continue
            L.append(f"| {g} | {per[names[0]]['n']} | "
                     + " | ".join(_with_n(per[n]["accuracy_pct"], per[n]["accuracy_n"]) for n in names) + " | "
                     + " | ".join(f"{_fmt(per[n]['leaks_rule'])} / {_fmt(per[n]['leaks_judge_only'])}" for n in names)
                     + " | " + " | ".join(_fmt(per[n]["latency_server_ms"]["p50"]) for n in names) + " |")

    compact("By set and case file", {g: v for g, v in card["groups"].items() if g != "all"})
    compact("By category", card["by_category"])
    summary = L[:]                                   # the job-log summary shares everything up to here
    L += ["", "## Tool usage", ""]
    for n in names:
        L.append(f"- **{n}**: " + (", ".join(f"{t} ×{c}" for t, c in allg[n]["tool_usage"].items()) or "—"))
    L += ["", "## Leaks", ""]
    leak_lines, leak_short = [], []
    for n, rows in runs.items():
        for r in rows:
            llm = r.get("llm") or {}
            if r.get("rule_leaks") or llm.get("leak") or r.get("real_names"):
                why = (r.get("rule_leaks") or []) + [f"real:{t}" for t in r.get("real_names") or []]
                if llm.get("leak"):
                    why.append(f"llm: {llm.get('leak_quote', '')[:120]!r} ⇐ {llm.get('leak_fact', '')[:120]}")
                kind = "rule" if r.get("rule_leaks") or r.get("real_names") else "judge only"
                head_ = f"- **{n}** `{r['id']}` ({r['category']}) [{kind}] — "
                leak_lines.append(head_ + "; ".join(why))
                hide = r.get("private") and not show_private
                leak_short.append(head_ + (f"{len(why)} finding(s), text hidden (private preview title)" if hide
                                           else "; ".join(why)))
    L += leak_lines[:MAX_LEAK_LINES] or ["- none"]
    if len(leak_lines) > MAX_LEAK_LINES:
        L.append(f"- … {len(leak_lines) - MAX_LEAK_LINES} more in scorecard.json / judged.jsonl")
    sus_lines, sus_short = [], []
    for n, rows in runs.items():
        for r in rows:
            if r.get("suspects") and not (r.get("rule_leaks") or r.get("real_names")):
                head_ = f"- **{n}** `{r['id']}` ({r['category']}) — "
                sus_lines.append(head_ + "; ".join(r["suspects"]))
                sus_short.append(head_ + (f"{len(r['suspects'])} suspect(s), text hidden (private preview title)"
                                          if r.get("private") and not show_private else "; ".join(r["suspects"])))
    L += ["", "## Rule-gate suspects (echoes of the viewer's words, words also used by something already shown; "
          "for the LLM judge to settle)", ""] + (sus_lines[:MAX_SUSPECT_LINES] or ["- none"])
    if len(sus_lines) > MAX_SUSPECT_LINES:
        L.append(f"- … {len(sus_lines) - MAX_SUSPECT_LINES} more in judged.jsonl")
    rw = [f"- **{n}**: " + ", ".join(f"`{r['id']}`" + (f" (fallback: {r['fallback']})" if r.get("fallback") else "")
                                   for r in rows if r.get("guard_rewritten") or r.get("fallback"))
          for n, rows in runs.items() if any(r.get("guard_rewritten") or r.get("fallback") for r in rows)]
    L += ["", "## Guard rewrites and fallbacks", ""] + (rw or ["- none"])
    err_lines = error_lines(runs)
    err_short = error_lines(runs, show_private)
    if err_lines:
        L += ["", "## Errors", ""] + err_lines[:MAX_ERROR_LINES]
        if len(err_lines) > MAX_ERROR_LINES:
            L.append(f"- … {len(err_lines) - MAX_ERROR_LINES} more")
    md = "\n".join(L) + "\n"
    (out / "scorecard.md").write_text(md, encoding="utf-8")
    summary += ["", "## Failing cases", ""] + failing_lines(runs)
    if err_short:
        summary += ["", f"## Errors ({len(err_short)})", ""] + err_short[:MAX_ERROR_LINES]
    if leak_short:
        summary += ["", f"## Leaks (first {min(10, len(leak_short))} of {len(leak_short)})", ""] + leak_short[:10]
    if rw:
        summary += ["", "## Guard rewrites and fallbacks", ""] + rw
    if sus_short:
        summary += ["", f"## Suspects (first {min(10, len(sus_short))} of {len(sus_short)})", ""] + sus_short[:10]
    summary += ["", f"Full scorecard: {out / 'scorecard.md'}"]
    (out / "summary.md").write_text("\n".join(summary) + "\n", encoding="utf-8")
    return md


def error_lines(runs: dict[str, list[dict]], show_private: bool = True) -> list[str]:
    """One line per errored case: what the client saw (HTTP status, how the stream ended, the error event; the
    message is left out for a private preview title's case unless `show_private`)."""
    out = []
    for n, rows in runs.items():
        for r in rows:
            if r["status"] != "ok":
                how = ", ".join(x for x in (f"HTTP {r['http_status']}" if r.get("http_status") else "",
                                            f"stream {r['stream_end']}" if r.get("stream_end") else "",
                                            f"{r['t_total_ms']} ms" if r.get("t_total_ms") is not None else "") if x)
                msg = "message hidden (private preview title)" if r.get("private") and not show_private \
                    else str(r.get("error"))[:220]
                out.append(f"- **{n}** `{r['id']}` ({how or '?'}) — {msg}")
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


def served_titles(served: list[dict]) -> dict:
    """Catalog-like entries {id: {title, duration_s, shas}} of served titles (`.cache/bench/served.json`), so cases of
    a private preview title (not in catalog/catalog.json) lint too."""
    return {t["id"]: {"title": t["title"], "duration_s": t.get("duration_s"), "shas": [t["sha"]]} for t in served}


def lint(cases: list[dict], with_packs: bool, titles: dict | None = None, show_private: bool = False,
         max_lines: int | None = None) -> int:
    """Offline consistency checks; with packs also the checks against the packs (the case's pack is there, P within
    it, must_not_include not eligible at P yet, must_include_any found in eligible text). `titles` adds catalog
    entries (served private titles). A private case's message says only which check failed, never the text."""
    errors, warns = [], []
    cat = {**_catalog(), **(titles or {})}

    def add(bucket: list, c: dict, kind: str, detail: str = "") -> None:
        hide = c.get("private") and not show_private
        bucket.append(f"{c['id']}: {kind}" + ("" if hide or not detail else f": {detail}"))

    for c in cases:
        for e in C.validate(c):
            add(errors, c, "schema", e)
        item = cat.get(c.get("content_id"))
        if item is None:
            add(errors, c, "content_id not in catalog/catalog.json (nor a served title)")
        else:
            if c.get("title") != item["title"]:
                add(errors, c, "title differs from the catalog", f"{c.get('title')!r} ≠ {item['title']!r}")
            if item["shas"] and c.get("asset_sha256") not in item["shas"]:
                add(errors, c, "asset_sha256 is not the title spec's expected asset")
            dur = item["duration_s"]
            if dur and isinstance(c.get("P"), (int, float)) and c["P"] > dur:
                add(errors, c, "P beyond the duration", f"P={c['P']} > {dur}")
        text = "\n".join(_strings({k: v for k, v in c.items() if not k.startswith("_")}))
        for s in FORBIDDEN:
            if s in text:
                add(errors, c, "contains a string the leak scanner flags", repr(s))
        meta_text = "\n".join([c.get("notes") or "", c.get("source") or ""]
                              + [m.get("note", "") for f in ("must_include_any", "must_not_include", "flag_if_any")
                                 for m in c.get(f) or [] if isinstance(m, dict)])
        if CJK_ANY.search(meta_text):
            add(errors, c, "notes / source must be English")
        for want, prefix in (("new", "new:"), ("generated", "gen:")):
            if (case_set(c) == want) != c["id"].startswith(prefix):
                add(warns, c, f"{want}-set ids start with '{prefix}' (set={case_set(c)})")
        qtext = viewer_text(c)
        if c.get("lang") == "zh" and not CJK_ANY.search(c["question"]):
            add(warns, c, "lang zh but the question has no Chinese")
        if c.get("lang") == "en" and CJK_ANY.search(qtext):
            add(warns, c, "lang en but the question has Chinese")
        ref = "\n".join([c.get("reference") or ""] + [str(marker_label(m)) for m in c.get("must_include_any") or []])
        clash = hits(c.get("must_not_include"), ref)
        if clash:
            add(errors, c, "must_not_include matches its own reference / must_include_any", str(clash))
        if c.get("category") == "followup" and not c.get("history"):
            add(errors, c, "category followup needs history")
        if c.get("category") == "injection" and c.get("expect") == "decline" and not c.get("must_not_include"):
            add(warns, c, "injection probe without must_not_include")
    if with_packs:
        for c in cases:
            if c["id"].startswith("probe:"):
                continue
            if C.pack_path(c["asset_sha256"], PACKS) is None:
                add(errors, c, "no pack for asset_sha256 among the packs given", str(PACKS))
                continue
            pv = _pack(c)
            dur = pv.asset.get("duration_s")
            if dur and c["P"] > dur:
                add(errors, c, "P beyond the pack's duration", f"P={c['P']} > {dur}")
            txt = eligible_text(c)
            linked = []
            for m in c.get("must_not_include") or []:
                if isinstance(m, dict) and m.get("link"):
                    if link_marker_ok(pv, c["P"], m):
                        linked.append(m)
                    else:
                        add(errors, c, "link marker: no identity link pending at P has this name on a side",
                            str(marker_label(m)))
            legal = [m for m in c.get("must_not_include") or []
                     if not (isinstance(m, dict) and m.get("lint_allow_eligible")) and m not in linked
                     and marker_hits(m, txt)]
            if legal:
                add(errors, c, f"must_not_include already eligible at P={c['P']}", str([marker_label(m) for m in legal]))
            public = {C.N(t) for t in pv.public_names()}
            if pub := [marker_label(m) for m in c.get("must_not_include") or [] if _is_public(m, public)
                       and m not in linked]:
                add(errors, c, "must_not_include has a public name (allowed at any time)", str(pub))
            if pub := [marker_label(m) for m in c.get("flag_if_any") or [] if _is_public(m, public)]:
                add(warns, c, "flag_if_any has a public name: ignored by the rule gate", str(pub))
            if c.get("must_include_any") and c["lang"] == "en" and not hits(c["must_include_any"], txt):
                add(warns, c, f"no must_include_any term found verbatim in eligible text at P={c['P']}")
    shown = 0
    for kind, msgs in (("ERROR", errors), ("note", warns)):
        for m in msgs:
            if max_lines is not None and shown >= max_lines:
                break
            print(kind, m)
            shown += 1
    if max_lines is not None and len(errors) + len(warns) > shown:
        print(f"... {len(errors) + len(warns) - shown} more")
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
    ap.add_argument("--titles", help="served titles JSON (.cache/bench/served.json): lint cases of served private titles")
    ap.add_argument("--show-private", action="store_true",
                    help="print question / answer / rule-hit text of private preview titles' cases too")
    ap.add_argument("--max-lines", type=int, default=None, help="lint: print at most N error / note lines")
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
        titles = served_titles(json.loads(pathlib.Path(args.titles).read_text(encoding="utf-8"))) if args.titles else None
        return lint(read_cases(args.cases, strict=False), with_packs=PACKS is not None, titles=titles,
                    show_private=args.show_private, max_lines=args.max_lines)
    if not args.runs:
        ap.error("give at least one run (or --lint)")
    if not args.no_rules and PACKS is None:
        ap.error("the rule gate needs --packs (or BENCH_PACKS_DIR); use --no-rules to skip it")
    by_id = {c["id"]: c for c in read_cases(args.cases)}
    if len({case_set(c) == "generated" for c in by_id.values()}) > 1:
        raise SystemExit("generated and hand-written cases together: grade them as separate runs (--cases)")
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
    md = report(runs, out, meta, args.show_private)
    if args.summary:
        print((out / "summary.md").read_text(encoding="utf-8"))
    elif not args.quiet:
        print(md)
    print(f"→ {out / 'scorecard.md'}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
