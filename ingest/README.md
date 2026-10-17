# ingest · knowledge-pack ingest pipeline

Turns one asset file (a short film or trailer) into a knowledge pack that follows the
[knowledge pack spec](../docs/knowledge-pack.md) and the [DDL](../schema/knowledge_pack_v1.sql), in two forms:

- **master pack**: everything, including `review_log`; stays on the ingest side.
- **device pack**: `review_log` stripped, only `prereviewed` / `reviewed` rows; this is what the online Q&A service loads.

All generated output goes under `.cache/` (not committed). For running the same pipeline as Cloud Run Jobs on GCS, see
[docs/cloud-deploy.md](../docs/cloud-deploy.md); for how the online side consumes packs, see [docs/online-qa.md](../docs/online-qa.md).

## Requirements

- Python ≥ 3.12; `pip install -r requirements-ingest.txt` (google-genai, scenedetect, opencv, Pillow, pytest).
- System `ffmpeg` / `ffprobe` (tested with 9.0.x; needs the `scdet` filter and `libx264`).
- **Gemini API key**: read from `~/.config/cedar/gemini_api_key` by default (override the path with `CEDAR_GEMINI_KEY_FILE`).
  The default backend is the Gemini Developer API (it clears `GOOGLE_GENAI_USE_VERTEXAI` and similar env vars); the key is
  never written to disk or printed.
  `CEDAR_GEMINI_BACKEND=enterprise` switches to Gemini Enterprise Agent Platform (ADC auth; video is read from the GCS asset;
  needs `CEDAR_GCP_PROJECT` and `CEDAR_INGEST_BUCKET`; transcription still uses the Developer API).
- **Embeddings** (Gemini Embedding 2) are cached in the sidecar `<work dir>/post/embeddings.db`. `CEDAR_EMBED=0` skips paid
  embedding calls; `CEDAR_EMBED_VIDEO=0` embeds text only.
- **Prompts** are not part of the code; `ingest/promptbook.py` loads them at runtime, in this order:
  1. `CEDAR_PROMPTS_SECRET`: a Secret Manager version name (`projects/<project>/secrets/<name>/versions/<v>`) holding a JSON
     bundle produced by `deploy/prompts/push_prompts.sh` (used by the cloud jobs; ADC credentials);
  2. `CEDAR_PROMPTS_DIR`: a directory with one `<name>.txt` per prompt;
  3. the repository's `prompts/` directory (default). In this public repo it contains **simplified reference prompts**.

  Every prompt and its `str.format` placeholders are registered in `promptbook.NAMES` and validated on load; a missing prompt
  or mismatched placeholders is an error. Changing a prompt changes the stage fingerprint, so the affected stages re-run.
- **Shared wordlists**: `schema/wordlists/` (`pack_meta.wordlists_version` is taken from its `VERSION`).
- **Title configs**: `ingest/titles/<catalog_id>.json` (license, asset kind, expected asset sha256, official subtitle sources
  and checksums, `vision_prompt` version). Put the subtitle files in `.cache/sources/<catalog_id>/subs/` (or pass `--subs-dir`).
  `catalog_id` is the item `id` in [`catalog/catalog.json`](../catalog/catalog.json).

## Usage

```bash
# Full pipeline (cached per stage; stages that are done and whose inputs are unchanged are skipped)
python3 -m ingest run --asset <asset file> --catalog-id <uuid>
# Run only some stages / force a stage to re-run
python3 -m ingest run --asset <asset file> --catalog-id <uuid> --stages shots,segments --force shots
python3 -m ingest check   [--sha <asset_sha256>]      # run the spec §10 checks (no writes); non-zero exit = failures
python3 -m ingest export  [--sha …]                   # auto rows + replay of all patches → re-export master / device
python3 -m ingest apply-patch <patch.json> [--sha …]  # validate → store in patches/ → re-export + check
python3 -m ingest status  [--sha …]                   # ledger: time and cost per stage
python3 -m ingest library --asset-dir <dir> [--cast-source <catalog export with cast>]   # register assets + detect mismatches → library.json
python3 -m ingest library-status                      # progress across all assets → library_status.json
python3 -m ingest cloud {run,pull,push-work,register,renew} …   # GCS-backed variants, see docs/cloud-deploy.md
python3 -m pytest                                     # unit tests
```

`--sha` can be omitted when `.cache/ingest/` holds exactly one pack. `--budget` (default $5) is a spending gate for paid
stages: the run stops when the ledger total plus the estimate for the next call would exceed it.

Tests inject placeholder prompts by default, so they do not depend on the real prompts. Tests marked `real_prompts` load
prompts the normal way and are skipped when none are available.

