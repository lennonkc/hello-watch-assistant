"""HTTP service: the web player, the gate API and the streaming question endpoint (contract: docs/web-api.md).

Run locally:
    WATCH_PACKS_DIR=… WATCH_VIDEO_DIR=… CEDAR_GCP_PROJECT=… .venv/bin/uvicorn server.main:app --port 8080
"""
from __future__ import annotations

import asyncio
import collections
import contextlib
import contextvars
import datetime as dt
import functools
import hashlib
import hmac
import json
import logging
import math
import os
import re
import threading
import time
import uuid

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from google.adk.agents.invocation_context import LlmCallsLimitExceededError
from google.adk.agents.run_config import RunConfig, StreamingMode
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types
from pydantic import BaseModel, Field
from starlette.requests import HTTPConnection

from server import agent as A
from server import config, guard, prompts
from server import context as C
from server import evidence as E
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

# True while the request carries a valid access code (cookie `watch_access`) or the bench token (header
# `X-Watch-Bench-Token`, so the benchmark host can test a private preview title without knowing any access code):
# private preview titles exist for it.
UNLOCKED: contextvars.ContextVar[bool] = contextvars.ContextVar("watch_unlocked", default=False)
PRIVATE_HEADERS = {"Cache-Control": "private, no-store"}


class AccessMiddleware:
    """Puts "unlocked" into `UNLOCKED` for the whole request (sync endpoints and tasks started by it inherit it), so
    `_title()` can hide private preview titles from every endpoint, the question endpoint included."""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        conn = HTTPConnection(scope)
        token = UNLOCKED.set(is_bench(conn) or _cookie_unlocks(conn))
        try:
            await self.app(scope, receive, send)
        finally:
            UNLOCKED.reset(token)


app.add_middleware(AccessMiddleware)


def _cookie_unlocks(conn: HTTPConnection) -> bool:
    """A valid `watch_access` cookie. An invalid one counts as a failed attempt of that IP (each distinct value once
    a minute: a browser holding an old cookie is not punished per request); while the IP is over the limit no cookie
    counts, valid or not, so guessing through any endpoint learns nothing."""
    cookie = conn.cookies.get(config.ACCESS_COOKIE)
    if not cookie:
        return False
    ip = client_ip(conn)
    if UNLOCKS.blocked(ip):
        return False
    if code_ok(cookie) and STORE.unlocked(cookie):
        return True
    UNLOCKS.fail(ip, hashlib.sha256(cookie.encode("utf-8", "replace")).hexdigest()[:16])
    return False


def code_ok(code) -> bool:
    """Access codes are printable ASCII without space, quotes, comma, semicolon or backslash (cookie-safe), 1–200
    characters; anything else is simply a wrong code. Real codes should be random and at least 20 characters."""
    return isinstance(code, str) and 0 < len(code) <= 200 and all(33 <= ord(ch) <= 126 and ch not in '",;\\'
                                                                  for ch in code)


def _title(title_id: str):
    t = STORE.titles.get(title_id)
    if t is None or (t.hidden and not UNLOCKED.get()):
        raise HTTPException(404, "unknown title")
    return t


@app.get("/healthz")
@app.get("/api/health")                                # Cloud Run reserves /healthz on its front end
def healthz():
    """Public films only: their count, and the pack each one serves (so a benchmark can grade against the same packs
    and refuse to mix revisions); `revision` is Cloud Run's revision name."""
    public = sorted((t for t in STORE.titles.values() if not t.hidden), key=lambda t: t.sha)
    return {"ok": True, "titles": len(public), "revision": os.environ.get("K_REVISION"),
            "packs": [{"sha": t.sha, "dist": t.dist, "version": t.pack["pack_meta"].get("pack_version")} for t in public]}


