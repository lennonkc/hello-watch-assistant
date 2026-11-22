"""Output guards (spec §7), applied to every answer before it leaves the server.

1. Real people's names (`blocklist`): never allowed.
2. Future canaries (`canaries`): a term is a spoiler while `P < active_until_s`.
3. Name visibility (§6): a character's name / non-generic alias must not appear before it is visible at P.
4. Identity co-occurrence (§5.3, online): while an identity link a = b is pending at P, no passage may refer to both
   a and b. A *reference* is a name, an alias, or the label — with any determiner and adjectives in front of its head
   noun ("the wolf" → "that wolf", "a grey wolf", "wolves"). A *passage* is a sentence plus the sentences after it that
   continue it with a pronoun ("A grey wolf has appeared. It is Bob."). When the question is about a (a is tapped,
   the question refers to a, or a is on screen without b and the question points at the screen: "who is this?"), b's
   specific references (names, aliases, its label: "the boy", "that boy") may not appear anywhere in the answer — not
   even in "the film hasn't said whether this is b"; the same holds the other way round for a question about b.

The gate (§5) is the primary defence; these checks are the safety net for wording the gate cannot see — for example the
model recalling the film from its training data. A pronoun or paraphrase that links a and b across sentences without
any lexical cue cannot be caught here (docs/online-qa.md §4a).
"""
from __future__ import annotations

import dataclasses
import re

from ingest.checks import name_terms
from ingest.derive import name_case_sensitive
from ingest.retrieval import EPS, margins
from ingest.textnorm import LATIN_WORD, has_cjk, normalize, term_matches, term_search, word_pattern
from server.knowledge import name_usable

L = f"[{LATIN_WORD}]"


def _prefix_match(term: str, text: str, case_sensitive: bool) -> bool:
    """`word` / `phrase` terms ending in `*` are prefix matches (`mark*` matches marked, not Denmark)."""
    t = normalize(term[:-1], case_sensitive)
    return re.search(word_pattern(t, prefix=True), normalize(text, case_sensitive)) is not None


def matches(term: str, text: str, match: str, case_sensitive: bool) -> bool:
    if match in ("word", "phrase") and term.endswith("*") and len(term) > 1:
        return _prefix_match(term, text, case_sensitive)
    return term_matches(term, text, match, case_sensitive)


# ── references to a character ────────────────────────────────────────

@dataclasses.dataclass(frozen=True)
class Refs:
    """Ways of referring to a character: (shown term, regex on the normalised text, case_sensitive)."""
    items: tuple = ()

    def find(self, text: str | None) -> str | None:
        if not text:
            return None
        for term, rx, cs in self.items:
            if re.search(rx, normalize(text, cs)):
                return term
        return None

    def without(self, keys: set) -> "Refs":
        return Refs(tuple(i for i in self.items if i[1] not in keys))


def _term(t: str, cs: bool = False) -> tuple:
    return (t, word_pattern(normalize(t, cs)), cs)


_HEAD_STOP = frozenset("in of with on at from by near under over wearing who that which whose holding carrying behind "
                       "beside and or to for".split())
_NOT_HEADS = frozenset("one ones other others thing someone somebody".split())
_ZH_DET = re.compile(r"^(?:那|这|一)(?:只|个|位|头|名|条|匹|群|对)")
_IRREGULAR = {"man": "men", "woman": "women", "child": "children", "person": "people", "mouse": "mice",
              "goose": "geese", "foot": "feet", "tooth": "teeth"}


def _plural(w: str) -> str:
    if w in _IRREGULAR:
        return _IRREGULAR[w]
    if w.endswith("man"):
        return w[:-3] + "men"
    if w.endswith("fe"):
        return w[:-2] + "ves"
    if w.endswith("f"):
        return w[:-1] + "ves"
    if re.search(r"[^aeiou]y$", w):
        return w[:-1] + "ies"
    return w + ("es" if re.search(r"(s|x|z|ch|sh)$", w) else "s")


def head_noun(label: str | None) -> str | None:
    """The head noun of an English label: "the cave dragon" → dragon, "the man in the hat" → man."""
    words = re.findall(r"[A-Za-zÀ-ɏ][A-Za-zÀ-ɏ'’-]*", label or "")
    while words and words[0].lower() in ("the", "a", "an", "this", "that"):
        words = words[1:]
    core = []
    for w in words:
        if w.lower() in _HEAD_STOP:
            break
        core.append(w)
    h = core[-1].lower() if core else ""
    return h if len(h) >= 3 and h not in _NOT_HEADS else None


