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
import logging
import math
import re
import time
import uuid

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types
from pydantic import BaseModel, Field

from server import agent as A
from server import config, guard, prompts
from server.knowledge import Moment
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
runner = Runner(app_name=APP_NAME, agent=A.build_agent(), session_service=session_service)


# ── catalog, media and gate ─────────────────────────────────────────

def _title(title_id: str):
    try:
        return STORE.get(title_id)
    except KeyError:
        raise HTTPException(404, "unknown title")


@app.get("/healthz")
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
    def __init__(self):
        self.minute: dict[str, collections.deque] = collections.defaultdict(collections.deque)
        self.day: dict[str, int] = collections.Counter()
        self.total, self.today = 0, dt.date.today()

    def check(self, ip: str) -> str | None:
        now, today = time.monotonic(), dt.date.today()
        if today != self.today:
            self.day.clear()
            self.total, self.today = 0, today
        q = self.minute[ip]
        while q and now - q[0] > 60:
            q.popleft()
        if len(q) >= config.ASK_PER_MINUTE:
            return "You're asking quickly. Give it a few seconds and try again."
        if self.day[ip] >= config.ASK_PER_DAY_PER_IP or self.total >= config.ASK_PER_DAY_TOTAL:
            return "The demo has reached its daily question limit. Please come back tomorrow."
        q.append(now)
        self.day[ip] += 1
        self.total += 1
        return None


LIMITER = Limiter()


def client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for")
    return fwd.split(",")[0].strip() if fwd else (request.client.host if request.client else "?")


# ── conversations ───────────────────────────────────────────────────

class Conversations:
    """Browser session → ADK session. A seek backwards (or a guard rewrite) starts a fresh ADK session, because
    earlier turns may contain things that are not eligible at the new P."""
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
            self.map.popitem(last=False)
        return cur["sid"]


CONVERSATIONS = Conversations()


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


async def _run_agent(user: str, sid: str, text: str, state: dict, emit) -> str:
    """One agent run; tool calls / results are forwarded to `emit` as they happen. Returns the final text."""
    msg = types.Content(role="user", parts=[types.Part.from_text(text=text)])
    final = ""
    async for ev in runner.run_async(user_id=user, session_id=sid, new_message=msg, state_delta=state):
        for fc in ev.get_function_calls():
            await emit("tool", {"id": fc.id, "name": fc.name, "label": A.TOOL_LABELS.get(fc.name, fc.name),
                                "args": A.args_preview(fc.args)})
        for fr in ev.get_function_responses():
            await emit("tool_result", {"id": fr.id, "name": fr.name,
                                       "summary": A._turn().summaries.get(fr.id, "done")})
        if ev.is_final_response() and ev.content and ev.content.parts:
            final += "".join(p.text for p in ev.content.parts if p.text and not p.thought)
    return final.strip()


@app.post("/api/ask")
async def ask(body: Ask, request: Request):
    t = _title(body.title_id)
    if body.lang not in ("en", "zh"):
        raise HTTPException(400, "lang must be en or zh")
    a = t.pack["asset"]
    p = min(body.p, a["duration_s"])
    if msg := LIMITER.check(client_ip(request)):
        return JSONResponse({"message": msg}, status_code=429)

    user = f"{body.session_id or uuid.uuid4().hex}:{t.id}"
    state = {"title_id": t.id, "p": p, "lang": body.lang, "tap": body.tap.model_dump() if body.tap else None}
    queue: asyncio.Queue = asyncio.Queue()

    async def emit(event: str, data) -> None:
        await queue.put(sse(event, data))

    async def work():
        started = time.monotonic()
        turn = A.Turn()
        A.TURN.set(turn)
        try:
            m = Moment(t, p, body.lang)
            counts = m.gate_counts()
            elig = sum(counts["eligible"].values())
            await emit("gate", {"p": round(p, 2), "eligible_total": elig,
                                "locked_total": sum(counts["total"].values()) - elig})
            sid = await CONVERSATIONS.get(user, p)
            text = await _run_agent(user, sid, body.question.strip(), state, emit)
            hits, rewritten = guard.violations(t.pack, text, p), False
            if hits or not text:
                # the bad draft must not stay in the conversation: retry in a fresh session
                rewritten = True
                log.warning("guard hit at p=%.1f: %s", p, [h["kind"] for h in hits])
                sid = await CONVERSATIONS.get(user, p, fresh=True)
                note = prompts.render("guard_retry", terms=", ".join(readable(h["term"]) for h in hits)) if hits else ""
                text = await _run_agent(user, sid, f"{body.question.strip()}\n\n{note}".strip(), state, emit)
                if not text or guard.violations(t.pack, text, p):
                    text = FALLBACK[body.lang]
                    await CONVERSATIONS.get(user, p, fresh=True)
            await emit("answer", {"text": text, "evidence": turn.evidence, "frames": turn.frames,
                                  "highlight": turn.highlight, "guard": {"checked": True, "rewritten": rewritten},
                                  "latency_ms": int((time.monotonic() - started) * 1000)})
        except Exception as e:                                     # noqa: BLE001 — report, never leak internals
            log.exception("ask failed")
            await emit("error", {"message": "The assistant hit a problem answering this. Please try again."
                                 if not isinstance(e, HTTPException) else str(e.detail)})
        finally:
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


# Static front end last, so /api routes win.
if config.WEB_DIR.is_dir():
    app.mount("/", StaticFiles(directory=config.WEB_DIR, html=True), name="web")
