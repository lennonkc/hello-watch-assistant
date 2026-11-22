"""The three public films must keep ingesting exactly as they do now: any change below re-runs (paid) stages for them
or changes their packs, so it has to be deliberate.

For each public title spec this fingerprints the stage plan (order, dependencies, every stage's fingerprint inputs), the
part-level cache keys that live outside the stage inputs (the diarization pass, recap checkpoints and their prompts,
the speaker prompt), and the outputs of the transcription, alignment and speaker code (the transcription calls, align
with and without the plain pass, cue generation, speaker voting). Placeholder prompts are used, so editing a prompt
text does not trip it; changing code that feeds a fingerprint does. A difference names the component: if the change
is intended, update EXPECTED in the same commit and say which stages re-run."""
import json
import pathlib
import types

import pytest

from ingest import gemini, recaps, util
from ingest.ledger import Ledger
from ingest.stages import ORDER, REGISTRY, Ctx, load_all
from ingest.stages import gemini_stages as G
from ingest.stages import speakers as S
from ingest.stages import subtitles as T
from ingest.tests.conftest import mini_pack
from ingest.util import CATALOG_PATH, TITLES_DIR, fingerprint, read_json, write_json

AUDIO = {"gemini_file": "files/a", "uri": "https://files/a", "mime_type": "audio/flac", "sha256": "5a" * 32,
         "expires_at": "2026-12-01T00:00:00Z", "how": "reused"}
WORDS = [{"start_offset": f"{1 + i * 0.4:.3f}s", "end_offset": f"{1.3 + i * 0.4:.3f}s", "text": w, "speaker": f"speaker_{i % 2}",
          "type": "word_info", "start_index": i}
         for i, w in enumerate("Hello there. Where is the dragon? It flew away north. 你好 吗".split())]
PLAIN = "Hello there. Hey! Where is the dragon? It flew away to the north. 你好 吗"


class FakeInteractions:
    def __init__(self):
        self.calls = []

    def create(self, **kw):
        self.calls.append(kw)
        granular = "timestamp_granularities" in kw["generation_config"]["transcription_config"]["mode"]
        content = [{"type": "text", "text": PLAIN}] if not granular else [{"annotations": WORDS}]
        doc = {"steps": [{"content": content}], "usage": {"total_input_tokens": 1000, "total_output_tokens": 200}}
        return types.SimpleNamespace(model_dump=lambda **_: doc)


def _h(obj) -> str:
    return fingerprint(obj)[:16]


def _film_pack(duration: float) -> dict:
    """mini_pack stretched to the film's length: a scene every 50 s, with one 7.8-minute gap between checkpoints (no
    checkpoint is added for it: recaps sit at scene ends only)."""
    p = mini_pack()
    p["asset"] = {**p["asset"], "duration_s": duration, "story_end_s": duration - 60, "credits_start_s": duration - 60}
    base = p["scenes"][0]
    starts = [s for s in range(0, int(duration) - 60, 50) if not 100 <= s < 520]
    p["scenes"] = [{**base, "scene_id": f"s{i:02d}", "start_s": float(s), "end_s": float(s + 50), "reveal_at_s": float(s + 50),
                    "snapped": i % 2, "summary_en": f"Scene {i}."} for i, s in enumerate(starts)]
    return p


