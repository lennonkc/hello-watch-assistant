"""The ADK agent (Gemini on Agent Platform) and its gate-aware tools. The server builds the answering context itself
(on screen + tap, recent dialogue, story so far, the paused frame; `server/context.py`) and `inject_context` puts it
into every model request of the question, so the everyday questions need one model call; `search_moments` and
`look_closer` remain callable for what the context does not cover. (The earlier design — five tools and a routing-table
instruction — was removed after three benchmark rounds; docs/online-qa.md §4a has the comparison.)

The locked position P never comes from the model: the server writes `title_id`, `p`, `lang` and `tap` into session
state, and each tool reads them from `ToolContext.state`. Tools build a `Moment` for that P, so the model can only ever
see items that are eligible at P — it has no argument with which to ask for more. `budget` caps tool rounds and stops
tool use after the soft deadline; `count_usage` collects token usage for the `answer` event and the log line.
"""
from __future__ import annotations

import asyncio
import contextvars
import dataclasses
import functools
import json
import math
import time

from google.adk.agents import LlmAgent
from google.adk.agents.readonly_context import ReadonlyContext
from google.adk.models import Gemini
from google.adk.tools import ToolContext
from google.genai import types

from ingest.retrieval import OnlineEmbedder
from server import config, prompts
from server.answer import split_sentences
from server.evidence import Candidate
from server.knowledge import TYPE_LABEL, Moment, secs, ts
from server.packs import STORE

EMBEDDER = OnlineEmbedder(timeout_s=config.EMBED_TIMEOUT_S, cooldown_s=config.EMBED_COOLDOWN_S)

LANG_NAME = {"en": "English", "zh": "Simplified Chinese"}
TOOL_LABELS = {
    "story_so_far": "Reading the story so far",
    "search_moments": "Searching what you have already seen",
    "recent_dialogue": "Listening back to the last lines",
    "on_screen": "Checking who and what is on screen",
    "look_closer": "Watching the last 30 seconds again",
}
TOOL_LABELS_ZH = {
    "story_so_far": "回顾到目前为止的剧情",
    "search_moments": "搜索你已经看过的内容",
    "recent_dialogue": "回听最近的台词",
    "on_screen": "查看画面里有谁、有什么",
    "look_closer": "重看最近 30 秒",
}


def tool_label(name: str, lang: str = "en") -> str:
    """The step-trace label of a lookup / tool, in the question's language."""
    return (TOOL_LABELS_ZH if lang == "zh" else TOOL_LABELS).get(name, name)


def say(lang: str, en: str, zh: str) -> str:
    return zh if lang == "zh" else en


ANSWER_NOW = ("Answer now, in the viewer's language, from the context and the tool results you already have. "
              "Do not call any more tools.")


@dataclasses.dataclass
class Turn:
    """Per-question side channel: UI extras (source candidates, reference stills, highlight; never shown to the model),
    the context and history `inject_context` sends, and the budget / usage counters."""
    summaries: dict = dataclasses.field(default_factory=dict)
    highlight: dict | None = None
    search_mode: str | None = None              # "vector" / "fts" for the last search_moments call
    context: object | None = None               # server.context.Context (context flow)
    history: list = dataclasses.field(default_factory=list)   # earlier turns as types.Content (context flow)
    started: float = dataclasses.field(default_factory=time.monotonic)
    deadline: float = math.inf                  # monotonic time the whole question must be answered by
    lang: str = "en"                            # the question's language (step labels)
    max_tool_rounds: int = 2
    soft_deadline_s: float = 9.0
    run_calls: int = 0                          # model calls in the current run (the budget counts these)
    model_calls: int = 0                        # all model calls of the question, nested look_closer included
    tools_stopped: bool = False
    input_tokens: int = 0
    output_tokens: int = 0
    thought_tokens: int = 0
    tools: list = dataclasses.field(default_factory=list)      # names, in call order, server-run lookups as "ctx:<name>"
    args_filter: object | None = None           # model-written tool arguments → what the step trace may show
    support: list = dataclasses.field(default_factory=list)    # evidence.Candidate: sources the answer may rest on
    ref_frames: list = dataclasses.field(default_factory=list) # (t, caption): the tapped entity's reference stills

    def add_usage(self, usage) -> None:
        if usage is None:
            return
        self.input_tokens += usage.prompt_token_count or 0
        self.output_tokens += (usage.candidates_token_count or 0) + (usage.thoughts_token_count or 0)
        self.thought_tokens += usage.thoughts_token_count or 0

    def usage(self) -> dict:
        return {"input_tokens": self.input_tokens, "output_tokens": self.output_tokens,
                "thought_tokens": self.thought_tokens, "model_calls": self.model_calls}


