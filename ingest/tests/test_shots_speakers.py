"""v1.1 shot-level understanding / character library / speakers: pure functions and assembly (no model calls)."""
import copy
import json

from ingest import assemble_shots, assemble_speakers
from ingest.checks import run_checks
from ingest.derive import derive_canaries, fts_rows
from ingest.stages import speakers as S
from ingest.stages import vision_shots as V
from ingest.tests.conftest import SHA, mini_pack


# ── Shots / boxes / character library ───────────────────────────────

def _shots(*bounds):
    return [{"shot_id": f"sh{i:03d}", "start_s": a, "end_s": b} for i, (a, b) in enumerate(zip(bounds, bounds[1:]))]


def test_merge_short_into_previous_and_leading_into_next():
    m = V.merge_short(_shots(0.0, 0.4, 3.0, 3.5, 6.0, 6.2))
    assert [(x["start_s"], x["end_s"], x["shot_ids"]) for x in m] == [
        (0.0, 3.5, ["sh000", "sh001", "sh002"]), (3.5, 6.2, ["sh003", "sh004"])]
    assert all(x["end_s"] - x["start_s"] >= 1.0 for x in m)


def test_merge_short_all_tiny_keeps_one():
    m = V.merge_short(_shots(0.0, 0.3, 0.6))
    assert [(x["start_s"], x["end_s"]) for x in m] == [(0.0, 0.6)]


def test_keyframe_inside_shot_nearest_mid():
    avail = set(range(0, 100))
    assert V.keyframe(10.2, 11.4, avail) == 11
    assert V.keyframe(10.0, 11.0, avail) == 10           # tN = pts ≤ N: N = the start itself is usable
    assert V.keyframe(10.0, 20.0, avail) == 15
    assert V.keyframe(10.2, 10.9, avail) is None
    assert V.keyframe(10.0, 14.0, {13}) == 13


def test_parse_box_normalizes_and_rejects():
    assert V.parse_box([100, 200, 300, 400]) == [100, 200, 300, 400]
    assert V.parse_box([300, 400, 100, 200]) == [100, 200, 300, 400]
    assert V.parse_box(["5", -3, 1200, 50.4]) == [5, 0, 1000, 50]
    assert V.parse_box([1, 2, 3]) is None
    assert V.parse_box([100, 100, 105, 400]) is None
    assert V.parse_box("bad") is None
    assert V.parse_box([None, 1, 2, 3]) is None


def test_position_from_box():
    assert V.position_from_box([0, 0, 500, 300]) == "left"
    assert V.position_from_box([0, 400, 500, 600]) == "center"
    assert V.position_from_box([0, 700, 500, 1000]) == "right"
    assert V.position_from_box([0, 0, 50, 50]) == "background"
    assert V.position_from_box(None) is None


def test_exemplar_candidates_respect_first_appearance_and_looks():
    ch = {"ref": "c01", "first": 20.0, "aps": [(10.0, 40.0), (60.0, 80.0)],
          "looks": [{"look_id": "l001", "from_s": 20.0, "to_s": 65.0}, {"look_id": "l002", "from_s": 65.0, "to_s": None}]}
    cands = V.exemplar_candidates(ch, set(range(0, 200)), story_end=75.0, k=8)
    assert all(c["t_s"] >= 20 for c in cands)
    assert {c["look_id"] for c in cands} == {"l001", "l002"}
    assert all(c["t_s"] < 65 for c in cands if c["look_id"] == "l001")
    assert all(65 <= c["t_s"] < 75 for c in cands if c["look_id"] == "l002")


