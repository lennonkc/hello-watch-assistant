"""HTTP service: the web player, the gate API and the streaming question endpoint (contract: docs/web-api.md).

Run locally:
    WATCH_PACKS_DIR=… WATCH_VIDEO_DIR=… CEDAR_GCP_PROJECT=… .venv/bin/uvicorn server.main:app --port 8080
"""
from __future__ import annotations

import asyncio
import collections
import contextlib
import datetime as dt
import functools
import hmac
import json
import logging
import math
import re
import time
import uuid

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from google.adk.agents.invocation_context import LlmCallsLimitExceededError
from google.adk.agents.run_config import RunConfig, StreamingMode
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types
from pydantic import BaseModel, Field

from server import agent as A
from server import config, guard, prompts
from server import context as C
from server.answer import GuardedAnswer, split_ready
from server.knowledge import Moment, ts
from server.packs import STORE

log = logging.getLogger("watch")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

APP_NAME = "hello_watch"
FALLBACK = {
    "en": "I can't answer that yet without giving away something the film hasn't shown you. Keep watching!",
    "zh": "现在回答会透露影片还没展示的内容,先继续看下去吧!",
}


@contextlib.asynccontextmanager
async def lifespan(_app: FastAPI):
    await asyncio.to_thread(STORE.load)
    log.info("loaded %d titles; prompts from %s", len(STORE.titles), prompts.source())
    yield


app = FastAPI(title="Hello Watch Assistant", lifespan=lifespan, docs_url=None, redoc_url=None)
session_service = InMemorySessionService()


# ── catalog, media and gate ─────────────────────────────────────────

def _title(title_id: str):
    try:
        return STORE.get(title_id)
    except KeyError:
        raise HTTPException(404, "unknown title")


@app.get("/healthz")
@app.get("/api/health")                                # Cloud Run reserves /healthz on its front end
def healthz():
    return {"ok": True, "titles": len(STORE.titles)}


@app.get("/api/titles")
def titles():
    out = []
    # the featured film first: the front end preselects the first title
    for t in sorted(STORE.titles.values(), key=lambda t: (t.title != config.FEATURED_TITLE, t.title)):
        a = t.pack["asset"]
        poster = int(a["story_start_s"] + min(90.0, (a["story_end_s"] - a["story_start_s"]) * 0.12))
        out.append({"id": t.id, "title": t.title, "duration_s": a["duration_s"], "story_start_s": a["story_start_s"],
                    "story_end_s": a["story_end_s"], "poster_t": poster, "license": t.license,
                    "attribution": t.attribution, "synopsis": t.synopsis})
    return out


@app.get("/api/titles/{title_id}/frames/{t}.jpg")
def frame(title_id: str, t: int):
    p = _title(title_id).thumb(t)
    if not p.exists():
        raise HTTPException(404, "no frame")
    return FileResponse(p, media_type="image/jpeg", headers={"Cache-Control": "public, max-age=86400"})


