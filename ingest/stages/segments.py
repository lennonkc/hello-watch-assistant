"""≤120 s media segments (along shot cuts), cut into deterministic segment files (sha256 goes in the pack, the files do not)."""
from __future__ import annotations

import math
import subprocess

from ingest.stages import Ctx, stage
from ingest.util import sha256_file

MAX_SEG = 120.0
ENCODE = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-threads", "1", "-c:a", "aac", "-b:a", "128k",
          "-map_metadata", "-1", "-fflags", "+bitexact", "-flags:v", "+bitexact", "-flags:a", "+bitexact"]


def plan_segments(cuts: list[float], dur: float, max_len: float = MAX_SEG) -> list[tuple[float, float]]:
    """Try n = ceil(dur/max)… equal-split targets snapped to cuts; if none work, greedily take the farthest cut ≤ max."""
    def balanced(n):
        b = [0.0]
        for k in range(1, n):
            cand = [c for c in cuts if 0 < c - b[-1] <= max_len]
            if not cand:
                return None
            b.append(min(cand, key=lambda c: abs(c - k * dur / n)))
        b.append(dur)
        return b if all(0 < y - x <= max_len for x, y in zip(b, b[1:])) else None

    for n in range(max(1, math.ceil(dur / max_len)), max(2, math.ceil(dur / max_len)) + 12):
        b = balanced(n)
        if b:
            return list(zip(b, b[1:]))
    b = [0.0]
    while dur - b[-1] > max_len:
        cand = [c for c in cuts if 0 < c - b[-1] <= max_len]
        b.append(max(cand) if cand else b[-1] + max_len)
    b.append(dur)
    return list(zip(b, b[1:]))


@stage("segments", deps=("register", "shots"), inputs=lambda ctx: {"max": MAX_SEG, "encode": ENCODE})
def run(ctx: Ctx) -> dict:
    dur = ctx.load("register")["duration_s"]
    plan = plan_segments(ctx.load("shots")["cuts"], dur)
    out_dir = ctx.wd / "segments"
    out_dir.mkdir(parents=True, exist_ok=True)
    segs = []
    for i, (a, b) in enumerate(plan):
        sid = f"seg{i:02d}"
        f = out_dir / f"{sid}.mp4"
        subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-ss", f"{a:.3f}", "-to", f"{b:.3f}",
                        "-i", str(ctx.asset), *ENCODE, str(f)], check=True)
        segs.append({"segment_id": sid, "start_s": round(a, 3), "end_s": round(b, 3), "file": f"segments/{sid}.mp4",
                     "file_sha256": sha256_file(f)})
    return {"segments": segs}
