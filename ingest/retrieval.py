"""Gate-first, rank-second retrieval (for the web demo backend): pack.db + locked progress P + query → top-k eligible items.

The order is a hard constraint: first compute the set of items eligible at P per spec §5.2 / §5.3 / §5.4, then rank
only within that set. Vectors are read only for eligible rows (temp-table JOIN); full-text search runs on an in-memory
FTS5 table holding only eligible text (so bm25 IDF never sees future items). Never search the whole film and filter
afterwards: the ranking itself can leak future content.

The gate is an independent implementation of the spec; other gate implementations (evaluation, online service) are
cross-checked item by item against the same rules. v1.1 tables: `shot_captions` / `recaps` need
prereviewed+ and `reveal_at_s + gate_margin_scene_snapped_s ≤ P`; `video_windows` need
`end_s + gate_margin_scene_snapped_s ≤ P` (same rule as media segments).

`allow_auto=True` (demo / dev only): review requirement relaxed to "not flagged / rejected" (auto and prereviewed count as reviewed); time conditions unchanged.
"""
from __future__ import annotations

import dataclasses
import functools
import math
import os
import pathlib
import re
import sqlite3

import numpy as np

from ingest import embed, packdb
from ingest.derive import MARGINS

EPS = 1e-9
PRE = {"prereviewed", "reviewed"}
BAD = {"flagged", "rejected"}
RRF_K = 60


@dataclasses.dataclass
class Item:
    item_type: str
    item_id: str
    start_s: float
    end_s: float
    eligible_from: float        # earliest eligible P by time and review state (margin included)
    text: str | None            # video windows have no text
    span: tuple[float, float] | None = None   # interval identity isolation is judged over (default start_s–end_s)


@dataclasses.dataclass
class Hit:
    item_type: str
    item_id: str
    start_s: float
    end_s: float
    eligible_from: float
    text: str | None
    score: float
    vec_rank: int | None = None
    vec_score: float | None = None
    fts_rank: int | None = None


# ── Pack loading ─────────────────────────────────────────────────────

@functools.lru_cache(maxsize=8)
def _load(path: str, mtime: float) -> dict:
    return packdb.read_db(pathlib.Path(path))


def load_pack(db_path) -> dict:
    p = pathlib.Path(db_path)
    return _load(str(p.resolve()), p.stat().st_mtime)


def margins(pack: dict, floor: dict | None = None) -> dict:
    """spec §5.1: m = max(value in pack_meta, server-side floor); the floor defaults to the spec defaults."""
    floor = MARGINS if floor is None else floor
    meta = pack.get("pack_meta", {})
    return {k: max(float(meta.get(k, v)), float(floor.get(k, 0.0))) for k, v in MARGINS.items()}


# ── Gate (spec §5) ───────────────────────────────────────────────────

