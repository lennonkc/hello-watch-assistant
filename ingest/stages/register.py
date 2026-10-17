"""Asset registration: sha256, ffprobe, video frame pts table (used for thumbnail frame picking)."""
from __future__ import annotations

import json
import subprocess

from ingest.stages import Ctx, stage


def ffprobe(path) -> dict:
    out = subprocess.run(["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)],
                         check=True, capture_output=True, text=True).stdout
    return json.loads(out)


def frame_pts(path) -> list[float]:
    """Presentation times of all video-stream packets (ascending) = the frame timeline."""
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "packet=pts_time",
                          "-of", "csv=p=0", str(path)], check=True, capture_output=True, text=True).stdout
    return sorted(float(x.strip().strip(",")) for x in out.splitlines() if x.strip() and x.strip() != "N/A")


@stage("register", inputs=lambda ctx: {"sha": ctx.sha, "catalog_id": ctx.catalog_id})
def run(ctx: Ctx) -> dict:
    probe = ffprobe(ctx.asset)
    v = next(s for s in probe["streams"] if s["codec_type"] == "video")
    audio = [s for s in probe["streams"] if s["codec_type"] == "audio"]
    num, den = (int(x) for x in v.get("avg_frame_rate", "0/1").split("/"))
    pts = frame_pts(ctx.asset)
    start = float(probe["format"].get("start_time", 0) or 0)
    return {
        "asset_sha256": ctx.sha,
        "duration_s": round(float(probe["format"]["duration"]), 3),
        "start_time_s": start,
        "video_fps": round(num / den, 3) if den else None,
        "width": v["width"], "height": v["height"],
        "video_codec": v.get("codec_name"),
        "audio_tracks": len(audio),
        "audio_langs": [a.get("tags", {}).get("language") for a in audio],
        "subtitle_streams": sum(1 for s in probe["streams"] if s["codec_type"] == "subtitle"),
        "frame_count": len(pts),
        "frame_pts": [round(t - start, 6) for t in pts],
    }
