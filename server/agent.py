"""The ADK agent: one LlmAgent (Gemini on Agent Platform) with five gate-aware tools.

The locked position P never comes from the model: the server writes `title_id`, `p`, `lang` and `tap` into session
state with `state_delta` on every question, and each tool reads them from `ToolContext.state`. Tools build a `Moment`
for that P, so the model can only ever see items that are eligible at P — it has no argument with which to ask for more.
"""
from __future__ import annotations

import asyncio
import contextvars
import dataclasses
import functools
import json

from google.adk.agents import LlmAgent
from google.adk.agents.readonly_context import ReadonlyContext
from google.adk.models import Gemini
from google.adk.tools import ToolContext
from google.genai import types

from server import config, prompts
from server.knowledge import TYPE_LABEL, Moment, ts
from server.packs import STORE

LANG_NAME = {"en": "English", "zh": "Simplified Chinese"}
TOOL_LABELS = {
    "story_so_far": "Reading the story so far",
    "search_moments": "Searching what you have already seen",
    "recent_dialogue": "Listening back to the last lines",
    "on_screen": "Checking who and what is on screen",
    "look_closer": "Watching the last 30 seconds again",
}


@dataclasses.dataclass
class Turn:
    """Per-question side channel for the UI (evidence, frames, highlight); never shown to the model."""
    summaries: dict = dataclasses.field(default_factory=dict)
    evidence: list = dataclasses.field(default_factory=list)
    frames: list = dataclasses.field(default_factory=list)
    highlight: dict | None = None

    def add_frame(self, t: int | None, caption: str) -> None:
        if t is not None and all(f["t"] != t for f in self.frames) and len(self.frames) < 6:
            self.frames.append({"t": t, "caption": caption})

    def add_evidence(self, typ: str, start_s: float, end_s: float, text: str | None) -> None:
        if len(self.evidence) < 8 and all(e["start_s"] != start_s or e["type"] != typ for e in self.evidence):
            self.evidence.append({"type": TYPE_LABEL.get(typ, typ), "start_s": round(start_s, 2),
                                  "end_s": round(end_s, 2), "text": text})


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


# ── tools ────────────────────────────────────────────────────────────

def story_so_far(tool_context: ToolContext) -> dict:
    """Recap of the story up to the viewer's pause point: the latest "story so far" recap plus every scene and event
    after it. Use it for "what did I miss / what's going on" questions and to orient yourself before answering."""
    m = moment_of(tool_context.state)
    out = m.story_so_far()
    turn = _turn()
    items = out.get("since_recap") or out.get("so_far") or []
    if "recap" in out:
        upto = _secs(out["recap"]["up_to"])
        turn.add_evidence("recap", upto, upto, out["recap"]["text"])
    for it in items[-3:]:
        turn.add_frame(m.evidence_frame(_secs(it["at"]), _secs(it["at"])), f"Earlier: {it['at']}")
    _summarise(tool_context, (f"Recap up to {out['recap']['up_to']} + " if "recap" in out else "") + f"{len(items)} later moments")
    return out


async def search_moments(query: str, tool_context: ToolContext) -> dict:
    """Search everything the viewer has already seen (scenes, events, shots, character facts, objects and spoken
    lines) for a topic, person or thing. Write the query in English. Returns results with timestamps."""
    m = moment_of(tool_context.state)
    hits = await asyncio.to_thread(m.search, query)
    turn = _turn()
    results = []
    for h in hits:
        results.append({"at": ts(h.start_s), "type": TYPE_LABEL.get(h.item_type, h.item_type), "text": h.text})
        if h.item_type != "cue":
            turn.add_evidence(h.item_type, h.start_s, h.end_s, h.text)
    for h in [h for h in hits if h.item_type in ("scene", "event", "shot_caption")][:2]:
        turn.add_frame(m.evidence_frame(h.start_s, h.end_s), f"Earlier: {ts(h.start_s)}")
    _summarise(tool_context, f"{len(results)} matches, all before {ts(m.P)}")
    return {"query": query, "results": results}


def recent_dialogue(seconds: int, tool_context: ToolContext) -> dict:
    """Lines spoken in the last `seconds` seconds before the pause (use 60 unless the viewer asks about something
    further back; at most 240), with who said each line when known."""
    m = moment_of(tool_context.state)
    lines = m.recent_dialogue(min(max(int(seconds or 60), 10), 240))
    for ln in lines[-3:]:
        _turn().add_evidence("cue", ln["t"], ln["t"], f"{ln['speaker']}: {ln['text']}")
    _summarise(tool_context, f"{len(lines)} lines" + (f" since {lines[0]['at']}" if lines else ""))
    return {"lines": [{k: v for k, v in ln.items() if k != "t"} for ln in lines]}


