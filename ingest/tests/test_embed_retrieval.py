"""Vectors and retrieval: window slicing, sidecar round trip, gate before ranking, empty-table robustness. Fully offline (fake embedder)."""
import hashlib
import re
import types

import numpy as np
import pytest

from ingest import build, embed, packdb, retrieval, util
from ingest.derive import fts_rows
from ingest.ledger import Ledger
from ingest.tests.conftest import SHA



def bow(text: str) -> list[float]:
    """Bag-of-words hash vector: same words, same direction; enough to make "semantics" predictable in tests."""
    v = [0.0] * embed.DIM
    for w in re.findall(r"\w+", text.lower()):
        v[int(hashlib.md5(w.encode()).hexdigest(), 16) % embed.DIM] += 1.0
    v[0] += 1e-3
    return v


class FakeEmbedder:
    mode = "frames"

    def __init__(self):
        self.text_calls, self.video_calls = 0, 0

    def estimate(self, texts, durations):
        return 0.0

    def embed_texts(self, texts):
        self.text_calls += 1
        return [bow(t.split("text: ", 1)[-1]) for t in texts], 0.0

    def embed_video(self, start, end):
        self.video_calls += 1
        return bow(f"window {start:.0f}"), 0.0


@pytest.fixture
def ctx(tmp_path, monkeypatch):
    monkeypatch.setattr(util, "CACHE", tmp_path)
    return types.SimpleNamespace(sha=SHA, wd=util.work_dir(SHA), ledger=Ledger(SHA, 1.0))


def qv(text):
    return np.asarray(bow(text), dtype=np.float32)


def write(pack, ctx, tmp_path, name="pack.db"):
    p = tmp_path / name
    packdb.write_db(pack, p, fts_rows(pack), embed.pack_rows(pack, ctx.wd))
    retrieval._load.cache_clear()
    return p


# ── Windows ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("a,b", [(0.0, 12.0), (0.0, 30.0), (10.0, 70.0), (0.0, 119.7), (3.2, 100.0)])
def test_windows_cover_segment_with_overlap(a, b):
    ws = embed.plan_windows(a, b)
    assert ws[0][0] == a and ws[-1][1] == pytest.approx(b, abs=1e-3)
    assert all(y - x <= embed.WIN_LEN + 1e-6 and y > x for x, y in ws)
    assert all(ws[i][1] - ws[i + 1][0] >= embed.WIN_OVERLAP - 1e-3 for i in range(len(ws) - 1))


# ── Sidecar round trip ────────────────────────────────────────────────

def test_sidecar_roundtrip_and_cache(pack, ctx, tmp_path):
    fake = FakeEmbedder()
    embed.add_embeddings(ctx, pack, fake)
    assert [w["window_id"] for w in pack["video_windows"]] == ["seg00:w00", "seg00:w01", "seg00:w02", "seg00:w03"]
    assert embed.sidecar_path(ctx.wd).exists()
    p = write(pack, ctx, tmp_path)
    rows = packdb.read_embeddings(p)
    kinds = {r["item_type"] for r in rows}
    assert kinds == {"cue", "scene", "event", "character_fact", "video_window"}       # empty tables (shot / recap / object) do not error
    assert len(rows) == len(pack["cues"]) + len(pack["scenes"]) + len(pack["events"]) + 1 + 4
    for r in rows:
        v = np.frombuffer(r["vector"], dtype="<f4")
        assert r["dim"] == embed.DIM == v.size and abs(np.linalg.norm(v) - 1) < 1e-5
    cue = next(r for r in rows if r["item_id"] == "en.official:001")
    want = np.asarray(bow("Hello, Bob."), dtype=np.float32)
    assert np.allclose(np.frombuffer(cue["vector"], dtype="<f4"), want / np.linalg.norm(want), atol=1e-6)
    # pack.json / rows.json forms carry no vectors; video_windows is an ordinary table
    norm = packdb.normalize_pack(pack)
    assert "embeddings" not in norm and len(norm["video_windows"]) == 4
    assert packdb.read_db(p)["video_windows"] == norm["video_windows"]
    # second run: fully cached, no calls
    calls = (fake.text_calls, fake.video_calls)
    embed.add_embeddings(ctx, pack, fake)
    assert (fake.text_calls, fake.video_calls) == calls


def test_edited_text_misses_cache(pack, ctx, tmp_path):
    embed.add_embeddings(ctx, pack, FakeEmbedder())
    pack["scenes"][0]["summary_en"] = "Edited by a reviewer."
    ids = {(r["item_type"], r["item_id"]) for r in embed.pack_rows(pack, ctx.wd)}
    assert ("scene", "s01") not in ids and ("scene", "s02") in ids


