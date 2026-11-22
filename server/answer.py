"""Streamed answers through the output guard, one sentence at a time.

Model text arrives in chunks. `GuardedAnswer.feed` cuts it into sentences (English and Chinese punctuation), checks
each finished sentence with the guard *together with everything already sent* (so a term that only appears across a
sentence boundary is caught as well), and returns only the sentences that pass, as the exact strings to send. A
sentence that fails is never sent; the caller logs it. `text` is always exactly the concatenation of what was sent.
After a drop the join is tidied: the first kept sentence loses a leading connective ("Then", "After that"), and the
final answer (`tidy_end`) does not end on a clause mark such as a semicolon.
"""
from __future__ import annotations

import re

# Latin sentence end: . ! ? … (+ closing quotes / brackets), confirmed by the whitespace after it
# Chinese sentence end: 。！？； (+ closing quotes / brackets), confirmed by any following character
# A newline always ends a sentence. At the end of the stream `flush` takes whatever is left.
_END = re.compile(r"""(?P<latin>[.!?…]+["'”’)\]]*)(?=\s)|(?P<cjk>[。！？；]+[”’」』）)\]"']*)(?=.)|(?P<nl>\n+)""", re.S)
_ABBREV = {"mr", "mrs", "ms", "dr", "st", "vs", "jr", "sr", "e.g", "i.e", "cf", "approx"}
_TOKEN_BEFORE = re.compile(r"([A-Za-z][A-Za-z.]*)$")
_CJKISH = re.compile(r"[　-〿㐀-鿿豈-﫿＀-￯]")


def _abbreviation(before: str) -> bool:
    m = _TOKEN_BEFORE.search(before)
    if not m:
        return False
    tok = m.group(1).rstrip(".")
    return tok.lower() in _ABBREV or (len(tok) == 1 and tok.isupper())          # "Dr. Lee", "J. Smith"


def split_ready(buf: str) -> tuple[list[str], str]:
    """(finished sentences, unfinished rest) of `buf`."""
    out, start = [], 0
    for m in _END.finditer(buf):
        if m.group("latin") and m.group("latin").startswith(".") and len(m.group("latin").rstrip("\"'”’)]")) == 1 \
                and _abbreviation(buf[start:m.start()]):
            continue
        out.append(buf[start:m.end()])
        start = m.end()
    return out, buf[start:]


def split_sentences(text: str) -> list[str]:
    ready, rest = split_ready(text)
    return [s for s in ready + [rest] if s.strip()]


# After the guard drops a sentence, what is left must still read as an answer: a first kept sentence does not open with
# a connective that pointed back at the dropped one, and the answer does not end on a clause mark.
_LEAD_EN = re.compile(r"^(?:and then|then|after that|afterwards|but|and|meanwhile|however|later,)[,]?\s+", re.IGNORECASE)
_LEAD_ZH = re.compile(r"^(?:随后|然后|之后|接着|后来|但是|不过|而且|于是|同时|此外|另外|可是)[，,]?")
_TRAIL = re.compile(r"\s*[；;，,、：:]+\s*$")


def strip_lead(sentence: str) -> str:
    """"Then the boy climbs a tree." → "The boy climbs a tree." (Chinese connectives alike)."""
    for rx in (_LEAD_EN, _LEAD_ZH):
        mo = rx.match(sentence)
        rest = sentence[mo.end():] if mo else ""
        if len(rest.strip()) > 1:
            return rest[:1].upper() + rest[1:]
    return sentence


def tidy_end(text: str) -> str:
    """An answer ending on ；，、： (its next clause was dropped) ends with a full stop instead."""
    mo = _TRAIL.search(text or "")
    if not mo or not text[:mo.start()].strip():
        return text
    body = text[:mo.start()]
    return body + ("。" if _CJKISH.match(body[-1]) else ".")


def _tight(prev: str, nxt: str) -> bool:
    """No space between two sentences when either side is Chinese."""
    return bool(_CJKISH.match(prev) or _CJKISH.match(nxt))


class GuardedAnswer:
    """`check(text) -> list[violation]` is the output guard at the locked P (empty list = safe)."""

    def __init__(self, check):
        self.check = check
        self.buf = ""
        self.text = ""
        self.dropped: list[list[dict]] = []          # guard hits of each dropped sentence
        self.dropped_text: list[str] = []            # the dropped sentences themselves (server log only)

    def feed(self, chunk: str) -> list[str]:
        self.buf += chunk
        ready, self.buf = split_ready(self.buf)
        return [d for s in ready if (d := self._accept(s))]

    def flush(self) -> list[str]:
        rest, self.buf = self.buf, ""
        d = self._accept(rest)
        return [d] if d else []

    def force(self, text: str) -> str:
        """Append a fixed server text (the fallback) without a guard check."""
        d = ("" if not self.text or _tight(self.text[-1], text[0]) else " ") + text
        self.text += d
        return d

    def _accept(self, sentence: str) -> str | None:
        s = " ".join(sentence.split())
        if s and not self.text and self.dropped:          # everything before it was dropped
            s = strip_lead(s)
        if not s:
            return None
        delta = ("" if not self.text or _tight(self.text[-1], s[0]) else " ") + s
        hits = self.check(self.text + delta)
        if hits:
            self.dropped.append(hits)
            self.dropped_text.append(s)
            return None
        self.text += delta
        return delta
