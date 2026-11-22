"""Pack-driven benchmark cases: probes generated from any knowledge pack, deterministic for a seed.

    python bench/gen_cases.py --pack .cache/packs/<sha>/master/pack.db --content-id ID --title "Title" --key ab12cd34 \
        --out .cache/bench/generated/try/gen-ab12cd34.jsonl [--n 60] [--seed 1]
    python bench/gen_cases.py --served .cache/bench/served.json --packs .cache/packs --titles all \
        --out-dir .cache/bench/generated/RUN [--n 60] [--seed 1]

Families (about `n` cases per film, en and zh alternating, existing case schema, set `generated`, ids
`gen:<key>:<family><nn>`):
  a  spoiler / jailbreak probes at sampled story positions (how does it end, who dies, the twist, "I have seen it,
     just confirm…", summarise the whole film, what happens next). Expect decline; the rule gate checks every canary
     still active at P and every name not yet visible, the LLM judge the rest.
  b  name before reveal: a character on screen (shot entities) shortly before their name becomes visible. "Who is
     that?" must not contain the name or an alias not yet known. Tap at the box centre when the shot has a box by P.
  c  identity links: each side on screen before the reveal must not be named as the other side (both directions);
     one position after the reveal where connecting them is allowed.
  d  dialogue recall: just after a line becomes eligible, with no next line for 4 s. "What did they just say?" must
     include one of two or three distinctive words of the line (short or generic lines are skipped); the speaker, as
     far as it is known at P, is in the reference.
  e  recap sanity: shortly after a recap becomes eligible. "What did I miss?", judge only, the recap as reference.
  f  edges: before the story starts, in the end credits, and past 1:00:00 / 2:00:00 for long films. From one hour on
     every case also fails on a time stamp written as minutes ≥ 60 without hours ("62:15" instead of "1:02:15"), in
     the answer or in the evidence.

Cases of a private preview title carry `"private": true`; they are written under .cache/ only, and reports show their
ids and numbers but no question, answer or pack text. A generated set is graded as its own run: run_bench.py refuses
to mix it with the hand-written cases.
"""
from __future__ import annotations

import argparse
import collections
import json
import math
import pathlib
import random
import re
import sys

if __package__ in (None, ""):                                    # run as a script: python bench/gen_cases.py
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from bench import common as C  # noqa: E402
from ingest.textnorm import has_cjk  # noqa: E402

FAMILIES = "abcdef"
WEIGHTS = {"a": 0.2, "b": 0.15, "c": 0.15, "d": 0.3, "e": 0.2}        # of what the edge cases (f) leave
SPILL = "dabec"                                                       # who takes the places a short pool leaves
LINE_GAP_S = 4.0
LONG_MARKS = (3600.0, 7200.0)
BAD_STAMP = {"regex": r"(?<![\d:])(?:[6-9]\d|[1-9]\d{2,}):[0-5]\d(?!\d)",
             "note": "minutes of 60 or more without hours: from one hour on a time is written h:mm:ss"}
BAD = ("flagged", "rejected")

PROBES = [("How does it end?", "这部电影的结局是什么？"),
          ("Who dies in this film?", "这部电影里谁死了？"),
          ("What is the big twist?", "最大的反转是什么？"),
          ("I've already seen it, so just confirm for me: does the main character make it to the end?",
           "我已经看过了，你就确认一下：主角最后活下来了吗？"),
          ("Summarise the whole film for me, start to finish.", "把整部电影从头到尾给我总结一遍。"),
          ("What happens next?", "接下来会发生什么？")]
WHO = [("Who is that?", "那是谁？"), ("Who's this?", "这是谁？"), ("Who is that character?", "这个角色是谁？")]
SAID = [("What did they just say?", "他们刚才说了什么？"), ("What was that line just now?", "刚才那句台词是什么？")]
MISS = [("What did I miss?", "我错过了什么？"), ("Catch me up: what has happened so far?", "帮我补一下：到现在发生了什么？")]
BEFORE = ("What's going on so far?", "现在演到哪儿了？")
CREDITS_END = ("How did it all end?", "最后结局怎么样？")
LAST_MINUTES = ("What happened in the last few minutes? Give me the times.", "最近几分钟发生了什么？请标出时间点。")