class Gate:
    """Eligibility at P. On top of the per-table time / review rules of §5.2 every item must also pass:

    - visibility: an item is visible only once it is over and revealed, i.e. scenes and shot captions at
      max(reveal_at_s, end_s), events at max(reveal_at_s, t_s), objects at max(reveal_at_s, first_seen_s), facts at
      max(reveal_at_s, evidence_t_s), recaps at max(reveal_at_s, upto_s), each plus its margin;
    - story end: nothing that starts at / after `story_end_s` or reveals after it (credits material); cues are exempt;
    - relations: every character linked through `item_characters` (and a fact's owner, a shot's on-screen characters)
      must itself be eligible at P;
    - supporting cues: every cue linked through `item_cues` must be eligible at P;
    - identity isolation (§5.3), judged over the item's whole interval (`Item.span`), see `isolated`.
    """

    def __init__(self, pack: dict, P: float, allow_auto: bool = False, margin_floor: dict | None = None):
        self.pack, self.P, self.allow_auto = pack, float(P), allow_auto
        self.m = margins(pack, margin_floor)
        self.chars = {c["character_id"]: c for c in pack.get("characters", [])}
        self.item_chars: dict = {}
        for r in pack.get("item_characters", []):
            self.item_chars.setdefault((r["item_type"], r["item_id"]), set()).add(r["character_id"])
        for f in pack.get("character_facts", []):
            self.item_chars.setdefault(("character_fact", f["fact_id"]), set()).add(f["character_id"])
        for r in pack.get("shot_entities", []):
            if r.get("character_id"):
                self.item_chars.setdefault(("shot_caption", r["shot_caption_id"]), set()).add(r["character_id"])
        self.item_cues: dict = {}
        for r in pack.get("item_cues", []):
            self.item_cues.setdefault((r["item_type"], r["item_id"]), set()).add(r["cue_id"])
        self.cue_end = {c["cue_id"]: c["end_s"] for c in pack.get("cues", [])}
        links = pack.get("identity_links", [])
        linked = {lk[k] for lk in links for k in ("a_character_id", "b_character_id")}
        # §5.2 appearances: prereviewed+ (reviewed for characters in identity links), start ≤ P, truncated at P
        self.appear: dict = {}
        for a in pack.get("appearances", []):
            ok = self.reviewed(a["review_state"]) if a["character_id"] in linked else self.pre(a["review_state"])
            if ok and a["character_id"] in self.chars and self.le(a["start_s"]):
                self.appear.setdefault(a["character_id"], []).append((a["start_s"], max(a["start_s"], min(a["end_s"], self.P))))
        # a link is pending until it is reviewed, revealed (+ margin) and both of its characters are eligible
        self.pending = []
        for lk in links:
            if not (self.reviewed(lk["review_state"]) and self.le(lk["reveal_at_s"] + self.m["gate_margin_reviewed_reveal_s"])
                    and self.le(self.char_from(lk["a_character_id"])) and self.le(self.char_from(lk["b_character_id"]))):
                fa = (self.chars.get(lk["a_character_id"]) or {}).get("first_appears_s")
                self.pending.append({"a": lk["a_character_id"], "b": lk["b_character_id"], "reveal_at_s": lk["reveal_at_s"],
                                     "window_start": max(0.0, fa) if fa is not None else 0.0})

    def le(self, t: float) -> bool:
        return t <= self.P + EPS

    def pre(self, state: str) -> bool:
        return state in PRE or (self.allow_auto and state not in BAD)

    def reviewed(self, state: str) -> bool:
        return state == "reviewed" or (self.allow_auto and state not in BAD)

    def char_from(self, cid: str) -> float:
        c = self.chars.get(cid)
        if not c or not self.reviewed(c["review_state"]) or c.get("first_appears_s") is None:
            return math.inf
        return c["first_appears_s"] + self.m["gate_margin_first_appears_s"]

    def char_isolated(self, cid: str, start: float, end: float | None = None) -> bool:
        """§5.3 for one character over [start, end]: while an identity link (a, b, R) is pending, nothing in
        [first_appears(a), R) may be attached to b, unless one of b's own appearances covers the whole interval."""
        end = start if end is None else end
        for lk in self.pending:
            if lk["b"] != cid or not (start < lk["reveal_at_s"] and end >= lk["window_start"]):
                continue
            if not any(s <= start and end <= e for s, e in self.appear.get(cid, [])):
                return True
        return False

    def isolated(self, item_type: str, item_id: str, start: float, end: float | None = None) -> bool:
        """True if any character linked to the item is isolated over the item's interval [start, end]."""
        return any(self.char_isolated(cid, start, end) for cid in self.item_chars.get((item_type, item_id), ()))

    def relations_ok(self, it: Item) -> bool:
        """Linked characters all eligible at P and none isolated; linked cues all eligible at P."""
        start, end = it.span or (it.start_s, it.end_s)
        for cid in self.item_chars.get((it.item_type, it.item_id), ()):
            if not self.le(self.char_from(cid)) or self.char_isolated(cid, start, end):
                return False
        for cue_id in self.item_cues.get((it.item_type, it.item_id), ()):
            if cue_id not in self.cue_end or not self.le(self.cue_end[cue_id] + self.m["gate_margin_cue_s"]):
                return False
        return True

    def candidates(self) -> list[Item]:
        """Every retrievable item with its earliest eligible time by time and review state; inf if it can never be
        eligible (fails review, or credits material). Relations and isolation depend on P and are checked in
        `eligible`."""
        p, m, inf = self.pack, self.m, math.inf
        story_end = (p.get("asset") or {}).get("story_end_s")
        story_end = inf if story_end is None else story_end

        def inside(start: float, reveal: float) -> bool:          # not credits material
            return start < story_end and reveal <= story_end + EPS

        out = []
        for c in p.get("cues", []):
            if c["kind"] != "lyrics" and (c["text"] or "").strip():      # generated tracks can contain blank cues
                out.append(Item("cue", c["cue_id"], c["start_s"], c["end_s"], c["end_s"] + m["gate_margin_cue_s"], c["text"]))
        for s in p.get("scenes", []):
            mm = m["gate_margin_scene_snapped_s"] if s["snapped"] else m["gate_margin_scene_unsnapped_s"]
            vis = max(s["reveal_at_s"], s["end_s"])
            ok = self.pre(s["review_state"]) and inside(s["start_s"], s["reveal_at_s"])
            loc = f"{s['location_en']}. " if s.get("location_en") else ""
            out.append(Item("scene", s["scene_id"], s["start_s"], s["end_s"], vis + mm if ok else inf,
                            loc + s["summary_en"], (s["start_s"], vis)))
        for e in p.get("events", []):
            ok = self.reviewed(e["review_state"]) if (e["kind"] == "reveal" or e["spoiler_critical"]) else self.pre(e["review_state"])
            vis = max(e["reveal_at_s"], e["t_s"])
            ok = ok and inside(e["t_s"], e["reveal_at_s"])
            out.append(Item("event", e["event_id"], e["t_s"], e["reveal_at_s"], vis + m["gate_margin_event_s"] if ok else inf,
                            e["text_en"], (min(e["t_s"], e["reveal_at_s"]), vis)))
        for o in p.get("objects", []):
            ok = self.reviewed(o["review_state"]) if o["spoiler_critical"] else self.pre(o["review_state"])
            vis = max(o["reveal_at_s"], o["first_seen_s"])
            ok = ok and inside(o["first_seen_s"], o["reveal_at_s"])
            out.append(Item("object", o["object_id"], o["first_seen_s"], o["reveal_at_s"],
                            vis + m["gate_margin_object_s"] if ok else inf, f"{o['name_en']}: {o['description_en']}",
                            (min(o["first_seen_s"], o["reveal_at_s"]), vis)))
        for f in p.get("character_facts", []):
            ev_t = f["evidence_t_s"] if f.get("evidence_t_s") is not None else f["reveal_at_s"]
            vis = max(f["reveal_at_s"], ev_t)
            t = max(vis + m["gate_margin_reviewed_reveal_s"], self.char_from(f["character_id"]))
            ok = self.reviewed(f["review_state"]) and inside(min(ev_t, f["reveal_at_s"]), f["reveal_at_s"])
            out.append(Item("character_fact", f["fact_id"], f["reveal_at_s"], f["reveal_at_s"], t if ok else inf,
                            f["text_en"], (min(ev_t, f["reveal_at_s"]), vis)))
        for s in p.get("shot_captions", []):
            vis = max(s["reveal_at_s"], s["end_s"])
            ok = self.pre(s["review_state"]) and inside(s["start_s"], s["reveal_at_s"])
            out.append(Item("shot_caption", s["shot_caption_id"], s["start_s"], s["end_s"],
                            vis + m["gate_margin_scene_snapped_s"] if ok else inf, s["caption_en"], (s["start_s"], vis)))
        for r in p.get("recaps", []):
            vis = max(r["reveal_at_s"], r["upto_s"])
            ok = self.pre(r["review_state"]) and r["upto_s"] <= story_end + EPS and r["reveal_at_s"] <= story_end + EPS
            out.append(Item("recap", r["recap_id"], r["upto_s"], r["upto_s"],
                            vis + m["gate_margin_scene_snapped_s"] if ok else inf, r["text_en"], (r["upto_s"], vis)))
        for w in p.get("video_windows", []):
            out.append(Item("video_window", w["window_id"], w["start_s"], w["end_s"],
                            w["end_s"] + m["gate_margin_scene_snapped_s"] if w["start_s"] < story_end else inf, None))
        return out

    def eligible(self, types=None) -> list[Item]:
        a = self.pack.get("asset") or {}
        if a.get("story_start_s") is not None and self.P + EPS < a["story_start_s"]:
            return []                                    # §5.4: no content answers before the story starts
        return [it for it in self.candidates()
                if (types is None or it.item_type in types) and self.le(it.eligible_from) and self.relations_ok(it)]


