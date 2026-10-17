"""Output guards (spec §7), applied to every answer before it leaves the server.

1. Real people's names (`blocklist`): never allowed.
2. Future canaries (`canaries`): a term is a spoiler while `P < active_until_s`.
3. Name visibility (§6): a character's name / non-generic alias must not appear before it is visible at P.

The gate (§5) is the primary defence; these checks are the safety net for wording the gate cannot see — for example the
model recalling the film from its training data.
"""
from __future__ import annotations

import re

from ingest.checks import name_terms
from ingest.retrieval import EPS, margins
from ingest.textnorm import has_cjk, normalize, term_matches


def _prefix_match(term: str, text: str, case_sensitive: bool) -> bool:
    """`word` / `phrase` terms ending in `*` are prefix matches (`mark*` matches marked, not Denmark)."""
    t = normalize(term[:-1], case_sensitive)
    if has_cjk(t):
        return t in normalize(text, case_sensitive)
    return re.search(r"(?<!\w)" + re.escape(t), normalize(text, case_sensitive)) is not None


def matches(term: str, text: str, match: str, case_sensitive: bool) -> bool:
    if match in ("word", "phrase") and term.endswith("*") and len(term) > 1:
        return _prefix_match(term, text, case_sensitive)
    return term_matches(term, text, match, case_sensitive)


def violations(pack: dict, text: str, P: float) -> list[dict]:
    """[{kind, term}] for every guard hit in `text` at P (empty = safe). Terms stay on the server."""
    hits = []
    for b in pack.get("blocklist", []):
        if term_matches(b["term"], text, "word", False):
            hits.append({"kind": "real_name", "term": b["term"]})
    for k in pack.get("canaries", []):
        if P + EPS < k["active_until_s"] and matches(k["term"], text, k["match"], bool(k["case_sensitive"])):
            hits.append({"kind": f"canary:{k['kind']}", "term": k["term"]})
    m_rev = margins(pack)["gate_margin_reviewed_reveal_s"]
    for _cid, term, known_at, cs in name_terms(pack):
        if P + EPS < known_at + m_rev and term_matches(term, text, "word", cs):
            hits.append({"kind": "name_not_yet_visible", "term": term})
    seen, out = set(), []
    for h in hits:
        if h["term"] not in seen:
            seen.add(h["term"])
            out.append(h)
    return out
