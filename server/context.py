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
    subjects: frozenset = frozenset()     # characters the answer is about (identity co-occurrence, server/guard.py)

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


def focus(m: Moment, screen: dict) -> frozenset:
    """Who the answer is about, for the identity co-occurrence guard: the tapped character; without a tap, every
    on-screen character that is the a-side of a pending identity link whose b-side is not on screen too."""
    ents = screen.get("entities", [])
    refs = [e.get("ref") for e in ents]
    if "tapped" in screen:
        ref = refs[screen["tapped"]]
        return frozenset([ref[1]]) if ref and ref[0] == "character" else frozenset()
    on = {r[1] for r in refs if r and r[0] == "character"}
    return frozenset(lk["a"] for lk in m.gate.pending if lk["a"] in on and lk["b"] not in on)


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
                else "LAST LINES SPOKEN: nothing has been said yet.")
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


def build(m: Moment, tap: dict | None, turn: A.Turn) -> Context:
    """Run the three lookups (bookkeeping into `turn` exactly as the tools do) and render the context. Blocking (pack
    reads, thumbnails that may come from Cloud Storage): the server calls it in a worker thread."""
    pos = ts(m.P)
    phase = {"story": "during the story", "before_story": "before the story has started",
             "credits": "the end credits are rolling; the story is over"}[m.phase]
    sections = [f"SERVER CONTEXT for this question. It covers only what the viewer has seen up to {pos} ({phase}); "
                f"nothing after {pos} exists for you."]

    screen = m.on_screen(tap, refs=True)
    ents = screen.get("entities", [])
    clean = {**screen, "entities": [{k: v for k, v in e.items() if k != "ref"} for e in ents]}
    lookups = [Lookup("ctx-1", "on_screen", A.TOOL_LABELS["on_screen"], A.note_on_screen(turn, m, clean))]
    frame, ringed, note = None, False, ""
    if m.phase == "story" and (frame := A.read_thumb(m.title, m.fp)) is not None:
        if tap and (marked := mark_tap(frame, tap)):
            frame, ringed = marked, True
        note = (f"PAUSED FRAME at {pos} (exactly what the viewer sees now)"
                + ("; the red ring marks where they tapped" if ringed else "") + ":")
        turn.add_frame(m.fp, f"Paused frame {pos}")
    if ents:
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

    story = m.story_so_far(limit=STORY_ITEMS)
    sections.append(_story(story))
    lookups.append(Lookup("ctx-3", "story_so_far", A.TOOL_LABELS["story_so_far"], A.note_story(turn, m, story, frames=False)))

    return Context(text="\n\n".join(sections), frame=frame, frame_note=note, lookups=lookups,
                   tapped_terms=terms, highlight_fresh=box_fresh, subjects=focus(m, screen))
