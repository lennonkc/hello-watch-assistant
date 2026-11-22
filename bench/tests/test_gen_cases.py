"""bench/gen_cases.py on a synthetic 2.5-hour pack: families, determinism, gate consistency, lint, private redaction."""
from __future__ import annotations

import copy
import json

import pytest

from bench import common as C
from bench import gen_cases as G
from bench import judge as J
from ingest import packdb
from ingest.derive import derive_canaries
from ingest.tests.conftest import SHA, mini_pack

R = "reviewed"
ZA, ZB, ZC, ZD = "甲乙丙丁戊己庚辛壬癸子丑寅卯辰巳午未申酉", "金木水火土日月星云雷风雨雪霜露", "东南西北春夏秋冬梅兰竹菊松柏桃李杏梨枣橘", "琴棋书画诗酒茶花鸟鱼虫草山石泉"
WORDS = ("lantern harbour copper violin meadow glacier pepper ribbon saddle thimble walnut falcon cobble anchor "
         "marble orchard canvas pebble kettle tundra quiver velvet bramble cinder").split()


def long_pack() -> dict:
    """Mini pack stretched to 2.5 h: Bob (c02) on screen 100–600 s and named at 600 s; the wolf (c03) is Bob, revealed
    at 4000 s; Carol (c04) named at 5000 s with the alias "the Queen" known at 6000 s; shots every 50 s with boxes,
    an English and a Chinese track, speakers, scenes every 600 s and recaps every 1200 s."""
    p = copy.deepcopy(mini_pack())
    p["asset"].update(duration_s=9000.0, story_start_s=30.0, story_end_s=8700.0, credits_start_s=8700.0,
                      asset_kind="film")
    p["shots"] = [{"shot_id": "sh000", "start_s": 0.0, "end_s": 9000.0}]
    p["segments"] = [{"segment_id": f"seg{i:03d}", "start_s": 120.0 * i, "end_s": 120.0 * (i + 1), "file_sha256": "00" * 32}
                     for i in range(75)]
    ch = {c["character_id"]: c for c in p["characters"]}
    ch["c02"].update(first_appears_s=100.0, name_known_at_s=600.0)
    ch["c03"].update(first_appears_s=2000.0)
    p["characters"].append({**ch["c01"], "character_id": "c04", "canonical_name": "Carol", "name_zh": "卡罗尔",
                            "display_label_pre_name": "the tall woman", "display_label_pre_name_zh": "高个女人",
                            "name_known_at_s": 5000.0, "name_public": 0, "name_source": "dialogue",
                            "first_appears_s": 3000.0})
    p["character_aliases"] = [{"character_id": "c04", "alias": "the Queen", "lang": "en", "alias_known_at_s": 6000.0,
                               "generic": 0}]
    for lk in p["character_looks"]:
        lk["from_s"], lk["reveal_at_s"] = {"c01": (31.0, 32.0), "c02": (100.0, 101.0), "c03": (2000.0, 2001.0)}[
            lk["character_id"]]
    p["character_facts"][0].update(reveal_at_s=700.0)
    p["identity_links"][0].update(reveal_at_s=4000.0)
    p["appearances"] = [{"character_id": c, "start_s": a, "end_s": b, "source": "test", "review_state": R}
                        for c, a, b in (("c01", 31.0, 8700.0), ("c02", 100.0, 600.0), ("c03", 2000.0, 4500.0),
                                        ("c04", 3000.0, 6500.0))]
    p["scenes"] = [{"scene_id": f"s{i:02d}", "start_s": 30.0 + 600 * i, "end_s": 30.0 + 600 * (i + 1),
                    "start_s_model": None, "end_s_model": None, "snapped": 1, "reveal_at_s": 30.0 + 600 * (i + 1),
                    "location_en": "A valley", "summary_en": f"The travellers cross the {WORDS[i]} valley.",
                    "summary_zh": f"旅人穿过第{i}个山谷。", "review_state": R, "source": "test"} for i in range(14)]
    p["events"] = [{**p["events"][0], "t_s": 650.0, "reveal_at_s": 652.0},
                   {**p["events"][1], "t_s": 4000.0, "reveal_at_s": 4002.0}]
    p["item_characters"] = [r for r in p["item_characters"] if r["item_type"] == "event"]
    p["item_cues"] = []
    p["subtitle_tracks"].append({**p["subtitle_tracks"][0], "track": "zh-Hans.official", "lang": "zh-Hans",
                                 "priority": 1})
    cues, zh = [], []
    for i in range(280):
        t = 40.0 + 30 * i
        if t > 8650:
            break
        w = [WORDS[(i * 7 + k) % len(WORDS)] for k in range(2)]
        tag = chr(97 + i % 26) + chr(97 + i // 26)                     # two words only this line has
        cues.append({"cue_id": f"en.official:{i:04d}", "track": "en.official", "start_s": t, "end_s": t + 2.0,
                     "text": f"We need the {w[0]} and the zan{tag} before the vel{tag} turns {w[1]}.",
                     "kind": "dialogue", "speaker_raw": None})
        u1, u2 = ZA[i % 20] + ZB[i // 20], ZC[i % 20] + ZD[i // 20]
        zh.append({"cue_id": f"zh-Hans.official:{i:04d}", "track": "zh-Hans.official", "start_s": t, "end_s": t + 2.0,
                   "text": f"我们得找到{u1}和{u2}才能过河。", "kind": "dialogue", "speaker_raw": None})
    p["cues"] = cues + zh
    p["cue_speakers"] = [{"cue_id": c["cue_id"], "character_id": "c02", "diar_label": "S1", "confidence": 0.9,
                          "method": "test", "review_state": R} for c in cues if 100 <= c["start_s"] < 600]
    shots, ents = [], []
    for i in range(int(8700 // 50)):
        a = 50.0 * i
        sid = f"sc{i:04d}"
        shots.append({"shot_caption_id": sid, "start_s": a, "end_s": a + 50.0, "reveal_at_s": a + 50.0,
                      "caption_en": "A wide shot of the road.", "caption_zh": "道路的远景。", "source": "test",
                      "review_state": R})
        on = [cid for cid, (x, y) in (("c02", (100, 600)), ("c03", (2000, 4500)), ("c04", (3000, 6500)))
              if a < y and a + 50 > x]
        for seq, cid in enumerate(on):
            ents.append({"shot_caption_id": sid, "seq": seq, "entity_type": "character", "character_id": cid,
                         "object_id": None, "label_en": "someone", "label_zh": "某人", "position": "center",
                         "bbox": json.dumps([100, 100 + 300 * seq, 600, 300 + 300 * seq]), "bbox_t_s": a + 1.0,
                         "action_en": None, "action_zh": None})
    p["shot_captions"], p["shot_entities"] = shots, ents
    p["recaps"] = [{"recap_id": f"r{i:02d}", "upto_scene_id": f"s{2 * i + 1:02d}", "upto_s": 30.0 + 1200 * (i + 1),
                    "reveal_at_s": 30.0 + 1200 * (i + 1), "text_en": f"So far the travellers crossed {2 * i + 2} valleys.",
                    "text_zh": f"到目前为止旅人穿过了{2 * i + 2}个山谷。", "source": "test", "review_state": R}
                   for i in range(7)]
    p["canaries"] = [k for k in p["canaries"] if k["kind"] != "name"]
    p["canaries"] = derive_canaries(p)
    return packdb.normalize_pack(p)


@pytest.fixture
def lp(tmp_path, monkeypatch):
    data = long_pack()
    root = tmp_path / "packs"
    packdb.write_db(data, root / SHA / "device-auto" / "pack.db")          # private titles serve device-auto packs
    monkeypatch.setattr(J, "PACKS", root)
    C._PACKS.clear()
    yield data, root
    C._PACKS.clear()


def test_families_and_determinism(lp):
    data, _root = lp
    cases, st = G.generate(data, "mini", "Mini", "abab", n=60, seed=3)
    assert len(cases) == 60 and set(st["families"]) == set("abcdef")
    assert cases == G.generate(copy.deepcopy(data), "mini", "Mini", "abab", n=60, seed=3)[0]
    assert cases != G.generate(data, "mini", "Mini", "abab", n=60, seed=4)[0]
    assert 20 <= st["langs"]["zh"] <= 40 and st["taps"] > 0
    assert len({c["id"] for c in cases}) == 60 and all(c["id"].startswith("gen:abab:") for c in cases)
    assert all(c["set"] == "generated" and not C.validate(c) for c in cases)


def test_cases_agree_with_the_gate(lp):
    data, _root = lp
    pv = C.PackView(data)
    cases, _st = G.generate(data, "mini", "Mini", "abab", n=80)
    by = {}
    for c in cases:
        by.setdefault(c["family"], []).append(c)
    for c in by["a"]:
        assert c["expect"] == "decline" and c["category"] == "spoiler_probe" and data["asset"]["story_start_s"] < c["P"]
    bob = [c for c in by["b"] if {"term": "Bob", "case_sensitive": True} in c["must_not_include"]]
    assert bob and all(100 < c["P"] < 601 and not pv.name_visible("c02", c["P"]) for c in bob)
    carol = [c for c in by["b"] if any(m.get("term") == "Carol" for m in c["must_not_include"])]
    assert carol and any(m.get("term") == "the Queen" for m in carol[0]["must_not_include"])  # alias not known yet
    assert all(c["tap"] is None or (0 < c["tap"]["x"] < 1 and 0 < c["tap"]["y"] < 1) for c in by["b"] + by["c"])
    before = [c for c in by["c"] if c["expect"] == "either"]
    after = [c for c in by["c"] if c["expect"] == "answer"]
    assert before and all(c["P"] < 4001 for c in before) and after and all(c["P"] > 4001 for c in after)
    # the wolf on screen before the reveal: Bob's name is known by then for his own sake → a suspect, not a leak
    assert any({"term": "Bob", "case_sensitive": True} in c["flag_if_any"] for c in before)
    for c in by["d"]:
        assert c["P"] >= 40 and (c["must_include_any"] or c["lang"] == "zh")
        line = next(q for q in data["cues"] if f"ended at {G.hms(q['end_s'])}" in c["reference"])
        assert line["end_s"] + 1.0 <= c["P"] <= line["end_s"] + 2.0
    pool = G.Generator(data, "mini", "Mini", "abab").fam_d()["en"]           # speaker as known at P: label, then name
    assert all("spoken by 'the boy'" in it["ref"][0] for it in pool if 100 < it["P"] < 600)
    assert not any("spoken by 'Bob'" in it["ref"][0] for it in pool if it["P"] < 601)
    for c in by["e"]:
        assert "valleys" in c["reference"] or "山谷" in c["reference"]
    stamps = [c for c in cases if c["P"] >= 3600]
    assert stamps and all(G.BAD_STAMP in c["must_not_include"] for c in stamps)
    assert C.marker_hits(G.BAD_STAMP, "at 62:15 she runs") and not C.marker_hits(G.BAD_STAMP, "at 1:02:15 she runs")
    edges = [c["P"] for c in by["f"]]
    assert 15.0 in edges and any(c > 8700 for c in edges) and any(3600 < c < 3900 for c in edges) \
        and any(7200 < c < 7500 for c in edges)


def test_generated_cases_lint_clean_with_the_served_titles(lp, capsys, tmp_path):
    data, root = lp
    served = [{"key": "pv1", "id": "pv-abababababab", "sha": SHA, "title": "Secret Title", "dist": "device-auto",
               "private": True, "label": "private preview title #1", "duration_s": 9000.0}]
    out = tmp_path / "gen"
    lines = G.generate_served(served, root, out, "private", n=40)
    assert lines[0].startswith("private preview title #1: 40 cases") and "Secret" not in "\n".join(lines)
    cases = C.read_cases([out])
    assert all(c["private"] and c["id"].startswith("gen:pv1:") for c in cases)
    assert J.lint(cases, with_packs=True, titles=J.served_titles(served)) == 0
    assert "0 errors" in capsys.readouterr().out
    assert G.generate_served(served, root, out, "public") == [] and not list(out.glob("*.jsonl"))


def test_private_lint_errors_name_no_pack_text(lp, capsys):
    data, _root = lp
    cases, _st = G.generate(data, "pv-abababababab", "Secret Title", "pv1", n=20, private=True)
    bad = dict(cases[0], must_not_include=[{"term": "travellers"}], title="Wrong", _file="gen-pv1")
    assert J.lint([bad], with_packs=True, titles={"pv-abababababab": {"title": "Secret Title", "duration_s": 9000.0,
                                                                       "shas": [SHA]}}) == 1
    out = capsys.readouterr().out
    assert bad["id"] in out and "travellers" not in out and "Secret" not in out and "Wrong" not in out
