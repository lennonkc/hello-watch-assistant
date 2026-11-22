"""The answering context the server builds for each question (context flow), from the gate-first views at P only.

One request carries everything the four everyday questions need (who is this / what is that / what did I miss / what
did they just say): the entities of the current shot and, for a tap, the point and the entity it geometrically falls
on; the recent dialogue; the story so far; and the paused frame (`thumb(floor(P))`) as an image with a ring at the
tap. The text goes into the model request through `agent.inject_context`; P, title, language and tap come from the
request on the server, never from the model.

The entity boxes come from a shot keyframe (`bbox_t_s ≤ floor(P)`), which can be seconds older than the paused frame.
So only the tap ring is drawn (it is ground truth), the boxes are given as text with their time, and the model is told
that the ring is authoritative.
"""
from __future__ import annotations

import dataclasses
import io
import re

from google.genai import types

from ingest.textnorm import term_matches
from server import agent as A
from server.knowledge import Moment, ts

DIALOGUE_S = 60.0               # "what did they just say": the last minute of lines …
DIALOGUE_FALLBACK = 4           # … or, if nothing was said in it, the last few lines before P
DIALOGUE_MAX = 12
STORY_ITEMS = 12
FRESH_BOX_S = 1                 # a box measured within 1 s of floor(P), in the shot P is in, matches the paused frame


@dataclasses.dataclass
class Lookup:
    """One server-run lookup, shown in the step trace like a tool call (`tool` / `tool_result` events)."""
    id: str
    name: str
    label: str
    summary: str


@dataclasses.dataclass
class Context:
    text: str
    frame: bytes | None
    frame_note: str
    lookups: list[Lookup]
    tapped_terms: list[str]               # words by which an answer refers to the tapped entity
    highlight_fresh: bool                 # the tapped entity's box was measured on (about) the paused frame
    subjects: frozenset = frozenset()     # the tapped character (identity co-occurrence, server/guard.py)
    screen: frozenset = frozenset()       # the characters on screen

    def parts(self) -> list[types.Part]:
        out = [types.Part.from_text(text=self.text)]
        if self.frame:
            out += [types.Part.from_text(text=self.frame_note),
                    types.Part.from_bytes(data=self.frame, mime_type="image/jpeg")]
        out.append(types.Part.from_text(text="THE VIEWER'S QUESTION:"))
        return out


def mark_tap(jpeg: bytes, tap: dict) -> bytes | None:
    """The paused frame with a ring at the tap, or None if it cannot be drawn (Pillow missing, unreadable image)."""
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        return None
    try:
        im = Image.open(io.BytesIO(jpeg)).convert("RGB")
    except Exception:                                          # noqa: BLE001
        return None
    w, h = im.size
    cx, cy, r, lw = float(tap["x"]) * w, float(tap["y"]) * h, max(10, w // 32), max(3, w // 160)
    d = ImageDraw.Draw(im)
    d.ellipse([cx - r - lw, cy - r - lw, cx + r + lw, cy + r + lw], outline=(0, 0, 0), width=lw)   # dark rim for contrast
    d.ellipse([cx - r, cy - r, cx + r, cy + r], outline=(255, 40, 40), width=lw)
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=88)
    return buf.getvalue()


def _entity_line(i: int, e: dict) -> str:
    bits = [f"{i}. {e['label']} ({e['kind']}"]
    if e.get("position"):
        bits[-1] += f", {e['position']}"
    bits[-1] += f", box {e['bbox']} measured at {ts(e['frame_t'])})"
    if e.get("look"):
        bits.append(f"Looks: {e['look']}")
    if e.get("description"):
        bits.append(e["description"])
    if e.get("facts"):
        bits.append("Known so far: " + "; ".join(e["facts"][-3:]))
    return " ".join(bits)


def fresh(m: Moment, screen: dict, e: dict) -> bool:
    """The entity's box matches the paused frame: measured within FRESH_BOX_S of floor(P), in the shot P is in (right
    after a cut the boxes still come from the shot before, which shows something else)."""
    return bool(screen.get("current_shot")) and m.fp - int(e["frame_t"]) <= FRESH_BOX_S


def focus(screen: dict) -> tuple[frozenset, frozenset]:
    """(tapped, on screen) characters, for the identity co-occurrence guard (`guard.identity_links`): the character the
    tap falls on, and every character among the shot's entities (those from the shot before a cut included — they
    may still be in view)."""
    refs = [e.get("ref") for e in screen.get("entities", [])]
    on = frozenset(r[1] for r in refs if r and r[0] == "character")
    ref = refs[screen["tapped"]] if "tapped" in screen else None
    return (frozenset([ref[1]]) if ref and ref[0] == "character" else frozenset()), on


