"""Send every benchmark case to a running service (`POST /api/ask`, SSE) and record raw results. No grading here
(see judge.py).

    python bench/run_bench.py --base http://127.0.0.1:8080 --run-id local-1
    python bench/run_bench.py --base https://<tagged-revision-url> --token "$(cat .cache/bench_token)" --run-id next-1
    python bench/run_bench.py --base https://<public-demo> --only ed-h0 --limit 3 --sleep 11     # rate-limited demo

Output: `bench/runs/<run_id>/results.jsonl` (one row per case) + `run.json`. Re-running the same run id skips cases
that already have a result (resume); `--retry-errors` re-runs only failed ones. Cases with `history` are sent as one
conversation: every earlier turn, then the graded question, all with the same `session_id`.

Per case: wall-clock total, time to the first `tool` event, time to first text (first `delta` event when the server
streams, else the `answer` event), the server's `latency_ms`, tool names split into server-run (ids `ctx-…`) and
model-called, `guard.rewritten`, and the `usage` / `timing` objects of the `answer` event when present.
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import datetime as dt
import json
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import pathlib
import uuid

if __package__ in (None, ""):                                    # run as a script: python bench/run_bench.py
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from bench.common import CASES_DIR, REPO, RUNS_DIR, read_cases, read_jsonl, write_jsonl  # noqa: E402

TIMEOUT_S = 180
TOKEN_HEADER = "X-Watch-Bench-Token"
SERVER_TOOL_PREFIX = "ctx-"


# ── SSE ──────────────────────────────────────────────────────────────

def iter_sse(lines):
    """Yield (event, data) from an iterable of raw byte / str lines (text/event-stream framing: `event:` / `data:`
    fields, blank line ends an event, `:` lines are comments, several `data:` lines are joined with newlines)."""
    ev, data = None, []
    for raw in lines:
        line = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw
        line = line.rstrip("\r\n")
        if not line:
            if data:
                yield ev or "message", "\n".join(data)
            ev, data = None, []
            continue
        if line.startswith(":"):
            continue
        field, _, value = line.partition(":")
        value = value[1:] if value.startswith(" ") else value
        if field == "event":
            ev = value.strip()
        elif field == "data":
            data.append(value)
    if data:
        yield ev or "message", "\n".join(data)


def _json(s: str):
    try:
        return json.loads(s or "{}")
    except json.JSONDecodeError:
        return {"_raw": s}


# ── client ───────────────────────────────────────────────────────────

class Client:
    def __init__(self, base: str, token: str | None = None, token_header: str = TOKEN_HEADER,
                 timeout: float = TIMEOUT_S, retry_429: int = 2, wait_429: float = 20.0):
        self.url = base.rstrip("/") + "/api/ask"
        self.token, self.token_header = token, token_header
        self.timeout, self.retry_429, self.wait_429 = timeout, retry_429, wait_429

    def _request(self, body: dict) -> urllib.request.Request:
        req = urllib.request.Request(self.url, data=json.dumps(body, ensure_ascii=False).encode(), method="POST")
        req.add_header("Content-Type", "application/json")
        req.add_header("Accept", "text/event-stream")
        req.add_header("User-Agent", "hello-watch-bench/1")
        if self.token:
            req.add_header(self.token_header, self.token)
        return req

    def turn(self, title_id: str, P: float, question: str, lang: str, session_id: str, tap=None) -> dict:
        """One question → one result dict (never raises for HTTP / stream problems)."""
        body = {"title_id": title_id, "p": P, "question": question, "lang": lang, "session_id": session_id}
        if tap:
            body["tap"] = {"x": tap["x"], "y": tap["y"]}
        for attempt in range(self.retry_429 + 1):
            res = self._once(body)
            if res.get("http_status") != 429 or attempt == self.retry_429:
                return res
            time.sleep(self.wait_429)
        return res

    def _once(self, body: dict) -> dict:
        t0 = time.monotonic()
        ms = lambda: round((time.monotonic() - t0) * 1000)  # noqa: E731
        events, deltas = [], []
        first = {"event": None, "tool": None, "delta": None, "answer": None}
        try:
            with urllib.request.urlopen(self._request(body), timeout=self.timeout) as r:
                status = r.status
                for ev, raw in iter_sse(r):
                    t = ms()
                    data = _json(raw)
                    first["event"] = first["event"] if first["event"] is not None else t
                    if ev in ("tool", "delta", "answer") and first[ev] is None:
                        first[ev] = t
                    if ev == "delta":
                        deltas.append((data or {}).get("text") or "")
                        continue                                 # deltas are summarised, not stored one by one
                    events.append({"event": ev, "data": data, "ms": t})
                    if ev == "done":
                        break
        except urllib.error.HTTPError as e:
            msg = e.read().decode("utf-8", "replace")[:500]
            d = _json(msg)
            if isinstance(d, dict) and (d.get("message") or d.get("detail")):
                msg = str(d.get("message") or d.get("detail"))
            return _error(f"HTTP {e.code}: {msg}", ms(), http_status=e.code)
        except Exception as e:                                   # timeout / connection reset: record, keep going
            return {**_error(f"{type(e).__name__}: {e}", ms()), **_partial(events, deltas, first)}
        out = _partial(events, deltas, first)
        out["http_status"] = status
        out["t_total_ms"] = ms()
        return out


def _error(msg: str, total_ms: int, http_status: int | None = None) -> dict:
    return {"status": "error", "error": msg, "http_status": http_status, "answer": "", "evidence_text": [],
            "tools": [], "server_tools": [], "model_tools": [], "t_total_ms": total_ms}


def _partial(events: list, deltas: list, first: dict) -> dict:
    ans = next((e["data"] for e in events if e["event"] == "answer"), None)
    err = next((e["data"] for e in events if e["event"] == "error"), None)
    tools = [e["data"] for e in events if e["event"] == "tool"]
    server = [t.get("name") for t in tools if str(t.get("id") or "").startswith(SERVER_TOOL_PREFIX)]
    model = [t.get("name") for t in tools if not str(t.get("id") or "").startswith(SERVER_TOOL_PREFIX)]
    evidence = (ans or {}).get("evidence") or []
    streamed = "".join(deltas)
    first_text = first["delta"] if first["delta"] is not None else first["answer"]
    return {
        "status": "ok" if ans else "error",
        "error": None if ans else ((err or {}).get("message") or "no answer event"),
        "answer": (ans or {}).get("text") or "",
        "evidence_text": [e.get("text") for e in evidence if isinstance(e, dict) and e.get("text")],
        "tools": [t.get("name") for t in tools], "server_tools": server, "model_tools": model,
        "t_first_event_ms": first["event"], "t_first_tool_ms": first["tool"], "t_first_text_ms": first_text,
        "t_answer_ms": first["answer"], "streamed": bool(deltas), "n_delta": len(deltas),
        "streamed_text": streamed if deltas and streamed.strip() != ((ans or {}).get("text") or "").strip() else None,
        "server_latency_ms": (ans or {}).get("latency_ms"),
        "guard_rewritten": ((ans or {}).get("guard") or {}).get("rewritten"),
        "usage": (ans or {}).get("usage"), "timing": (ans or {}).get("timing"),
        "gate": next((e["data"] for e in events if e["event"] == "gate"), None),
        "raw": {"events": [e for e in events if e["event"] != "answer"], "answer": ans},
    }


def run_case(client: Client, c: dict) -> dict:
    """All turns of one case in one conversation; the graded result is the last turn's."""
    sid = f"bench-{uuid.uuid4().hex[:12]}"
    prior = []
    for h in c.get("history") or []:
        r = client.turn(c["content_id"], h.get("P", c["P"]), h["question"], h.get("lang", c["lang"]), sid, h.get("tap"))
        prior.append({"question": h["question"], "P": h.get("P", c["P"]), "status": r["status"], "error": r.get("error"),
                      "answer": r.get("answer"), "t_total_ms": r.get("t_total_ms"), "tools": r.get("tools")})
        if r["status"] != "ok":
            return {**r, "status": "error", "error": f"history turn failed: {r.get('error')}", "history": prior,
                    "session_id": sid}
    r = client.turn(c["content_id"], c["P"], c["question"], c["lang"], sid, c.get("tap"))
    return {**r, "history": prior or None, "session_id": sid}


