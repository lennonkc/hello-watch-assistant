"""Sintel semantic-localization PoC: for one set of "where is it" questions, compare top-3 hit rates of vector / full-text only / hybrid, and verify every result is eligible.

Usage: `python -m ingest.retrieval_poc <pack.db> [--allow-auto] [--text-only] [--json out.json]` (query embedding uses `CEDAR_GEMINI_BACKEND`).
Hit: any top-3 item's time span overlaps the gold span (widened 3 s at each end). Each question is tested at two P values: just after the gold span becomes visible (+8 s) and at the end of the story (745 s).
Questions are deliberately paraphrased (not copied from scene summaries) and mix Chinese and English; full-text search of English items is nearly useless for Chinese questions, which is exactly the gap vectors fill.
"""
from __future__ import annotations

import argparse
import json
import sys

from ingest import embed, retrieval

TOL = 3.0
P_END = 745.0
# (question, gold span [a, b]) — hand-labelled from Sintel event / scene times
QUESTIONS = [
    ("where does she get jumped by a bald guy in the snow", (42.0, 73.0)),
    ("她在暴风雪里倒下是哪一段", (97.0, 101.0)),
    ("a warm light from a lonely hut in the mountains", (83.0, 86.0)),
    ("the old man feeds her something hot", (124.0, 137.0)),
    ("when does she tell him what she is looking for", (143.0, 145.0)),
    ("她在城里的小巷翻找食物", (160.0, 174.0)),
    ("finding a hurt little dragon up on a roof", (182.0, 207.0)),
    ("when she gives the creature a name", (228.0, 232.3)),
    ("the little dragon chasing a chicken", (250.0, 251.0)),
    ("小龙学会飞的那段", (273.0, 307.5)),
    ("大龙从天上把小龙抓走", (316.0, 323.0)),
    ("crossing sand dunes and a bamboo forest", (353.0, 386.0)),
    ("fighting a striped animal in a canyon", (386.0, 392.0)),
    ("老人叫出她名字的时候", (457.8, 464.0)),
    ("the huge tree with a cave under it", (474.5, 488.0)),
    ("the beast breathes fire at her", (543.0, 544.0)),
    ("she stabs the monster with her weapon", (602.0, 604.0)),
    ("她认出那条龙就是自己养大的小龙", (621.6, 624.0)),
    ("她发现自己变老了", (641.0, 646.0)),
    ("sitting sadly on a cliff by the tree", (680.0, 733.0)),
]
MODES = ("vector", "fts", "hybrid")


def is_hit(h, gold) -> bool:
    a, b = gold
    return h.start_s <= b + TOL and h.end_s >= a - TOL


def run(db, allow_auto: bool = False, k: int = 3, embed_query=None, types=None) -> dict:
    rows, violations = [], 0
    for q, gold in QUESTIONS:
        qvec = (embed_query or retrieval.query_vector)(q)
        for tag, P in (("just_after", gold[1] + 8.0), ("story_end", P_END)):
            rec = {"q": q, "gold": gold, "P": P, "at": tag}
            for mode in MODES:
                hits = retrieval.search(db, P, q, k=k, mode=mode, allow_auto=allow_auto, qvec=qvec, types=types)
                violations += sum(1 for h in hits if h.eligible_from > P + 1e-9)
                rec[mode] = {"hit": any(is_hit(h, gold) for h in hits),
                             "top": [f"{h.item_type}:{h.item_id}@{h.start_s:.0f}-{h.end_s:.0f}" for h in hits]}
            rows.append(rec)
    summary = {}
    for tag in ("just_after", "story_end", "all"):
        sel = [r for r in rows if tag == "all" or r["at"] == tag]
        summary[tag] = {m: f"{sum(r[m]['hit'] for r in sel)}/{len(sel)}" for m in MODES}
    zh = [r for r in rows if any("一" <= ch <= "鿿" for ch in r["q"])]
    summary["zh_questions"] = {m: f"{sum(r[m]['hit'] for r in zh)}/{len(zh)}" for m in MODES}
    return {"k": k, "allow_auto": allow_auto, "types": sorted(types) if types else "all", "summary": summary,
            "gate_violations": violations, "rows": rows}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("db")
    ap.add_argument("--allow-auto", action="store_true")
    ap.add_argument("--text-only", action="store_true", help="exclude video_window items")
    ap.add_argument("--json")
    a = ap.parse_args(argv)
    res = run(a.db, a.allow_auto, types=set(embed.TEXT_TYPES) if a.text_only else None)
    if a.json:
        with open(a.json, "w", encoding="utf-8") as f:
            json.dump(res, f, ensure_ascii=False, indent=1)
    print(json.dumps({k: res[k] for k in ("k", "allow_auto", "types", "summary", "gate_violations")}, ensure_ascii=False, indent=1))
    return 0 if res["gate_violations"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