def _tap_text(m: Moment, tap: dict, out: dict, ents: list[dict], ringed: bool) -> str:
    y, x = round(float(tap["y"]) * 1000), round(float(tap["x"]) * 1000)
    where = (f"The viewer tapped the paused frame at y={y}, x={x} on the 0–1000 grid"
             + ("; the red ring on the attached frame marks the spot and is authoritative." if ringed else "."))
    spot = "the ring" if ringed else "that point"
    if "tapped" in out:
        e = ents[out["tapped"]]
        age = m.fp - int(e["frame_t"])
        if fresh(m, out, e):
            return (f"{where} By box geometry the tap falls on entity {out['tapped'] + 1} ({e['label']}), whose box was "
                    f"measured on this frame. Answer about what is under {spot}.")
        when = (f"measured {age} s before the pause" if out.get("current_shot")
                else "measured in the shot before this one")
        return (f"{where} By box geometry the tap falls on entity {out['tapped'] + 1} ({e['label']}), but that box was "
                f"{when} and things may have moved: check what is actually under {spot} and answer about that.")
    if ents:
        return f"{where} No identified entity's box contains that point; answer about what is under {spot}."
    return f"{where} Nothing in this frame has been identified; describe what is under {spot}."


def _dialogue(m: Moment) -> tuple[list[dict], str]:
    lines = m.recent_dialogue(DIALOGUE_S)
    head = "LAST LINES SPOKEN (oldest first):"
    if not lines:
        lines = m.recent_dialogue(DIALOGUE_S, at_least=DIALOGUE_FALLBACK)
        head = ("Nothing was said in the last minute. THE LAST LINES BEFORE THAT (oldest first):" if lines
                else "LAST LINES SPOKEN: nothing has been said yet." if m.has_subtitles
                else "LAST LINES SPOKEN: not available — this film has no subtitle track, so what was said is unknown.")
    lines = lines[-DIALOGUE_MAX:]
    return lines, "\n".join([head] + [f"[{ln['at']}] {ln['speaker']}: \"{ln['text']}\"" for ln in lines])


def _story(out: dict) -> str:
    rows = []
    if "recap" in out:
        rows.append(f"Recap up to {out['recap']['up_to']}: {out['recap']['text']}")
        items = out.get("since_recap", [])
        if items:
            rows.append("After that:")
    else:
        items = out.get("so_far", [])
    rows += [f"[{it['at']}] {it['text']}" for it in items]
    return "STORY SO FAR:\n" + ("\n".join(rows) if rows else "Nothing has happened yet.")


def tapped_terms(m: Moment, tap: dict | None) -> list[str]:
    """Mention terms of the entity the tap geometrically falls on (empty if none)."""
    if not tap:
        return []
    out = m.on_screen(tap, refs=True)
    return m.mention_terms(out["entities"][out["tapped"]]["ref"]) if "tapped" in out else []


def mentions(text: str, terms: list[str]) -> bool:
    return any(term_matches(t, text, "phrase", False) for t in terms)


_PROBE_EN = re.compile(
    r"\b(?:endings?|finale|final scene|(?:in|at) the end(?! of)|how (?:does|will|did) [\w ]{1,30}? end|"
    r"what(?:'s| is| will)? (?:happen|happens|going to happen|gonna happen|comes?)(?: \w+)? (?:next|later|after|then)|"
    r"what happens to|happen(?:s)? (?:next|later|in the end)|later (?:on|in the (?:film|movie|story))|"
    r"who (?:dies|will die|survives|wins|is the (?:killer|murderer|traitor|villain|mole|spy|culprit))|"
    r"(?:die|dies|survive|survives|killed|escape|escapes|win|wins|come back|comes back|end up|get together)\s*\?|"
    r"turns? out|twist|real identity|true identity|secretly|spoil(?:er|ers)?)\b"
    r"|^\s*(?:(?:is|are|was)(?: \w+){1,3} (?:really|actually|secretly)|will|is \w+(?: \w+)? going to|are \w+(?: \w+)? going to|does \w+(?: \w+)? (?:die|survive|win|escape|"
    r"find|come back|get))\b", re.IGNORECASE)