def test_pick_exemplars_per_look_gap_and_fallback():
    cands = [{"t_s": t, "look_id": "l1"} for t in (10, 12, 20, 30, 40)]
    verdicts = [{"k": 1, "visible": True, "clarity": 0.9, "box_2d": [0, 0, 500, 500]},
                {"k": 2, "visible": True, "clarity": 0.95, "box_2d": [0, 0, 500, 500]},   # < 5 s from k1
                {"k": 3, "visible": True, "clarity": 0.8, "box_2d": [0, 0, 500, 500], "others_similar": True},
                {"k": 4, "visible": False, "clarity": 1.0, "box_2d": [0, 0, 500, 500]},
                {"k": 5, "visible": True, "clarity": 0.7, "box_2d": [0, 0, 500, 500]}]
    got = V.pick_exemplars(cands, verdicts)
    assert [x["t_s"] for x in got] == [12, 20, 40]
    low = V.pick_exemplars(cands[:1], [{"k": 1, "visible": True, "clarity": 0.2, "box_2d": [0, 0, 500, 500]}])
    assert [x["t_s"] for x in low] == [10]                # keep at least one
    assert V.pick_exemplars(cands[:1], [{"k": 1, "visible": True, "clarity": 0.9}]) == []   # no box, not kept


def test_best_exemplar_prefers_current_look():
    exs = [{"t_s": 30, "look_from": 20.0, "clarity": 0.6, "box": [0, 0, 1, 1]},
           {"t_s": 90, "look_from": 80.0, "clarity": 0.9, "box": [0, 0, 1, 1]}]
    assert V.best_exemplar(exs, 50)["t_s"] == 30
    assert V.best_exemplar(exs, 100)["t_s"] == 90
    assert V.best_exemplar(exs, 5)["t_s"] == 90          # none started yet: take the clearest
    assert V.best_exemplar([], 5) is None


def test_clean_shot_maps_unknown_refs_to_other():
    item = {"caption_en": " {c01} runs. ", "caption_zh": "", "characters": [
        {"ref": "c09", "descriptor_en": "a guard", "position": "left", "box_2d": [0, 0, 500, 300]},
        {"ref": "c01", "descriptor_en": "girl", "position": "nowhere", "box_2d": [0, 700, 500, 1000]}],
        "objects": [{"ref": "o01", "name_en": "sword", "position": "center"}, {"ref": "zz", "name_en": " "}]}
    got = V.clean_shot(item, {"c01"}, {"o01"})
    assert got["caption_en"] == "{c01} runs." and got["caption_zh"] is None
    assert [c["ref"] for c in got["characters"]] == ["other", "c01"]
    assert got["characters"][1]["position"] == "right"
    assert [o["ref"] for o in got["objects"]] == ["o01"]


# ── Speakers ────────────────────────────────────────────────────────

def test_parse_words_repairs_offsets():
    raw = {"steps": [{"content": [{"annotations": [
        {"type": "word_info", "start_index": 0, "start_offset": "107s", "end_offset": "7.300s", "text": "This", "speaker": "spk_1"},
        {"type": "word_info", "start_index": 5, "start_offset": "7.300s", "end_offset": "8s", "text": "blade", "speaker": "spk_1"},
        {"type": "word_info", "start_index": 11, "start_offset": "111.6s", "end_offset": "111.7s", "text": "It", "speaker": "spk_2"}]}]}]}
    ws = S.parse_words(raw)
    assert [w["s"] for w in ws] == [107.0, 107.0, 111.6]
    assert all(w["e"] >= w["s"] for w in ws)
    assert [w["spk"] for w in ws] == ["spk_1", "spk_1", "spk_2"]


def test_assign_diar_majority_overlap():
    words = [{"s": 1.0, "e": 1.5, "spk": "a"}, {"s": 1.5, "e": 3.0, "spk": "b"}, {"s": 10, "e": 11, "spk": "a"}]
    cues = [{"cue_id": "x1", "start_s": 1.0, "end_s": 3.0}, {"cue_id": "x2", "start_s": 20.0, "end_s": 21.0}]
    assert S.assign_diar(cues, words) == {"x1": "b", "x2": None}


def test_windows_and_batches():
    cues = [{"cue_id": f"q{i}", "start_s": t, "end_s": t + 1} for i, t in enumerate((10, 13, 30, 31, 100))]
    ws = S.windows(cues, dur=105.0)
    assert [(w["start_s"], w["end_s"], len(w["cues"])) for w in ws] == [(9.0, 15.0, 2), (29.0, 33.0, 2), (99.0, 102.0, 1)]
    assert [len(b) for b in S.batches(ws, max_lines=3)] == [1, 2]
    assert [len(b) for b in S.batches(ws, max_s=6)] == [1, 1, 1]


