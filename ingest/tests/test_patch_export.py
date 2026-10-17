"""Review patch semantics, master / device export, versioning and idempotency."""
import json

import pytest
from PIL import Image

from ingest import build, util
from ingest.patch import PatchError, apply_patch
from ingest.tests.conftest import SHA


def patch(reviewer, *ops):
    return {"format": "cedar-review-patch/1", "asset_sha256": SHA, "reviewer": reviewer, "created_at": "2026-10-14T00:00:00Z",
            "ops": list(ops)}


def test_claude_prereview_sets_prereviewed_and_logs(pack):
    pack["events"][0]["review_state"] = "auto"
    out = apply_patch(pack, patch("claude-prereview", {"op": "edit", "table": "events", "key": {"event_id": "e001"},
                                                       "set": {"reveal_at_s": 12.5}, "why": "w", "seconds_spent": 3}))
    e = next(x for x in out["events"] if x["event_id"] == "e001")
    assert e["review_state"] == "prereviewed" and e["reveal_at_s"] == 12.5
    logs = [r for r in out["review_log"] if r["item_id"] == "e001"]
    assert {r["field"] for r in logs} == {"reveal_at_s", "review_state"}
    assert all(r["reviewer"] == "claude-prereview" for r in logs)


def test_prereview_never_downgrades_human(pack):
    out = apply_patch(pack, patch("claude-prereview", {"op": "approve", "table": "events", "key": {"event_id": "e001"}}))
    assert next(x for x in out["events"] if x["event_id"] == "e001")["review_state"] == "reviewed"


def test_human_sets_reviewed(pack):
    pack["character_facts"][0]["review_state"] = "prereviewed"
    out = apply_patch(pack, patch("reviewer-2", {"op": "approve", "table": "character_facts", "key": {"fact_id": "f001"}}))
    assert out["character_facts"][0]["review_state"] == "reviewed"


@pytest.mark.parametrize("bad", [
    {"op": "edit", "table": "events", "key": {"event_id": "e001"}, "set": {"review_state": "reviewed"}},
    {"op": "approve", "table": "events", "key": {"event_id": "nope"}},
    {"op": "add", "table": "canaries", "row": {"term": "x", "lang": "en", "match": "word", "case_sensitive": 0,
                                                "active_until_s": 1, "kind": "name", "ref_type": "character", "ref_id": "c01"}},
    {"op": "approve", "table": "review_log", "key": {"log_id": 1}},
])
def test_invalid_ops_rejected(pack, bad):
    with pytest.raises(PatchError):
        apply_patch(pack, patch("reviewer-1", bad))


def test_reviewer_must_be_role_id(pack):
    with pytest.raises(PatchError):
        apply_patch(pack, patch("Jane Doe", {"op": "approve", "table": "events", "key": {"event_id": "e001"}}))


@pytest.fixture
def workdir(pack, tmp_path, monkeypatch):
    monkeypatch.setattr(util, "CACHE", tmp_path)
    wd = util.work_dir(SHA)
    th = wd / "thumbs"
    th.mkdir(parents=True)
    rows = []
    for t in (0, 1):
        p = th / f"t{t:05d}.jpg"
        Image.new("RGB", (64, 36), (t * 90, 40, 40)).save(p, "JPEG", quality=70)
        rows.append({"t_s": t, "path": f"thumbs/{p.name}", "width": 64, "height": 36, "bytes": p.stat().st_size})
    pack["thumbnails"] = rows
    pack["events"][0]["review_state"] = "auto"
    pack["pack_meta"] = {}
    util.write_json(wd / "auto" / "rows.json", pack)
    return wd


