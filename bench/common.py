"""Shared parts of the benchmark: case format and validation, term matching, and a read-only view of a master pack.

The pack view (`PackView`) is an independent re-implementation of the name / alias / canary rules (spec §6, §7) on
top of the pack tables; the online service's guard is NOT imported, so the benchmark cross-checks it rather than
repeating it. Eligibility of content items reuses `ingest.retrieval.Gate` with `allow_auto=True` (the same review
relaxation the demo service runs with); time conditions are unchanged.
"""
from __future__ import annotations

import functools
import json
import math
import os
import pathlib
import re
import sys

HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from ingest.textnorm import LATIN_WORD, has_cjk, normalize  # noqa: E402

CASES_DIR = HERE / "cases"
RUNS_DIR = pathlib.Path(os.environ.get("BENCH_RUNS_DIR") or HERE / "runs")
CATEGORIES = ("what_did_i_miss", "who_is_this", "what_is_that", "what_did_they_say", "explain_this_line", "free",
              "spoiler_probe", "injection", "followup")
EXPECT = ("answer", "decline", "either")
SETS = ("base", "new", "generated")       # generated: bench/gen_cases.py, never mixed into a run with the others
EPS = 1e-9
# what the viewer receives with an answer (bench/judge.py `channels`); a marker may name the ones it applies to
CHANNELS = ("answer", "evidence", "frame", "highlight", "tool_args", "tool_summary", "streamed", "history")
# Strings a public-repo leak scanner flags; case text must never contain them (assembled here so that this file
# itself does not trip the scanner).
FORBIDDEN = tuple(s.replace("|", "") for s in ("ev|al/", "real|_titles", "gol|den/"))
CJK_ANY = re.compile(r"[　-〿㐀-鿿豈-﫿＀-￯]")


# ── cases ────────────────────────────────────────────────────────────

def read_cases(paths=None, strict: bool = True) -> list[dict]:
    """Read `*.jsonl` case files (a directory or files). Each case gets `_file` (file stem) for grouping."""
    out = []
    for p in paths or [CASES_DIR]:
        p = pathlib.Path(p)
        files = sorted(p.glob("*.jsonl")) if p.is_dir() else [p]
        for f in files:
            for n, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
                if not line.strip():
                    continue
                c = json.loads(line)
                errs = validate(c)
                if errs and strict:
                    raise SystemExit(f"{f.name}:{n} {c.get('id')}: {'; '.join(errs)}")
                c["_file"] = f.stem
                out.append(c)
    ids = [c["id"] for c in out]
    dup = sorted({i for i in ids if ids.count(i) > 1})
    if dup:
        raise SystemExit(f"duplicate case ids: {dup}")
    return out


def case_set(c: dict) -> str:
    return c.get("set") or "base"


def _tap_ok(tap) -> bool:
    return isinstance(tap, dict) and all(isinstance(tap.get(k), (int, float)) and 0 <= tap[k] <= 1 for k in ("x", "y"))