def plan(spec: dict, item: dict, tmp: pathlib.Path, monkeypatch) -> dict:
    monkeypatch.setattr(util, "CACHE", tmp / "cache")
    sha = "c3" * 32
    ctx = Ctx(sha=sha, asset=tmp / "film.mp4", catalog_id=spec["catalog_id"], title=spec, catalog_item=item,
              subs_dir=tmp / "subs", ledger=Ledger(sha, 100.0))
    write_json(ctx.stage_path("register"), {"duration_s": float(item["duration_s"]), "audio_tracks": 1, "_fingerprint": "r"})
    out = {"order": _h(ORDER), "deps": _h({n: [list(s.deps), s.paid] for n, s in sorted(REGISTRY.items())})}
    for name in ORDER:
        if name == "align":
            out["stage:align:no-plain"] = _h(REGISTRY[name].inputs(ctx))
            write_json(ctx.stage_path("transcribe_plain"), {"_fingerprint": "p"})
        out[f"stage:{name}"] = _h(REGISTRY[name].inputs(ctx))
    ctx.stage_path("transcribe_plain").unlink()

    # the transcription calls (one Interactions call per pass, same config) and their stage outputs
    fake = types.SimpleNamespace(interactions=FakeInteractions())
    monkeypatch.setattr(G.gemini, "client", lambda which=None: fake)
    monkeypatch.setattr(G, "ensure_audio_file", lambda ctx, c: AUDIO)
    tr = G.run_transcribe(ctx)
    tp = G.run_transcribe_plain(ctx)
    out["call:transcribe"] = _h([{k: v for k, v in c.items()} for c in fake.interactions.calls])
    out["out:transcribe"] = _h(tr)
    out["out:transcribe_plain"] = _h(tp)

    # align: generated track with the plain merge (no official track in the spec's language) and without it
    write_json(ctx.stage_path("subtitles"), {"tracks": [], "name_sources": [], "_fingerprint": "s"})
    write_json(ctx.stage_path("transcribe"), {**tr, "_fingerprint": "t"})
    write_json(ctx.stage_path("transcribe_plain"), {**tp, "_fingerprint": "p"})
    out["out:align:plain"] = _h(T.run_align(ctx))
    ctx.stage_path("transcribe_plain").unlink()
    out["out:align"] = _h(T.run_align(ctx))
    words = T.words_from_transcript(tr["raw"])
    out["fn:generated_cues"] = _h(T.generated_cues(words))
    out["fn:merge_plain"] = _h(T.merge_plain(words, PLAIN))

    # speakers: the diarization pass key, the prompt, parsing, assignment, windows and the vote
    out["key:diarize"] = _h([gemini.TRANSCRIBE_MODEL, AUDIO["sha256"], S.DIAR_MODE, spec.get("dialogue_lang", "en")])
    out["prompt:speaker"] = _h([S.P.speaker_prompt("", [], ""),
                                S.P.speaker_prompt("", [{"ref": "c01", "label": "the girl", "kind": "person"}], "L01 x")])
    sw = S.parse_words(tr["raw"])
    cues = [{"cue_id": f"q{i}", "start_s": 1.0 + i * 1.2, "end_s": 1.9 + i * 1.2, "text": "x"} for i in range(5)]
    diar = S.assign_diar(cues, sw)
    visual = {"q0": ("c01", 0.9), "q1": ("c01", 0.8), "q2": ("c02", 0.4), "q3": (None, 0.2), "q4": ("c01", 0.7)}
    out["fn:speakers"] = _h([sw, diar, S.vote(visual, diar), S.batches(S.windows(cues, 20.0)),
                             S.decide("c01", 0.5, ("c02", 0.7, 3), plausible=False)])

    # recaps: checkpoints and the prompt (= cache key) of each
    pk = _film_pack(float(item["duration_s"]))
    cps = recaps.checkpoints(pk)
    out["recaps:checkpoints"] = _h(cps)
    out["recaps:keys"] = _h([fingerprint([gemini.VISION_MODEL, recaps.prompt_for(pk, cp), recaps.RECAP_SCHEMA]) for cp in cps])
    return out


def public_specs():
    items = {it["id"]: it for it in read_json(CATALOG_PATH)["items"]}
    return [(json.loads((TITLES_DIR / f"{cid}.json").read_text()), items[cid]) for cid in sorted(items)]


