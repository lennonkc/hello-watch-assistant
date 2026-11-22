"""What an answer shows as its sources (context flow): only the items the emitted answer rests on, each checked by the
same guard as the answer.

The context flow gives the model the recap, the last lines and the tapped entity's facts whether or not the answer uses
them, so listing all of them would show the viewer things the answer never said — the leak path the benchmark found
(an eligible line naming the b-side of a pending identity link next to an answer about the a-side). So the lookups and
tools only offer *candidates* (`Turn.support`); after the answer is final, `select` keeps those that share content
words with it (a recap is offered sentence by sentence, so one fact does not bring the whole recap along), drops every
one the guard objects to relative to the answer's subject, and keeps at most four. A refusal or a fallback shows no
sources and no frames.
"""
from __future__ import annotations

import dataclasses
import re

from ingest.textnorm import normalize
from server.knowledge import Moment, ts

CAP = 4
FRAMES_CAP = 4
STOP = frozenset("""
a an the and or but if then than that this these those there here is are was were be been being am do does did have has
had not no yes it its he she they them his her their him you your we our i me my of in on at to from by for with about
as into over under up down out off just only very too also so such what who whom which when where why how all any some
more most other another one two can could would should will may might must shall again still yet ever never now later
earlier before after while during since until film shown show seen see says said say tell told just like
""".split())
REFUSAL = re.compile(
    r"\b(hasn'?t|has not|haven'?t|have not)\s+(been\s+)?(shown|revealed|said|told|explained|made clear)"
    r"|\b(can'?t|cannot|can not)\s+(tell|say|answer)|\bnot\s+(been\s+)?(shown|revealed)\s+yet"
    r"|\bno one has (spoken|said)|\bnobody has (spoken|said)"
    r"|还没(有)?(展示|透露|说明|揭示|提到|说)|尚未|无法(判断|确定|回答)|看不出", re.IGNORECASE)


@dataclasses.dataclass
class Candidate:
    """One possible source. `kind`: recap (one sentence of it), story, line, fact, search."""
    kind: str
    type: str
    start_s: float
    end_s: float
    text: str
    frame_at: float | None = None             # a frame of this moment can illustrate it

    def row(self) -> dict:
        return {"type": self.type, "start_s": round(self.start_s, 2), "end_s": round(self.end_s, 2), "text": self.text}


def words(text: str | None) -> set[str]:
    """Content words: English words of 3+ letters that are not function words (a trailing plural s dropped), and
    Chinese character bigrams."""
    t = normalize(text or "")
    out = set()
    for w in re.findall(r"[a-z][a-z']{2,}", t):
        w = w.removesuffix("'s")
        if w in STOP or len(w) < 3:
            continue
        out.add(w[:-1] if w.endswith("s") and len(w) > 4 else w)
    for run in re.findall(r"[㐀-鿿]+", t):
        out |= {run[i:i + 2] for i in range(len(run) - 1)}
    return out


def is_refusal(answer: str) -> bool:
    return bool(REFUSAL.search(answer or ""))


def select(candidates: list[Candidate], answer: str, check, *, about_tapped: bool = False,
           refusal: bool = False) -> list[Candidate]:
    """The candidates the answer rests on: two shared content words (one for a fact about the tapped entity when the
    answer is about it), that pass `check`, best first, at most CAP; shown oldest first."""
    if refusal or not (answer or "").strip() or is_refusal(answer):
        return []
    aw = words(answer)
    seen, scored = set(), []
    for i, c in enumerate(candidates):
        key = (c.type, round(c.start_s, 2), c.text)
        if key in seen or not c.text:
            continue
        seen.add(key)
        n = len(aw & words(c.text))
        if n >= (1 if c.kind == "fact" and about_tapped else 2) and not check(c.text):
            scored.append((-n, i, c))
    best = [c for _n, _i, c in sorted(scored)[:CAP]]
    return sorted(best, key=lambda c: (c.start_s, c.kind))


def frames(m: Moment, kept: list[Candidate], answer: str, check, *, ref_frames=(), refusal: bool = False) -> list[dict]:
    """Frames for what is shown: one per kept source that has a moment, the tapped entity's reference stills when the
    answer is about it, and the paused frame — all at or before floor(P), captions checked like the sources. One
    concurrent fetch for all of them (`Title.prefetch`). Blocking: call it in a worker thread."""
    if refusal or not (answer or "").strip() or is_refusal(answer):
        return []
    wants = []                                              # (caption, [preferred seconds…])
    for c in kept:
        if c.frame_at is not None:
            t = min(int(c.frame_at), m.fp)
            wants.append((f"Earlier: {ts(c.frame_at)}", [t, int(c.start_s), m.fp]))
    wants += [(cap, [t]) for t, cap in ref_frames]
    wants.append((f"Paused frame {ts(m.P)}", [m.fp]))
    asked = sorted({t for _cap, ts_ in wants for t in ts_ if 0 <= t <= m.fp})
    have = set(m.title.prefetch(asked)) if asked else set()
    out, used = [], set()
    for cap, prefs in wants:
        t = next((t for t in prefs if t in have and 0 <= t <= m.fp), None)
        if t is not None and t not in used and not check(cap):
            used.add(t)
            out.append({"t": t, "caption": cap})
    return out[-FRAMES_CAP:] if len(out) > FRAMES_CAP else out