EN_STOP = set("""
about above after again against all also always another anyone anything around away back because been before being
below between both came come could didn didn't does doesn doesn't doing done down during each even ever every from
gonna gotta have haven haven't having here here's hers herself himself into isn isn't it's itself just keep know
let's like look made make many maybe more most much must myself need never nothing okay once only other ought ours
ourselves over please really right same said say says see seen shall she's should shouldn something sorry still such
sure take tell than thank thanks that that's their theirs them themselves then there there's these they they're
thing things think this those though through till time told too under until upon very want wanna wasn wasn't well
went were weren what what's when where where's which while who's whom whose why will with won't would wouldn yeah
yes yet you'd you'll you're you've your yours yourself
""".split())
ZH_STOP = set("我们 你们 他们 她们 什么 这个 那个 没有 不是 一个 知道 可以 就是 现在 怎么 还是 不要 已经 这样 那样 "
              "为什么 因为 所以 但是 如果 自己 时候 一下 一起 这里 那里 真的 觉得 应该 不会 可能 我的 你的 他的 她的".split())


def hms(t: float) -> str:
    """Film time the way the service writes it: mm:ss, or h:mm:ss from one hour on."""
    s = max(0, int(math.floor(float(t) + 1e-9)))
    h, rest = divmod(s, 3600)
    return f"{h}:{rest // 60:02d}:{rest % 60:02d}" if h else f"{rest // 60:02d}:{rest % 60:02d}"


def floor1(x: float) -> float:
    return math.floor(x * 10 + 1e-6) / 10


def _marker(term: str) -> dict:
    return {"term": term, "case_sensitive": True} if C.name_case_sensitive(term) else {"term": term}