def test_no_sidecar_no_embeddings(pack, ctx, tmp_path, monkeypatch):
    monkeypatch.setenv("CEDAR_EMBED", "0")
    embed.add_embeddings(ctx, pack)                    # no API calls, only window slicing
    assert pack["video_windows"] and embed.pack_rows(pack, ctx.wd) == []
    p = write(pack, ctx, tmp_path)
    assert packdb.read_embeddings(p) == []
    assert retrieval.search(p, 95, "Bob", mode="vector", qvec=qv("Bob")) == []
    assert retrieval.search(p, 95, "Bob", mode="hybrid", qvec=qv("Bob"))          # degrades to full-text


def test_export_writes_embeddings_into_master_and_device(pack, ctx, tmp_path):
    th = ctx.wd / "thumbs"
    th.mkdir(parents=True)
    pack["thumbnails"] = []
    pack["events"][0]["review_state"] = "auto"                     # the device pack has neither auto rows nor their vectors
    embed.add_embeddings(ctx, pack, FakeEmbedder())
    pack["pack_meta"] = {}
    util.write_json(ctx.wd / "auto" / "rows.json", packdb.normalize_pack(pack))
    r1 = build.export_all(SHA)
    m = packdb.read_embeddings(ctx.wd / "master" / "pack.db")
    d = packdb.read_embeddings(ctx.wd / "device" / "pack.db")
    assert ("event", "e001") in {(r["item_type"], r["item_id"]) for r in m}
    assert ("event", "e001") not in {(r["item_type"], r["item_id"]) for r in d}
    assert len(packdb.read_db(ctx.wd / "device" / "pack.db")["video_windows"]) == 4
    assert build.export_all(SHA)["pack_version"] == r1["pack_version"]          # same content, same version


# ── Gate before ranking ───────────────────────────────────────────────

def test_future_items_never_ranked(pack, ctx, tmp_path):
    """A future item whose vector equals the query and whose text matches verbatim still never appears before P, in any mode."""
    embed.add_embeddings(ctx, pack, FakeEmbedder())
    p = write(pack, ctx, tmp_path)
    q = "The wolf turns out to be Bob."                 # = e002.text_en(reveal 62 s + 3)
    for mode in ("vector", "fts", "hybrid"):
        for P in (20.0, 55.0, 64.9):
            hits = retrieval.search(p, P, q, k=50, mode=mode, qvec=qv(q))
            assert all(h.eligible_from <= P + 1e-9 for h in hits), (mode, P)
            assert "e002" not in {h.item_id for h in hits}
        hits = retrieval.search(p, 65.0, q, k=1, mode=mode, qvec=qv(q))
        assert hits[0].item_id == "e002", mode


def test_vector_rank_only_sees_eligible(pack, ctx, tmp_path, monkeypatch):
    embed.add_embeddings(ctx, pack, FakeEmbedder())
    p = write(pack, ctx, tmp_path)
    seen = []
    orig = retrieval.vector_rank
    monkeypatch.setattr(retrieval, "vector_rank", lambda db, items, q, **kw: seen.extend(items) or orig(db, items, q, **kw))
    retrieval.search(p, 30.0, "Bob", mode="vector", qvec=qv("Bob"))
    assert seen and all(it.eligible_from <= 30.0 for it in seen)
    assert {it.item_type for it in seen} <= {"cue", "scene", "event", "character_fact", "video_window"}


def test_before_story_start_returns_nothing(pack, ctx, tmp_path):
    pack["asset"]["story_start_s"] = 20.0
    embed.add_embeddings(ctx, pack, FakeEmbedder())
    p = write(pack, ctx, tmp_path)
    assert retrieval.search(p, 15.0, "Hello Bob", mode="hybrid", qvec=qv("Hello Bob")) == []


def test_allow_auto_switch(pack, ctx, tmp_path):
    pack["events"][0]["review_state"] = "auto"
    embed.add_embeddings(ctx, pack, FakeEmbedder())
    p = write(pack, ctx, tmp_path)
    q = "Alice greets Bob."
    assert "e001" not in {h.item_id for h in retrieval.search(p, 30, q, k=20, mode="fts")}
    assert "e001" in {h.item_id for h in retrieval.search(p, 30, q, k=20, mode="fts", allow_auto=True)}
    assert "e001" not in {h.item_id for h in retrieval.search(p, 14.9, q, k=20, mode="fts", allow_auto=True)}