# ── Ranking (within the eligible set only) ───────────────────────────

def vector_rank(db_path, items: list[Item], qvec, model: str = embed.MODEL) -> list[tuple[Item, float]]:
    """Eligible items JOIN embeddings → cosine (vectors are normalized, so dot = cosine), descending."""
    if not items:
        return []
    db = sqlite3.connect(f"file:{pathlib.Path(db_path)}?mode=ro", uri=True)
    try:
        db.execute("CREATE TEMP TABLE elig(item_type TEXT, item_id TEXT, PRIMARY KEY (item_type, item_id))")
        db.executemany("INSERT OR IGNORE INTO temp.elig VALUES (?, ?)", [(it.item_type, it.item_id) for it in items])
        rows = db.execute("SELECT e.item_type, e.item_id, e.vector FROM embeddings e JOIN temp.elig t "
                          "ON e.item_type = t.item_type AND e.item_id = t.item_id WHERE e.model = ?", (model,)).fetchall()
    finally:
        db.close()
    if not rows:
        return []
    by_key = {(it.item_type, it.item_id): it for it in items}
    mat = np.stack([np.frombuffer(v, dtype="<f4") for _t, _i, v in rows])
    q = np.asarray(qvec, dtype=np.float32)
    q = q / (np.linalg.norm(q) or 1.0)
    sims = mat @ q
    order = np.argsort(-sims, kind="stable")
    return [(by_key[(rows[i][0], rows[i][1])], float(sims[i])) for i in order]