_PROBE_ZH = re.compile(r"结局|结尾|最后(?:会|怎么|是|谁|的)|接下来(?:会|发生|怎么)|后来(?:会|怎么|发生)|以后(?:会|怎么)|后面(?:会|怎么)|"
                       r"将来|会不会死|会死|谁(?:会)?死|死了吗|活下来|活到最后|凶手|叛徒|卧底|内鬼|真实身份|真正的身份|真身|"
                       r"其实是|原来是|反转|剧透|会怎样|会发生什么|最终")


def spoiler_probe(question: str | None) -> bool:
    """The question asks about what has not been shown yet: the ending, what happens next, who dies, a secret
    identity, a twist. Such a question gets a decline, never an answer."""
    q = question or ""
    return bool(_PROBE_EN.search(q) or _PROBE_ZH.search(q))


PROBE_NOTE = ("ABOUT THIS QUESTION: it asks about something the viewer has not seen yet (what happens later, how it ends, "
              "who someone really is, who dies). Your whole reply is one short, friendly sentence saying that hasn't been "
              "shown yet; you may add one sentence about what is on screen right now. Do not name, describe or hint at "
              "any person, place, event or object in connection with what comes later, and do not repeat the viewer's "
              "guess.")


def build(m: Moment, tap: dict | None, turn: A.Turn, frame: bytes | None | type(...) = ..., question: str = "") -> Context:
    """Run the three lookups (bookkeeping into `turn` exactly as the tools do) and render the context. `frame`: the
    paused frame's bytes as the caller read them under its own time bound (None = not available); left out, it is
    read here. Blocking (pack reads, thumbnails that may come from Cloud Storage): the server calls it in a worker
    thread."""
    pos = ts(m.P)
    sections = [f"SERVER CONTEXT for this question. It covers only what the viewer has seen up to {pos} ({m.phase_words}); "
                f"nothing after {pos} exists for you."]
    if spoiler_probe(question):
        sections.append(PROBE_NOTE)

    screen = m.on_screen(tap, refs=True)
    ents = screen.get("entities", [])
    clean = {**screen, "entities": [{k: v for k, v in e.items() if k != "ref"} for e in ents]}
    lookups = [Lookup("ctx-1", "on_screen", A.TOOL_LABELS["on_screen"], A.note_on_screen(turn, m, clean))]
    ringed, note = False, ""
    if m.phase != "story":
        frame = None
    elif frame is ...:
        frame = A.read_thumb(m.title, m.fp)
    if frame is not None:
        if tap and (marked := mark_tap(frame, tap)):
            frame, ringed = marked, True
        note = (f"PAUSED FRAME at {pos} (exactly what the viewer sees now)"
                + ("; the red ring marks where they tapped" if ringed else "") + ":")
    if ents and not screen.get("current_shot"):
        started = screen.get("shot_started")
        sections.append(f"ON SCREEN: nothing identified yet in the current shot (a cut just happened). JUST BEFORE THE "
                        f"CUT, in the shot that started at {started}, these were identified; they may no longer be in "
                        f"view, so go by the attached frame (boxes are [ymin, xmin, ymax, xmax] on a 0–1000 grid):\n"
                        + "\n".join(_entity_line(i + 1, e) for i, e in enumerate(ents)))
    elif ents:
        sections.append("ON SCREEN (entities identified in the current shot; boxes are [ymin, xmin, ymax, xmax] on a "
                        "0–1000 grid, measured on a keyframe at the time given):\n"
                        + "\n".join(_entity_line(i + 1, e) for i, e in enumerate(ents)))
    else:
        sections.append("ON SCREEN: " + (screen.get("note") or "nothing identified.")
                        .replace("use look_closer to inspect it", "rely on the attached frame"))
    terms = []
    box_fresh = False
    if tap and m.phase == "story":
        sections.append("TAP: " + _tap_text(m, tap, screen, ents, ringed=ringed))
        if "tapped" in screen:
            e = ents[screen["tapped"]]
            terms, box_fresh = m.mention_terms(e["ref"]), fresh(m, screen, e)

    lines, dialogue = _dialogue(m)
    sections.append(dialogue)
    lookups.append(Lookup("ctx-2", "recent_dialogue", A.TOOL_LABELS["recent_dialogue"], A.note_dialogue(turn, lines)))

    story = m.story_so_far(limit=STORY_ITEMS, refs=True)
    sections.append(_story(story))
    lookups.append(Lookup("ctx-3", "story_so_far", A.TOOL_LABELS["story_so_far"], A.note_story(turn, m, story)))

    tapped, on = focus(screen)
    return Context(text="\n\n".join(sections), frame=frame, frame_note=note, lookups=lookups,
                   tapped_terms=terms, highlight_fresh=box_fresh, subjects=tapped, screen=on)