def validate(c: dict) -> list[str]:
    """Schema-level problems of one case (no pack needed)."""
    errs = []
    for k in ("id", "content_id", "asset_sha256", "title", "P", "lang", "question", "category", "expect"):
        if k not in c:
            errs.append(f"missing {k}")
    if c.get("lang") not in ("en", "zh"):
        errs.append("lang must be en / zh")
    if c.get("category") not in CATEGORIES:
        errs.append(f"category {c.get('category')!r}")
    if c.get("expect") not in EXPECT:
        errs.append(f"expect {c.get('expect')!r}")
    if case_set(c) not in SETS:
        errs.append(f"set {c.get('set')!r}")
    if not isinstance(c.get("P"), (int, float)) or not math.isfinite(c["P"]) or c["P"] < 0:
        errs.append("P must be a finite number ≥ 0")
    if c.get("tap") is not None and not _tap_ok(c["tap"]):
        errs.append("tap must be {x, y} in 0–1")
    for i, h in enumerate(c.get("history") or []):
        if not isinstance(h, dict) or not h.get("question"):
            errs.append(f"history[{i}] needs a question")
            continue
        if not isinstance(h.get("P", c.get("P")), (int, float)) or h.get("P", c.get("P", 0)) > c.get("P", 0):
            errs.append(f"history[{i}].P must be ≤ P (a seek backwards starts a fresh conversation)")
        if h.get("tap") is not None and not _tap_ok(h["tap"]):
            errs.append(f"history[{i}].tap must be {{x, y}} in 0–1")
    for field in ("must_include_any", "must_not_include", "flag_if_any"):
        for m in c.get(field) or []:
            if isinstance(m, dict) and "regex" in m:
                try:
                    re.compile(m["regex"])
                except re.error as e:
                    errs.append(f"{field}: bad regex {m['regex']!r}: {e}")
            elif not (isinstance(m, str) and m) and not (isinstance(m, dict) and m.get("term")):
                errs.append(f"{field}: bad marker {m!r}")
            if isinstance(m, dict) and "link" in m and not (m["link"] is True or (isinstance(m["link"], str)
                                                                        and m["link"])):
                errs.append(f"{field}: link must be true or an identity link id")
            if isinstance(m, dict) and m.get("channels") is not None and not (
                    isinstance(m["channels"], list) and set(m["channels"]) <= set(CHANNELS)):
                errs.append(f"{field}: channels must be a list of {', '.join(CHANNELS)}")
    if not (c.get("reference") or c.get("must_include_any") or c.get("notes") or c.get("category") == "spoiler_probe"
            or c.get("expect") == "decline"):
        errs.append("needs reference, must_include_any or grading notes")
    return errs


# ── term matching (markers) ──────────────────────────────────────────
# Marker = "term" (whole word, case-folded) | {"term", "case_sensitive", "whole_word"} | {"regex", "ignore_case"}.
# Text is folded like the pack's own matching (NFKC, accents dropped, Traditional → Simplified, lower case), plus
# curly quotes → straight and runs of - _ whitespace → one space. Both only make matching more permissive.

_QUOTES = str.maketrans({"’": "'", "‘": "'", "ʼ": "'", "＇": "'", "“": '"', "”": '"'})
_COLLAPSE = re.compile(r"[-_\s]+")


@functools.lru_cache(maxsize=4096)                # the same eligible text of a long film is folded for every marker
def N(text: str, case_sensitive: bool = False) -> str:
    s = normalize((text or "").translate(_QUOTES), True)
    s = _COLLAPSE.sub(" ", s)
    return s if case_sensitive else s.lower()


_WORD_CH = re.compile(f"[{LATIN_WORD}]")


def _word(ch: str) -> bool:
    """The pack's own boundary rule (`ingest.textnorm.LATIN_WORD`): only ASCII letters / digits / underscore and
    Latin letters U+00C0–U+024F are word characters; CJK, other scripts, spaces and punctuation are boundaries."""
    return bool(ch) and _WORD_CH.match(ch) is not None


Span = tuple  # (folded text, start, end): where a term matched, for reports


def find_span(term: str, text: str, case_sensitive: bool = False, whole_word: bool = True,
              prefix: bool = False) -> Span | None:
    """First match of `term` in `text` after folding both (None: no match). Latin terms match whole words by the
    pack's boundary rule: a CJK character or a letter of another script is never a word character, so a Latin term
    right next to Chinese text still matches. CJK terms match as substrings. `_` is folded to a space first, so
    "x_Bob" matches here although the service's own matcher does not (the bench is the stricter one there)."""
    t, x = N(term, case_sensitive), N(text, case_sensitive)
    if not t:
        return None
    ww = whole_word and not has_cjk(t)
    start = 0
    while (i := x.find(t, start)) >= 0:
        before = x[i - 1] if i else ""
        after = x[i + len(t)] if i + len(t) < len(x) else ""
        if not ww or (not _word(before) and (prefix or not _word(after))):
            return x, i, i + len(t)
        start = i + 1
    return None


