"""Streamed answers through the output guard, one sentence at a time.

Model text arrives in chunks. `GuardedAnswer.feed` cuts it into sentences (English and Chinese punctuation), checks
each finished sentence with the guard *together with everything already sent* (so a term that only appears across a
sentence boundary is caught as well), and returns only the sentences that pass, as the exact strings to send. A
sentence that fails is never sent; the caller logs it. `text` is always exactly the concatenation of what was sent.
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


def _tight(prev: str, nxt: str) -> bool:
    """No space between two sentences when either side is Chinese."""
    return bool(_CJKISH.match(prev) or _CJKISH.match(nxt))


class GuardedAnswer:
    """`check(text) -> list[violation]` is the output guard at the locked P (empty list = safe)."""

    def __init__(self, check):
        self.check = check
        self.buf = ""
        self.text = ""
        self.sent: list[str] = []
        self.dropped: list[list[dict]] = []          # guard hits of each dropped sentence

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
        self.sent.append(d)
        return d

    def _accept(self, sentence: str) -> str | None:
        s = " ".join(sentence.split())
        if not s:
            return None
        delta = ("" if not self.text or _tight(self.text[-1], s[0]) else " ") + s
        hits = self.check(self.text + delta)
        if hits:
            self.dropped.append(hits)
            return None
        self.text += delta
        self.sent.append(delta)
        return delta
