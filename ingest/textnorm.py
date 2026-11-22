"""Shared word lists (spec §7.4) and canary / blocklist matching (spec §7.1–7.2).

Normalization reuses the `schema/wordlists/normalize.py` reference implementation, keeping the builder, online service, and evaluation in lockstep.
"""
from __future__ import annotations

import functools
import importlib.util
import re

from ingest.util import REPO

WORDLISTS_DIR = REPO / "schema" / "wordlists"
CJK = re.compile(r"[\u3400-\u9fff\uf900-\ufaff]")
# Characters that continue a Latin-script word. Only these bound a Latin term: CJK characters are \w in Python's
# regex, so a `\b`-style boundary would miss "Bob" in "那是Bob。".
LATIN_WORD = "0-9A-Za-z_\u00c0-\u024f"


def _load_ref():
    spec = importlib.util.spec_from_file_location("cedar_wordlists_normalize", WORDLISTS_DIR / "normalize.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_ref = _load_ref()


@functools.lru_cache(maxsize=1)
def wordlists_version() -> str:
    return (WORDLISTS_DIR / "VERSION").read_text(encoding="utf-8").strip()


@functools.lru_cache(maxsize=1)
def en_common() -> frozenset[str]:
    return frozenset(ln.strip() for ln in (WORDLISTS_DIR / "en_common.txt").read_text(encoding="utf-8").splitlines()
                     if ln.strip() and not ln.startswith("#"))


def is_common(word: str) -> bool:
    return word.lower() in en_common()


@functools.lru_cache(maxsize=200_000)
def normalize(text: str, case_sensitive: bool = False) -> str:
    return _ref.normalize(text or "", case_sensitive=case_sensitive)


def has_cjk(s: str) -> bool:
    return bool(CJK.search(s or ""))


def word_pattern(t: str, prefix: bool = False) -> str:
    """Regex for a normalised term: CJK terms match as substrings; a Latin-script term must not be glued to another
    Latin letter, digit or underscore on either side (on the left only, for a `prefix` match). Anything else —
    spaces, punctuation, CJK — is a boundary, so "Bob" matches in "那是Bob。" and possessive "Bob's" matches too."""
    if has_cjk(t):
        return re.escape(t)
    return rf"(?<![{LATIN_WORD}])" + re.escape(t) + ("" if prefix else rf"(?![{LATIN_WORD}])")


@functools.lru_cache(maxsize=8192)
def _compile(term: str, match: str, case_sensitive: bool) -> re.Pattern:
    if match == "regex":
        return re.compile(normalize(term, True), 0 if case_sensitive else re.IGNORECASE)
    return re.compile(word_pattern(normalize(term, case_sensitive)))


def term_matches(term: str, text: str, match: str = "word", case_sensitive: bool = False) -> bool:
    """`word` / `phrase`: whole word / whole phrase (substring for CJK); `regex`: search on the normalized text."""
    if not text or not term:
        return False
    return bool(_compile(term, match, bool(case_sensitive)).search(normalize(text, bool(case_sensitive))))


def term_search(term: str, text: str, match: str = "word", case_sensitive: bool = False) -> str | None:
    """Like `term_matches`, but returns the matched text (normalised), or None."""
    if not text or not term:
        return None
    mo = _compile(term, match, bool(case_sensitive)).search(normalize(text, bool(case_sensitive)))
    return mo.group(0) if mo else None


def tokens(term: str) -> list[str]:
    """Tokens of a name / alias / canary (Latin ≥3 chars; CJK runs), for the id-opacity check. Regex metacharacters are stripped first."""
    t = normalize(re.sub(r"\\[a-zA-Z]|[\^$.*+?()\[\]{}|\\]", " ", term))
    return re.findall(r"[a-z0-9]{3,}", t) + re.findall(r"[\u3400-\u9fff]+", t)