TURN: contextvars.ContextVar[Turn] = contextvars.ContextVar("turn")


def _turn() -> Turn:
    try:
        return TURN.get()
    except LookupError:
        return Turn()


@functools.lru_cache(maxsize=64)
def _moment(title_id: str, p: float, lang: str) -> Moment:
    return Moment(STORE.get(title_id), p, lang)


def moment_of(state) -> Moment:
    return _moment(state["title_id"], float(state["p"]), state.get("lang", "en"))


def _summarise(tool_context: ToolContext, text: str) -> None:
    _turn().summaries[tool_context.function_call_id] = text


# ── bookkeeping shared by the tools and the server-built context ─────
# Lookups and tools only offer source candidates (`Turn.support`); which ones are shown, with which frames, is decided
# after the answer (`server.evidence`).

def note_story(turn: Turn, m: Moment, out: dict) -> str:
    items = out.get("since_recap") or out.get("so_far") or []
    if "recap" in out:
        upto = secs(out["recap"]["up_to"])
        turn.support += [Candidate("recap", "recap", upto, upto, s.strip()) for s in split_sentences(out["recap"]["text"])]
    for it in items:
        at = secs(it["at"])
        turn.support.append(Candidate("story", TYPE_LABEL.get(it["type"], it["type"]), at, at, it["text"], frame_at=at,
                                      text_zh=m.text_zh(it.get("ref"))))
    if "recap" in out:
        return say(m.lang, f"Recap up to {out['recap']['up_to']} + {len(items)} later moments",
                   f"截至 {out['recap']['up_to']} 的回顾 + 之后 {len(items)} 个片段")
    return say(m.lang, f"{len(items)} moments so far", f"到目前为止 {len(items)} 个片段")


def note_dialogue(turn: Turn, lines: list[dict], lang: str = "en") -> str:
    turn.support += [Candidate("line", "line", ln["t"], ln["t"], f"{ln['speaker']}: {ln['text']}") for ln in lines]
    if lang == "zh":
        return f"{len(lines)} 句台词" + (f"（{lines[0]['at']} 起）" if lines else "")
    return f"{len(lines)} lines" + (f" since {lines[0]['at']}" if lines else "")


def note_on_screen(turn: Turn, m: Moment, out: dict) -> str:
    """No highlight from a shot before a cut: its boxes show something else."""
    ents = out.get("entities", [])
    focus = [ents[out["tapped"]]] if "tapped" in out else ents
    if "tapped" in out and not out.get("previous_shot"):
        e = ents[out["tapped"]]
        turn.highlight = {"bbox": e["bbox"], "label": e["label"], "frame_t": e["frame_t"]}
    for e in focus:
        zh = e.get("facts_zh") or [None] * len(e.get("facts", []))
        turn.support += [Candidate("fact", "character", m.P, m.P, f"{e['label']}: {fact}",
                                   text_zh=f"{e['label']}：{z}" if z else None)
                         for fact, z in zip(e.get("facts", []), zh)]
    if "tapped" in out:
        e = ents[out["tapped"]]
        turn.ref_frames = [(t, say(m.lang, f"{e['label']} at {ts(t)}", f"{e['label']}（{ts(t)}）"))
                           for t in e.get("reference_frames", [])]
        return say(m.lang, f"Tapped: {e['label']}", f"点中：{e['label']}")
    if ents:
        return say(m.lang, f"{len(ents)} on screen", f"画面里有 {len(ents)} 个")
    return say(m.lang, "Nothing identified on screen", "画面里还没有识别出任何东西")