## Stages and outputs (`.cache/ingest/<asset_sha256>/`)

| Stage | Output | Notes |
|---|---|---|
| `register` | `stages/register.json` | sha256, ffprobe, table of video frame pts |
| `shots` | `stages/shots.json` | PySceneDetect ContentDetector (27, 12 frames) ∪ cuts found only by ffmpeg `scdet` (10), de-duplicated within ±0.25 s |
| `segments` | `stages/segments.json`, `segments/segNN.mp4` | ≤120 s, cut on shot boundaries; deterministic re-encode (single-threaded x264 + bitexact); sha256 goes into the pack |
| `subtitles` | `stages/subtitles.json` | parses official tracks (encoding from the title config, BOM auto-detected); a second translation in the same language is used only to extract translated names |
| `thumbs` | `thumbs/tNNNNN.jpg`, `stages/thumbs.json` | one per second, **tN = last frame with pts ≤ N**, 640 px wide, JPEG q75 |
| `upload` | `registry.json` (spec §4 sidecar), `audio_file.json` | reuses an already-registered Files API file with the same sha256 when possible; otherwise uploads under an ASCII symlink name (the SDK puts the file name in an HTTP header) |
| `identify` | `stages/identify.json` | asset identification: on-screen title cards, title / episode markers, whether it matches the catalog item, dialogue language, real people's names in cards or narration (→ blocklist). Trailers at fps 2 |
| `transcribe` | `stages/transcribe.json` | gemini-3.5-transcribe with word-level timestamps; only an alignment reference when an official track exists; skipped without audio |
| `transcribe_plain` | `stages/transcribe_plain.json` | plain pass (no timestamps) when there is no official track in that language; `align` uses it to fill sentences missed by the word-level pass |
| `align` | `stages/align.json` | official track vs. transcript: offset / cumulative drift corrected only above 0.3 s. Without an official track, builds a `<lang>.generated` track from the word-level pass, inserting word runs found only by the plain pass into gaps between neighboring words (gaps < 0.3 s are not filled) |
| `vision_full` | `stages/vision_full.json` | whole film at fps 1: **the authoritative source for the character set, identity links, looks, facts, scenes, events and objects** |
| `vision_seg` | `stages/vision_seg.json`, `vision_seg_parts/` | per-segment fps 2 recall pass (given the full roster, refers to known characters by ref only): new characters, appearance intervals, events in gaps, new objects |
| `vision_shots` | `stages/vision_shots.json`, `vision_shots_parts/` | v1.1. **In-film character bank**: for each character look, 1 fps thumbnail candidates within its appearances; the model judges visibility / sharpness and returns a box; ≤3 per look → `character_exemplars`. **Shot-level captions**: shots (< 1 s merged into a neighbor) are batched per segment; segment video at fps 2 + one keyframe per shot + reference crops of the characters likely present (AutoAD-II) → `shot_captions` / `shot_entities` (position, box on the keyframe, action). Prompts see only character ids and labels; on assembly `{cNN}` is replaced by the name or label visible at that time, sentences hitting a reviewer canary are removed (if all of them hit, `reveal_at_s` is pushed later), and real people's names are dropped |
| `speakers` | `stages/speakers.json`, `speakers_parts/` | v1.1. Transcription with `diarization_mode=speaker` → a diarization label per cue; lines are grouped into windows and the speaker is judged from window video + reference crops + characters on screen in the shot; weighted vote per label (a cluster's character may override vision only if it appears within ±20 s of the line) → `cue_speakers`; non-reference tracks inherit by time overlap; labels are written back to `cues.speaker_raw` |
| `credits` | `stages/credits.json` | real people's names from the end credits → `blocklist` (spec §7.1 variant rules) |
| `build` | `auto/rows.json`, `auto/aux.json` | assembles spec rows (all `auto`); scene boundaries snap to cuts (start ±1.5 s, end may only extend); model reveal times +0.999 (integer-second truncation); adds progressive `recaps` and embeddings |
| `export` | `master/`, `device/`, `checks.json`, `review/needs_human.json`, `versions.json` | see below |

Each stage output carries an input fingerprint (fingerprints of the stages it depends on + parameters / prompts / models);
an unchanged fingerprint means the stage is skipped and the ledger records a `cached` line.

### Export and versions

master content = auto rows + `patches/*.json` replayed in order + derived data (name / alias canaries, `active_until_s` of
reviewer canaries, `text_fts`). The spec §10 checks then run, and items with locatable problems are set to `flagged` (derived
from the checks, not stored as input). The content sha256 is recorded in `versions.json`: **re-exporting identical content
gives the same `pack_version`, the same `built_at` and the same bytes**; only changed content mints a new version.
Each pack directory contains `pack.db`, `pack.json`, `thumbs/` and `MANIFEST.json` (per-file sha256).
`review_complete = 1` if and only if all 11 checks pass (read conservatively: leftover `auto` / `flagged` rows also count as incomplete).

Device pack: `review_log` is removed, only `prereviewed` / `reviewed` rows are kept, and dangling relations are cleaned up.
`title_info` has no `review_state` column and can only be judged human-approved via `review_log`, so the device pack
**contains only the `title_info` fields approved by a human**.

### Catalog matching and name sources

- **Asset registration** (`ingest/library.py`): file name → catalog item (Chinese title first, then English title); sha256 and
  duration; mismatch categories `catalog` / `duration` (> 2 s) / `content` (identify says it is a different work, or an episode
  marker) / `kind` (form differs from the title config) / `sha256` (not in the title config's expected list). Details live only in `.cache/ingest/library.json`; the pack's
  `pack_meta.asset_mismatch` records only the categories. The catalog synopsis of a mismatched asset is not used as a
  `name_public` source.
- **`vision_prompt`** in the title config: `v1` keeps the original prompts (pinned for Sintel so its stage fingerprints stay
  stable); the default `v2` adds names from the catalog synopsis, full presence tracking, lyric intervals, trailer rules (no
  invented causality across jump cuts; names on title cards go to the blocklist only) and non-English dialogue handling
  (knowledge in English, quotes in the original language).
- **Presence**: the builder adds `item_characters(present)` from appearance intervals (scene overlap ≥ min(0.9 s, scene length / 2);
  events / objects ±1 s).
- **Blocklist sources**: end credits ∪ identify title cards / narration ∪ catalog cast / directors (`--cast-source`, cached in
  `.cache/sources/<id>/cast.json`, never committed).

## Review patches (`cedar-review-patch/1`)

Human or automated review is applied as patches; `apply-patch` validates them, stores them under `patches/` and re-exports.

```json
{"format": "cedar-review-patch/1", "asset_sha256": "…", "base_pack_version": 6, "reviewer": "reviewer-1",
 "created_at": "2026-10-14T09:00:00Z", "seconds_total": 1830,
 "ops": [{"op": "approve", "table": "identity_links", "key": {"link_id": "k01"}, "spoiler_critical": 1, "seconds_spent": 95},
         {"op": "edit", "table": "events", "key": {"event_id": "e031"}, "set": {"reveal_at_s": 647.0}, "why": "…"},
         {"op": "reject" | "flag", "table": "…", "key": {…}},
         {"op": "add", "table": "canaries", "row": {…}}, {"op": "delete", "table": "character_aliases", "key": {…}}]}
```

- Rows are located by table + primary key (`appearances` has none and uses `{character_id, start_s}`). Every changed field
  writes one `review_log` row.
- Reviewer `claude-*` (automated pre-review) → items become `prereviewed` (never downgrading a human `reviewed`); any other
  reviewer → `reviewed`. Reviewer ids must be role-style (`[a-z][a-z0-9-]{1,31}`), never a real person's name.
- Name / alias canaries are derived by the builder and cannot be edited directly; to add a Chinese spelling or a
  transliteration variant, add a `character_aliases` row.
- Patches locate rows by primary key, so do not rebuild the auto rows (`build`) after reviewing unless you are prepared to
  re-create the patches: builder changes can make `add` / `delete` ops miss.

## Cost and time (Sintel, 888 s, v1 stages, measured 2026-10-14)

| Stage | Time | Cost |
|---|---|---|
| register / shots / segments / subtitles / thumbs | 0.2 / 12.5 / 132 / 0 / 9.7 s | — |
| upload (reused existing full-film registration; audio uploaded fresh) | 116 s | — |
| transcribe (word-level) | 11.6 s | $0.048 |
| vision_full (fps 1) | 99 s | $0.105 |
| vision_seg (10 segments at fps 2, sequential) | 291 s | $0.217 |
| credits (145 s of end credits, fps 2) | 34 s | $0.034 |
| build + export (incl. 11 checks) | ≈1 s | — |
| **Total** | ≈12 min | **$0.404** |

Estimated at $0.75 / $3.75 per million tokens for 3.8 Flash and $2 / $12 for 3.5 Transcribe (thinking tokens billed as
output); every call is recorded in `ledger.jsonl`.

## Known limitations

- Dissolves and rolling end credits have no cut; when a boundary cannot be snapped the model time is kept (`snapped = 0`, 5 s margin).
- Descriptive labels for unnamed characters are not stable across runs; re-running the vision stages changes auto rows and ids,
  so existing patches can no longer be replayed (pin the roster before re-running).
- Generated subtitles are only produced when there is no official track.
