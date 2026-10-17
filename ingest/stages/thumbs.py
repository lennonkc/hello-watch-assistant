"""Thumbnails: one per second, tN = last frame with pts ≤ N (not the nearest frame), 640 px wide, JPEG."""
from __future__ import annotations

import bisect
import os

from ingest.stages import Ctx, stage
from ingest.util import sha256_file

WIDTH = 640
QUALITY = 75


def pick_frames(pts: list[float], duration: float) -> dict[int, int]:
    """{N: frame index}, N = 0..floor(duration); only N for which a frame with pts ≤ N exists."""
    out = {}
    for n in range(0, int(duration) + 1):
        i = bisect.bisect_right(pts, n + 1e-9) - 1
        if i >= 0:
            out[n] = i
    return out


@stage("thumbs", deps=("register",), inputs=lambda ctx: {"width": WIDTH, "quality": QUALITY, "rule": "pts<=N"})
def run(ctx: Ctx) -> dict:
    import cv2
    from PIL import Image

    reg = ctx.load("register")
    pts = reg["frame_pts"]
    want = pick_frames(pts, reg["duration_s"])
    by_frame: dict[int, list[int]] = {}
    for n, i in want.items():
        by_frame.setdefault(i, []).append(n)
    out_dir = ctx.wd / "thumbs"
    out_dir.mkdir(parents=True, exist_ok=True)
    cap = cv2.VideoCapture(str(ctx.asset))
    idx, rows = 0, []
    last = max(by_frame) if by_frame else -1
    while idx <= last:
        ok, frame = cap.read()
        if not ok:
            raise RuntimeError(f"decode stopped at frame {idx}, expected {len(pts)} frames")
        if idx in by_frame:
            img = Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
            h = round(img.height * WIDTH / img.width / 2) * 2
            img = img.resize((WIDTH, h), Image.LANCZOS)
            for n in by_frame[idx]:
                p = out_dir / f"t{n:05d}.jpg"
                img.save(p, "JPEG", quality=QUALITY, optimize=True)
                rows.append({"t_s": n, "path": f"thumbs/t{n:05d}.jpg", "width": WIDTH, "height": h,
                             "bytes": os.path.getsize(p), "frame_index": idx, "frame_pts": pts[idx],
                             "sha256": sha256_file(p)})
        idx += 1
    cap.release()
    rows.sort(key=lambda r: r["t_s"])
    return {"thumbnails": rows, "total_bytes": sum(r["bytes"] for r in rows)}