@app.get("/api/titles/{title_id}/video")
def video(title_id: str):
    t = _title(title_id)
    if f := STORE.video_file(t):
        return FileResponse(f, media_type="video/mp4")
    if not config.ASSET_BUCKET:
        raise HTTPException(503, "no video source configured")
    return RedirectResponse(_signed_url(t.sha, int(time.time() // 3000)), status_code=302)


@functools.lru_cache(maxsize=16)
def _signed_url(sha: str, _epoch: int) -> str:
    """V4 signed URL valid for 2 h, re-issued every 50 min (the cache key changes). Signs through the IAM API with the
    service's own identity, so no key file is needed (requires roles/iam.serviceAccountTokenCreator on itself)."""
    import google.auth
    from google.auth.transport.requests import Request as AuthRequest
    from google.cloud import storage

    creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
    creds.refresh(AuthRequest())
    blob = storage.Client(credentials=creds).bucket(config.ASSET_BUCKET).blob(f"assets/{sha}.mp4")
    return blob.generate_signed_url(version="v4", expiration=dt.timedelta(hours=2), method="GET",
                                    service_account_email=getattr(creds, "service_account_email", None),
                                    access_token=creds.token)


@app.get("/api/titles/{title_id}/gate")
def gate(title_id: str, p: float):
    if not math.isfinite(p) or p < 0:
        raise HTTPException(400, "bad p")
    return Moment(_title(title_id), p).gate_counts()


# ── rate limiting (per instance; max instances is capped at deploy time) ──

class Limiter:
    """Per-IP questions per minute, per-IP and total questions per day. `check` returns None (allowed) or the 429 body:
    `{"message", "limit": "per_minute" | "daily", "scope": "ip" | "total", "retry_after_s"}` so the UI can word it."""

    def __init__(self):
        self.minute: dict[str, collections.deque] = collections.defaultdict(collections.deque)
        self.day: dict[str, int] = collections.Counter()
        self.total, self.today = 0, dt.date.today()

    def check(self, ip: str) -> dict | None:
        now, today = time.monotonic(), dt.date.today()
        if today != self.today:
            self.day.clear()
            self.total, self.today = 0, today
        q = self.minute[ip]
        while q and now - q[0] > 60:
            q.popleft()
        if len(q) >= config.ASK_PER_MINUTE:
            wait = max(1, math.ceil(60 - (now - q[0]))) if q else 60
            return {"message": "You're asking quickly. Give it a few seconds and try again.", "limit": "per_minute",
                    "scope": "ip", "retry_after_s": wait}
        if self.day[ip] >= config.ASK_PER_DAY_PER_IP or self.total >= config.ASK_PER_DAY_TOTAL:
            midnight = dt.datetime.combine(today + dt.timedelta(days=1), dt.time())
            return {"message": "The demo has reached its daily question limit. Please come back tomorrow.",
                    "limit": "daily", "scope": "ip" if self.day[ip] >= config.ASK_PER_DAY_PER_IP else "total",
                    "retry_after_s": max(1, int((midnight - dt.datetime.now()).total_seconds()))}
        q.append(now)
        self.day[ip] += 1
        self.total += 1
        return None


LIMITER = Limiter()
BENCH_HEADER = "x-watch-bench-token"


def client_ip(request: Request) -> str:
    """The address Google's front end saw. It appends that to X-Forwarded-For, so the LAST hop is the real client;
    earlier hops are whatever the client sent and cannot be trusted for rate limiting."""
    fwd = [h.strip() for h in request.headers.get("x-forwarded-for", "").split(",") if h.strip()]
    return fwd[-1] if fwd else (request.client.host if request.client else "?")


def is_bench(request: Request) -> bool:
    """Benchmark runs against a deployed revision skip the limiter when they carry `WATCH_BENCH_TOKEN` in the
    `X-Watch-Bench-Token` header. Constant-time compare; the token is never logged."""
    sent = request.headers.get(BENCH_HEADER)
    return bool(config.BENCH_TOKEN and sent) and hmac.compare_digest(sent.encode(), config.BENCH_TOKEN.encode())


# ── agents and conversations ────────────────────────────────────────

RUNNERS = {"context": Runner(app_name=APP_NAME, agent=A.build_context_agent(), session_service=session_service),
           "tools": Runner(app_name=APP_NAME, agent=A.build_agent(), session_service=session_service)}
TOOL_ROUNDS = {"context": config.TOOL_ROUNDS, "tools": 3}
TIMEOUT_FALLBACK = {
    "en": "Sorry, that took me too long to work out. Please ask again in a moment.",
    "zh": "抱歉,这个问题我想得太久了,请稍后再问一次。",
}
EPS = 1e-9


class Conversations:
    """`tools` flow: browser session → ADK session. A seek backwards (or a guard rewrite) starts a fresh ADK session,
    because earlier turns may contain things that are not eligible at the new P."""
    MAX_TURNS = 8

    def __init__(self):
        self.map: collections.OrderedDict = collections.OrderedDict()

    async def get(self, key: str, p: float, fresh: bool = False) -> str:
        cur = self.map.get(key)
        if fresh or cur is None or p + 1.0 < cur["p"] or cur["turns"] >= self.MAX_TURNS:
            s = await session_service.create_session(app_name=APP_NAME, user_id=key)
            if cur:
                with contextlib.suppress(Exception):
                    await session_service.delete_session(app_name=APP_NAME, user_id=key, session_id=cur["sid"])
            cur = {"sid": s.id, "p": p, "turns": 0}
        cur["p"], cur["turns"] = max(cur["p"], p), cur["turns"] + 1
        self.map[key] = cur
        self.map.move_to_end(key)
        while len(self.map) > 500:
            old_key, old = self.map.popitem(last=False)
            with contextlib.suppress(Exception):
                await session_service.delete_session(app_name=APP_NAME, user_id=old_key, session_id=old["sid"])
        return cur["sid"]


CONVERSATIONS = Conversations()


class History:
    """`context` flow: per browser session, the turns the viewer actually received — (P, question, the answer exactly
    as sent) — so a dropped draft or a tool payload never reaches a later prompt. A question at P gets only the turns
    asked at or before P: the gate is monotone in P, so everything in them is still eligible; after a seek backwards
    the later turns are hidden, not deleted, and come back when the viewer catches up."""
    MAX_KEYS, MAX_TURNS = 500, 12

    def __init__(self):
        self.map: collections.OrderedDict = collections.OrderedDict()

    def contents(self, key: str, p: float) -> tuple[list[types.Content], int]:
        turns = [t for t in self.map.get(key, []) if t["p"] <= p + EPS][-config.HISTORY_TURNS:]
        out = []
        for t in turns:
            out += [types.Content(role="user", parts=[types.Part.from_text(text=f"[asked at {ts(t['p'])}] {t['q']}")]),
                    types.Content(role="model", parts=[types.Part.from_text(text=t["a"])])]
        return out, len(turns)

    def add(self, key: str, p: float, q: str, a: str) -> None:
        turns = self.map.setdefault(key, [])
        turns.append({"p": p, "q": q, "a": a})
        del turns[:-self.MAX_TURNS]
        self.map.move_to_end(key)
        while len(self.map) > self.MAX_KEYS:
            self.map.popitem(last=False)


HISTORY = History()


# ── ask ─────────────────────────────────────────────────────────────

class Tap(BaseModel):
    x: float = Field(ge=0, le=1)
    y: float = Field(ge=0, le=1)


class Ask(BaseModel):
    title_id: str
    p: float = Field(ge=0)
    question: str = Field(min_length=1, max_length=config.QUESTION_MAX_CHARS)
    lang: str = "en"
    tap: Tap | None = None
    session_id: str | None = Field(default=None, max_length=80)


def readable(term: str) -> str:
    """Canary regexes such as `\\b(aged|older)\\b` → `aged / older` for the retry note."""
    return re.sub(r"\\b|[()?^$\[\]{}+*\\]", "", term).replace("|", " / ")


def sse(event: str, data) -> str:
    return f"event: {event}\ndata: {A.dumps(data)}\n\n"


class Streamer:
    """Routes streamed model text into the guarded answer, one model call ("step") at a time.

    A step's first sentence is held until a second sentence starts or the step ends without a function call, so a
    one-sentence "Let me look closer." before a tool call is never sent (`held_preamble`). A longer preamble can get
    its first sentence out before the call arrives (`leaked_preamble`; guarded, so safe, just out of place). Text from
    a step that ended in a tool call never counts as the answer: `answer_text()` leaves it out, so the final `answer`
    event replaces it, and if nothing else was written the question gets a fallback instead."""

    def __init__(self, answer: GuardedAnswer, emit, rec: dict):
        self.answer, self.emit, self.rec = answer, emit, rec
        self.pending, self.released = "", False
        self.step_sent: list[str] = []          # deltas sent during the current model call
        self.kept: list[str] = []               # deltas of model calls that ended without a tool call
        self.preamble: list[str] = []           # deltas of model calls that then called a tool

    async def _send(self, deltas: list[str]) -> None:
        for d in deltas:
            if self.rec.get("first_text_ms") is None:
                self.rec["first_text_ms"] = int((time.monotonic() - self.rec["t0"]) * 1000)
            self.step_sent.append(d)
            await self.emit("delta", {"text": d})

    async def send_final(self, text: str) -> None:
        """A fixed reply (fallback): sent and counted as the answer."""
        await self._send([self.answer.force(text)])
        self.kept += self.step_sent
        self.step_sent = []

    def answer_text(self) -> str:
        text = "".join(self.kept)
        return text.lstrip() if self.preamble else text

    async def text(self, chunk: str) -> None:
        if self.released:
            await self._send(self.answer.feed(chunk))
            return
        self.pending += chunk
        ready, rest = split_ready(self.pending)
        if ready and rest.strip():
            self.released = True
            held, self.pending = self.pending, ""
            await self._send(self.answer.feed(held))

    async def end_step(self, has_calls: bool) -> None:
        if has_calls:
            if self.pending.strip() or self.answer.buf.strip():
                self.rec["held_preamble"] += 1
                log.info("dropped text written before a tool call: %r", (self.pending + self.answer.buf)[:200])
            if self.step_sent:
                self.rec["leaked_preamble"] += 1
                self.preamble += self.step_sent
            self.pending, self.answer.buf = "", ""
        else:
            held, self.pending = self.pending, ""
            await self._send(self.answer.feed(held) + self.answer.flush())
            self.kept += self.step_sent
        self.step_sent, self.released = [], False

    async def abort(self) -> None:
        """The run stopped early (deadline, error): send the sentences that are complete — including a last one that
        ends with sentence punctuation — and drop a half-written one."""
        held, self.pending = self.pending, ""
        deltas = self.answer.feed(held)
        if re.search(r"[.!?。！？…][\"'”’)）]*$", self.answer.buf.rstrip()):
            deltas += self.answer.flush()
        await self._send(deltas)
        if self.answer.buf.strip():
            self.rec["cut_off"] = True
        self.kept += self.step_sent
        self.answer.buf, self.released, self.step_sent = "", False, []


async def _run(runner: Runner, user: str, sid: str, text: str, emit, turn: A.Turn, deadline: float,
               sink: Streamer | None = None, streaming: bool = True, state_delta: dict | None = None) -> tuple[str, str]:
    """One agent run → (final text, status ∈ ok / timeout / limit / error). Tool calls and results go to `emit` as
    they happen; with a `sink`, model text goes to it as it streams. The run is bounded twice: no event for
    STEP_TIMEOUT_S (a stalled model call or tool) and the question's overall deadline."""
    msg = types.Content(role="user", parts=[types.Part.from_text(text=text)])
    rc = RunConfig(streaming_mode=StreamingMode.SSE if streaming else StreamingMode.NONE,
                   max_llm_calls=turn.max_tool_rounds + 2)
    turn.run_calls = 0
    final, status, streamed = "", "ok", False
    agen = runner.run_async(user_id=user, session_id=sid, new_message=msg, run_config=rc, state_delta=state_delta)
    try:
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                raise TimeoutError
            try:
                ev = await asyncio.wait_for(agen.__anext__(), min(config.STEP_TIMEOUT_S, left))
            except StopAsyncIteration:
                break
            parts = (ev.content.parts or []) if ev.content else []
            chunk = "".join(p.text for p in parts if p.text and not p.thought)
            if ev.partial:
                if chunk:
                    streamed = True
                    if sink:
                        await sink.text(chunk)
                continue
            calls = ev.get_function_calls()
            for fc in calls:
                turn.tools.append(fc.name)
                args = A.args_preview(fc.args)
                if args and turn.args_filter:
                    args = await asyncio.to_thread(turn.args_filter, args)
                await emit("tool", {"id": fc.id, "name": fc.name, "label": A.TOOL_LABELS.get(fc.name, fc.name),
                                    "args": args})
            for fr in ev.get_function_responses():
                await emit("tool_result", {"id": fr.id, "name": fr.name, "summary": turn.summaries.get(fr.id, "done")})
            if ev.content and ev.content.role == "model":            # one model call completed
                if sink:
                    if chunk and not streamed:
                        await sink.text(chunk)
                    await sink.end_step(bool(calls))
                if ev.is_final_response() and chunk:
                    final += chunk
                streamed = False
    except TimeoutError:
        status = "timeout"
        log.warning("agent run timed out after %.1f s", time.monotonic() - turn.started)
    except LlmCallsLimitExceededError:
        status = "limit"
    except Exception:                                            # noqa: BLE001 — the caller decides what the viewer sees
        status = "error"
        log.exception("agent run failed")
    finally:
        with contextlib.suppress(Exception):
            await agen.aclose()
    if sink and status != "ok":
        await sink.abort()
    return final.strip(), status


def _retry_note(hits: list[dict]) -> str:
    terms = list(dict.fromkeys(readable(h["term"]) for h in hits))
    return prompts.render("guard_retry", terms=", ".join(terms)) if terms else ""


async def _throwaway(runner: Runner, user: str, state: dict, text: str, emit, turn, deadline, sink, streaming):
    """A run in a fresh ADK session that is deleted afterwards (context flow: the history lives in HISTORY)."""
    s = await session_service.create_session(app_name=APP_NAME, user_id=user, state=state)
    try:
        return await _run(runner, user, s.id, text, emit, turn, deadline, sink, streaming)
    finally:
        with contextlib.suppress(Exception):
            await session_service.delete_session(app_name=APP_NAME, user_id=user, session_id=s.id)


class AskFailed(Exception):
    pass


def _checker(t, m, p: float, subjects, question: str, turn: A.Turn):
    """The output guard for this question (spec §7 plus identity co-occurrence, `server/guard.py`). It also filters the
    model-written tool arguments the step trace shows: names not visible yet become labels, and arguments the guard
    still objects to are not shown at all. Blocking: call it in a worker thread."""
    links = guard.identity_links(m, subjects, question)

    def check(text: str) -> list[dict]:
        return guard.violations(t.pack, text, p, links)

    def args_filter(args: dict) -> dict:
        clean = {k: (m.sanitize(v) if isinstance(v, str) else v) for k, v in args.items()}
        return {} if check(" ".join(v for v in clean.values() if isinstance(v, str))) else clean

    turn.args_filter = args_filter
    return check


async def flow_context(t, body: Ask, p: float, user: str, state: dict, emit, turn: A.Turn, rec: dict) -> dict:
    """Server-built context, then one streamed run; each finished sentence is guarded before it is sent."""
    m = A.moment_of(state)
    ctx = await asyncio.to_thread(C.build, m, state["tap"], turn)
    check = await asyncio.to_thread(_checker, t, m, p, ctx.subjects, body.question, turn)
    for lk in ctx.lookups:
        turn.tools.append(f"ctx:{lk.name}")
        await emit("tool", {"id": lk.id, "name": lk.name, "label": lk.label, "args": {}})
        await emit("tool_result", {"id": lk.id, "name": lk.name, "summary": lk.summary})
        if lk.name == "on_screen" and turn.highlight and ctx.highlight_fresh:
            await emit("highlight", turn.highlight)
    turn.context = ctx
    turn.history, rec["history_turns"] = HISTORY.contents(user, p)
    answer = GuardedAnswer(check)
    sink = Streamer(answer, emit, rec)
    runner, deadline, question = RUNNERS["context"], turn.started + config.ASK_DEADLINE_S, body.question.strip()
    _text, status = await _throwaway(runner, user, state, question, emit, turn, deadline, sink, True)
    if not sink.answer_text() and status in ("ok", "limit"):
        # nothing survived the guard (or the model wrote nothing but a preamble): one non-streamed retry with the note
        rec["retried"] = True
        hits = [h for d in answer.dropped for h in d]
        note = _retry_note(hits)
        _text, status = await _throwaway(runner, user, state, f"{question}\n\n{note}".strip(), emit, turn, deadline,
                                         sink, False)
    rec["status"] = status
    if answer.dropped:
        rec["dropped_kinds"] = sorted({h["kind"] for d in answer.dropped for h in d})
        log.warning("guard dropped %d sentence(s) at p=%.1f: %s", len(answer.dropped), p, rec["dropped_kinds"])
    text = sink.answer_text()
    if not text:
        if status == "error" and not sink.preamble:
            raise AskFailed
        rec["fallback"] = "timeout" if status in ("timeout", "error") else "spoiler"
        rec["preamble_only"] = bool(sink.preamble)
        await sink.send_final((TIMEOUT_FALLBACK if rec["fallback"] == "timeout" else FALLBACK)[body.lang])
        text = sink.answer_text()
    else:
        HISTORY.add(user, p, question, text)
    rec["dropped"] = len(answer.dropped)
    return {"text": text, "terms": ctx.tapped_terms, "check": check,
            "rewritten": bool(answer.dropped or rec["retried"] or rec["fallback"] or sink.preamble)}


async def flow_tools(t, body: Ask, p: float, user: str, state: dict, emit, turn: A.Turn, rec: dict) -> dict:
    """The earlier design: the agent picks among five tools; the whole answer is guarded, then sent at once."""
    runner, deadline, question = RUNNERS["tools"], turn.started + config.ASK_DEADLINE_S, body.question.strip()
    m = A.moment_of(state)
    subjects = await asyncio.to_thread(lambda: C.focus(m, m.on_screen(state["tap"], refs=True)))
    check = await asyncio.to_thread(_checker, t, m, p, subjects, body.question, turn)
    sid = await CONVERSATIONS.get(user, p)
    text, status = await _run(runner, user, sid, question, emit, turn, deadline, None, False, state)
    hits, rewritten = check(text), False
    if (hits or not text) and status in ("ok", "limit"):
        # the bad draft must not stay in the conversation: retry in a fresh session
        rewritten, rec["retried"] = True, True
        log.warning("guard hit at p=%.1f: %s", p, [h["kind"] for h in hits])
        rec["dropped_kinds"] = sorted({h["kind"] for h in hits})
        sid = await CONVERSATIONS.get(user, p, fresh=True)
        text, status = await _run(runner, user, sid, f"{question}\n\n{_retry_note(hits)}".strip(), emit, turn,
                                  deadline, None, False, state)
    rec["status"] = status
    if not text and status == "error":
        raise AskFailed
    if not text or check(text):
        rewritten = True
        rec["fallback"] = "timeout" if status == "timeout" else "spoiler"
        text = (TIMEOUT_FALLBACK if status == "timeout" else FALLBACK)[body.lang]
        await CONVERSATIONS.get(user, p, fresh=True)
    rec["first_text_ms"] = int((time.monotonic() - rec["t0"]) * 1000)
    await emit("delta", {"text": text})
    terms = await asyncio.to_thread(C.tapped_terms, A.moment_of(state), state["tap"])
    return {"text": text, "terms": terms, "check": check, "rewritten": rewritten}


FLOWS = {"context": flow_context, "tools": flow_tools}


@app.post("/api/ask")
async def ask(body: Ask, request: Request):
    t = _title(body.title_id)
    if body.lang not in ("en", "zh"):
        raise HTTPException(400, "lang must be en or zh")
    a = t.pack["asset"]
    p = min(body.p, a["duration_s"])
    bench = is_bench(request)
    if not bench and (limited := LIMITER.check(client_ip(request))):
        return JSONResponse(limited, status_code=429, headers={"Retry-After": str(limited["retry_after_s"])})
    flow = request.headers.get("x-watch-flow") if bench else None
    flow = flow if flow in FLOWS else (config.FLOW if config.FLOW in FLOWS else "context")

    user = f"{body.session_id or uuid.uuid4().hex}:{t.id}"
    state = {"title_id": t.id, "p": p, "lang": body.lang, "tap": body.tap.model_dump() if body.tap else None}
    queue: asyncio.Queue = asyncio.Queue()

    async def emit(event: str, data) -> None:
        await queue.put(sse(event, data))

    async def work():
        turn = A.Turn(max_tool_rounds=TOOL_ROUNDS[flow], soft_deadline_s=config.SOFT_DEADLINE_S)
        A.TURN.set(turn)
        rec = {"t0": turn.started, "first_text_ms": None, "held_preamble": 0, "leaked_preamble": 0, "retried": False,
               "fallback": None, "dropped": 0, "dropped_kinds": [], "history_turns": 0, "status": "ok",
               "cut_off": False, "highlight": False}
        try:
            counts = await asyncio.to_thread(lambda: A.moment_of(state).gate_counts())
            elig = sum(counts["eligible"].values())
            await emit("gate", {"p": round(p, 2), "eligible_total": elig,
                                "locked_total": sum(counts["total"].values()) - elig})
            out = await FLOWS[flow](t, body, p, user, state, emit, turn, rec)
            # the highlight only when the answer is about the tapped entity, so the box never contradicts the text
            highlight = turn.highlight if turn.highlight and C.mentions(out["text"], out["terms"]) else None
            total = int((time.monotonic() - turn.started) * 1000)
            rec["highlight"] = highlight is not None
            # evidence rows are gated, but next to an answer about the a-side of a pending identity link even an
            # eligible line naming the b-side would hint at it: the same guard decides what is listed
            evidence = await asyncio.to_thread(lambda: [e for e in turn.evidence if not out["check"](e["text"] or "")])
            await emit("answer", {"text": out["text"], "evidence": evidence, "frames": turn.frames,
                                  "highlight": highlight,
                                  "guard": {"checked": True, "rewritten": out["rewritten"], "dropped": rec["dropped"]},
                                  "latency_ms": total, "usage": turn.usage(),
                                  "timing": {"first_text_ms": rec["first_text_ms"], "total_ms": total}, "flow": flow})
        except Exception as e:                                     # noqa: BLE001 — report, never leak internals
            rec["status"] = "error"
            if not isinstance(e, AskFailed):
                log.exception("ask failed")
            await emit("error", {"message": "The assistant hit a problem answering this. Please try again."
                                 if not isinstance(e, HTTPException) else str(e.detail)})
        finally:
            _log_question(flow, t, p, body, bench, turn, rec)
            await emit("done", {})
            await queue.put(None)

    task = asyncio.create_task(work())

    async def stream():
        try:
            while (item := await queue.get()) is not None:
                yield item
        finally:
            if not task.done():
                task.cancel()

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


def _log_question(flow: str, t, p: float, body: Ask, bench: bool, turn: A.Turn, rec: dict) -> None:
    """One JSON line per question (no question text, no tokens or secrets)."""
    rec = {k: v for k, v in rec.items() if k != "t0"}
    log.info("ask %s", json.dumps({
        "flow": flow, "title": t.id, "p": round(p, 2), "lang": body.lang, "tap": body.tap is not None, "bench": bench,
        "model": config.MODEL, "tools": turn.tools, "search_mode": turn.search_mode, "usage": turn.usage(),
        "timing": {"first_text_ms": rec.pop("first_text_ms"), "total_ms": int((time.monotonic() - turn.started) * 1000)},
        "tools_stopped": turn.tools_stopped, "embed_down": A.EMBEDDER.last_error is not None
        and time.monotonic() < A.EMBEDDER.down_until, **rec}, ensure_ascii=False))


# Static front end last, so /api routes win.
if config.WEB_DIR.is_dir():
    app.mount("/", StaticFiles(directory=config.WEB_DIR, html=True), name="web")