EXPECTED = {
    "79400f81-eef7-4ebf-ad77-7aea38346b20": {
        "call:transcribe": "94472640dc0f1ff6",
        "deps": "669270f071359073",
        "fn:generated_cues": "dd6085905e3186ad",
        "fn:merge_plain": "cb1ffebfbb73889f",
        "fn:speakers": "0051887701b715c2",
        "key:diarize": "4ac0027374874304",
        "order": "a841592a0660756e",
        "out:align": "738b190207643595",
        "out:align:plain": "feb3c3eade0d62be",
        "out:transcribe": "6339ec6c5669ceb5",
        "out:transcribe_plain": "8c01e889ed2d66d1",
        "prompt:speaker": "4b6d7293bf50a26b",
        "recaps:checkpoints": "c16274d5f5580ea2",
        "recaps:keys": "ea08de93e5df37d0",
        "stage:align": "19d1d3c5ab409ca9",
        "stage:align:no-plain": "190cac8eb64cb733",
        "stage:credits": "da209bfa561c5148",
        "stage:identify": "6b8c6a2a115f2f89",
        "stage:register": "cc2dade3f1a112d9",
        "stage:segments": "0e63adb0fa587933",
        "stage:shots": "75b342de22776f93",
        "stage:speakers": "9f42c95ddd7e313b",
        "stage:subtitles": "350b1d233491e8d7",
        "stage:thumbs": "b4a76162243e62d9",
        "stage:transcribe": "8cdcc5c0a1e5f40b",
        "stage:transcribe_plain": "0a9511daea28aed2",
        "stage:upload": "4fcbfb13d36c682e",
        "stage:vision_full": "debc64a9bf09f7f9",
        "stage:vision_seg": "fbac87c1fcbd6bf7",
        "stage:vision_shots": "733a0e746fa658d4",
    },
    "9e9cbbc5-02d4-46b1-a1b3-f1a47e7e50fe": {
        "call:transcribe": "94472640dc0f1ff6",
        "deps": "669270f071359073",
        "fn:generated_cues": "dd6085905e3186ad",
        "fn:merge_plain": "cb1ffebfbb73889f",
        "fn:speakers": "0051887701b715c2",
        "key:diarize": "4ac0027374874304",
        "order": "a841592a0660756e",
        "out:align": "738b190207643595",
        "out:align:plain": "feb3c3eade0d62be",
        "out:transcribe": "6339ec6c5669ceb5",
        "out:transcribe_plain": "8c01e889ed2d66d1",
        "prompt:speaker": "4b6d7293bf50a26b",
        "recaps:checkpoints": "f457624645a3c7cc",
        "recaps:keys": "eea067c203d6fa59",
        "stage:align": "19d1d3c5ab409ca9",
        "stage:align:no-plain": "190cac8eb64cb733",
        "stage:credits": "da209bfa561c5148",
        "stage:identify": "05a8c1e19d500df2",
        "stage:register": "a4510b663d575035",
        "stage:segments": "0e63adb0fa587933",
        "stage:shots": "75b342de22776f93",
        "stage:speakers": "9f42c95ddd7e313b",
        "stage:subtitles": "350b1d233491e8d7",
        "stage:thumbs": "b4a76162243e62d9",
        "stage:transcribe": "8cdcc5c0a1e5f40b",
        "stage:transcribe_plain": "0a9511daea28aed2",
        "stage:upload": "4fcbfb13d36c682e",
        "stage:vision_full": "57269b8899a8e0d3",
        "stage:vision_seg": "fbac87c1fcbd6bf7",
        "stage:vision_shots": "733a0e746fa658d4",
    },
    "c4c02a81-ae5c-4a91-aef1-eea196f91c17": {
        "call:transcribe": "047d2dbf6323b0e2",
        "deps": "669270f071359073",
        "fn:generated_cues": "dd6085905e3186ad",
        "fn:merge_plain": "cb1ffebfbb73889f",
        "fn:speakers": "0051887701b715c2",
        "key:diarize": "4ac0027374874304",
        "order": "a841592a0660756e",
        "out:align": "738b190207643595",
        "out:align:plain": "bddc0f5af2c7ebbd",
        "out:transcribe": "6339ec6c5669ceb5",
        "out:transcribe_plain": "171edc090bd22f8a",
        "prompt:speaker": "4b6d7293bf50a26b",
        "recaps:checkpoints": "49e8cb20c57a5763",
        "recaps:keys": "36444dc6fcf13c8d",
        "stage:align": "19d1d3c5ab409ca9",
        "stage:align:no-plain": "190cac8eb64cb733",
        "stage:credits": "da209bfa561c5148",
        "stage:identify": "b4e5efe553b6d6f0",
        "stage:register": "24b3d6b3774a870c",
        "stage:segments": "0e63adb0fa587933",
        "stage:shots": "75b342de22776f93",
        "stage:speakers": "9f42c95ddd7e313b",
        "stage:subtitles": "3e0f58da4bcd5a78",
        "stage:thumbs": "b4a76162243e62d9",
        "stage:transcribe": "8cdcc5c0a1e5f40b",
        "stage:transcribe_plain": "4f0506d66f94c3fa",
        "stage:upload": "4fcbfb13d36c682e",
        "stage:vision_full": "1473e0fcd140605a",
        "stage:vision_seg": "5e858c4f544f7b12",
        "stage:vision_shots": "733a0e746fa658d4",
    },
}


@pytest.mark.parametrize("spec, item", public_specs(), ids=lambda x: x.get("title", "") if isinstance(x, dict) else "")
def test_short_films_ingest_exactly_as_before(spec, item, tmp_path, monkeypatch):
    load_all()
    got = plan(spec, item, tmp_path, monkeypatch)
    want = EXPECTED.get(spec["catalog_id"])
    if want is None:
        pytest.fail("no expected values for " + spec["catalog_id"] + ": " + json.dumps(got, sort_keys=True))
    assert {k: v for k, v in got.items() if want.get(k) != v} == {}
    assert set(got) == set(want)