def test_export_idempotent_and_versioned(workdir, tmp_path):
    r1 = build.export_all(SHA)
    b1 = (workdir / "device" / "pack.db").read_bytes(), (workdir / "master" / "MANIFEST.json").read_bytes()
    r2 = build.export_all(SHA)
    b2 = (workdir / "device" / "pack.db").read_bytes(), (workdir / "master" / "MANIFEST.json").read_bytes()
    assert r1["pack_version"] == r2["pack_version"] == 1 and b1 == b2
    pf = tmp_path / "p.json"
    pf.write_text(json.dumps(patch("claude-prereview", {"op": "approve", "table": "events", "key": {"event_id": "e001"}})))
    r3 = build.apply_patch(SHA, pf)
    assert r3["pack_version"] == 2
    assert build.export_all(SHA)["pack_version"] == 2


def test_device_pack_strips_review_log_and_unreviewed(workdir):
    res = build.export_all(SHA)
    m = json.loads((workdir / "master" / "pack.json").read_text())
    d = json.loads((workdir / "device" / "pack.json").read_text())
    assert m["pack_meta"]["distribution"] == "master" and d["pack_meta"]["distribution"] == "device"
    assert m["review_log"] and d["review_log"] == []
    assert "e001" in {e["event_id"] for e in m["events"]}
    assert "e001" not in {e["event_id"] for e in d["events"]}              # auto rows stay out of device
    assert not any(x["item_id"] == "e001" for x in d["item_characters"] + d["item_cues"])
    assert res["review_complete"] is False                                   # auto items → coverage check fails
    man = json.loads((workdir / "device" / "MANIFEST.json").read_text())
    assert {f["path"] for f in man["files"]} >= {"pack.db", "pack.json", "thumbs/t00000.jpg"}
    assert all(len(f["sha256"]) == 64 for f in man["files"])
    import sqlite3
    for dist, has in (("device", False), ("master", True)):
        con = sqlite3.connect(workdir / dist / "pack.db")
        names = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        con.close()
        assert ("review_log" in names) is has                                 # device drops even the empty table


def test_review_complete_when_all_checks_pass(workdir):
    rows = json.loads((workdir / "auto" / "rows.json").read_text())
    rows["events"][0]["review_state"] = "reviewed"
    util.write_json(workdir / "auto" / "rows.json", rows)
    res = build.export_all(SHA)
    assert res["review_complete"] is True, res["checks_failed"]
    assert json.loads((workdir / "device" / "MANIFEST.json").read_text())["review_complete"] is True


def test_review_ui_patch_shape_applies(pack):
    """Op shape exported by the review UI (with ts / seconds_spent / spoiler_critical; why is optional)."""
    pack["identity_links"][0]["review_state"] = "prereviewed"
    ui = {"format": "cedar-review-patch/1", "asset_sha256": SHA, "base_pack_version": 1, "reviewer": "reviewer-1",
          "created_at": "2026-10-14T07:35:40Z", "seconds_total": 34,
          "ops": [{"op": "approve", "table": "identity_links", "key": {"link_id": "k01"}, "spoiler_critical": 1,
                   "seconds_spent": 1.2, "ts": "2026-10-14T07:35:36Z"},
                  {"op": "edit", "table": "events", "key": {"event_id": "e002"}, "why": "t", "spoiler_critical": 1,
                   "seconds_spent": 0, "ts": "2026-10-14T07:35:36Z", "set": {"reveal_at_s": 62.5}},
                  {"op": "approve", "table": "title_info", "key": {"field": "title"}, "spoiler_critical": 0,
                   "seconds_spent": 0, "ts": "2026-10-14T07:35:36Z"},
                  {"op": "add", "table": "canaries", "row": {"term": "狼人", "lang": "zh", "match": "word", "case_sensitive": 0,
                   "active_until_s": 0, "kind": "identity_clue", "ref_type": "identity_link", "ref_id": "k01"}, "why": "x"}]}
    out = apply_patch(pack, ui)
    assert out["identity_links"][0]["review_state"] == "reviewed"
    assert any(r["item_type"] == "identity_link" and r["spoiler_critical"] == 1 and r["seconds_spent"] == 1.2
               for r in out["review_log"])
    assert any(k["term"] == "狼人" for k in out["canaries"])