@app.get("/api/titles")
def titles():
    out = []
    unlocked = UNLOCKED.get()
    # the featured film first: the front end preselects the first title; private preview titles after the public ones
    for t in sorted(STORE.titles.values(), key=lambda t: (t.hidden, t.title != config.FEATURED_TITLE, t.title)):
        if t.hidden and not unlocked:
            continue
        a = t.pack["asset"]
        poster = int(a["story_start_s"] + min(90.0, (a["story_end_s"] - a["story_start_s"]) * 0.12))
        item = {"id": t.id, "title": t.title, "duration_s": a["duration_s"], "story_start_s": a["story_start_s"],
                "story_end_s": a["story_end_s"], "poster_t": poster, "license": t.license,
                "attribution": t.attribution, "synopsis": t.synopsis, "playback": t.playback}
        if t.hidden:
            item["private"] = True
        out.append(item)
    return JSONResponse(out, headers=PRIVATE_HEADERS if unlocked else None)


@app.post("/api/unlock")
async def unlock(request: Request):
    """Access code (JSON body `{"code": "…"}`; never in a URL, so it reaches no log) → 204 with the `watch_access`
    cookie (30 days), or 404. At most `UNLOCK_FAILURES_PER_MINUTE` FAILED attempts per IP and minute (wrong codes,
    malformed bodies, invalid access cookies); after that 429, even for the right code, until the minute is over.
    Successes are not counted: judges behind one address do not lock each other out."""
    ip = client_ip(request)
    if wait := UNLOCKS.blocked(ip):
        return JSONResponse({"message": "Too many attempts. Try again in a minute.", "retry_after_s": wait},
                            status_code=429, headers={"Retry-After": str(wait), **PRIVATE_HEADERS})
    try:
        data = await request.json()
    except Exception:                                      # not JSON: same answer as a wrong code
        data = None
    code = data.get("code") if isinstance(data, dict) else None
    if not code_ok(code) or not STORE.unlocked(code):
        UNLOCKS.fail(ip)
        raise HTTPException(404, "not found")
    r = Response(status_code=204, headers=PRIVATE_HEADERS)
    r.set_cookie(config.ACCESS_COOKIE, code, max_age=config.ACCESS_COOKIE_DAYS * 86400, path="/", secure=True,
                 httponly=True, samesite="lax")
    return r


class Attempts:
    """Failed attempts per IP in a sliding one-minute window (thread-safe: the middleware runs for sync endpoints
    too). `fail(ip, key)` counts a keyed failure (an invalid cookie value) once per window."""

    def __init__(self, per_minute: int) -> None:
        self.per_minute = per_minute
        self.seen: collections.OrderedDict[str, collections.deque] = collections.OrderedDict()
        self.lock = threading.Lock()

    def _window(self, ip: str, now: float) -> collections.deque:          # lock held
        q = self.seen.setdefault(ip, collections.deque())
        self.seen.move_to_end(ip)
        while q and now - q[0][0] > 60:
            q.popleft()
        while len(self.seen) > 10_000:
            self.seen.popitem(last=False)
        return q

    def blocked(self, ip: str) -> int:
        """0 = allowed, else the seconds until the oldest failure leaves the window."""
        now = time.monotonic()
        with self.lock:
            q = self._window(ip, now)
            return max(1, math.ceil(60 - (now - q[0][0]))) if len(q) >= self.per_minute else 0

    def fail(self, ip: str, key: str | None = None) -> None:
        now = time.monotonic()
        with self.lock:
            q = self._window(ip, now)
            if key is None or all(k != key for _t, k in q):
                q.append((now, key))


UNLOCKS = Attempts(config.UNLOCK_FAILURES_PER_MINUTE)


@app.get("/api/titles/{title_id}/frames/{t}.jpg")
def frame(title_id: str, t: int):
    title = _title(title_id)
    p = title.thumb(t)
    if not p.exists():
        raise HTTPException(404, "no frame")
    headers = PRIVATE_HEADERS if title.hidden else {"Cache-Control": "public, max-age=86400"}
    return FileResponse(p, media_type="image/jpeg", headers=headers)


