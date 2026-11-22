"""What an answer shows as its sources (both flows): only the items the emitted answer rests on, each checked by the
same guard as the answer.

The context flow gives the model the recap, the last lines and the tapped entity's facts whether or not the answer uses
them, so listing all of them would show the viewer things the answer never said — the leak path the benchmark found
(an eligible line naming the b-side of a pending identity link next to an answer about the a-side). So the lookups and
tools only offer *candidates* (`Turn.support`); after the answer is final, `select` keeps those that share content
words with the sentences of the answer that assert something (a sentence that only says the film has not shown or said
something yet rests on nothing), drops every one the guard objects to relative to the answer's subject, and keeps at
most four. A recap is offered sentence by sentence, so one fact does not bring the whole recap along. A pure refusal or
a fallback shows no sources and no frames.

Chinese answers: the candidates are mostly English, so the English label / name of every character the answer names in
Chinese is added to its words, and a candidate is also matched through its own Chinese text when the pack has one
(shown instead of the English to a Chinese viewer).
"""
from __future__ import annotations

import dataclasses
import re

from ingest.textnorm import normalize
from server.answer import split_sentences
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
_REFUSAL_EN = [
    r"\b(?:has|have|had|is|are|was|were)(?:n'?t| not)\s+(?:yet\s+|really\s+|actually\s+)?(?:been\s+)?(?:shown|revealed|"
    r"explained|made clear|established|confirmed|disclosed|identified|answered|addressed|explored|covered|clear|known|"
    r"said|told|mentioned|named|stated|given)\b",
    r"\b(?:we|i|you|viewers?|the (?:film|movie|story|show|audience))\s+(?:still\s+)?(?:do|does|did|can|could)"
    r"(?:n'?t| not|not)\s+(?:yet\s+|really\s+)?(?:know|say|show|reveal|tell|explain|mention|see|answer|confirm|"
    r"determine|be sure)\b",
    r"\b(?:can'?t|cannot|can not|couldn'?t|unable to)\s+(?:yet\s+)?(?:tell|say|answer|determine|confirm|be sure)\b",
    r"\bnot\s+(?:yet\s+)?(?:been\s+)?(?:shown|revealed|known|clear|explained|mentioned)\b",
    r"\b(?:no one|nobody)\s+(?:has|had)\s+(?:yet\s+)?(?:spoken|said|explained|mentioned|named|revealed)\b",
    r"\b(?:unclear|unknown|a mystery|remains to be seen|yet to be (?:seen|revealed|shown|explained|named)|wait and see|"
    r"too early to (?:say|tell)|hard to say|no way to (?:know|tell)|not sure|no (?:indication|information|explanation) "
    r"(?:yet|so far))\b",
    r"\bwe'?ll (?:have to |need to |just have to )?(?:wait|see)\b",
    r"\bwithout (?:spoiling|spoilers|giving (?:it|anything) away)\b",
]
_REFUSAL_ZH = (r"还没(?:有)?(?:展示|透露|说明|揭示|揭晓|提到|交代|说|讲|告诉|表明|解释)|尚未|无法(?:判断|确定|回答|得知|知道|说)|"
               r"看不出|还不(?:知道|清楚|确定|明确)|不清楚|不确定|未知|没有(?:提到|说明|透露|交代|揭示|解释)|暂时(?:还)?(?:不|没|无法)|"
               r"(?:还是)?个谜|不得而知|目前(?:还)?(?:不|没)|剧透")
REFUSAL = re.compile("|".join(_REFUSAL_EN + [_REFUSAL_ZH]), re.IGNORECASE)


@dataclasses.dataclass
class Candidate:
    """One possible source. `kind`: recap (one sentence of it), story, line, fact, search."""
    kind: str
    type: str
    start_s: float
    end_s: float
    text: str
    frame_at: float | None = None             # a frame of this moment can illustrate it
    text_zh: str | None = None                # the pack's Chinese text of the same row, if any

    def shown(self, lang: str) -> str:
        return self.text_zh if lang == "zh" and self.text_zh else self.text

    def row(self, lang: str = "en") -> dict:
        return {"type": self.type, "start_s": round(self.start_s, 2), "end_s": round(self.end_s, 2),
                "text": self.shown(lang)}