# ── tools ────────────────────────────────────────────────────────────

# The lookups the server runs itself (`server/context.py`) keep their step labels: on_screen, recent_dialogue,
# story_so_far. Every tool does its pack reads and thumbnail lookups in a worker thread: `Title.thumb()` may fetch
# from Cloud Storage on demand, and nothing on the answer path may block the event loop (other viewers' streams share
# it).

async def search_moments(query: str, tool_context: ToolContext) -> dict:
    """Search everything the viewer has already seen (scenes, events, shots, character facts, objects and spoken
    lines) for a topic, person or thing. Write the query in English. Returns results with timestamps."""
    m = moment_of(tool_context.state)
    qvec = await EMBEDDER.vector(query)                   # bounded, no retries; None → full-text ranking
    hits = await asyncio.to_thread(m.search, query, qvec)
    turn = _turn()
    turn.search_mode = "vector" if qvec is not None else "fts"
    results = []
    for h in hits:
        results.append({"at": ts(h.start_s), "type": TYPE_LABEL.get(h.item_type, h.item_type), "text": h.text})
        turn.support.append(Candidate("search", TYPE_LABEL.get(h.item_type, h.item_type), h.start_s, h.end_s, h.text,
                                      frame_at=h.start_s if h.item_type in ("scene", "event", "shot_caption") else None,
                                      text_zh=m.text_zh((h.item_type, h.item_id))))
    _summarise(tool_context, say(m.lang, f"{len(results)} matches, all before {ts(m.P)}",
                                 f"{len(results)} 条结果，都在 {ts(m.P)} 之前"))
    return {"query": query, "results": results}


