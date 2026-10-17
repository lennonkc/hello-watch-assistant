"""Pure logic of cloud ingest: sharding, forward-only LATEST, mirror planning, append-only publishing, renewal checks, sidecar write-back, catalog matching."""
from __future__ import annotations

import base64
import datetime as dt
import importlib.util
import pathlib

import pytest

from ingest.cloud import gcs, register
from ingest.cloud.job import next_latest, shard

UTC = dt.timezone.utc
NOW = dt.datetime(2026, 10, 14, 12, 0, 0, tzinfo=UTC)


def test_shard_partitions_stably():
    shas = ["c", "a", "b", "a", "d", "e"]
    parts = [shard(shas, i, 2) for i in range(2)]
    assert parts == [["a", "c", "e"], ["b", "d"]]
    assert sorted(sum(parts, [])) == ["a", "b", "c", "d", "e"]
    assert shard(shas, 0, 1) == ["a", "b", "c", "d", "e"]
    assert shard(["a"], 3, 4) == []
    with pytest.raises(ValueError):
        shard(shas, 2, 2)


def test_latest_only_moves_forward():
    v2 = {"pack_version": 2, "path": "packs/x/v2/"}
    v3 = {"pack_version": 3, "path": "packs/x/v3/"}
    assert next_latest(None, v2) == v2
    assert next_latest(v2, v3) == v3
    assert next_latest(v3, v2) is None          # re-exporting an older version does not move the pointer back
    assert next_latest(v3, dict(v3)) is None    # same content is not rewritten
    v3b = {**v3, "review_complete": True}
    assert next_latest(v3, v3b) == v3b


def test_plan_mirror():
    up, rm = gcs.plan_mirror({"a": "1", "b": "2", "c": "3"}, {"a": "1", "b": "X", "z": "9"})
    assert up == ["b", "c"] and rm == ["z"]


class FakeBlob:
    def __init__(self, store, name):
        self.store, self.name = store, name

    @property
    def crc32c(self):
        import google_crc32c
        return base64.b64encode(google_crc32c.Checksum(self.store[self.name]).digest()).decode()

    def upload_from_filename(self, path, if_generation_match=None, **kw):
        if if_generation_match == 0 and self.name in self.store:
            from google.api_core.exceptions import PreconditionFailed
            raise PreconditionFailed("exists")
        self.store[self.name] = pathlib.Path(path).read_bytes()

    def delete(self):
        del self.store[self.name]


class FakeBucket:
    name = "bkt"

    def __init__(self):
        self.store: dict[str, bytes] = {}
        self.client = self

    def list_blobs(self, bkt, prefix):
        return [FakeBlob(self.store, n) for n in sorted(self.store) if n.startswith(prefix)]

    def blob(self, name, **kw):
        return FakeBlob(self.store, name)

    def get_blob(self, name):
        return FakeBlob(self.store, name) if name in self.store else None


def test_publish_dir_is_create_only(tmp_path):
    pytest.importorskip("google.api_core")
    (tmp_path / "thumbs").mkdir()
    (tmp_path / "pack.db").write_bytes(b"db")
    (tmp_path / "thumbs" / "t00000.jpg").write_bytes(b"jpg")
    b = FakeBucket()
    assert gcs.publish_dir(b, tmp_path, "packs/s/v1/device") == {"created": 2, "unchanged": 0}
    assert gcs.publish_dir(b, tmp_path, "packs/s/v1/device") == {"created": 0, "unchanged": 2}
    (tmp_path / "pack.db").write_bytes(b"different")
    with pytest.raises(gcs.ImmutableConflict):
        gcs.publish_dir(b, tmp_path, "packs/s/v1/device")
    assert b.store["packs/s/v1/device/pack.db"] == b"db"


def test_push_dir_mirrors_and_skips_unchanged(tmp_path):
    (tmp_path / "a.json").write_text("1")
    (tmp_path / "b.json").write_text("2")
    b = FakeBucket()
    b.store["work/s/stale.json"] = b"x"
    assert gcs.push_dir(b, tmp_path, "work/s") == {"uploaded": 2, "deleted": 1, "unchanged": 0}
    assert gcs.push_dir(b, tmp_path, "work/s") == {"uploaded": 0, "deleted": 0, "unchanged": 2}
    assert sorted(b.store) == ["work/s/a.json", "work/s/b.json"]