def test_vote_and_decide():
    visual = {"a": ("c01", 0.9), "b": ("c01", 0.8), "c": (None, 0.3), "d": ("c02", 0.9), "e": ("c01", 0.5), "f": ("c02", 0.4)}
    diar = {"a": "S1", "b": "S1", "c": "S1", "d": "S2", "e": "S3", "f": "S3"}
    cl = S.vote(visual, diar)
    assert cl["S1"][0] == "c01" and cl["S1"][2] == 2
    assert "S2" not in cl                                 # only one vote
    assert "S3" not in cl                                 # split: share below 0.6
    assert S.decide("c01", 0.9, cl["S1"])[2] == "diarization+visual"
    assert S.decide(None, 0.3, cl["S1"])[:1] == ("c01",)
    assert S.decide(None, 0.3, cl["S1"])[2] == "diarization-vote"
    assert S.decide("c02", 0.95, cl["S1"])[0] == "c02"   # strong visual evidence overrides the voice cluster
    assert S.decide("c02", 0.5, cl["S1"])[0] == "c01"
    assert S.decide(None, 0.2, None) == (None, 0.2, "visual")
    assert S.decide("c02", 0.5, cl["S1"], plausible=False) == ("c02", 0.5, "visual")   # cluster's character not nearby: no override
    assert S.decide(None, 0.3, cl["S1"], plausible=False)[0] is None
    ros = [{"ref": "c01", "aps": [(100.0, 120.0)]}]
    assert S.near_appearance(ros, "c01", {"start_s": 135.0, "end_s": 136.0})
    assert not S.near_appearance(ros, "c01", {"start_s": 150.0, "end_s": 151.0})


def test_inherit_other_tracks():
    ref = [{"cue_id": "en:1", "start_s": 1.0, "end_s": 3.0}]
    other = [{"cue_id": "zh:1", "start_s": 1.1, "end_s": 3.1}, {"cue_id": "zh:2", "start_s": 5.0, "end_s": 6.0}]
    got = S.inherit(other, ref, {"en:1": {"cue_id": "en:1", "character_id": "c01", "method": "visual"}})
    assert set(got) == {"zh:1"} and got["zh:1"]["method"] == "visual+track_align" and got["zh:1"]["character_id"] == "c01"
    assert got["zh:1"]["cue_id"] == "zh:1"


# ── Assembly + checks ───────────────────────────────────────────────

class FakeAsm:
    def __init__(self, st):
        self.pack = copy.deepcopy(mini_pack())
        self.pack["canaries"] = [k for k in self.pack["canaries"] if k["kind"] in ("identity_clue", "event")]
        self.st = st
        self.aux = {"name_renderings": {}}
        self.reg = {"asset_sha256": SHA}

    def people(self):
        return [("Jane Realperson", "credits")]


def _vs():
    sh = lambda a, b, cap, zh, chars, objs=(), kf=None: {  # noqa: E731
        "segment_id": "seg00", "k": 1, "start_s": a, "end_s": b, "shot_ids": ["sh000"], "keyframe_t": kf if kf is not None else int(a),
        "caption_en": cap, "caption_zh": zh, "characters": list(chars), "objects": list(objs)}
    ch = lambda ref, d="someone", box=(0, 0, 500, 500), act=None: {  # noqa: E731
        "ref": ref, "descriptor_en": d, "descriptor_zh": None, "position": "center", "box": list(box) if box else None,
        "action_en": act, "action_zh": None}
    return {"model": "m", "fps": 2.0,
            "exemplars": [{"character_id": "c02", "t_s": 4, "look_id": "l002", "box": [0, 0, 500, 500], "clarity": 0.9},
                          {"character_id": "c02", "t_s": 8, "look_id": "l002", "box": [0, 0, 500, 500], "clarity": 0.9},
                          {"character_id": "c01", "t_s": 3, "look_id": "l002", "box": [0, 0, 500, 500], "clarity": 0.9}],
            "shots": [
                sh(6.0, 8.0, "{c02} waves at {c01}.", "{c02}向{c01}挥手。", [ch("c02", act="{c02} waves"), ch("c01")]),
                sh(8.0, 9.5, "Bob smiles.", "鲍勃笑了。", [ch("c02")]),                     # name too early: replaced with a descriptor
                sh(56.0, 58.0, "{c03} prowls. It lets out a howl.", "{c03}徘徊。", [ch("c03"), ch("c02", "boy")]),  # clue sentence removed; b not attached before its reveal
                sh(58.0, 59.0, "A howl.", None, [ch("c03")]),                               # whole caption hits: deferred
                sh(70.0, 72.0, "Jane Realperson waves.", None, [ch("c01")]),               # real person's name: dropped
                sh(73.0, 75.0, "{c01} holds a lamp.", None, [ch("c01")],
                   [{"ref": None, "name_en": "lamp", "name_zh": "灯", "position": "left", "box": [10, 10, 200, 200]}]),
            ]}