def find_term(term: str, text: str, case_sensitive: bool = False, whole_word: bool = True, prefix: bool = False) -> bool:
    return find_span(term, text, case_sensitive, whole_word, prefix) is not None


def _regex_span(pattern: str, text: str, flags: int) -> Span | None:
    x = N(text, True)
    m = re.search(pattern, x, flags)
    return (x, m.start(), m.end()) if m else None


def marker_span(marker, text: str) -> Span | None:
    m = {"term": marker} if isinstance(marker, str) else marker
    if "regex" in m:
        return _regex_span(m["regex"], text, re.IGNORECASE if m.get("ignore_case") else 0)
    return find_span(m["term"], text, bool(m.get("case_sensitive")), m.get("whole_word", True))


def marker_hits(marker, text: str) -> bool:
    return marker_span(marker, text) is not None


def marker_label(marker) -> str:
    m = {"term": marker} if isinstance(marker, str) else marker
    return m.get("term") or m.get("regex")


def hits(markers, text: str) -> list[str]:
    return [marker_label(m) for m in markers or [] if marker_hits(m, text)]


def canary_span(row: dict, text: str) -> Span | None:
    """Canary row (spec §7.2): match = word | phrase | regex; a word / phrase ending in `*` is a prefix match."""
    cs = bool(row.get("case_sensitive"))
    term = row["term"]
    if row.get("match") == "regex":
        return _regex_span(term, text, 0 if cs else re.IGNORECASE)
    if term.endswith("*") and len(term) > 1:
        return find_span(term[:-1], text, cs, True, prefix=True)
    return find_span(term, text, cs, True)


def canary_hits(row: dict, text: str) -> bool:
    return canary_span(row, text) is not None


def snippet(span: Span, pad: int = 24) -> str:
    """The matched part in brackets with some context, from the folded text: `…she [stabs] the…`."""
    x, i, j = span
    a, b = max(0, i - pad), min(len(x), j + pad)
    return ("…" if a else "") + x[a:i] + "[" + x[i:j] + "]" + x[j:b] + ("…" if b < len(x) else "")


def name_case_sensitive(name: str) -> bool:
    from ingest.derive import name_case_sensitive as ncs
    return bool(ncs(name))


# ── packs ────────────────────────────────────────────────────────────

def packs_dir() -> pathlib.Path | None:
    v = os.environ.get("BENCH_PACKS_DIR") or os.environ.get("WATCH_PACKS_DIR")
    return pathlib.Path(v).expanduser() if v else None


_PACKS: dict = {}


PACK_DISTS = ("master", "device-auto", "device")      # the first one present is used (private titles: device-auto)


def pack_path(sha: str, root: pathlib.Path | None = None) -> pathlib.Path | None:
    root = root or packs_dir()
    if root is None:
        return None
    return next((p for d in PACK_DISTS if (p := pathlib.Path(root) / sha / d / "pack.db").exists()), None)


def pack_for(sha: str, root: pathlib.Path | None = None) -> "PackView":
    """The pack `<root>/<sha>/<dist>/pack.db`, master first (root: `--packs`, else BENCH_PACKS_DIR / WATCH_PACKS_DIR).
    Raises SystemExit when there is none."""
    root = root or packs_dir()
    if root is None:
        raise SystemExit("no packs: pass --packs DIR or set BENCH_PACKS_DIR (layout <asset_sha>/master/pack.db)")
    key = (str(root), sha)
    if key not in _PACKS:
        db = pack_path(sha, root)
        if db is None:
            raise SystemExit(f"pack missing: {pathlib.Path(root) / sha}/{{{','.join(PACK_DISTS)}}}/pack.db")
        from ingest import packdb
        _PACKS[key] = PackView(packdb.read_db(db))
    return _PACKS[key]