def _iso(d: dt.datetime) -> str:
    return register.iso(d)


def test_renew_window():
    side = {"asset_sha256": "s",
            "asset_file": {"storage_uri": "gs://b/assets/s.mp4", "gemini_file": "files/a", "expires_at": _iso(NOW + dt.timedelta(hours=11))},
            "segments": [
                {"segment_id": "seg00", "storage_uri": "gs://b/0", "gemini_file": "files/0", "expires_at": _iso(NOW + dt.timedelta(hours=13))},
                {"segment_id": "seg01", "storage_uri": "gs://b/1", "gemini_file": "files/1", "expires_at": None},
                {"segment_id": "seg02", "storage_uri": "gs://b/2", "gemini_file": None, "expires_at": _iso(NOW + dt.timedelta(days=9))},
                {"segment_id": "seg03", "storage_uri": "gs://b/3", "gemini_file": "files/3", "expires_at": _iso(NOW - dt.timedelta(hours=1))}]}
    assert register.due_entries(side, NOW) == ["gs://b/assets/s.mp4", "gs://b/1", "gs://b/2", "gs://b/3"]
    assert register.due_entries(side, NOW, dt.timedelta(hours=1)) == ["gs://b/1", "gs://b/2", "gs://b/3"]


def test_apply_registrations_keeps_c1_shape():
    side = {"asset_sha256": "s", "asset_file": {"storage_uri": "gs://b/a", "gemini_file": None, "expires_at": None},
            "segments": [{"segment_id": "seg00", "file_sha256": "f0", "storage_uri": "gs://b/0", "gemini_file": None,
                          "expires_at": None}]}
    out = register.apply_registrations(side, {"gs://b/a": {"gemini_file": "files/x", "expires_at": "2026-11-13T00:00:00Z",
                                                           "how": "gcs"}})
    assert out["asset_file"] == {"storage_uri": "gs://b/a", "gemini_file": "files/x", "expires_at": "2026-11-13T00:00:00Z"}
    assert out["segments"] == side["segments"]
    assert side["asset_file"]["gemini_file"] is None     # input is not mutated
    assert set(out) == {"asset_sha256", "asset_file", "segments"}


def test_iso_roundtrip():
    t = dt.datetime(2026, 11, 13, 0, 0, 0, tzinfo=UTC)
    assert register.parse_iso(register.iso(t)) == t
    assert register.iso(None) is None and register.parse_iso(None) is None


def _upload_tool():
    p = pathlib.Path(__file__).resolve().parents[3] / "deploy" / "ingest" / "upload_assets.py"
    if not p.exists():      # deploy/ is not in the image
        pytest.skip("deploy/ingest/upload_assets.py not present")
    spec = importlib.util.spec_from_file_location("upload_assets", p)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def test_upload_tool_catalog_match_and_index_merge():
    m = _upload_tool()
    items = [{"id": "1", "title": "Spring", "title_zh": "春"},
             {"id": "2", "title": "Agent 327: Operation Barbershop", "title_zh": "特工327:理发店行动"},
             {"id": "3", "title": "WOLF WARRIOR II", "title_zh": "战狼2"}]
    assert m.match_catalog("春 Spring.mp4", items)["id"] == "1"
    assert m.match_catalog("特工327:理发店行动 Agent 327: Operation Barbershop.mp4", items)["id"] == "2"
    assert m.match_catalog("xx Wolf Warrior 2.mp4", items)["id"] == "3"
    assert m.match_catalog("未知 Unknown.mp4", items) is None
    old = {"assets": [{"asset_sha256": "a", "title": "Spring", "file": "old"}, {"asset_sha256": "b", "title": "B"}]}
    new = m.merge_index(old, [{"asset_sha256": "a", "title": "Spring", "file": "new"}])
    assert new["count"] == 2 and [a["asset_sha256"] for a in new["assets"]] == ["b", "a"]
    assert new["assets"][1]["file"] == "new"