def words(text: str | None, names=()) -> set[str]:
    """Content words: English words of 3+ letters that are not function words (a trailing plural s dropped), and
    Chinese character bigrams. `names`: (Chinese, English) pairs; a Chinese name counts as its English word(s), not
    as the bigrams it would give (a three-character name would otherwise be two shared words on its own)."""
    t = text or ""
    for zh, en in sorted(names, key=lambda n: -len(n[0])):
        if zh and zh in t:
            t = t.replace(zh, f" {en} ")
    t = normalize(t)
    out = set()
    for w in re.findall(r"[a-z][a-z']{2,}", t):
        w = w.removesuffix("'s")
        if w in STOP or len(w) < 3:
            continue
        out.add(w[:-1] if w.endswith("s") and len(w) > 4 else w)
    for run in re.findall(r"[㐀-鿿]+", t):
        out |= {run[i:i + 2] for i in range(len(run) - 1)}
    return out


def is_refusal_sentence(sentence: str) -> bool:
    return bool(REFUSAL.search(sentence or ""))


def assertive(answer: str | None) -> str:
    """The sentences of the answer that assert something: all but those saying the film has not shown / said / made
    clear something yet (or that the assistant cannot say)."""
    return " ".join(s.strip() for s in split_sentences(answer or "") if not is_refusal_sentence(s))


def is_refusal(answer: str | None) -> bool:
    """Nothing in the answer asserts anything."""
    return not assertive(answer).strip()


def answer_words(answer: str, names=()) -> set[str]:
    """`words` of the assertive part, Chinese names read as their English (`names`)."""
    return words(assertive(answer), names)


def select(candidates: list[Candidate], answer: str, check, *, about_tapped: bool = False, refusal: bool = False,
           lang: str = "en", names=()) -> list[Candidate]:
    """The candidates the answer rests on: two shared content words (one for a fact about the tapped entity when the
    answer is about it) with the assertive sentences, the shown text passing `check`, best first, at most CAP; shown
    oldest first."""
    if refusal or is_refusal(answer):
        return []
    aw = answer_words(answer, names)
    seen, scored = set(), []
    for i, c in enumerate(candidates):
        key = (c.type, round(c.start_s, 2), c.text)
        if key in seen or not c.text:
            continue
        seen.add(key)
        n = len(aw & (words(c.text) | words(c.text_zh, names)))
        if n >= (1 if c.kind == "fact" and about_tapped else 2) and not check(c.shown(lang)):
            scored.append((-n, i, c))
    best = [c for _n, _i, c in sorted(scored)[:CAP]]
    return sorted(best, key=lambda c: (c.start_s, c.kind))


def frame_plan(m: Moment, kept: list[Candidate], *, ref_frames=()) -> list[tuple[str, list[int]]]:
    """(caption, [preferred seconds…]) for the frames to show: one per kept source that has a moment, the tapped
    entity's reference stills, and the paused frame — all at or before floor(P)."""
    wants = []
    for c in kept:
        if c.frame_at is not None:
            t = min(int(c.frame_at), m.fp)
            cap = f"更早：{ts(c.frame_at)}" if m.lang == "zh" else f"Earlier: {ts(c.frame_at)}"
            wants.append((cap, [t, int(c.start_s), m.fp]))
    wants += [(cap, [t]) for t, cap in ref_frames]
    wants.append((f"暂停画面 {ts(m.P)}" if m.lang == "zh" else f"Paused frame {ts(m.P)}", [m.fp]))
    return [(cap, [t for t in prefs if 0 <= t <= m.fp]) for cap, prefs in wants]


def frames(m: Moment, plan: list[tuple[str, list[int]]], check) -> list[dict]:
    """The planned frames that exist, captions checked like the sources, at most FRAMES_CAP. One concurrent fetch for
    all of them (`Title.prefetch`). Blocking: call it in a worker thread."""
    asked = sorted({t for _cap, prefs in plan for t in prefs})
    have = set(m.title.prefetch(asked)) if asked else set()
    out, used = [], set()
    for cap, prefs in plan:
        t = next((t for t in prefs if t in have), None)
        if t is not None and t not in used and not check(cap):
            used.add(t)
            out.append({"t": t, "caption": cap})
    return out[-FRAMES_CAP:] if len(out) > FRAMES_CAP else out