class PackView:
    """Indexes over a pack dict plus the name-visibility rules at a given P (allow_auto review relaxation)."""

    def __init__(self, data: dict):
        from ingest.retrieval import margins
        self.data = data
        self.asset = data["asset"]
        self.m = margins(data)
        self.m_rev = self.m["gate_margin_reviewed_reveal_s"]
        self.chars = {c["character_id"]: c for c in data.get("characters", [])}
        self.aliases = data.get("character_aliases", [])
        self.links = data.get("identity_links", [])
        self.blocklist = data.get("blocklist", [])
        self.canaries = data.get("canaries", [])
        self.title_info = data.get("title_info", [])
        self.zh = self._zh_texts(data)

    @staticmethod
    def _zh_texts(d: dict) -> dict:
        """(item_type, item_id) → the Chinese text of a gate item (the gate itself carries the English text only)."""
        out = {}
        for r in d.get("scenes", []):
            out[("scene", r["scene_id"])] = r.get("summary_zh")
        for r in d.get("events", []):
            out[("event", r["event_id"])] = r.get("text_zh")
        for r in d.get("objects", []):
            out[("object", r["object_id"])] = ": ".join(x for x in (r.get("name_zh"), r.get("description_zh")) if x)
        for r in d.get("character_facts", []):
            out[("character_fact", r["fact_id"])] = r.get("text_zh")
        for r in d.get("shot_captions", []):
            out[("shot_caption", r["shot_caption_id"])] = r.get("caption_zh")
        for r in d.get("shot_entities", []):
            k = ("shot_caption", r["shot_caption_id"])
            extra = " ".join(x for x in (r.get("label_zh"), r.get("action_zh"), r.get("label_en"), r.get("action_en"))
                             if x)
            if extra:
                out[k] = ((out.get(k) or "") + "\n" + extra).strip()
        for r in d.get("recaps", []):
            out[("recap", r["recap_id"])] = r.get("text_zh")
        return {k: v for k, v in out.items() if v}

    @staticmethod
    def reviewed_ok(state) -> bool:
        return state not in ("flagged", "rejected")

    def name_visible(self, cid: str, P: float) -> bool:
        c = self.chars[cid]
        if c.get("name_public"):
            return True
        k = c.get("name_known_at_s")
        return k is not None and k + self.m_rev <= P + EPS

    def visible_name(self, cid: str, P: float, lang: str = "en") -> str:
        c = self.chars[cid]
        if self.name_visible(cid, P):
            return (c.get("name_zh") or c["canonical_name"]) if lang == "zh" else c["canonical_name"]
        return (c.get("display_label_pre_name_zh") or c["display_label_pre_name"]) if lang == "zh" \
            else c["display_label_pre_name"]

    def alias_known(self, a: dict, P: float) -> bool:
        return a.get("alias_known_at_s") is not None and a["alias_known_at_s"] + self.m_rev <= P + EPS

    def appeared(self, cid: str, P: float) -> bool:
        fa = self.chars[cid].get("first_appears_s")
        return fa is not None and fa <= P + EPS

    def name_variants(self, cid: str) -> list[str]:
        c = self.chars[cid]
        out = [c["canonical_name"]] + ([c["name_zh"]] if c.get("name_zh") else [])
        out += [k["term"] for k in self.canaries if k.get("ref_type") == "character" and k.get("ref_id") == cid
                and k.get("kind") == "name" and k.get("match") != "regex"]
        return list(dict.fromkeys(t for t in out if t))

    def public_names(self) -> list[str]:
        """Names that are `name_public` (spec §6: on the title page / synopsis): allowed at any P."""
        return list(dict.fromkeys(t for c in self.chars.values() if c.get("name_public")
                                  for t in (c.get("canonical_name"), c.get("name_zh")) if t))

    def hidden_terms(self, P: float) -> list[tuple[str, bool, str, str]]:
        """[(term, case_sensitive, character_id, kind)]: names not yet visible at P and aliases not yet known."""
        out = []
        for cid, c in self.chars.items():
            if self.name_visible(cid, P):
                continue
            labels = {N(c.get("display_label_pre_name") or ""), N(c.get("display_label_pre_name_zh") or "")}
            for term in self.name_variants(cid):
                if N(term) not in labels:            # unnamed characters: canonical_name may equal the label
                    out.append((term, name_case_sensitive(term), cid, "name"))
        for a in self.aliases:
            if not a.get("generic") and not self.alias_known(a, P):
                out.append((a["alias"], name_case_sensitive(a["alias"]), a["character_id"], "alias"))
        return out

    def active_canaries(self, P: float) -> list[dict]:
        return [k for k in self.canaries if P + EPS < k["active_until_s"]]

    def pending_links(self, P: float) -> list[dict]:
        return [lk for lk in self.links
                if not (self.reviewed_ok(lk.get("review_state")) and lk["reveal_at_s"] + self.m_rev <= P + EPS)]

    def known_names(self, P: float) -> list[str]:
        out = [self.visible_name(cid, P, "en") for cid in self.chars if self.appeared(cid, P)]
        out += [a["alias"] for a in self.aliases if self.alias_known(a, P)]
        return list(dict.fromkeys(out))

    def gate(self, P: float):
        from ingest import retrieval
        return retrieval.Gate(self.data, P, allow_auto=True)

    def seen_items(self, P: float) -> list[tuple[str, str]]:
        """[(label, text)] of what the viewer could have seen or heard by P, one entry per item and language: items
        eligible at P (cues only of English / Chinese tracks), character looks already shown, title info. Used to
        tell "a canary word that also describes something already shown" (that item is named) from a leak."""
        lang = {t["track"]: (t.get("lang") or "").lower() for t in self.data.get("subtitle_tracks", [])}
        track = {c["cue_id"]: c["track"] for c in self.data.get("cues", [])}
        out = []
        for it in self.gate(P).eligible():
            if it.item_type == "cue" and not lang.get(track.get(it.item_id), "").startswith(("en", "zh")):
                continue
            label = f"{it.item_type}@{mmss(it.start_s)}"
            out += [(label, t) for t in (it.text, self.zh.get((it.item_type, it.item_id))) if t]
        for lk in self.data.get("character_looks", []):
            if self.reviewed_ok(lk.get("review_state")) and lk.get("reveal_at_s") is not None \
                    and lk["reveal_at_s"] + self.m_rev <= P + EPS and lk.get("from_s", 0) <= P + EPS:
                out += [(f"look@{mmss(lk['from_s'] or 0)}", lk[f]) for f in ("description_en", "description_zh")
                        if lk.get(f)]
        if self.asset.get("story_start_s") is None or self.asset["story_start_s"] <= P + EPS:
            out += [("title_info", t[f]) for t in self.title_info for f in ("text_en", "text_zh") if t.get(f)]
        return out

    def eligible_text(self, P: float) -> str:
        """Every text eligible at P (allow_auto), English and Chinese, + character looks already shown + visible names
        of characters already on screen + title info."""
        items = self.gate(P).eligible()
        texts = [it.text for it in items if it.text] + [self.zh[k] for it in items
                                                        if (k := (it.item_type, it.item_id)) in self.zh]
        looks = [lk.get(f) or "" for lk in self.data.get("character_looks", [])
                 if self.reviewed_ok(lk.get("review_state")) and lk.get("reveal_at_s") is not None
                 and lk["reveal_at_s"] + self.m_rev <= P + EPS for f in ("description_en", "description_zh")]
        names = [self.visible_name(cid, P, lang) for cid in self.chars if self.appeared(cid, P) for lang in ("en", "zh")]
        titles = [t.get(f) or "" for t in self.title_info for f in ("text_en", "text_zh")]
        return "\n".join(texts + looks + names + titles)


def mmss(t: float) -> str:
    t = int(t)
    return f"{t // 60:02d}:{t % 60:02d}"


def write_jsonl(path, rows) -> None:
    path = pathlib.Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
    tmp.replace(path)


def read_jsonl(path) -> list[dict]:
    path = pathlib.Path(path)
    if not path.exists():
        return []
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]