def _label_refs(label: str | None) -> tuple[list, list]:
    """(specific, loose) references for one label. Specific: the label, the label without its article when that is
    still two words or more ("cave dragon"), and "the / that / this … <head>" with up to two words in between. Loose
    adds the bare head noun and its plural under any determiner ("a wolf", "wolves"). Chinese: the label, without a
    leading "那只 / 一个 …", and loosely the part after the last 的."""
    label = (label or "").strip()
    if not label:
        return [], []
    if has_cjk(label):
        core = _ZH_DET.sub("", label)
        spec = [_term(label)] + ([_term(core)] if core and core != label else [])
        tail = core.rsplit("的", 1)[-1] if "的" in core else ""
        return spec, spec + ([_term(tail)] if len(tail) >= 2 else [])
    spec = [_term(label)]
    rest = re.sub(r"^(the|a|an)\s+", "", label, flags=re.IGNORECASE)
    if rest != label and len(rest.split()) >= 2:
        spec.append(_term(rest))
    head = head_noun(label)
    if not head:
        return spec, spec
    nouns = f"(?:{re.escape(head)}|{re.escape(_plural(head))})"
    word = rf"{L}[{LATIN_WORD}'’-]*"
    spec.append((f"the {head}", rf"(?<!{L})(?:the|that|this|these|those)\s+(?:{word}\s+){{0,2}}{nouns}(?!{L})", False))
    return spec, spec + [(head, rf"(?<!{L}){nouns}(?!{L})", False)]


def _refs(m, cid: str, aliases: list[dict]) -> tuple[Refs, Refs]:
    """(specific, loose) references to a character at the Moment `m`: its names and non-generic aliases (whether or
    not visible yet — a hidden one is a reference too; a name that is also a common noun stays case-sensitive), its
    pre-name labels in both languages; visible generic aliases only loosely."""
    c = m.chars[cid]
    spec, loose = [], []
    for n in (c["canonical_name"], c.get("name_zh"), *(a["alias"] for a in aliases if not a["generic"])):
        if n:
            t = _term(n, bool(name_case_sensitive(n)))
            spec.append(t)
            loose.append(t)
    for lab in dict.fromkeys((c["display_label_pre_name"], c["display_label_pre_name_zh"])):
        s, lo = _label_refs(lab)
        spec += s
        loose += lo
    loose += [_term(a["alias"]) for a in aliases if a["generic"] and m.visible(a["alias_known_at_s"])]
    return Refs(tuple(dict.fromkeys(spec))), Refs(tuple(dict.fromkeys(loose)))


# ── what the question is about ───────────────────────────────────────

_POINTING = re.compile(r"(?<![A-Za-z])(?:this|that|these|those|he|she|it|him|her|they|them|his|its)(?![A-Za-z])"
                       r"|[这那他她它牠]", re.IGNORECASE)
_NOT_POINTING = re.compile(r"(?<![A-Za-z])(?:this|that)\s+(?:film|movie|story|scene|part|moment|point|time)(?![A-Za-z])"
                           r"|这(?:部|个故事|场|段|一?幕|里|时|一集)|那(?:时|里)|其他", re.IGNORECASE)


def points_at_screen(question: str) -> bool:
    """"Who is this?", "What is he doing?", "她是谁" — but not "what is this film about"."""
    return bool(_POINTING.search(_NOT_POINTING.sub(" ", question or "")))


@dataclasses.dataclass(frozen=True)
class Link:
    """A pending identity link a = b as the guard sees it for one question."""
    a: str
    b: str
    a_refs: Refs                 # any reference to a (loose)
    b_refs: Refs
    a_specific: Refs             # names, aliases, the label ("the wolf", "that grey wolf")
    b_specific: Refs
    focus: bool                  # the question is about a: b's specific references may not appear anywhere
    focus_b: bool                # the question is about b: a's specific references may not appear anywhere
    present: bool                # a is on screen and b is not


