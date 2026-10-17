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


@functools.lru_cache(maxsize=8192)
def _compile(term: str, match: str, case_sensitive: bool) -> re.Pattern:
    if match == "regex":
        return re.compile(normalize(term, True), 0 if case_sensitive else re.IGNORECASE)
    t = normalize(term, case_sensitive)
    if has_cjk(t):
        return re.compile(re.escape(t))  # CJK: substring match
    return re.compile(r"(?<!\w)" + re.escape(t) + r"(?!\w)")  # Latin: whole word; possessive X's matches naturally


def term_matches(term: str, text: str, match: str = "word", case_sensitive: bool = False) -> bool:
    """`word` / `phrase`: whole word / whole phrase (substring for CJK); `regex`: search on the normalized text."""
    if not text or not term:
        return False
    return bool(_compile(term, match, bool(case_sensitive)).search(normalize(text, bool(case_sensitive))))


def tokens(term: str) -> list[str]:
    """Tokens of a name / alias / canary (Latin ≥3 chars; CJK runs), for the id-opacity check. Regex metacharacters are stripped first."""
    t = normalize(re.sub(r"\\[a-zA-Z]|[\^$.*+?()\[\]{}|\\]", " ", term))
    return re.findall(r"[a-z0-9]{3,}", t) + re.findall(r"[\u3400-\u9fff]+", t)