def test_v11_tables_gated(pack, ctx, tmp_path):
    pack["shot_captions"] = [{"shot_caption_id": "sc000", "start_s": 0.0, "end_s": 50.0, "reveal_at_s": 50.0,
                              "caption_en": "A girl walks through a meadow.", "caption_zh": None, "source": "t",
                              "review_state": "prereviewed"}]
    pack["recaps"] = [{"recap_id": "r01", "upto_scene_id": "s01", "upto_s": 50.0, "reveal_at_s": 50.0,
                       "text_en": "Alice met Bob in a meadow.", "text_zh": None, "source": "t", "review_state": "auto"}]
    embed.add_embeddings(ctx, pack, FakeEmbedder())
    p = write(pack, ctx, tmp_path)
    ids = lambda P, **kw: {h.item_id for h in retrieval.search(p, P, "meadow", k=50, mode="fts", **kw)}  # noqa: E731
    assert "sc000" not in ids(52.9) and "sc000" in ids(53.0)
    assert "r01" not in ids(60) and "r01" in ids(60, allow_auto=True)
    wins = {h.item_id: h for h in retrieval.search(p, 40.0, "window", k=50, mode="vector", qvec=qv("window 0"))
            if h.item_type == "video_window"}
    assert wins and all(h.end_s + 3.0 <= 40.0 for h in wins.values())


# ── Backend switch ────────────────────────────────────────────────────

def test_backend_switch_defaults_to_developer(monkeypatch):
    from ingest import gemini
    monkeypatch.delenv("CEDAR_GEMINI_BACKEND", raising=False)
    assert gemini.backend() == "developer"
    monkeypatch.setenv("CEDAR_GEMINI_BACKEND", "vertex")
    with pytest.raises(ValueError):
        gemini.backend()


def test_enterprise_asset_uses_gcs_without_files_api(ctx, monkeypatch):
    from ingest import gemini
    from ingest.stages import gemini_stages
    monkeypatch.setenv("CEDAR_GEMINI_BACKEND", "enterprise")
    monkeypatch.setenv("CEDAR_INGEST_BUCKET", "gs://bkt/")
    monkeypatch.setattr(gemini, "gcs_exists", lambda uri: True)
    monkeypatch.setattr(gemini, "ensure_file", lambda *a, **k: pytest.fail("Files API must not be used"))
    rec = gemini_stages.ensure_asset_file(ctx, None)
    assert rec["uri"] == f"gs://bkt/assets/{SHA}.mp4" and rec["how"] == "gcs"
    assert util.read_json(gemini_stages.registry_path(ctx))["asset_file"]["storage_uri"] == rec["uri"]
    part = gemini.video_part(rec, 1.0, 60.0, 75.0)
    assert part.file_data.file_uri.startswith("gs://") and part.video_metadata.end_offset in ("75.000s", "75s")
    monkeypatch.setattr(gemini, "gcs_exists", lambda uri: False)
    with pytest.raises(RuntimeError, match="upload_assets"):
        gemini_stages.ensure_asset_file(ctx, None)


def test_reviewed_text_embedded_at_build(pack, ctx, tmp_path):
    """A pre-review patch edits a summary: build also embeds the patched text, so the exported master has complete vectors."""
    pd = ctx.wd / "patches"
    pd.mkdir(parents=True)
    pack["scenes"][0]["review_state"] = "auto"
    util.write_json(pd / "001-claude-prereview.json", {
        "format": "cedar-review-patch/1", "asset_sha256": SHA, "reviewer": "claude-prereview",
        "created_at": "2026-10-14T00:00:00Z",
        "ops": [{"op": "edit", "table": "scenes", "key": {"scene_id": "s01"}, "set": {"summary_en": "Alice and Bob meet."},
                 "why": "w", "seconds_spent": 1}]})
    embed.add_embeddings(ctx, pack, FakeEmbedder())
    view = embed.reviewed_view(ctx, pack)
    assert next(s for s in view["scenes"] if s["scene_id"] == "s01")["summary_en"] == "Alice and Bob meet."
    assert ("scene", "s01") in {(r["item_type"], r["item_id"]) for r in embed.pack_rows(view, ctx.wd)}
    assert ("scene", "s01") in {(r["item_type"], r["item_id"]) for r in embed.pack_rows(pack, ctx.wd)}


def test_partial_failure_still_logs_cost(pack, ctx):
    class Boom(FakeEmbedder):
        def embed_video(self, start, end):
            raise RuntimeError("Server disconnected")

        def embed_texts(self, texts):
            vecs, _ = super().embed_texts(texts)
            return vecs, 0.01
    with pytest.raises(RuntimeError):
        embed.add_embeddings(ctx, pack, Boom())
    e = [x for x in ctx.ledger.entries() if x["stage"] == "embed"]
    assert e and e[-1]["status"] == "failed" and e[-1]["cost_usd"] == 0.01 and e[-1]["new_video"] == 0
