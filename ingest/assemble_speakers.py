"""`speakers` stage output → cue_speakers rows.

Character attachment follows the same time constraints as shot entities (`attachable`: not before first appearance; not to b before the
identity reveal); failures are nulled (= unknown / off-screen).
Diarization labels are also backfilled into `cues.speaker_raw` (when empty).
"""
from __future__ import annotations

from ingest.assemble_shots import attachable


def rows(asm) -> None:
    sp = asm.st.get("speakers") or {}
    cues = {c["cue_id"]: c for c in asm.pack["cues"]}
    out = []
    for x in sorted(sp.get("cues", []), key=lambda x: x["cue_id"]):
        cue = cues.get(x["cue_id"])
        if cue is None:
            continue
        cid, method = x.get("character_id"), x["method"]
        if cid and not attachable(asm, cid, cue["start_s"], cue["end_s"]):
            cid, method = None, method + "+detached"
        if x.get("diar_label") and not cue.get("speaker_raw"):
            cue["speaker_raw"] = x["diar_label"]
        out.append({"cue_id": x["cue_id"], "character_id": cid, "diar_label": x.get("diar_label"),
                    "confidence": round(min(1.0, max(0.0, float(x.get("confidence") or 0))), 3), "method": method,
                    "review_state": "auto"})
    asm.pack["cue_speakers"] = out