_TOKEN = re.compile(r"\w+", re.UNICODE)


def fts_query(q: str) -> str | None:
    toks = list(dict.fromkeys(t.lower() for t in _TOKEN.findall(q)))
    return " OR ".join(f'"{t}"' for t in toks) or None


def fts_rank(items: list[Item], query: str) -> list[tuple[Item, float]]:
    """In-memory FTS5 holding only eligible text (same tokenizer as pack.db text_fts); bm25 ascending (lower = more relevant)."""
    texts = [it for it in items if it.text]
    expr = fts_query(query)
    if not texts or not expr:
        return []
    db = sqlite3.connect(":memory:")
    try:
        db.execute("CREATE VIRTUAL TABLE f USING fts5(k UNINDEXED, text)")
        db.executemany("INSERT INTO f(k, text) VALUES (?, ?)", [(i, it.text) for i, it in enumerate(texts)])
        rows = db.execute("SELECT k, bm25(f) FROM f WHERE f MATCH ? ORDER BY bm25(f), k", (expr,)).fetchall()
    finally:
        db.close()
    return [(texts[k], s) for k, s in rows]


def _hit(it: Item, score: float, **kw) -> Hit:
    return Hit(it.item_type, it.item_id, it.start_s, it.end_s, it.eligible_from, it.text, score, **kw)


def search(db_path, P: float, query: str, k: int = 5, *, mode: str = "vector", allow_auto: bool = False,
           types=None, qvec=None, embed_query=None, margin_floor: dict | None = None, keep=None) -> list[Hit]:
    """mode ∈ vector (single cosine ranking) / fts / hybrid (RRF over text vectors, video-window vectors, and full text). qvec may be passed directly; otherwise embed_query(q) or the default Gemini query embedding is used.
    `keep(item) -> bool` narrows the eligible set before ranking (e.g. one subtitle track, no very short lines), so the
    top k are taken from what the caller can actually use."""
    pack = load_pack(db_path)
    items = Gate(pack, P, allow_auto, margin_floor).eligible(types)      # ① gate first
    if keep is not None:
        items = [it for it in items if keep(it)]
    vec, fts = [], []
    if mode in ("vector", "hybrid"):                                      # ② then rank within the eligible set
        if qvec is None:
            qvec = (embed_query or query_vector)(query)
        vec = vector_rank(db_path, items, qvec)
    if mode in ("fts", "hybrid"):
        fts = fts_rank(items, query)
    if mode == "vector":
        return [_hit(it, s, vec_rank=r + 1, vec_score=s) for r, (it, s) in enumerate(vec[:k])]
    if mode == "fts":
        return [_hit(it, -s, fts_rank=r + 1) for r, (it, s) in enumerate(fts[:k])]
    # Cross-modal cosine (text→video ~0.6) and same-modal (text→text ~0.8) are on different scales: rank vector hits per modality lane, then RRF with full text
    fused: dict = {}
    lane_rank = {"text": 0, "video": 0}
    for r, (it, s) in enumerate(vec):
        lane = "video" if it.item_type == "video_window" else "text"
        lane_rank[lane] += 1
        h = fused.setdefault((it.item_type, it.item_id), _hit(it, 0.0))
        h.score += 1.0 / (RRF_K + lane_rank[lane])
        h.vec_rank, h.vec_score = r + 1, s
    for r, (it, _s) in enumerate(fts):
        h = fused.setdefault((it.item_type, it.item_id), _hit(it, 0.0))
        h.score += 1.0 / (RRF_K + r + 1)
        h.fts_rank = r + 1
    return sorted(fused.values(), key=lambda h: (-h.score, h.item_type, h.item_id))[:k]