async def look_closer(question: str, tool_context: ToolContext) -> dict:
    """Look at the current frame and the last ~30 seconds of picture before the pause to answer a visual question
    (what is that object, what is happening, what does someone look like). Slower than the other tools: use it when
    they do not answer the question."""
    m, turn = moment_of(tool_context.state), _turn()
    # the whole tool (stills + the nested call) fits in what is left of the question minus the answer step after it
    budget = min(config.LOOK_TIMEOUT_S, turn.deadline - time.monotonic() - config.ANSWER_RESERVE_S)
    if budget < 1.0:
        _summarise(tool_context, say(m.lang, "No time left to look closer", "没有时间再细看了"))
        return {"observation": "", "note": "no time left to look closer; answer from what you have"}
    end = time.monotonic() + budget
    chars = ", ".join(c["label"] for c in m.known_characters()) or "none yet"
    lang = LANG_NAME.get(m.lang, "English")
    text = prompts.render("look_closer", position=ts(m.P), characters=chars, question=question, lang=lang)
    parts = [types.Part.from_text(text=text)]
    win = m.near_window(config.NEAR_WINDOW_S)
    video = bool(win and config.LOOK_VIDEO and config.ASSET_BUCKET)
    # the paused frame and (without the video clip) up to LOOK_STILLS stills spread over the window (at least 3 s
    # apart), all ≤ floor(P), read concurrently
    step = max(3, math.ceil((win[1] - win[0]) / max(1, config.LOOK_STILLS - 1))) if win else 3
    wanted = [m.fp] + ([] if video or not win else list(range(int(win[1]), int(win[0]) - 1, -step))[::-1])
    try:                                        # on-demand stills from a slow bucket: look at less, not later
        jpegs = await asyncio.wait_for(asyncio.to_thread(read_thumbs, m.title, wanted), budget / 2)
    except TimeoutError:
        ctx_frame = getattr(turn.context, "frame", None)
        jpegs = [ctx_frame] + [None] * (len(wanted) - 1)
    frame, stills = jpegs[0], [(t, b) for t, b in zip(wanted[1:], jpegs[1:]) if b is not None]
    if frame is None and not stills and not video:
        _summarise(tool_context, say(m.lang, "Could not read the frames in time", "没能及时读取画面"))
        return {"observation": "", "note": "the frames could not be read in time; answer from what you have"}
    if video:
        # whole-film clip from Cloud Storage (has audio, but adds tens of seconds of latency)
        parts.append(types.Part(
            file_data=types.FileData(file_uri=f"gs://{config.ASSET_BUCKET}/assets/{m.title.sha}.mp4", mime_type="video/mp4"),
            video_metadata=types.VideoMetadata(start_offset=f"{win[0]:.1f}s", end_offset=f"{win[1]:.1f}s", fps=1)))
        span = f"frame + video {ts(win[0])}–{ts(win[1])}"
        span_zh = f"当前画面 + 视频 {ts(win[0])}–{ts(win[1])}"
    elif win:
        for t, jpeg in stills:
            parts += [types.Part.from_text(text=f"[{ts(t)}]"), types.Part.from_bytes(data=jpeg, mime_type="image/jpeg")]
        span = f"frame + {len(stills)} stills {ts(win[0])}–{ts(win[1])}"
        span_zh = f"当前画面 + {len(stills)} 张截图 {ts(win[0])}–{ts(win[1])}"
    else:
        span, span_zh = "current frame", "当前画面"
    if frame is not None:
        parts += [types.Part.from_text(text=f"[{ts(m.P)}] current frame"),
                  types.Part.from_bytes(data=frame, mime_type="image/jpeg")]
    turn.model_calls += 1
    try:
        r = await asyncio.wait_for(vision_generate(parts), max(0.1, end - time.monotonic()))
    except Exception as e:                                     # noqa: BLE001 — timeout / model error: answer without it
        _summarise(tool_context, say(m.lang, "Could not look closer in time", "没能及时细看"))
        return {"observation": "", "note": f"looking closer failed ({type(e).__name__}); answer from what you have"}
    turn.add_usage(r.usage_metadata)
    _summarise(tool_context, say(m.lang, f"Looked at the {span}", f"看了{span_zh}"))
    return {"observation": m.sanitize((r.text or "").strip()), "looked_at": span}


def read_thumb(title, t: int) -> bytes | None:
    """One thumbnail's bytes, or None. Blocking (may fetch from Cloud Storage): call it in a worker thread."""
    return read_thumbs(title, [t])[0]


def read_thumbs(title, ts_: list[int]) -> list[bytes | None]:
    """Several thumbnails' bytes (None where the film has no frame), fetched together: one `Title.prefetch` call
    downloads the missing ones concurrently for on-demand titles. Blocking: call it in a worker thread."""
    have = set(title.prefetch(ts_))
    return [title.thumb(t).read_bytes() if int(t) in have else None for t in ts_]


async def vision_generate(parts: list) -> types.GenerateContentResponse:
    """The nested `look_closer` model call (separate so tests can replace it)."""
    return await _vision_client().aio.models.generate_content(
        model=config.MODEL, contents=[types.Content(role="user", parts=parts)],
        config=types.GenerateContentConfig(temperature=0.2, max_output_tokens=2048,
                                           media_resolution=types.MediaResolution.MEDIA_RESOLUTION_LOW,
                                           thinking_config=types.ThinkingConfig(thinking_level="LOW"),
                                           http_options=types.HttpOptions(timeout=int(config.LOOK_TIMEOUT_S * 1000))))


@functools.cache
def _vision_client():
    from google import genai
    return genai.Client(enterprise=True, project=config.project(), location=config.LOCATION)


# ── callbacks ────────────────────────────────────────────────────────