@app.get("/api/titles/{title_id}/video")
def video(title_id: str):
    t = _title(title_id)
    headers = PRIVATE_HEADERS if t.hidden else None
    if f := STORE.video_file(t):
        return FileResponse(f, media_type="video/mp4", headers=headers)
    if not config.ASSET_BUCKET:
        raise HTTPException(503, "no video source configured")
    return RedirectResponse(_signed_url(t.video_object, int(time.time() // 3000)), status_code=302, headers=headers)


@functools.lru_cache(maxsize=16)
def _signed_url(object_name: str, _epoch: int) -> str:
    """V4 signed URL of a bucket object, valid for 2 h, re-issued every 50 min (the cache key changes). Signs through
    the IAM API with the service's own identity, so no key file is needed (requires
    roles/iam.serviceAccountTokenCreator on itself)."""
    import google.auth
    from google.auth.transport.requests import Request as AuthRequest
    from google.cloud import storage

    creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
    creds.refresh(AuthRequest())
    blob = storage.Client(credentials=creds).bucket(config.ASSET_BUCKET).blob(object_name)
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
    """The client address as the first trusted proxy saw it. Google's front end appends the address it saw to
    X-Forwarded-For, so with `WATCH_TRUSTED_PROXY_HOPS=1` (the default) the LAST entry is the client; each load balancer
    in front adds one more trusted entry. Earlier entries are whatever the client sent and cannot be trusted for rate
    limiting. Repeated header lines count as one comma-separated list."""
    raw = ",".join(request.headers.getlist("x-forwarded-for"))
    fwd = [h.strip() for h in raw.split(",") if h.strip()]
    if not fwd:
        return request.client.host if request.client else "?"
    return fwd[-config.TRUSTED_PROXY_HOPS] if len(fwd) >= config.TRUSTED_PROXY_HOPS else fwd[0]


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
            _forget_user(old_key)
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


PREAMBLE = re.compile(
    r"^\W*(?:(?:ok(?:ay)?|sure|alright|all right|first|right)\W+)?(?:let me|let's (?:see|check|look|take)|"
    r"i'll (?:check|look|search|take|need|have to|watch|see|find|go|start)|"
    r"i will (?:check|look|search|take|watch|see|find)|"
    r"i'm going to|i am going to|i need to (?:check|look|search|watch|see)|one moment|just a (?:moment|second|sec)|"
    r"give me a (?:moment|second|sec)|hold on|bear with me)\b"
    r"|^\W*(?:好的?[,，]?\s*)?(?:让我|我来|我先|我去|我需要|我要(?:先)?(?:看|查|找|检查|搜)|稍等|请稍等|等一下|我查|我看一下|先看)",
    re.IGNORECASE)


def looks_like_preamble(sentence: str) -> bool:
    """"Let me look at the last scene." / "稍等，我查一下。" — words about what the model is about to do."""
    return bool(PREAMBLE.search(sentence or ""))


class Streamer:
    """Routes streamed model text into the guarded answer, one model call ("step") at a time.

    The hold rule: a step's text is held until its first sentence is complete and a second one has started (or the
    step ends without a function call); if that first sentence looks like a preamble ("Let me look closer."), the
    whole step is held until the step ends — sent if it ends without a function call, dropped if it calls a tool
    (`held_preamble`). If the run stops early (deadline, stall, error) the held text is sent only when its first
    sentence is a complete sentence that does not look like a preamble. Text that did go out in a step that then
    called a tool (`leaked_preamble`; guarded, so safe, just out of place) never counts as the answer: `answer_text()`
    leaves it out, so the final `answer` event replaces it, and if nothing else was written the question gets a
    fallback instead."""

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
        if ready and rest.strip() and not looks_like_preamble(ready[0]):
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
        """The run stopped early (deadline, stall, error): send the sentences that are complete — including a last one
        that ends with sentence punctuation — and drop a half-written one; held text that starts like a preamble is
        dropped (a tool call was probably on its way)."""
        held, self.pending = self.pending, ""
        if not self.released and looks_like_preamble(held):
            self.rec["held_preamble"] += 1
            log.info("dropped text written before a stop: %r", held[:200])
            held = ""
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
                args = A.tool_args(fc.args)
                if args and turn.args_filter:                  # whole arguments: a cut could hide part of a name
                    args = await asyncio.to_thread(turn.args_filter, args)
                await emit("tool", {"id": fc.id, "name": fc.name, "label": A.TOOL_LABELS.get(fc.name, fc.name),
                                    "args": A.args_preview(args)})
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
    """The words to avoid: what was actually written for a regex canary, not every alternative of the pattern."""
    terms = list(dict.fromkeys(h.get("match") or readable(h["term"]) for h in hits))
    return prompts.render("guard_retry", terms=", ".join(terms)) if terms else ""


async def _throwaway(runner: Runner, user: str, state: dict, text: str, emit, turn, deadline, sink, streaming):
    """A run in a fresh ADK session that is deleted afterwards (context flow: the history lives in HISTORY)."""
    s = await session_service.create_session(app_name=APP_NAME, user_id=user, state=state)
    try:
        return await _run(runner, user, s.id, text, emit, turn, deadline, sink, streaming)
    finally:
        with contextlib.suppress(Exception):
            await session_service.delete_session(app_name=APP_NAME, user_id=user, session_id=s.id)
        _forget_user(user)


def _forget_user(user: str) -> None:
    """Deleting a session leaves an empty per-user entry in the in-memory service: drop it."""
    for table in (getattr(session_service, "sessions", {}), getattr(session_service, "user_state", {})):
        users = table.get(APP_NAME, {})
        if user in users and not users[user]:
            del users[user]


class AskFailed(Exception):
    pass


def _checker(t, m, p: float, tapped, screen, question: str, turn: A.Turn):
    """The output guard for this question (spec §7 plus identity co-occurrence, `server/guard.py`). It also filters the
    model-written tool arguments the step trace shows: names not visible yet become labels, and arguments the guard
    still objects to are not shown at all. Blocking: call it in a worker thread."""
    links = guard.identity_links(m, tapped, question, screen)

    def check(text: str) -> list[dict]:
        return guard.violations(t.pack, text, p, links)

    def args_filter(args: dict) -> dict:
        clean = {k: (m.sanitize(v) if isinstance(v, str) else v) for k, v in args.items()}
        return {} if check(" ".join(v for v in clean.values() if isinstance(v, str))) else clean

    turn.args_filter = args_filter
    return check, links


async def flow_context(t, body: Ask, p: float, user: str, state: dict, emit, turn: A.Turn, rec: dict) -> dict:
    """Server-built context, then one streamed run; each finished sentence is guarded before it is sent."""
    m = A.moment_of(state)
    ctx = await asyncio.to_thread(C.build, m, state["tap"], turn)
    check, links = await asyncio.to_thread(_checker, t, m, p, ctx.subjects, ctx.screen, body.question, turn)
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
        # the spoiler wording only when the guard held something back; otherwise (time, tool rounds, nothing written)
        # the neutral one
        rec["fallback"] = "spoiler" if answer.dropped else "timeout"
        rec["preamble_only"] = bool(sink.preamble)
        await sink.send_final((TIMEOUT_FALLBACK if rec["fallback"] == "timeout" else FALLBACK)[body.lang])
        text = sink.answer_text()
    rec["cut_off"] = bool(rec["cut_off"] or (status in ("timeout", "error") and not rec["fallback"]))
    if body.session_id and not rec["fallback"] and not rec["cut_off"]:
        HISTORY.add(user, p, question, text)          # only whole answers; no session: no conversation to keep
    rec["dropped"] = len(answer.dropped)
    return {"text": text, "terms": ctx.tapped_terms, "links": links,
            "rewritten": bool(answer.dropped or rec["retried"] or rec["fallback"] or sink.preamble)}


async def flow_tools(t, body: Ask, p: float, user: str, state: dict, emit, turn: A.Turn, rec: dict) -> dict:
    """The earlier design: the agent picks among five tools; the whole answer is guarded, then sent at once."""
    runner, deadline, question = RUNNERS["tools"], turn.started + config.ASK_DEADLINE_S, body.question.strip()
    m = A.moment_of(state)
    tapped, screen = await asyncio.to_thread(lambda: C.focus(m.on_screen(state["tap"], refs=True)))
    check, links = await asyncio.to_thread(_checker, t, m, p, tapped, screen, body.question, turn)
    anon = not body.session_id                         # no conversation to keep: a session that is deleted after

    async def run(text: str, fresh: bool = False) -> tuple[str, str]:
        if anon:
            return await _throwaway(runner, user, state, text, emit, turn, deadline, None, False)
        sid = await CONVERSATIONS.get(user, p, fresh=fresh)
        return await _run(runner, user, sid, text, emit, turn, deadline, None, False, state)

    text, status = await run(question)
    hits, rewritten = check(text), False
    if (hits or not text) and status in ("ok", "limit"):
        # the bad draft must not stay in the conversation: retry in a fresh session
        rewritten, rec["retried"] = True, True
        log.warning("guard hit at p=%.1f: %s", p, [h["kind"] for h in hits])
        rec["dropped_kinds"] = sorted({h["kind"] for h in hits})
        text, status = await run(f"{question}\n\n{_retry_note(hits)}".strip(), fresh=True)
    rec["status"] = status
    if not text and status == "error":
        raise AskFailed
    if not text or check(text):
        rewritten = True
        rec["fallback"] = "spoiler" if (text or rec["dropped_kinds"]) else "timeout"
        text = (TIMEOUT_FALLBACK if rec["fallback"] == "timeout" else FALLBACK)[body.lang]
        if not anon:
            await CONVERSATIONS.get(user, p, fresh=True)
    rec["first_text_ms"] = int((time.monotonic() - rec["t0"]) * 1000)
    await emit("delta", {"text": text})
    terms = await asyncio.to_thread(C.tapped_terms, A.moment_of(state), state["tap"])
    return {"text": text, "terms": terms, "links": links, "rewritten": rewritten}


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
            evidence, frames, highlight = await _sources(t, p, state, turn, out, rec)
            total = int((time.monotonic() - turn.started) * 1000)
            rec["highlight"], rec["evidence"] = highlight is not None, len(evidence)
            await emit("answer", {"text": out["text"], "evidence": evidence, "frames": frames,
                                  "highlight": highlight,
                                  "guard": {"checked": True, "rewritten": out["rewritten"], "dropped": rec["dropped"]},
                                  "cut_off": bool(rec["cut_off"]),
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


def _pick_sources(t, p: float, state: dict, turn: A.Turn, out: dict, rec: dict):
    """(evidence rows, frame plan, highlight, check) to show with the final answer, both flows. Only the sources the
    answer rests on (`server.evidence`); a pure refusal or a fallback lists none. Every evidence line, frame caption and
    the highlight label goes through the guard relative to the answer's subject (`guard.source_violations`). The
    highlight only when the answer is about the tapped entity, so the box never contradicts the text."""
    text, m = out["text"], A.moment_of(state)

    def check(s: str) -> list[dict]:
        return guard.source_violations(t.pack, s or "", p, out["links"], text)

    about_tapped = C.mentions(text, out["terms"])
    highlight = turn.highlight if turn.highlight and about_tapped and not check(turn.highlight["label"]) else None
    refusal = bool(rec.get("fallback")) or E.is_refusal(text)
    kept = E.select(turn.support, text, check, about_tapped=about_tapped, refusal=refusal, lang=m.lang,
                    names=m.zh_names() if m.lang == "zh" else ())
    plan = [] if refusal else E.frame_plan(m, kept, ref_frames=turn.ref_frames if about_tapped else ())
    return [c.row(m.lang) for c in kept], plan, highlight, check


async def _sources(t, p: float, state: dict, turn: A.Turn, out: dict, rec: dict):
    """(evidence, frames, highlight) within `SOURCES_TIMEOUT_S`: the answer is complete, so a slow frame fetch or a
    failure costs the sources, never the answer (frames first, then everything)."""
    end = time.monotonic() + config.SOURCES_TIMEOUT_S
    try:
        evidence, plan, highlight, check = await asyncio.wait_for(
            asyncio.to_thread(_pick_sources, t, p, state, turn, out, rec), config.SOURCES_TIMEOUT_S)
    except Exception as e:                                       # noqa: BLE001
        rec["sources"] = f"failed: {type(e).__name__}"
        log.warning("sources skipped: %r", e)
        return [], [], None
    if not plan:
        return evidence, [], highlight
    try:
        frames = await asyncio.wait_for(asyncio.to_thread(E.frames, A.moment_of(state), plan, check),
                                        max(0.05, end - time.monotonic()))
    except Exception as e:                                       # noqa: BLE001
        rec["sources"] = f"frames failed: {type(e).__name__}"
        log.warning("frames skipped: %r", e)
        frames = []
    return evidence, frames, highlight


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
