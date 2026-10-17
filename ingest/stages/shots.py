"""Shot detection: PySceneDetect ContentDetector ∪ cuts found only by ffmpeg scdet (deduped within ±0.25 s)."""
from __future__ import annotations

import re
import subprocess

from ingest.stages import Ctx, stage

CONTENT = {"threshold": 27.0, "min_scene_len": 12}
SCDET_THRESHOLD = 10.0
MATCH_TOL = 0.25


def pyscenedetect_cuts(path) -> list[float]:
    from scenedetect import ContentDetector, detect
    scenes = detect(str(path), ContentDetector(**CONTENT), show_progress=False)
    return [round(s[0].get_seconds(), 3) for s in scenes[1:]]


def scdet_cuts(path, threshold: float = SCDET_THRESHOLD) -> list[float]:
    p = subprocess.run(["ffmpeg", "-hide_banner", "-nostats", "-i", str(path), "-an",
                        "-vf", f"scdet=threshold={threshold}:sc_pass=1,metadata=print:file=-", "-f", "null", "-"],
                       check=True, capture_output=True, text=True)
    cuts, cur_t = [], None
    for line in p.stdout.splitlines():
        m = re.match(r"frame:\d+\s+pts:\d+\s+pts_time:([\d.]+)", line)
        if m:
            cur_t = float(m.group(1))
            continue
        m = re.match(r"\s*lavfi\.scd\.score=([\d.]+)", line)
        if m and cur_t is not None and float(m.group(1)) >= threshold:
            if not cuts or cur_t - cuts[-1] >= 0.5:
                cuts.append(round(cur_t, 3))
    return cuts


def union_cuts(a: list[float], b: list[float], tol: float = MATCH_TOL) -> tuple[list[float], list[float]]:
    extra = [x for x in b if not a or min(abs(x - y) for y in a) > tol]
    return sorted(set(a) | set(extra)), extra


@stage("shots", deps=("register",), inputs=lambda ctx: {"content": CONTENT, "scdet": SCDET_THRESHOLD, "tol": MATCH_TOL})
def run(ctx: Ctx) -> dict:
    reg = ctx.load("register")
    dur = reg["duration_s"]
    a = pyscenedetect_cuts(ctx.asset)
    b = scdet_cuts(ctx.asset)
    cuts, extra = union_cuts(a, b)
    cuts = [c for c in cuts if 0 < c < dur]
    bounds = [0.0] + cuts + [dur]
    shots = [{"shot_id": f"sh{i:03d}", "start_s": round(x, 3), "end_s": round(y, 3)}
             for i, (x, y) in enumerate(zip(bounds, bounds[1:])) if y > x]
    return {"detector": f"pyscenedetect ContentDetector{CONTENT} ∪ ffmpeg scdet(threshold={SCDET_THRESHOLD})-only",
            "n_content": len(a), "n_scdet": len(b), "added_from_scdet": extra, "cuts": cuts, "shots": shots}