def test_assemble_shots_rows_and_guards(monkeypatch):
    asm = FakeAsm({"vision_shots": _vs()})
    monkeypatch.setattr(assemble_shots, "reviewer_canaries",
                        lambda a: [k for k in derive_canaries(a.pack) if k["kind"] in ("identity_clue", "event")])
    assemble_shots.rows(asm)
    ex = asm.pack["character_exemplars"]
    assert [(e["character_id"], e["t_s"]) for e in ex] == [("c01", 3), ("c02", 8)]   # c02 t=4 is before its first appearance at 5 s
    assert ex[1]["look_id"] == "l002" and ex[0]["look_id"] is None                     # look belongs to another character → nulled
    assert json.loads(ex[0]["bbox"]) == [0, 0, 500, 500]
    caps = {c["start_s"]: c for c in asm.pack["shot_captions"]}
    assert caps[6.0]["caption_en"] == "The boy waves at Alice."                         # Bob's name visible only at 10 s; Alice is public
    assert caps[6.0]["caption_zh"] == "男孩向爱丽丝挥手。"
    assert caps[8.0]["caption_en"] == "The boy smiles." and caps[8.0]["caption_zh"] == "男孩笑了。"
    assert caps[56.0]["caption_en"] == "The wolf prowls." and caps[56.0]["reveal_at_s"] == 58.0
    assert caps[58.0]["caption_en"] == "A howl." and caps[58.0]["reveal_at_s"] >= 61.5
    assert 70.0 not in caps
    assert all(c["reveal_at_s"] >= c["end_s"] for c in caps.values())
    ents = [e for e in asm.pack["shot_entities"] if e["shot_caption_id"] == caps[56.0]["shot_caption_id"]]
    assert [(e["character_id"], e["label_en"]) for e in ents] == [("c03", "the wolf"), (None, "boy")]
    e6 = [e for e in asm.pack["shot_entities"] if e["shot_caption_id"] == caps[6.0]["shot_caption_id"]]
    assert e6[0]["label_en"] == "the boy" and e6[0]["action_en"] == "The boy waves" and e6[0]["bbox_t_s"] == 6
    lamp = [e for e in asm.pack["shot_entities"] if e["entity_type"] == "object"]
    assert lamp[0]["label_en"] == "lamp" and lamp[0]["position"] == "left"
    # new tables go through checks: no early-name (3), identity-clue (6), real-name (9) or canary (11) issues
    asm.pack["canaries"] = derive_canaries(asm.pack)
    rep = run_checks(asm.pack, fts_rows(asm.pack))
    for c in rep["checks"]:
        if c["id"] in (3, 6, 9, 11):
            assert not [i for i in c["issues"] if "shot_caption" in i["msg"]], c