def inject_context(callback_context, llm_request) -> None:
    """Context flow: earlier turns (only those asked at or before P, text only) first, then the server-built context
    (frame, on screen, tap, dialogue, story) in front of the viewer's question. Rebuilt for every model call of the
    question and never written to the session, so stale context cannot pile up across turns."""
    turn = _turn()
    ctx = turn.context
    contents = list(llm_request.contents)
    i = next((k for k, c in enumerate(contents)
              if c.role == "user" and not any(p.function_response for p in c.parts or [])), None)
    if ctx is not None and i is not None:
        contents[i] = types.Content(role="user", parts=[*ctx.parts(), *(contents[i].parts or [])])
    llm_request.contents = [*turn.history, *contents]


def budget(callback_context, llm_request) -> None:
    """Every model call: count it; once the tool rounds are used up or the soft deadline has passed, forbid further
    function calls (the tools stay declared, so the function calls already in this request stay valid)."""
    turn = _turn()
    turn.run_calls += 1
    turn.model_calls += 1
    late = time.monotonic() - turn.started >= turn.soft_deadline_s
    if turn.run_calls > turn.max_tool_rounds or late:
        turn.tools_stopped = True
        llm_request.config.tool_config = types.ToolConfig(
            function_calling_config=types.FunctionCallingConfig(mode=types.FunctionCallingConfigMode.NONE))
        llm_request.append_instructions([ANSWER_NOW])


def count_usage(callback_context, llm_response) -> None:
    if not llm_response.partial:
        _turn().add_usage(llm_response.usage_metadata)


# ── agents ───────────────────────────────────────────────────────────

def _characters(m: Moment) -> str:
    return "\n".join(f"- {c['label']}" + (f" (Chinese: {c['label_zh']})" if c["label_zh"] else "")
                     for c in m.known_characters()) or "- (nobody yet)"


def context_instruction(ctx: ReadonlyContext) -> str:
    m = moment_of(ctx.state)
    return prompts.render("agent_context", title=m.title.title, position=ts(m.P), phase=m.phase_words,
                          lang=LANG_NAME.get(m.lang, "English"), characters=_characters(m))


def _gemini() -> Gemini:
    return Gemini(model=config.MODEL,
                  client_kwargs={"enterprise": True, "project": config.project(), "location": config.LOCATION},
                  retry_options=types.HttpRetryOptions(attempts=2, initial_delay=0.5, max_delay=1.0))


def _generate_config(**kw) -> types.GenerateContentConfig:
    return types.GenerateContentConfig(temperature=0.3, max_output_tokens=4096,
                                       thinking_config=types.ThinkingConfig(thinking_level=config.THINKING_LEVEL), **kw)


def build_context_agent(model=None) -> LlmAgent:
    """`context` flow: the server-built context in every request, two tools for what it does not contain."""
    return LlmAgent(
        name="watch_companion_ctx",
        description="Answers questions about a paused film from a server-built, spoiler-gated context.",
        model=model or _gemini(), instruction=context_instruction,
        tools=[search_moments, look_closer],
        before_model_callback=[inject_context, budget], after_model_callback=count_usage,
        generate_content_config=_generate_config(media_resolution=types.MediaResolution.MEDIA_RESOLUTION_MEDIUM))


def tool_args(args: dict | None) -> dict:
    """The scalar arguments of a model-written tool call, whole (the guard sees them before they are shortened)."""
    return {k: v for k, v in (args or {}).items() if k != "tool_context" and isinstance(v, (str, int, float, bool))}


def args_preview(args: dict) -> dict:
    """Tool-call arguments for the UI trace: strings over 120 characters cut at a word boundary."""
    def cut(v: str) -> str:
        if len(v) <= 120:
            return v
        head = v[:120]
        return (head.rsplit(" ", 1)[0] if " " in head else head) + "…"
    return {k: cut(v) if isinstance(v, str) else v for k, v in args.items()}


def dumps(o) -> str:
    return json.dumps(o, ensure_ascii=False)
