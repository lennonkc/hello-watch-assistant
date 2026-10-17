"""Small deterministic stage helpers: thumbnail frame picking, segmentation, cut merging, subtitle parsing, zh rendering extraction, blocklist variants, normalization parity with the shared vocabulary, gate."""
from ingest import textnorm
from ingest.assemble import blocklist_variants, match_quote, nearest_cut, zh_renderings
from ingest.gate import Gate
from ingest.stages.segments import plan_segments
from ingest.stages.shots import union_cuts
from ingest.stages.subtitles import parse_srt
from ingest.stages.thumbs import pick_frames


def test_thumb_is_last_frame_not_after_n():
    pts = [0.0, 0.4, 0.9, 1.3, 2.0, 2.6]
    got = pick_frames(pts, 2.7)
    assert got == {0: 0, 1: 2, 2: 4}          # t1 takes 0.9 (≤1), not the closer 1.3


def test_segments_le_120_and_cover():
    cuts = [float(x) for x in range(7, 900, 13)]
    segs = plan_segments(cuts, 888.0)
    assert segs[0][0] == 0 and segs[-1][1] == 888.0
    assert all(0 < b - a <= 120 for a, b in segs)
    assert all(a2 == b1 for (_, b1), (a2, _) in zip(segs, segs[1:]))


def test_union_cuts_tolerance():
    cuts, extra = union_cuts([10.0, 20.0], [10.2, 30.0])
    assert cuts == [10.0, 20.0, 30.0] and extra == [30.0]


def test_parse_srt_multiline_and_bom():
    s = "﻿1\r\n00:01:47,250 --> 00:01:50,500\r\nThis blade\r\nhas a past.\r\n\r\n2\r\n00:02:00,000 --> 00:02:01,000\r\n[thunder]\r\n"
    c = parse_srt(s)
    assert c[0] == {"start_s": 107.25, "end_s": 110.5, "text": "This blade has a past.", "kind": "dialogue"}
    assert c[1]["kind"] == "sfx"


def test_zh_renderings_from_aligned_tracks():
    en = [{"start_s": 1, "end_s": 2, "text": "Good night, Scales."}, {"start_s": 5, "end_s": 6, "text": "Scales!"},
          {"start_s": 8, "end_s": 9, "text": "scales of fish"}]
    zh = [{"start_s": 1, "end_s": 2, "text": "晚安,史科尔斯"}, {"start_s": 5, "end_s": 6, "text": "史科尔斯!"}]
    assert zh_renderings("Scales", en, [zh]) == ["史科尔斯"]
    assert zh_renderings("Scales", en[:1], [zh]) == []          # a single supporting cue is not enough to infer


def test_match_quote_links_sentences():
    cues = [{"cue_id": "a", "start_s": 457.8, "end_s": 460.5, "text": "These are dragon lands, Sintel."},
            {"cue_id": "b", "start_s": 460.85, "end_s": 464.0, "text": "You are closer than you know."}]
    got = match_quote("These are dragon lands, Sintel. You are closer than you know.", cues, 458.0)
    assert [c["cue_id"] for c in got] == ["a", "b"]


def test_blocklist_variants_rules():
    v = blocklist_variants("Gustav Göransson")
    assert "Gustav Göransson" in v and "Gustav Goransson" in v and "Göransson" in v and "Gustav" in v
    assert "Lee" not in blocklist_variants("Joseph C. Lee")          # <5 chars
    assert "Brown" not in blocklist_variants("Sarah Brown")          # common word


def test_nearest_cut_window():
    assert nearest_cut([10.0, 20.0], 11.0, 9.5, 12.5) == 10.0
    assert nearest_cut([10.0, 20.0], 15.0, 13.5, 16.5) is None


def test_normalize_matches_shared_vectors():
    rows = [ln.split("\t") for ln in (textnorm.WORDLISTS_DIR / "normalize_vectors.tsv").read_text(encoding="utf-8").splitlines()
            if ln and not ln.startswith("#")]
    assert rows
    for src, folded, kept in rows:
        assert textnorm.normalize(src) == folded
        assert textnorm.normalize(src, True) == kept


def test_term_matching_semantics():
    assert textnorm.term_matches("Scales", "Good night, Scales's friend", "word", True)
    assert not textnorm.term_matches("Scales", "tan scales", "word", True)
    assert textnorm.term_matches("小鳞", "晚安  小鱗", "word", False)          # traditional → simplified
    assert textnorm.term_matches(r"\bscar(s|red)?\b", "a SCARRED wing", "regex", False)
    assert not textnorm.term_matches(r"\bscar(s|red)?\b", "scary", "regex", False)


def test_gate_never_returns_items_beyond_margin(pack):
    g = Gate(pack)
    for P in [x / 2 for x in range(0, 201)]:
        e = g.eligible(P)
        assert all(c["end_s"] + 1 <= P for c in e["cues"])
        assert all(x["reveal_at_s"] + 3 <= P for x in e["events"])
        assert all(x["reveal_at_s"] + 1 <= P for x in e["identity_links"] + e["character_facts"])
        assert all(c["first_appears_s"] + 2 <= P for c in e["characters"])
        names = [t for _, _, f, t in g.texts(P) if f == "canonical_name"]
        if P < 11:
            assert "Bob" not in names


def test_gate_excludes_unreviewed(pack):
    pack["identity_links"][0]["review_state"] = "prereviewed"
    assert Gate(pack).eligible(90)["identity_links"] == []
    assert Gate(pack, assume_reviewed=True).eligible(90)["identity_links"]
