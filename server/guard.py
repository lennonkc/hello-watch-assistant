"""Output guards (spec §7), applied to every answer before it leaves the server.

1. Real people's names (`blocklist`): never allowed.
2. Future canaries (`canaries`): a term is a spoiler while `P < active_until_s`.
3. Name visibility (§6): a character's name / non-generic alias must not appear before it is visible at P.
4. Identity co-occurrence (§5.3, online): while an identity link a = b is pending at P, no sentence may contain both a
   way of referring to a (its label at P, a visible name or alias) and a name / non-generic alias of b; and when the
   answer is about a (the tapped character, an on-screen a whose b is not on screen, or an a the question mentions),
   b's names may not appear at all — not even in "the film hasn't said whether this is b".

The gate (§5) is the primary defence; these checks are the safety net for wording the gate cannot see — for example the
model recalling the film from its training data.
"""
from __future__ import annotations

import dataclasses
import re

from ingest.checks import name_terms
from ingest.derive import name_case_sensitive
from ingest.retrieval import EPS, margins
from ingest.textnorm import normalize, term_matches, word_pattern


def _prefix_match(term: str, text: str, case_sensitive: bool) -> bool:
    """`word` / `phrase` terms ending in `*` are prefix matches (`mark*` matches marked, not Denmark)."""
    t = normalize(term[:-1], case_sensitive)
    return re.search(word_pattern(t, prefix=True), normalize(text, case_sensitive)) is not None


def matches(term: str, text: str, match: str, case_sensitive: bool) -> bool:
    if match in ("word", "phrase") and term.endswith("*") and len(term) > 1:
        return _prefix_match(term, text, case_sensitive)
    return term_matches(term, text, match, case_sensitive)


@dataclasses.dataclass(frozen=True)
class Link:
    """A pending identity link a = b as the guard sees it: (term, case_sensitive) pairs for each side."""
    a: str
    b: str
    a_terms: tuple
    b_terms: tuple
    focus: bool                  # the answer is about a: b's names may not appear anywhere in it


def _label_forms(label: str | None) -> list[str]:
    """A label and, if what follows a leading article is still specific (two words or more), the label without it:
    "the cave dragon" → also "cave dragon"; "the wolf" stays as is ("wolf" alone would hit every mention of a wolf)."""
    label = (label or "").strip()
    rest = re.sub(r"^(the|a|an)\s+", "", label, flags=re.IGNORECASE)
    return [label] + ([rest] if rest != label and len(rest.split()) >= 2 else []) if label else []


def identity_links(m, subjects=(), question: str = "") -> list[Link]:
    """The links still pending at the Moment `m` (`Gate.pending`). `subjects`: character ids the answer is about
    (tapped / on-screen focus); an a-side the question refers to counts too."""
    chars, mr = m.chars, m.m["gate_margin_reviewed_reveal_s"]
    aliases: dict = {}
    for al in m.pack.get("character_aliases", []):
        if not al["generic"]:
            aliases.setdefault(al["character_id"], []).append(al)
    out, seen = [], set()
    for lk in m.gate.pending:
        a, b = lk["a"], lk["b"]
        if (a, b) in seen or a not in chars or b not in chars:
            continue
        seen.add((a, b))
        ca, cb = chars[a], chars[b]
        a_terms = [(f, False) for lab in (m.label(a, "en"), m.label(a, "zh")) for f in _label_forms(lab)]
        if m.name_visible(ca):
            a_terms += [(n, bool(name_case_sensitive(n))) for n in (ca["canonical_name"], ca.get("name_zh")) if n]
        a_terms += [(al["alias"], bool(name_case_sensitive(al["alias"]))) for al in aliases.get(a, [])
                    if al["alias_known_at_s"] + mr <= m.P + EPS]
        b_terms = [(n, bool(name_case_sensitive(n))) for n in (cb["canonical_name"], cb.get("name_zh")) if n]
        b_terms += [(al["alias"], bool(name_case_sensitive(al["alias"]))) for al in aliases.get(b, [])]
        a_keys = {normalize(t) for t, _ in a_terms}
        b_terms = [(t, cs) for t, cs in dict.fromkeys(b_terms) if normalize(t) not in a_keys]
        a_terms = list(dict.fromkeys(a_terms))
        focus = a in subjects or any(term_matches(t, question, "word", cs) for t, cs in a_terms)
        out.append(Link(a, b, tuple(a_terms), tuple(b_terms), focus))
    return out


def _sentences(text: str) -> list[str]:
    from server.answer import split_sentences
    return split_sentences(text)


def _cooccurrence(links, text: str) -> list[dict]:
    hits = []
    for lk in links:
        b_hit = next((t for t, cs in lk.b_terms if term_matches(t, text, "word", cs)), None)
        if b_hit is None:
            continue
        if lk.focus or any(any(term_matches(t, s, "word", cs) for t, cs in lk.b_terms)
                           and any(term_matches(t, s, "word", cs) for t, cs in lk.a_terms) for s in _sentences(text)):
            hits.append({"kind": "identity_cooccurrence", "term": b_hit})
    return hits


def _hit(terms, text: str) -> str | None:
    return next((t for t, cs in terms if term_matches(t, text, "word", cs)), None)


def source_violations(pack: dict, text: str, P: float, links, answer: str) -> list[dict]:
    """A source shown next to `answer` (evidence line, frame caption, highlight label): rules 1–4 on its own text, and
    rule 4 relative to the answer's subject — next to an answer about a (or naming a) no source may name b, and next
    to an answer naming b no source may refer to a."""
    hits = violations(pack, text, P, links)
    for lk in links:
        about_a = lk.focus or _hit(lk.a_terms, answer) is not None
        about_b = _hit(lk.b_terms, answer) is not None
        b_here, a_here = _hit(lk.b_terms, text), _hit(lk.a_terms, text)
        if about_a and b_here:
            hits.append({"kind": "identity_cooccurrence", "term": b_here})
        elif about_b and a_here:
            hits.append({"kind": "identity_cooccurrence", "term": a_here})
    return hits


def violations(pack: dict, text: str, P: float, links=()) -> list[dict]:
    """[{kind, term}] for every guard hit in `text` at P (empty = safe). Terms stay on the server. `links`: the pending
    identity links from `identity_links` (rule 4)."""
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
    hits += _cooccurrence(links, text)
    seen, out = set(), []
    for h in hits:
        if h["term"] not in seen:
            seen.add(h["term"])
            out.append(h)
    return out