def identity_links(m, tapped=(), question: str = "", screen=()) -> list[Link]:
    """The links still pending at the Moment `m` (`Gate.pending`; rejected links never are). `tapped`: the tapped
    character(s); `screen`: the characters of the current shot; the question decides the rest (module docstring)."""
    chars = m.chars
    aliases: dict = {}
    for al in m.pack.get("character_aliases", []):
        aliases.setdefault(al["character_id"], []).append(al)
    pointed = not tapped and points_at_screen(question)
    out, seen = [], set()
    for lk in m.gate.pending:
        a, b = lk["a"], lk["b"]
        if (a, b) in seen or a not in chars or b not in chars:
            continue
        seen.add((a, b))
        a_spec, a_loose = _refs(m, a, aliases.get(a, []))
        b_spec, b_loose = _refs(m, b, aliases.get(b, []))
        shared = {i[1] for i in a_loose.items} & {i[1] for i in b_loose.items}    # e.g. both labels end in "man"
        a_spec, a_loose, b_spec, b_loose = (r.without(shared) for r in (a_spec, a_loose, b_spec, b_loose))
        qa, qb = a_loose.find(question) is not None, b_loose.find(question) is not None
        focus = _about(a in tapped, b in tapped, qa, qb, pointed and a in screen and b not in screen)
        focus_b = _about(b in tapped, a in tapped, qb, qa, pointed and b in screen and a not in screen)
        out.append(Link(a, b, a_loose, b_loose, a_spec, b_spec, focus, focus_b, a in screen and b not in screen))
    return out


def _about(x_tapped: bool, y_tapped: bool, x_asked: bool, y_asked: bool, x_pointed: bool) -> bool:
    """Is the question about side x of a link? The tap decides if it is on either side; else the question's words if
    they name exactly one side ("Is the wolf Bob?" names both: neither is barred, the per-passage rule still holds);
    else a pointing question ("who is this?") with only x on screen."""
    if x_tapped or y_tapped:
        return x_tapped
    if x_asked or y_asked:
        return x_asked and not y_asked
    return x_pointed


# ── passages ─────────────────────────────────────────────────────────

_CONTINUES = re.compile(r"^\W*(?:this|that)(?![A-Za-z])"
                        r"|^\W*(?:[A-Za-z'’-]+\W+){0,4}?(?:it|he|she|they|him|her|them|its|his|their)(?![A-Za-z])"
                        r"|^[\s\"'“‘「（(]*[这那]|^[^。！？]{0,5}?(?<!其)[它他她牠]", re.IGNORECASE)


def _sentences(text: str) -> list[str]:
    from server.answer import split_sentences
    return split_sentences(text)


def passages(text: str) -> list[str]:
    """Sentences, each joined with the ones after it that continue it with a pronoun (one among their first five
    words, or a leading "this / that"; in Chinese 它 / 他 / 她 among the first six characters or a leading 这 / 那)."""
    out: list[str] = []
    for s in _sentences(text):
        if out and _CONTINUES.search(s.strip()):
            out[-1] += " " + s
        else:
            out.append(s)
    return out


def _cooccurrence(links, text: str) -> list[dict]:
    hits, parts = [], None
    for lk in links:
        hit = (lk.focus and lk.b_specific.find(text)) or (lk.focus_b and lk.a_specific.find(text)) or None
        if hit is None:
            parts = passages(text) if parts is None else parts
            hit = next((b for p in parts if (b := lk.b_refs.find(p)) and lk.a_refs.find(p)), None)
        if hit:
            hits.append({"kind": "identity_cooccurrence", "term": hit})
    return hits


def source_violations(pack: dict, text: str, P: float, links, answer: str) -> list[dict]:
    """A source shown next to `answer` (evidence line, frame caption, highlight label): rules 1–4 on its own text, and
    rule 4 relative to the answer's subject — next to an answer about a (the question is about a, the answer refers to
    a, or a is on screen and the answer does not refer to b) no source may refer to b, and next to an answer about b no
    source may refer to a."""
    hits = violations(pack, text, P, links)
    for lk in links:
        about_b = lk.focus_b or lk.b_refs.find(answer) is not None
        about_a = lk.focus or lk.a_refs.find(answer) is not None or (lk.present and not about_b)
        if about_a and (t := lk.b_refs.find(text)):
            hits.append({"kind": "identity_cooccurrence", "term": t})
        if about_b and (t := lk.a_refs.find(text)):
            hits.append({"kind": "identity_cooccurrence", "term": t})
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
            hit = {"kind": f"canary:{k['kind']}", "term": k["term"]}
            if k["match"] == "regex":                       # the words actually written, for the retry note
                hit["match"] = term_search(k["term"], text, "regex", bool(k["case_sensitive"]))
            hits.append(hit)
    m = margins(pack)
    for _cid, term, known_at, cs in name_terms(pack):
        if not name_usable(known_at, P, m) and term_matches(term, text, "word", cs):
            hits.append({"kind": "name_not_yet_visible", "term": term})
    hits += _cooccurrence(links, text)
    seen, out = set(), []
    for h in hits:
        if h["term"] not in seen:
            seen.add(h["term"])
            out.append(h)
    return out