def _git_commit() -> str | None:
    try:
        return subprocess.run(["git", "-C", str(REPO), "rev-parse", "--short=12", "HEAD"], capture_output=True,
                              text=True, timeout=5).stdout.strip() or None
    except Exception:
        return None


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--base", required=True, help="service base URL (local server or a tagged Cloud Run revision)")
    ap.add_argument("--token", default=os.environ.get("BENCH_TOKEN"),
                    help="limiter-bypass token, sent as --token-header (default: env BENCH_TOKEN)")
    ap.add_argument("--token-file", help="read the token from this file instead")
    ap.add_argument("--token-header", default=os.environ.get("BENCH_TOKEN_HEADER", TOKEN_HEADER))
    ap.add_argument("--cases", nargs="*", default=[str(CASES_DIR)])
    ap.add_argument("--only", help="comma list of case-id prefixes, e.g. ed-,c5:sintel,new:tap")
    ap.add_argument("--run-id", default=None)
    ap.add_argument("--label", default="", help="free text stored in run.json (e.g. WATCH_FLOW=…)")
    ap.add_argument("--workers", type=int, default=1, help="parallel cases (default 1: clean latency)")
    ap.add_argument("--sleep", type=float, default=0.0, help="seconds between questions (rate-limited demo: 11)")
    ap.add_argument("--limit", type=int, default=None, help="run at most N cases this invocation")
    ap.add_argument("--timeout", type=float, default=TIMEOUT_S)
    ap.add_argument("--retry-errors", action="store_true")
    args = ap.parse_args(argv)
    token = args.token
    if args.token_file:
        token = open(args.token_file, encoding="utf-8").read().strip() or None

    cases = read_cases(args.cases)
    if args.only:
        pre = tuple(p for p in args.only.split(",") if p)
        cases = [c for c in cases if c["id"].startswith(pre)]
    run_id = args.run_id or f"{dt.datetime.now():%m%d-%H%M}"
    out = RUNS_DIR / run_id
    out.mkdir(parents=True, exist_ok=True)
    rpath = out / "results.jsonl"
    done = {r["id"]: r for r in read_jsonl(rpath)}
    if args.retry_errors:
        done = {k: v for k, v in done.items() if v["status"] == "ok"}
    todo = [c for c in cases if c["id"] not in done]
    if args.limit is not None:
        todo = todo[:args.limit]
    meta = {"run_id": run_id, "base": args.base, "label": args.label, "cases": len(cases), "commit": _git_commit(),
            "workers": args.workers, "token": bool(token), "started": dt.datetime.now().isoformat(timespec="seconds")}
    mpath = out / "run.json"
    if mpath.exists():
        old = json.loads(mpath.read_text(encoding="utf-8"))
        meta = {**old, **{k: v for k, v in meta.items() if k not in ("started",)}, "resumed": meta["started"]}
    mpath.write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")

    client = Client(args.base, token, args.token_header, args.timeout)
    lock = threading.Lock()
    pace = {"next": 0.0}

    def one(c):
        if args.sleep:
            with lock:                                           # global pacing across workers
                wait = pace["next"] - time.monotonic()
                pace["next"] = max(pace["next"], time.monotonic()) + args.sleep * (1 + len(c.get("history") or []))
            if wait > 0:
                time.sleep(wait)
        try:
            res = run_case(client, c)
        except Exception as e:                                   # never abort the whole run
            res = _error(f"{type(e).__name__}: {e}", None)
        return {"id": c["id"], **res, "finished": dt.datetime.now().isoformat(timespec="seconds")}

    rows = dict(done)
    print(f"{run_id}: {len(todo)} to run ({len(done)} done) → {args.base}", flush=True)
    with cf.ThreadPoolExecutor(max(1, args.workers)) as ex:
        for i, r in enumerate(ex.map(one, todo), 1):
            rows[r["id"]] = r
            print(f"  [{i}/{len(todo)}] {r['id']} {r['status']} {r.get('t_total_ms')} ms"
                  f" first_text={r.get('t_first_text_ms')} tools={r.get('tools')}"
                  + (f" ERROR {r.get('error')}" if r["status"] != "ok" else ""), flush=True)
            write_jsonl(rpath, sorted(rows.values(), key=lambda x: x["id"]))
    errs = sum(r["status"] != "ok" for r in rows.values())
    print(f"done: {len(rows)} results, {errs} errors → {rpath}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