def test_assemble_speakers_rows():
    sp = {"cues": [{"cue_id": "en.official:001", "character_id": "c02", "diar_label": "spk_1", "confidence": 0.9,
                    "method": "diarization+visual"},
                   {"cue_id": "en.official:002", "character_id": "c02", "diar_label": "spk_1", "confidence": 0.7,
                    "method": "diarization-vote"},                                  # 60 s < reveal 60.5, b has no appearance coverage → nulled
                   {"cue_id": "missing", "character_id": "c01", "diar_label": None, "confidence": 1, "method": "visual"}]}
    asm = FakeAsm({"speakers": sp})
    assemble_speakers.rows(asm)
    got = {r["cue_id"]: r for r in asm.pack["cue_speakers"]}
    assert set(got) == {"en.official:001", "en.official:002"}
    assert got["en.official:001"]["character_id"] == "c02" and got["en.official:001"]["review_state"] == "auto"
    assert got["en.official:002"]["character_id"] is None and got["en.official:002"]["method"].endswith("+detached")
    assert asm.pack["cues"][0]["speaker_raw"] == "spk_1"


def test_effective_view_applies_patch_aliases_and_edits(monkeypatch):
    vs = {"model": "m", "fps": 2.0, "exemplars": [{"character_id": "c03", "t_s": 56, "look_id": "l003", "box": [0, 0, 500, 500],
                                                   "clarity": 0.9}],
          "shots": [{"segment_id": "seg00", "k": 1, "start_s": 6.0, "end_s": 8.0, "shot_ids": ["sh000"], "keyframe_t": 6,
                     "caption_en": "Bobby runs.", "caption_zh": "鲍比在跑。",
                     "characters": [{"ref": "c02", "descriptor_en": "boy", "descriptor_zh": None, "position": "left",
                                     "box": None, "action_en": None, "action_zh": None}], "objects": []}]}
    ops = [{"op": "add", "table": "character_aliases", "row": {"character_id": "c02", "alias": "鲍比", "lang": "zh",
                                                              "alias_known_at_s": 10.0, "generic": 0}},
           {"op": "add", "table": "character_aliases", "row": {"character_id": "c02", "alias": "Bobby", "lang": "en",
                                                              "alias_known_at_s": 10.0, "generic": 0}},
           {"op": "edit", "table": "characters", "key": {"character_id": "c02"}, "set": {"display_label_pre_name": "the kid"}},
           {"op": "edit", "table": "characters", "key": {"character_id": "c03"}, "set": {"first_appears_s": 57.0}}]
    asm = FakeAsm({"vision_shots": vs})
    monkeypatch.setattr(assemble_shots, "patch_ops", lambda a: ops)
    assemble_shots.rows(asm)
    cap = asm.pack["shot_captions"][0]
    assert cap["caption_en"] == "The kid runs." and cap["caption_zh"] == "男孩在跑。"
    assert asm.pack["shot_entities"][0]["label_en"] == "the kid"
    assert asm.pack["character_exemplars"] == []          # patch moves first appearance to 57 s, so the 56 s exemplar is dropped


def test_verified_and_circle(tmp_path):
    picks = [{"t_s": 1}, {"t_s": 2}, {"t_s": 3}, {"t_s": 4}]
    vs = [{"k": 1, "circled_is_target": True, "other_inside": False, "confidence": 0.9},
          {"k": 2, "circled_is_target": True, "other_inside": True, "confidence": 0.9},     # circled is someone else riding on the target
          {"k": 3, "circled_is_target": False, "other_inside": False, "confidence": 0.9},
          {"k": 4, "circled_is_target": True, "other_inside": False, "confidence": 0.3}]
    assert [x["t_s"] for x in V.verified(picks, vs)] == [1]
    two = [{"t_s": 1, "clarity": 0.6}, {"t_s": 2, "clarity": 0.9}]
    crowded = [{"k": k, "circled_is_target": True, "other_inside": True, "confidence": 0.9} for k in (1, 2)]
    assert [x["t_s"] for x in V.verified(two, crowded)] == [2]                          # none pass: keep the clearest
    from PIL import Image
    p = tmp_path / "t.jpg"
    Image.new("RGB", (640, 360), (0, 0, 0)).save(p)
    out = Image.open(__import__("io").BytesIO(V.circled_jpeg(p, [250, 250, 750, 750])))
    assert out.size == (640, 360) and out.getpixel((320, 180)) == (0, 0, 0)
    assert max(out.getpixel((x, 180))[0] for x in range(100, 200)) > 150                  # left edge of the circle is red