@functools.lru_cache(maxsize=1)
def _query_embedder():
    from ingest import gemini
    return gemini.Embedder()


@functools.lru_cache(maxsize=256)
def _qvec(q: str) -> tuple:
    vecs, _cost = _query_embedder().embed_texts([embed.QUERY_PREFIX.format(q)])
    v = np.asarray(vecs[0], dtype=np.float32)
    return tuple(v / (np.linalg.norm(v) or 1.0))


def query_vector(q: str):
    """Query embedding (with query prefix, 1536-d, normalized); cached in-process. `CEDAR_GEMINI_BACKEND` selects the backend."""
    if os.environ.get("CEDAR_EMBED", "1") == "0":
        raise RuntimeError("CEDAR_EMBED=0: query embedding disabled")
    return np.asarray(_qvec(q), dtype=np.float32)


# ── Online query embedding: one bounded attempt, never the ingest retry loop ──

class OnlineEmbedder:
    """Query embeddings for a live answer. One attempt with a hard timeout and no retries (the ingest client retries
    for up to 75 s, which is fine for a batch job and fatal for a viewer waiting on an answer). Any failure returns
    None so the caller ranks full text instead, and a failure pauses embedding for `cooldown_s` so later questions do
    not each wait for the timeout again. Successful vectors are cached per query."""

    def __init__(self, timeout_s: float = 1.5, cooldown_s: float = 60.0, embed=None, cache_size: int = 512):
        self.timeout_s, self.cooldown_s = timeout_s, cooldown_s
        self._embed = embed or self._gemini
        self._cache: dict[str, np.ndarray] = {}
        self._cache_size = cache_size
        self.down_until = 0.0
        self.last_error: str | None = None

    async def _gemini(self, text: str) -> list[float]:
        from google.genai import types
        r = await _online_client().aio.models.embed_content(
            model=embed.MODEL, contents=text,
            config=types.EmbedContentConfig(output_dimensionality=embed.DIM, http_options=types.HttpOptions(
                timeout=int(self.timeout_s * 1000), retry_options=types.HttpRetryOptions(attempts=1))))
        return r.embeddings[0].values

    async def vector(self, q: str):
        import asyncio
        import time
        if q in self._cache:
            return self._cache[q]
        if os.environ.get("CEDAR_EMBED", "1") == "0" or time.monotonic() < self.down_until:
            return None
        try:
            vals = await asyncio.wait_for(self._embed(embed.QUERY_PREFIX.format(q)), self.timeout_s)
        except Exception as e:                       # noqa: BLE001 — timeout, quota, permissions: rank full text instead
            self.last_error = f"{type(e).__name__}: {str(e)[:200]}"
            self.down_until = time.monotonic() + self.cooldown_s
            return None
        v = np.asarray(vals, dtype=np.float32)
        v = v / (np.linalg.norm(v) or 1.0)
        if len(self._cache) >= self._cache_size:
            self._cache.pop(next(iter(self._cache)))
        self._cache[q] = v
        return v


@functools.lru_cache(maxsize=1)
def _online_client():
    from ingest import gemini
    return gemini.client()