def on_screen(tool_context: ToolContext) -> dict:
    """Who and what is in the frame at the pause point, with their screen position, what is known about each
    character so far, and — if the viewer tapped the frame — which entity they tapped (`tapped` is an index into
    `entities`)."""
    state = tool_context.state
    m = moment_of(state)
    out = m.on_screen(state.get("tap"))
    turn = _turn()
    ents = out.get("entities", [])
    focus = [ents[out["tapped"]]] if "tapped" in out else ents
    if "tapped" in out:
        e = ents[out["tapped"]]
        turn.highlight = {"bbox": e["bbox"], "label": e["label"], "frame_t": e["frame_t"]}
    for e in focus:
        for fact in e.get("facts", [])[-2:]:
            turn.add_evidence("character_fact", m.P, m.P, f"{e['label']}: {fact}")
    for e in focus:
        for t in e.get("reference_frames", []):
            turn.add_frame(t, f"{e['label']} at {ts(t)}")
    if "tapped" in out:
        _summarise(tool_context, f"Tapped: {ents[out['tapped']]['label']}")
    else:
        _summarise(tool_context, f"{len(ents)} on screen" if ents else "Nothing identified on screen")
    return out


async def look_closer(question: str, tool_context: ToolContext) -> dict:
    """Look at the current frame and the last ~30 seconds of picture before the pause to answer a visual question
    (what is that object, what is happening, what does someone look like). Slower than the other tools: use it when
    they do not answer the question."""
    m = moment_of(tool_context.state)
    chars = ", ".join(c["label"] for c in m.known_characters()) or "none yet"
    lang = LANG_NAME.get(m.lang, "English")
    text = prompts.render("look_closer", position=ts(m.P), characters=chars, question=question, lang=lang)
    parts = [types.Part.from_text(text=text)]
    frame = m.title.thumb(m.fp)
    win = m.near_window(config.NEAR_WINDOW_S)
    if win and config.LOOK_VIDEO and config.ASSET_BUCKET:
        # whole-film clip from Cloud Storage (has audio, but adds tens of seconds of latency)
        parts.append(types.Part(
            file_data=types.FileData(file_uri=f"gs://{config.ASSET_BUCKET}/assets/{m.title.sha}.mp4", mime_type="video/mp4"),
            video_metadata=types.VideoMetadata(start_offset=f"{win[0]:.1f}s", end_offset=f"{win[1]:.1f}s", fps=1)))
        span = f"frame + video {ts(win[0])}–{ts(win[1])}"
    elif win:
        # the same window as stills from the per-second thumbnails (all ≤ floor(P)); fast and local
        stills = [t for t in range(int(win[0]), int(win[1]) + 1, 3) if m.title.thumb(t).exists()]
        for t in stills:
            parts += [types.Part.from_text(text=f"[{ts(t)}]"),
                      types.Part.from_bytes(data=m.title.thumb(t).read_bytes(), mime_type="image/jpeg")]
        span = f"frame + {len(stills)} stills {ts(win[0])}–{ts(win[1])}"
    else:
        span = "current frame"
    if frame.exists():
        parts += [types.Part.from_text(text=f"[{ts(m.P)}] current frame"),
                  types.Part.from_bytes(data=frame.read_bytes(), mime_type="image/jpeg")]
    r = await _vision_client().aio.models.generate_content(
        model=config.MODEL, contents=[types.Content(role="user", parts=parts)],
        config=types.GenerateContentConfig(temperature=0.2, max_output_tokens=2048,
                                           media_resolution=types.MediaResolution.MEDIA_RESOLUTION_LOW,
                                           thinking_config=types.ThinkingConfig(thinking_level="LOW")))
    _turn().add_frame(m.fp if frame.exists() else None, f"Paused frame {ts(m.P)}")
    _summarise(tool_context, f"Looked at the {span}")
    return {"observation": (r.text or "").strip(), "looked_at": span}


def _secs(mmss: str) -> float:
    mm, ss = mmss.split(":")
    return int(mm) * 60 + int(ss)


@functools.cache
def _vision_client():
    from google import genai
    return genai.Client(enterprise=True, project=config.project(), location=config.LOCATION)


# ── agent ────────────────────────────────────────────────────────────

def instruction(ctx: ReadonlyContext) -> str:
    state = ctx.state
    m = moment_of(state)
    chars = "\n".join(f"- {c['label']}" + (f" (Chinese: {c['label_zh']})" if c["label_zh"] else "")
                      for c in m.known_characters()) or "- (nobody yet)"
    tap = ("The viewer tapped a point on the paused frame: call on_screen first to see who or what they mean."
           if state.get("tap") else "")
    phase = {"story": "during the story", "before_story": "before the story starts",
             "credits": "during the end credits"}[m.phase]
    return prompts.render("agent_instruction", title=m.title.title, position=ts(m.P), phase=phase,
                          lang=LANG_NAME.get(m.lang, "English"), characters=chars, tap=tap)


def build_agent() -> LlmAgent:
    return LlmAgent(
        name="watch_companion",
        description="Answers questions about a paused film using only what the viewer has already seen.",
        model=Gemini(model=config.MODEL,
                     client_kwargs={"enterprise": True, "project": config.project(), "location": config.LOCATION}),
        instruction=instruction,
        tools=[story_so_far, search_moments, recent_dialogue, on_screen, look_closer],
        generate_content_config=types.GenerateContentConfig(
            temperature=0.3, max_output_tokens=4096,
            thinking_config=types.ThinkingConfig(thinking_level="LOW")),
    )


def args_preview(args: dict | None) -> dict:
    """Tool-call arguments for the UI trace (short strings only)."""
    return {k: (v if not isinstance(v, str) else v[:120]) for k, v in (args or {}).items()
            if k != "tool_context" and isinstance(v, (str, int, float, bool))}


def dumps(o) -> str:
    return json.dumps(o, ensure_ascii=False)
