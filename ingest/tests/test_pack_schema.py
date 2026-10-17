"""DDL build, foreign keys, pack.json ⇄ pack.db round trip."""
import json
import sqlite3

import pytest

from ingest import packdb
from ingest.derive import fts_rows


def test_ddl_tables_match_contract():
    t = set(packdb.tables())
    for name in ("pack_meta", "asset", "title_info", "shots", "segments", "subtitle_tracks", "cues", "characters",
                 "character_aliases", "character_looks", "character_facts", "identity_links", "appearances", "scenes",
                 "events", "objects", "object_sightings", "item_characters", "item_cues", "thumbnails", "embeddings",
                 "blocklist", "canaries", "review_log"):
        assert name in t


def test_write_read_roundtrip(pack, tmp_path):
    packdb.write_db(pack, tmp_path / "pack.db", fts_rows(pack))
    back = packdb.read_db(tmp_path / "pack.db")
    assert back == packdb.normalize_pack(pack)
    # rebuild from the JSON text, same content
    js = json.loads(json.dumps(back, ensure_ascii=False))
    packdb.write_db(js, tmp_path / "b.db", fts_rows(js))
    assert packdb.read_db(tmp_path / "b.db") == back
    assert len(packdb.read_fts(tmp_path / "pack.db")) == len(fts_rows(pack))


def test_same_content_same_bytes(pack, tmp_path):
    packdb.write_db(pack, tmp_path / "a.db", fts_rows(pack))
    packdb.write_db(pack, tmp_path / "b.db", fts_rows(pack))
    assert (tmp_path / "a.db").read_bytes() == (tmp_path / "b.db").read_bytes()


def test_foreign_key_violation_rejected(pack, tmp_path):
    pack["character_aliases"].append({"character_id": "c99", "alias": "ghost", "lang": "en", "alias_known_at_s": 1.0,
                                      "generic": 0})
    with pytest.raises(sqlite3.IntegrityError):
        packdb.write_db(pack, tmp_path / "pack.db")
    assert not (tmp_path / "pack.db").exists()


def test_check_constraint_rejected(pack, tmp_path):
    pack["segments"][0]["end_s"] = 130.0      # > 120.5 s
    with pytest.raises(sqlite3.IntegrityError):
        packdb.write_db(pack, tmp_path / "pack.db")


def test_review_state_enum(pack, tmp_path):
    pack["events"][0]["review_state"] = "spike_reviewed"
    with pytest.raises(sqlite3.IntegrityError):
        packdb.write_db(pack, tmp_path / "pack.db")
