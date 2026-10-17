"""Progressive recaps: checkpoints, past-only inputs, name visibility, post-generation recheck and retry."""
import types

from ingest import recaps


def test_checkpoints_one_per_story_scene(pack):
    cps = recaps.checkpoints(pack)
    assert [(c["scene_id"], c["upto_s"], c["reveal_at_s"]) for c in cps] == [("s01", 50.0, 50.0), ("s02", 100.0, 100.0)]


def test_unsnapped_scene_reveal_shifted(pack):
    pack["scenes"][0]["snapped"] = 0
    assert recaps.checkpoints(pack)[0]["reveal_at_s"] == 52.0


def test_inputs_only_from_the_past(pack):
    inp = recaps.inputs_at(pack, 50.0)
    assert [t for _, t in inp["scenes"]] == ["Alice meets Bob."]
    assert [t for _, t in inp["events"]] == ["Alice greets Bob."]
    assert [t for _, t in inp["dialogue"]] == ["Hello, Bob."]
    assert "wolf" not in recaps.prompt_for(pack, recaps.checkpoints(pack)[0]).lower()


def test_character_refs_follow_name_visibility(pack):
    assert recaps.character_refs(pack, 8.0) == [{"en": "Alice", "zh": "爱丽丝"}, {"en": "the boy", "zh": "男孩"}]
    assert recaps.character_refs(pack, 12.0)[1] == {"en": "Bob", "zh": "鲍勃"}


def test_violations(pack):
    assert recaps.violations(pack, "Then Bob waves.", 5.0) == ["Bob"]
    assert recaps.violations(pack, "Then Bob waves.", 20.0) == []
    assert recaps.violations(pack, "Jane Realperson smiles.", 20.0) == ["Jane Realperson"]


def test_add_recaps_retries_then_keeps_clean_draft(pack, tmp_path, monkeypatch):
    drafts = iter([{"recap_en": "Jane Realperson walks.", "recap_zh": "x"}, {"recap_en": "Alice meets Bob.", "recap_zh": "爱丽丝遇见鲍勃。"},
                   {"recap_en": "Alice meets Bob; a wolf appears.", "recap_zh": "狼出现了。"}])
    calls = []

    def fake_generate(c, prompt):
        calls.append(prompt)
        return next(drafts), {}, 0.1

    monkeypatch.setattr(recaps, "_generate", fake_generate)
    monkeypatch.setattr(recaps.gemini, "client", lambda: None)
    monkeypatch.setattr(recaps, "WORKERS", 1)
    logs = []
    ledger = types.SimpleNamespace(guard=lambda est: None, log=lambda *a, **k: logs.append(a))
    ctx = types.SimpleNamespace(wd=tmp_path, ledger=ledger)
    recaps.add_recaps(ctx, pack)
    assert [r["text_en"] for r in pack["recaps"]] == ["Alice meets Bob.", "Alice meets Bob; a wolf appears."]
    assert "Jane Realperson" in calls[1] and len(calls) == 3
    assert all(r["review_state"] == "auto" for r in pack["recaps"])
    recaps.add_recaps(ctx, pack)          # second run is fully cached
    assert len(calls) == 3