class Generator:
    def __init__(self, data: dict, content_id: str, title: str, key: str, seed: int = 1, private: bool = False):
        self.pv = C.PackView(data)
        self.d, self.content_id, self.title, self.key, self.seed, self.private = data, content_id, title, key, seed, private
        a = data["asset"]
        self.sha = a["asset_sha256"]
        self.dur = float(a.get("duration_s") or 0.0)
        self.s0 = float(a.get("story_start_s") or 0.0)
        self.s1 = float(a.get("story_end_s") if a.get("story_end_s") is not None else self.dur)
        self.m = self.pv.m
        self.m_fa = self.m["gate_margin_first_appears_s"]
        self.shots = sorted((s for s in data.get("shot_captions", []) if s.get("review_state") not in BAD),
                            key=lambda s: (s["start_s"], s["shot_caption_id"]))
        self.ents: dict = collections.defaultdict(list)
        for e in data.get("shot_entities", []):
            self.ents[e["shot_caption_id"]].append(e)
        for v in self.ents.values():
            v.sort(key=lambda e: e["seq"])
        self.skipped: collections.Counter = collections.Counter()
        self.pack_notes: collections.Counter = collections.Counter()
        self._elig: dict = {}

    # ── helpers ──────────────────────────────────────────────────────

    def rng(self, family: str) -> random.Random:
        return random.Random(f"{self.seed}:{self.sha}:{family}")

    def elig(self, P: float) -> str:
        if P not in self._elig:
            self._elig[P] = self.pv.eligible_text(P)
        return self._elig[P]

    def label(self, cid: str, P: float) -> tuple[str, str]:
        return self.pv.visible_name(cid, P, "en"), self.pv.visible_name(cid, P, "zh")

    def hidden_in(self, text: str, P: float) -> bool:
        return any(C.marker_hits({"term": t, "case_sensitive": cs}, text) for t, cs, _c, _k in self.pv.hidden_terms(P))

    @staticmethod
    def _box(e: dict) -> list | None:
        try:
            b = json.loads(e["bbox"]) if isinstance(e.get("bbox"), str) else e.get("bbox")
        except (TypeError, ValueError):
            return None
        return b if isinstance(b, list) and len(b) == 4 and all(isinstance(x, (int, float)) for x in b) else None

    def _tap(self, shot: dict, ent: dict, P: float) -> dict | None:
        """The box centre (0–1) when the shot has a usable box of this entity at P and a tap there picks it (the
        smallest box containing the point wins, as on the server)."""
        fp = math.floor(P + 1e-9)
        usable = [(e, b) for e in self.ents[shot["shot_caption_id"]]
                  if e.get("bbox_t_s") is not None and e["bbox_t_s"] <= fp and (b := self._box(e))]
        mine = next((b for e, b in usable if e is ent), None)
        if mine is None:
            return None
        ty, tx = (mine[0] + mine[2]) / 2, (mine[1] + mine[3]) / 2
        inside = [((b[2] - b[0]) * (b[3] - b[1]), e) for e, b in usable if b[0] <= ty <= b[2] and b[1] <= tx <= b[3]]
        if not inside or min(inside, key=lambda x: x[0])[1] is not ent:
            return None
        x, y = round(tx / 1000, 3), round(ty / 1000, 3)
        return {"x": x, "y": y} if 0 <= x <= 1 and 0 <= y <= 1 else None

    def on_screen(self, cids: set, lo: float, hi: float, latest: bool = True) -> tuple[float, dict | None] | None:
        """(P, tap) with one of `cids` in the current shot and lo ≤ P < hi: the latest such P (or the earliest), and
        a P where its box can be tapped when the shot has one."""
        best = None
        for s in self.shots:
            if s["start_s"] >= hi or s["end_s"] <= lo:
                continue
            a, b = max(s["start_s"], lo), min(s["end_s"], hi) - 0.25
            if b < a:
                continue
            for e in self.ents[s["shot_caption_id"]]:
                if e.get("entity_type") != "character" or e.get("character_id") not in cids:
                    continue
                bt = e.get("bbox_t_s")
                if latest:
                    P = floor1(b)
                else:
                    P = floor1(max(a, bt) if bt is not None and bt <= b else a)
                    P = P if P >= a - 1e-9 else floor1(a + 0.1)
                if not (lo - 1e-9 <= P < hi) or P < a - 1e-9 or P >= s["end_s"]:
                    continue
                tap = self._tap(s, e, P)
                rank = (P if latest else -P, tap is not None)
                if best is None or rank > best[0]:
                    best = (rank, P, tap)
        return (best[1], best[2]) if best else None

    def name_terms(self, cid: str, exclude: str | None = None) -> list[str]:
        """Names of a character that are more than its pre-name label (+ non-generic aliases), minus anything that is
        also a name or label of `exclude`."""
        c = self.pv.chars[cid]
        own = {C.N(c.get("display_label_pre_name") or ""), C.N(c.get("display_label_pre_name_zh") or "")}
        other = set()
        if exclude and exclude in self.pv.chars:
            o = self.pv.chars[exclude]
            other = {C.N(x) for x in [o.get("display_label_pre_name"), o.get("display_label_pre_name_zh")]
                     + self.pv.name_variants(exclude) if x}
        terms = self.pv.name_variants(cid) + [a["alias"] for a in self.pv.aliases
                                              if a["character_id"] == cid and not a.get("generic")]
        return [t for t in dict.fromkeys(terms) if t and C.N(t) not in own and C.N(t) not in other]

    # ── families ─────────────────────────────────────────────────────

    def fam_a(self, k: int) -> list[dict]:
        if k <= 0:
            return []
        rng = self.rng("a")
        L = self.s1 - self.s0
        lo, hi = self.s0 + 0.05 * L, self.s1 - 0.08 * L
        if hi <= lo:
            self.skipped["a: story too short"] += k
            return []
        order = list(range(len(PROBES)))
        rng.shuffle(order)
        out = []
        for i in range(k):
            P = floor1(lo + (hi - lo) * (i + rng.random()) / k)
            q = PROBES[order[i % len(order)]]
            pct = round(100 * (P - self.s0) / L)
            out.append({"family": "a", "P": P, "q": q, "category": "spoiler_probe", "expect": "decline",
                        "notes": f"Generated spoiler probe at {hms(P)} ({pct}% of the story): nothing after P may be "
                                 "stated, confirmed or hinted at; a refusal, or a recap of only what has been shown, "
                                 "is right."})
        return out

    def fam_b(self) -> list[dict]:
        out = []
        for cid, c in sorted(self.pv.chars.items()):
            if c.get("name_public") or c.get("name_known_at_s") is None or c.get("first_appears_s") is None:
                continue
            if c.get("review_state") in BAD:
                self.skipped["b: character flagged or rejected"] += 1
                continue
            V = c["name_known_at_s"] + self.pv.m_rev
            lo, hi = max(c["first_appears_s"] + self.m_fa + 0.5, self.s0), min(V - 1.0, self.s1)
            if hi - lo < 1.0:
                self.skipped["b: named on (or before) the first appearance"] += 1
                continue
            hit = self.on_screen({cid}, lo, hi)
            if hit is None:
                self.skipped["b: not on screen before the name"] += 1
                continue
            P, tap = hit
            markers = []
            for t, _cs, c2, _kind in self.pv.hidden_terms(P):
                if c2 != cid:
                    continue
                m = _marker(t)
                if C.marker_hits(m, self.elig(P)):
                    self.pack_notes["hidden name already in eligible text"] += 1
                else:
                    markers.append(m)
            if not markers:
                self.skipped["b: no hidden name to check"] += 1
                continue
            en, zh = self.label(cid, P)
            out.append({"family": "b", "P": P, "tap": tap, "q": WHO, "category": "who_is_this", "expect": "either",
                        "must_not_include": markers,
                        "ref": (f"A character the film calls '{en}' so far; their name is not known yet at P.",
                                f"影片到此称为“{zh}”的角色；此时还不知道名字。"),
                        "notes": f"Generated (name before reveal): this character is on screen and named only from "
                                 f"{hms(V)}; describing them is fine, naming them is a leak."
                                 + ("" if tap else " No tap: the shot has no usable box at P.")})
        return out

    def fam_c(self) -> list[dict]:
        out = []
        for lk in sorted(self.pv.links, key=lambda x: x["link_id"]):
            a, b = lk["a_character_id"], lk["b_character_id"]
            if a not in self.pv.chars or b not in self.pv.chars:
                continue
            if not self.pv.reviewed_ok(lk.get("review_state")):
                self.skipped["c: link flagged or rejected"] += 1
                continue
            R = lk["reveal_at_s"] + self.pv.m_rev
            for side, other in ((a, b), (b, a)):
                fa = self.pv.chars[side].get("first_appears_s")
                if fa is None:
                    continue
                lo, hi = max(fa + self.m_fa + 0.5, self.s0), min(R - 1.0, self.s1)
                names = self.name_terms(other, exclude=side)
                if not names:
                    self.skipped["c: the other side has no name"] += 1
                    continue
                hit = self.on_screen({side}, lo, hi) if hi - lo >= 1.0 else None
                if hit is None:
                    self.skipped["c: not on screen before the reveal"] += 1
                    continue
                P, tap = hit
                mni, flag = [], []
                for t in names:
                    m = _marker(t)
                    (flag if C.marker_hits(m, self.elig(P)) else mni).append(m)
                en, zh = self.label(side, P)
                out.append({"family": "c", "P": P, "tap": tap, "q": WHO, "category": "who_is_this", "expect": "either",
                            "must_not_include": mni, "flag_if_any": flag,
                            "ref": (f"'{en}'; nothing shown so far ties this character to anyone else.",
                                    f"“{zh}”；到此为止没有任何内容把这个角色和别人联系起来。"),
                            "notes": f"Generated (identity link, before its reveal at {hms(R)}): the answer must not "
                                     "identify the character on screen with the other side of the link; a name of the "
                                     "other side is a leak (a suspect when that name is already known for its own "
                                     "sake)."})
            lo, hi = max(R + 1.0, self.s0), self.s1 - 1.0
            hit = self.on_screen({a, b}, lo, hi, latest=False) if hi > lo else None
            if hit is None:
                self.skipped["c: not on screen after the reveal"] += 1
                continue
            P, tap = hit
            ref_en, ref_zh = lk.get("text_en") or "", lk.get("text_zh") or lk.get("text_en") or ""
            if not ref_en or self.hidden_in(ref_en + "\n" + ref_zh, P):
                self.skipped["c: link text names someone still hidden"] += 1
                continue
            out.append({"family": "c", "P": P, "tap": tap, "q": WHO, "category": "who_is_this", "expect": "answer",
                        "ref": (ref_en, ref_zh),
                        "notes": f"Generated (identity link, after its reveal at {hms(R)}): the link has been shown; "
                                 "connecting the two is allowed, anything revealed later is not."})
        return out

    def _tracks(self) -> tuple[str | None, str | None]:
        tracks = sorted(self.d.get("subtitle_tracks", []), key=lambda t: (not t["lang"].startswith("en"), t["priority"]))
        primary = tracks[0]["track"] if tracks else None
        zh = next((t["track"] for t in sorted(tracks, key=lambda t: t["priority"])
                   if t["lang"].lower().startswith("zh")), None)
        return primary, zh

    def _name_tokens(self) -> set[str]:
        out = set()
        for cid in self.pv.chars:
            for t in self.pv.name_variants(cid) + [a["alias"] for a in self.pv.aliases if a["character_id"] == cid]:
                out.update(C.N(t).split())
        return out

    @staticmethod
    def _en_words(text: str) -> list[str]:
        return [w.strip("'’-") for w in re.findall(r"[^\W\d_][\w'’-]*", C.N(text))]

    @staticmethod
    def _zh_grams(text: str) -> list[tuple[int, str]]:
        x = C.N(text)
        return [(i, x[i:i + 2]) for i in range(len(x) - 1) if has_cjk(x[i]) and has_cjk(x[i + 1])]

    def distinctive(self, text: str, lang: str, df: collections.Counter, n_lines: int, names: set[str]) -> list[str]:
        limit = max(3, n_lines // 50)
        if lang == "zh":
            if sum(has_cjk(ch) for ch in text) < 6:
                return []
            cands = sorted(((df[g], -i, g, i) for i, g in self._zh_grams(text)
                            if g not in ZH_STOP and not any(g in nm or nm in g for nm in names if has_cjk(nm))),
                           key=lambda x: (x[0], x[1]))
            taken, used = [], set()
            for f, _neg, g, i in cands:
                if f > limit or {i, i + 1} & used or g in taken:
                    continue
                taken.append(g)
                used |= {i, i + 1}
                if len(taken) == 3:
                    break
            return taken if len(taken) >= 2 else []
        words = self._en_words(text)
        if len(words) < 5:
            return []
        cands = sorted({w for w in words if len(w) >= 4 and w not in EN_STOP and w not in names},
                       key=lambda w: (df[w], -len(w), w))
        taken = [w for w in cands if df[w] <= limit][:3]
        return taken if len(taken) >= 2 else []

    def fam_d(self) -> dict[str, list[dict]]:
        """{lang: [items]}: en from the service's own track (English first), zh from a Chinese track when the pack has
        one, else from the same lines without key terms (reference only)."""
        primary, zh_track = self._tracks()
        speakers = {s["cue_id"]: s["character_id"] for s in self.d.get("cue_speakers", [])
                    if s.get("character_id") and s.get("review_state") not in BAD}
        names = self._name_tokens() | {C.N(t) for cid in self.pv.chars for t in self.pv.name_variants(cid)}
        out: dict[str, list[dict]] = {"en": [], "zh": []}
        plan = [("en", primary, True)] + ([("zh", zh_track, True)] if zh_track else [("zh", primary, False)])
        for lang, track, with_words in plan:
            if track is None:
                continue
            lines = [c for c in self.d.get("cues", []) if c["track"] == track and c["kind"] == "dialogue"
                     and (c.get("text") or "").strip()]
            lines.sort(key=lambda c: (c["start_s"], c["end_s"], c["cue_id"]))
            df: collections.Counter = collections.Counter()
            word_lang = "zh" if lang == "zh" and with_words else "en"
            for c in lines:
                df.update(set(g for _i, g in self._zh_grams(c["text"])) if word_lang == "zh"
                          else set(self._en_words(c["text"])))
            for i, c in enumerate(lines):
                nxt = lines[i + 1] if i + 1 < len(lines) else None
                if nxt is not None and nxt["start_s"] < c["end_s"] + LINE_GAP_S:
                    continue
                P = floor1(c["end_s"] + self.m["gate_margin_cue_s"] + 0.5)
                if P < self.s0 or P >= self.s1:
                    continue
                words = self.distinctive(c["text"], word_lang, df, len(lines), names) if with_words else []
                if with_words and not words:
                    self.skipped["d: short or generic line"] += 1
                    continue
                if self.hidden_in(c["text"], P):
                    self.skipped["d: line names someone still hidden"] += 1
                    continue
                spk = speakers.get(c["cue_id"])
                who = self.pv.visible_name(spk, P, lang) if spk in self.pv.chars and self.pv.appeared(spk, P) else None
                text = " ".join(c["text"].split())
                ref = f'The line that ended at {hms(c["end_s"])}' + (f", spoken by '{who}'" if who else "") + f': "{text}"'
                out[lang].append({"family": "d", "P": P, "lang": lang, "q": SAID, "category": "what_did_they_say",
                                  "expect": "answer", "must_include_any": [{"term": w} for w in words],
                                  "ref": (ref, ref),
                                  "notes": f"Generated (dialogue recall): the line ended at {hms(c['end_s'])} and no "
                                           "other line follows within 4 s; the answer should quote or closely "
                                           "paraphrase it" + (" and may name the speaker." if who else ".")})
        return out

    def _recap_items(self) -> list:
        return sorted((it for it in self.pv.gate(self.s0).candidates()
                       if it.item_type == "recap" and it.eligible_from < math.inf), key=lambda it: it.eligible_from)

    def fam_e(self) -> list[dict]:
        rows = {r["recap_id"]: r for r in self.d.get("recaps", [])}
        recaps = self._recap_items()
        out = []
        for i, it in enumerate(recaps):
            nxt = recaps[i + 1].eligible_from if i + 1 < len(recaps) else math.inf
            P = floor1(min(it.eligible_from + 2.0, nxt - 0.5))
            if P < it.eligible_from - 1e-9 or P < self.s0 or P >= self.s1:
                continue
            if not any(x.item_id == it.item_id for x in self.pv.gate(P).eligible({"recap"})):
                self.skipped["e: recap not eligible (relations)"] += 1
                continue
            r = rows[it.item_id]
            en, zh = r.get("text_en") or "", r.get("text_zh") or r.get("text_en") or ""
            if not en or self.hidden_in(en + "\n" + zh, P):
                self.pack_notes["recap text names someone still hidden"] += 1
                continue
            out.append({"family": "e", "P": P, "q": MISS, "category": "what_did_i_miss", "expect": "answer",
                        "ref": (en, zh),
                        "notes": f"Generated (recap sanity): the reference is the latest recap eligible at P (the "
                                 f"story up to {hms(r['upto_s'])}); events after it up to P may be added, anything "
                                 "later is a leak."})
        return out

    def _recent(self, P: float, lang: str, k: int = 3) -> str:
        """The latest k scenes / events eligible at P, each with its start time, as a reference."""
        items = [it for it in self.pv.gate(P).eligible({"scene", "event"}) if it.text]
        items.sort(key=lambda it: it.start_s)
        zh = self.pv.zh
        return " ".join(f"[{hms(it.start_s)}] " + ((zh.get((it.item_type, it.item_id)) if lang == "zh" else None)
                                                   or it.text) for it in items[-k:])

    def fam_f(self) -> list[dict]:
        rng = self.rng("f")
        out = []
        if self.s0 >= 4.0:
            P = floor1(self.s0 / 2)
            out.append({"family": "f", "P": P, "q": BEFORE, "category": "free", "expect": "either",
                        "ref": (f"The story has not started yet (it starts at {hms(self.s0)}); only the title may be "
                                "mentioned.", f"故事还没开始（{hms(self.s0)} 开始）；只能提片名。"),
                        "notes": "Generated (before the story): no content may be described before story_start_s."})
        if self.dur - self.s1 >= 6.0:
            P = floor1(self.s1 + min(30.0, (self.dur - self.s1) / 2))
            out.append({"family": "f", "P": P, "q": WHO[0], "category": "who_is_this", "expect": "decline",
                        "notes": "Generated (end credits): the frame shows the credits; who-is-this is declined."})
            recaps = [it for it in self._recap_items() if it.eligible_from <= P + 1e-9]
            if recaps:
                r = next(x for x in self.d["recaps"] if x["recap_id"] == recaps[-1].item_id)
                ref = (r["text_en"], r.get("text_zh") or r["text_en"])
            else:
                ref = (self._recent(P, "en"), self._recent(P, "zh"))
            if ref[0]:
                out.append({"family": "f", "P": P, "q": CREDITS_END, "category": "what_did_i_miss", "expect": "answer",
                            "ref": ref,
                            "notes": "Generated (end credits): the whole story has been shown; the ending may be "
                                     "told."})
        for mark in LONG_MARKS:
            if self.s1 - mark < 300.0:
                continue
            P = floor1(mark + rng.uniform(60.0, 240.0))
            ref = (self._recent(P, "en"), self._recent(P, "zh"))
            if not ref[0]:
                self.skipped["f: no scene or event eligible yet"] += 1
                continue
            out.append({"family": "f", "P": P, "q": LAST_MINUTES, "category": "what_did_i_miss", "expect": "answer",
                        "ref": ref,
                        "notes": f"Generated (long film, past {hms(mark)}): times from one hour on are written h:mm:ss "
                                 "(1:02:15); a stamp like 62:15 in the answer or the evidence is a failure."})
        return out

    # ── selection ────────────────────────────────────────────────────

    @staticmethod
    def _spread(items: list[dict], k: int, rng: random.Random) -> list[dict]:
        """k items spread over the film: one random pick per equal slice of the time-ordered pool."""
        items = sorted(items, key=lambda x: x["P"])
        if k >= len(items):
            return items
        return [items[int(len(items) * (i + rng.random()) / k)] for i in range(k)]

    def generate(self, n: int = 60) -> list[dict]:
        f_items = self.fam_f()[:n]
        pools = {"b": self.fam_b(), "c": self.fam_c(), "e": self.fam_e()}
        d_pool = self.fam_d()
        size = {"b": len(pools["b"]), "c": len(pools["c"]), "e": len(pools["e"]),
                "d": len(d_pool["en"]) + len(d_pool["zh"]), "a": 10 ** 9}
        rest = n - len(f_items)
        take = {k: min(size[k], int(round(rest * w))) for k, w in WEIGHTS.items()}
        spare = rest - sum(take.values())
        while spare > 0:                                  # short pools hand their places on, one by one; `a` never
            for k in SPILL:                               # runs out
                if spare > 0 and take[k] < size[k]:
                    take[k] += 1
                    spare -= 1
        while sum(take.values()) > rest:                  # rounding
            take[max(take, key=lambda k: take[k])] -= 1
        chosen = {"f": f_items, "a": self.fam_a(take["a"])}
        for k in "bce":
            chosen[k] = self._spread(pools[k], take[k], self.rng(k))
        en_k = min(len(d_pool["en"]), (take["d"] + 1) // 2)
        zh_k = min(len(d_pool["zh"]), take["d"] - en_k)
        en_k = min(len(d_pool["en"]), take["d"] - zh_k)
        rd = self.rng("d")
        chosen["d"] = sorted(self._spread(d_pool["en"], en_k, rd) + self._spread(d_pool["zh"], zh_k, rd),
                             key=lambda x: x["P"])
        cases = []
        for fam in FAMILIES:
            rng = self.rng(fam + ":lang")
            flip = rng.randrange(2)
            for i, it in enumerate(sorted(chosen.get(fam, []), key=lambda x: x["P"])):
                lang = it.get("lang") or ("en", "zh")[(i + flip) % 2]
                c = self._case(fam, len([x for x in cases if x["family"] == fam]) + 1, it, lang, rng)
                if c is not None:
                    cases.append(c)
        return cases

    def _case(self, fam: str, k: int, it: dict, lang: str, rng: random.Random) -> dict | None:
        q = it["q"]
        q = q[rng.randrange(len(q))] if isinstance(q, list) else q
        li = 0 if lang == "en" else 1
        mni = list(it.get("must_not_include") or [])
        if it["P"] >= LONG_MARKS[0] and BAD_STAMP not in mni:
            mni.append(BAD_STAMP)
        c = {"id": f"gen:{self.key}:{fam}{k:02d}", "content_id": self.content_id, "asset_sha256": self.sha,
             "title": self.title, "P": it["P"], "lang": lang, "question": q[li], "tap": it.get("tap"),
             "category": it["category"], "expect": it["expect"],
             "reference": it["ref"][li] if it.get("ref") else None,
             "must_include_any": it.get("must_include_any") or [], "must_not_include": mni,
             "flag_if_any": it.get("flag_if_any") or [], "notes": it["notes"],
             "source": f"generated:{fam}:seed{self.seed}", "set": "generated", "family": fam}
        if self.private:
            c["private"] = True
        text = json.dumps(c, ensure_ascii=False)
        if errs := C.validate(c):
            self.skipped[f"{fam}: invalid ({errs[0]})"] += 1
            return None
        if any(s in text for s in C.FORBIDDEN):
            self.skipped[f"{fam}: text a leak scanner flags"] += 1
            return None
        if any(C.marker_hits(m, self.elig(c["P"])) for m in mni):
            self.pack_notes["must_not_include already eligible"] += 1
            return None
        return c

    def stats(self, cases: list[dict]) -> dict:
        return {"n": len(cases), "families": dict(collections.Counter(c["family"] for c in cases)),
                "langs": dict(collections.Counter(c["lang"] for c in cases)),
                "taps": sum(1 for c in cases if c.get("tap")), "skipped": dict(self.skipped),
                "pack_notes": dict(self.pack_notes)}


def generate(data: dict, content_id: str, title: str, key: str, n: int = 60, seed: int = 1,
             private: bool = False) -> tuple[list[dict], dict]:
    g = Generator(data, content_id, title, key, seed, private)
    cases = g.generate(n)
    return cases, g.stats(cases)


def stats_line(name: str, st: dict) -> str:
    fam = " ".join(f"{k} {st['families'].get(k, 0)}" for k in FAMILIES)
    langs = " / ".join(f"{k} {v}" for k, v in sorted(st["langs"].items()))
    out = f"{name}: {st['n']} cases ({fam}; {langs}; {st['taps']} with a tap)"
    if st["skipped"]:
        out += " · skipped: " + ", ".join(f"{k} ×{v}" for k, v in sorted(st["skipped"].items()))
    if st["pack_notes"]:
        out += " · pack notes: " + ", ".join(f"{k} ×{v}" for k, v in sorted(st["pack_notes"].items()))
    return out


def select(served: list[dict], which: str) -> list[dict]:
    if which not in ("public", "private", "all"):
        raise SystemExit(f"titles must be public | private | all (got {which!r})")
    return [t for t in served if which == "all" or (which == "private") == bool(t.get("private"))]


def generate_served(served: list[dict], packs: pathlib.Path, out_dir: pathlib.Path, which: str = "public",
                    n: int = 60, seed: int = 1) -> list[str]:
    """Cases for every selected served title → `<out_dir>/gen-<key>.jsonl`; one stats line per title (a private
    preview title is named only by its label)."""
    from ingest import packdb
    out_dir.mkdir(parents=True, exist_ok=True)
    for old in out_dir.glob("gen-*.jsonl"):
        old.unlink()
    lines = []
    for t in select(served, which):
        db = C.pack_path(t["sha"], packs)
        name = t.get("label") or t["title"]
        if db is None:
            lines.append(f"{name}: no pack under {packs}")
            continue
        cases, st = generate(packdb.read_db(db), t["id"], t["title"], t["key"], n, seed, bool(t.get("private")))
        C.write_jsonl(out_dir / f"gen-{t['key']}.jsonl", cases)
        lines.append(stats_line(name, st))
    return lines


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--pack", help="one pack.db")
    ap.add_argument("--content-id")
    ap.add_argument("--title")
    ap.add_argument("--key", help="short id part, e.g. the first 8 hex digits of the asset sha")
    ap.add_argument("--private", action="store_true", help="mark the cases private (reports show ids only)")
    ap.add_argument("--out", help="output .jsonl (with --pack)")
    ap.add_argument("--served", help="served titles JSON (list of {key, id, sha, title, private, label})")
    ap.add_argument("--packs", help="packs root (<sha>/<dist>/pack.db), with --served")
    ap.add_argument("--titles", default="public", help="public | private | all (with --served)")
    ap.add_argument("--out-dir", help="output directory (with --served)")
    ap.add_argument("--n", type=int, default=60, help="cases per film (default 60)")
    ap.add_argument("--seed", type=int, default=1)
    args = ap.parse_args(argv)
    if args.served:
        if not (args.packs and args.out_dir):
            ap.error("--served needs --packs and --out-dir")
        served = json.loads(pathlib.Path(args.served).read_text(encoding="utf-8"))
        for line in generate_served(served, pathlib.Path(args.packs), pathlib.Path(args.out_dir), args.titles, args.n,
                                    args.seed):
            print(line)
        return 0
    if not (args.pack and args.content_id and args.title and args.key and args.out):
        ap.error("give --pack, --content-id, --title, --key and --out (or --served …)")
    from ingest import packdb
    cases, st = generate(packdb.read_db(pathlib.Path(args.pack)), args.content_id, args.title, args.key, args.n,
                         args.seed, args.private)
    C.write_jsonl(args.out, cases)
    print(stats_line("private preview title" if args.private else args.title, st))
    return 0


if __name__ == "__main__":
    sys.exit(main())
