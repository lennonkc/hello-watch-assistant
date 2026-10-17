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
    start_s: float              # item start time (identity isolation is judged on it)
    end_s: float
    eligible_from: float        # earliest eligible P (margin included); eligible ⇔ eligible_from ≤ P
    text: str | None            # video windows have no text


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
        self.appear: dict = {}
        for a in pack.get("appearances", []):
            self.appear.setdefault(a["character_id"], []).append((a["start_s"], a["end_s"]))
        self.pending = [lk for lk in pack.get("identity_links", [])
                        if not (self.reviewed(lk["review_state"]) and self.le(lk["reveal_at_s"] + self.m["gate_margin_reviewed_reveal_s"]))]

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

    def isolated(self, item_type: str, item_id: str, t: float) -> bool:
        """§5.3: under a not-yet-eligible identity link (a, b, R), items tied to b within [first_appears(a), R) are ineligible (unless b is on screen)."""
        linked = self.item_chars.get((item_type, item_id))
        if not linked:
            return False
        for lk in self.pending:
            a = self.chars.get(lk["a_character_id"]) or {}
            fa = a.get("first_appears_s")
            if fa is None or lk["b_character_id"] not in linked:
                continue
            on_screen = any(s <= t <= e for s, e in self.appear.get(lk["b_character_id"], []))
            if fa <= t < lk["reveal_at_s"] and not on_screen:
                return True
        return False

    def candidates(self) -> list[Item]:
        """Every retrievable item with its earliest eligible time; inf if it fails review."""
        p, m, inf = self.pack, self.m, math.inf
        out = []
        for c in p.get("cues", []):
            if c["kind"] != "lyrics":
                out.append(Item("cue", c["cue_id"], c["start_s"], c["end_s"], c["end_s"] + m["gate_margin_cue_s"], c["text"]))
        for s in p.get("scenes", []):
            mm = m["gate_margin_scene_snapped_s"] if s["snapped"] else m["gate_margin_scene_unsnapped_s"]
            ok = self.pre(s["review_state"])
            loc = f"{s['location_en']}. " if s.get("location_en") else ""
            out.append(Item("scene", s["scene_id"], s["start_s"], s["end_s"], s["reveal_at_s"] + mm if ok else inf,
                            loc + s["summary_en"]))
        for e in p.get("events", []):
            ok = self.reviewed(e["review_state"]) if (e["kind"] == "reveal" or e["spoiler_critical"]) else self.pre(e["review_state"])
            out.append(Item("event", e["event_id"], e["t_s"], e["reveal_at_s"],
                            e["reveal_at_s"] + m["gate_margin_event_s"] if ok else inf, e["text_en"]))
        for o in p.get("objects", []):
            ok = self.reviewed(o["review_state"]) if o["spoiler_critical"] else self.pre(o["review_state"])
            out.append(Item("object", o["object_id"], o["first_seen_s"], o["reveal_at_s"],
                            o["reveal_at_s"] + m["gate_margin_object_s"] if ok else inf, f"{o['name_en']}: {o['description_en']}"))
        for f in p.get("character_facts", []):
            t = max(f["reveal_at_s"] + m["gate_margin_reviewed_reveal_s"], self.char_from(f["character_id"]))
            out.append(Item("character_fact", f["fact_id"], f["reveal_at_s"], f["reveal_at_s"],
                            t if self.reviewed(f["review_state"]) else inf, f["text_en"]))
        for s in p.get("shot_captions", []):
            out.append(Item("shot_caption", s["shot_caption_id"], s["start_s"], s["end_s"],
                            s["reveal_at_s"] + m["gate_margin_scene_snapped_s"] if self.pre(s["review_state"]) else inf,
                            s["caption_en"]))
        for r in p.get("recaps", []):
            out.append(Item("recap", r["recap_id"], r["upto_s"], r["upto_s"],
                            r["reveal_at_s"] + m["gate_margin_scene_snapped_s"] if self.pre(r["review_state"]) else inf,
                            r["text_en"]))
        for w in p.get("video_windows", []):
            out.append(Item("video_window", w["window_id"], w["start_s"], w["end_s"],
                            w["end_s"] + m["gate_margin_scene_snapped_s"], None))
        return out

    def eligible(self, types=None) -> list[Item]:
        a = self.pack.get("asset") or {}
        if a.get("story_start_s") is not None and self.P + EPS < a["story_start_s"]:
            return []                                    # §5.4: no content answers before the story starts
        return [it for it in self.candidates()
                if (types is None or it.item_type in types) and self.le(it.eligible_from)
                and not self.isolated(it.item_type, it.item_id, it.start_s)]


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
           types=None, qvec=None, embed_query=None, margin_floor: dict | None = None) -> list[Hit]:
    """mode ∈ vector (single cosine ranking) / fts / hybrid (RRF over text vectors, video-window vectors, and full text). qvec may be passed directly; otherwise embed_query(q) or the default Gemini query embedding is used."""
    pack = load_pack(db_path)
    items = Gate(pack, P, allow_auto, margin_floor).eligible(types)      # ① gate first
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
