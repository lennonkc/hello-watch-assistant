-- Cedar knowledge pack schema v1 (pack.db, SQLite 3.35+, requires FTS5)
-- Spec: docs/knowledge-pack.md. All times are in seconds (REAL), on the asset timeline (the file identified by asset.asset_sha256).
-- Conventions:
--   1. Every row holding text that may leave through the spoiler gate must carry a "visible from" time column; reveal_at_s = the latest moment by which all information in the text has appeared on screen or in dialogue.
--   2. All primary-key ids are opaque (e.g. 'c07', 'f031') and must not contain character names, aliases or any canary term.
--   3. review_state: auto (model output) → prereviewed (automated pre-review) → reviewed (human) / flagged / rejected.

PRAGMA foreign_keys = ON;

-- ── Metadata ─────────────────────────────────────────────────────────

CREATE TABLE pack_meta (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
-- Required keys: schema_version ('1'), pack_version (integer, monotonically increasing per asset), asset_sha256, built_at (RFC 3339),
--   generator (pipeline name@version), review_complete ('0'|'1'), fixture ('0'|'1'), distribution ('master'|'device'),
--   gate_margin_cue_s, gate_margin_scene_snapped_s, gate_margin_scene_unsnapped_s, gate_margin_event_s,
--   gate_margin_object_s, gate_margin_first_appears_s, gate_margin_reviewed_reveal_s, wordlists_version
-- Optional keys: models (JSON: {purpose: model ID}), prompt_sha256 (JSON: {purpose: sha256})

CREATE TABLE asset (
  asset_sha256    TEXT PRIMARY KEY,           -- sha256 of the asset file (lowercase hex); the pack's sole anchor
  content_id      TEXT,                       -- catalog id; one content_id may map to several assets (re-encodes)
  title           TEXT NOT NULL,
  asset_kind      TEXT NOT NULL CHECK (asset_kind IN ('film', 'short', 'trailer', 'clip')),
  duration_s      REAL NOT NULL,
  video_fps       REAL,
  width           INTEGER,
  height          INTEGER,
  audio_tracks    INTEGER NOT NULL,           -- 0 = no audio track
  has_dialogue    INTEGER NOT NULL CHECK (has_dialogue IN (0, 1)),
  dialogue_lang   TEXT,                       -- BCP 47, e.g. 'en'
  license         TEXT NOT NULL,              -- e.g. 'CC-BY-3.0', 'CC-BY-ND-4.0', 'commercial-trailer'
  derivatives_ok  INTEGER NOT NULL CHECK (derivatives_ok IN (0, 1)),  -- 0 = no clip-based recaps allowed (e.g. BY-ND)
  story_start_s   REAL NOT NULL,              -- start of the story proper (excluding opening titles)
  story_end_s     REAL NOT NULL,              -- end of the story proper; end credits follow
  credits_start_s REAL
);

-- Title-level, spoiler-free text (data source for the title_facts tool). Treated as reveal_at_s = asset.story_start_s; included in name-visibility and canary scans.
CREATE TABLE title_info (
  field   TEXT PRIMARY KEY,                   -- e.g. 'logline', 'genre', 'year'
  text_en TEXT NOT NULL,
  text_zh TEXT,
  source  TEXT NOT NULL
);

-- ── Temporal structure ───────────────────────────────────────────────

CREATE TABLE shots (
  shot_id TEXT PRIMARY KEY,
  start_s REAL NOT NULL,
  end_s   REAL NOT NULL CHECK (end_s > start_s)
);

-- Media segments of ≤120 s (0.5 s tolerance), cut on shot boundaries; the files themselves are not in the pack (registration info lives in the sidecar).
CREATE TABLE segments (
  segment_id  TEXT PRIMARY KEY,
  start_s     REAL NOT NULL,
  end_s       REAL NOT NULL CHECK (end_s > start_s AND end_s - start_s <= 120.5),
  file_sha256 TEXT NOT NULL
);

-- ── Dialogue ─────────────────────────────────────────────────────────

CREATE TABLE subtitle_tracks (
  track            TEXT PRIMARY KEY,          -- '<lang>.<source>', e.g. 'en.official', 'en.generated', 'zh-Hans.official'
  lang             TEXT NOT NULL,             -- BCP 47
  source           TEXT NOT NULL CHECK (source IN ('official', 'generated', 'human')),
  priority         INTEGER NOT NULL,          -- with several tracks in one language, the lower number wins (official < human < generated)
  source_url       TEXT,
  license          TEXT,
  sha256           TEXT,                      -- sha256 of the original subtitle file
  offset_s         REAL NOT NULL DEFAULT 0,   -- shift already applied
  drift_s_per_hour REAL NOT NULL DEFAULT 0,   -- stretch already applied
  alignment_note   TEXT
);

CREATE TABLE cues (
  cue_id      TEXT PRIMARY KEY,               -- '<track>:<nnn>', e.g. 'en.official:017'
  track       TEXT NOT NULL REFERENCES subtitle_tracks(track),
  start_s     REAL NOT NULL,
  end_s       REAL NOT NULL CHECK (end_s >= start_s),
  text        TEXT NOT NULL,
  kind        TEXT NOT NULL CHECK (kind IN ('dialogue', 'lyrics', 'sign', 'sfx')),
  speaker_raw TEXT                            -- raw speaker label (untrusted); human-confirmed speakers live in item_characters (relation='speaker')
);

-- ── Characters ───────────────────────────────────────────────────────

CREATE TABLE characters (
  character_id              TEXT PRIMARY KEY,
  canonical_name            TEXT NOT NULL,    -- character name (never the actor's name)
  name_zh                   TEXT,             -- reviewed Chinese name
  display_label_pre_name    TEXT NOT NULL,    -- label used before the name is visible, e.g. 'the young woman'
  display_label_pre_name_zh TEXT NOT NULL,
  name_known_at_s           REAL,             -- when the name is first spoken/shown in the film; NULL = never revealed in the film
  name_public               INTEGER NOT NULL CHECK (name_public IN (0, 1)),  -- 1 = name may be used early (human decision)
  name_source               TEXT,             -- 'dialogue' | 'on_screen_text' | 'title' | 'credits' | 'catalog'
  kind                      TEXT NOT NULL CHECK (kind IN ('person', 'creature', 'robot', 'other')),
  role                      TEXT NOT NULL CHECK (role IN ('protagonist', 'supporting', 'minor', 'mentioned_only')),
  first_appears_s           REAL,             -- NULL only when role = 'mentioned_only'
  review_state              TEXT NOT NULL CHECK (review_state IN ('auto', 'prereviewed', 'flagged', 'reviewed', 'rejected')),
  source                    TEXT NOT NULL
);

CREATE TABLE character_aliases (
  character_id     TEXT NOT NULL REFERENCES characters(character_id),
  alias            TEXT NOT NULL,
  lang             TEXT NOT NULL,
  alias_known_at_s REAL NOT NULL,
  generic          INTEGER NOT NULL CHECK (generic IN (0, 1)),  -- 1 = generic term (e.g. 'the dragon'); must not establish identity on its own
  PRIMARY KEY (character_id, alias, lang)
);

-- Appearance changes over time (costume changes, aging); appearance itself can be a spoiler or a clue. A look describes only attributes visible on screen at from_s.
CREATE TABLE character_looks (
  look_id          TEXT PRIMARY KEY,
  character_id     TEXT NOT NULL REFERENCES characters(character_id),
  from_s           REAL NOT NULL,
  to_s             REAL,                      -- internal use only, never output (would expose later changes)
  description_en   TEXT NOT NULL,
  description_zh   TEXT,
  reveal_at_s      REAL NOT NULL,             -- ≥ from_s
  spoiler_critical INTEGER NOT NULL DEFAULT 0 CHECK (spoiler_critical IN (0, 1)),
  review_state     TEXT NOT NULL CHECK (review_state IN ('auto', 'prereviewed', 'flagged', 'reviewed', 'rejected'))
);

CREATE TABLE character_facts (
  fact_id          TEXT PRIMARY KEY,
  character_id     TEXT NOT NULL REFERENCES characters(character_id),
  text_en          TEXT NOT NULL,
  text_zh          TEXT,
  reveal_at_s      REAL NOT NULL,
  basis            TEXT NOT NULL CHECK (basis IN ('shown', 'dialogue', 'inferred')),
  evidence_t_s     REAL,                      -- on-screen moment when basis = shown; supporting lines for dialogue are in item_cues
  spoiler_critical INTEGER NOT NULL DEFAULT 0 CHECK (spoiler_critical IN (0, 1)),
  review_state     TEXT NOT NULL CHECK (review_state IN ('auto', 'prereviewed', 'flagged', 'reviewed', 'rejected')),
  source           TEXT NOT NULL
);

-- Identity twists (e.g. "the stranger = the lost brother"). Before the reveal, a and b must be presented as different characters (spec §5.3 identity isolation).
CREATE TABLE identity_links (
  link_id        TEXT PRIMARY KEY,
  a_character_id TEXT NOT NULL REFERENCES characters(character_id),  -- the character as the viewer perceives it before the reveal
  b_character_id TEXT NOT NULL REFERENCES characters(character_id),  -- its true identity
  relation       TEXT NOT NULL,               -- e.g. 'same_individual', 'parent_of'
  reveal_at_s    REAL NOT NULL,               -- snapped by a human to the confirming moment (line / shot)
  text_en        TEXT NOT NULL,
  text_zh        TEXT,
  review_state   TEXT NOT NULL CHECK (review_state IN ('auto', 'prereviewed', 'flagged', 'reviewed', 'rejected'))
);

-- On-screen intervals; attached to "the character as the viewer perceives it at that time" (before a twist, attach to a, not b).
CREATE TABLE appearances (
  character_id TEXT NOT NULL REFERENCES characters(character_id),
  start_s      REAL NOT NULL,
  end_s        REAL NOT NULL CHECK (end_s >= start_s),
  source       TEXT NOT NULL,
  review_state TEXT NOT NULL CHECK (review_state IN ('auto', 'prereviewed', 'flagged', 'reviewed', 'rejected'))
);

-- ── Scenes / events / objects ────────────────────────────────────────

CREATE TABLE scenes (
  scene_id      TEXT PRIMARY KEY,
  start_s       REAL NOT NULL,
  end_s         REAL NOT NULL CHECK (end_s > start_s),
  start_s_model REAL,                         -- raw model boundary (for auditing)
  end_s_model   REAL,
  snapped       INTEGER NOT NULL CHECK (snapped IN (0, 1)),  -- 1 = boundaries snapped to shot cuts
  reveal_at_s   REAL NOT NULL,                -- default = end_s
  location_en   TEXT,
  summary_en    TEXT NOT NULL,
  summary_zh    TEXT,
  review_state  TEXT NOT NULL CHECK (review_state IN ('auto', 'prereviewed', 'flagged', 'reviewed', 'rejected')),
  source        TEXT NOT NULL
);

CREATE TABLE events (
  event_id         TEXT PRIMARY KEY,
  t_s              REAL NOT NULL,
  reveal_at_s      REAL NOT NULL,
  kind             TEXT NOT NULL CHECK (kind IN ('action', 'dialogue', 'reveal', 'arrival', 'departure', 'other')),
  text_en          TEXT NOT NULL,
  text_zh          TEXT,
  spoiler_critical INTEGER NOT NULL DEFAULT 0 CHECK (spoiler_critical IN (0, 1)),
  review_state     TEXT NOT NULL CHECK (review_state IN ('auto', 'prereviewed', 'flagged', 'reviewed', 'rejected')),
  source           TEXT NOT NULL
);

CREATE TABLE objects (
  object_id        TEXT PRIMARY KEY,
  name_en          TEXT NOT NULL,
  name_zh          TEXT,
  description_en   TEXT NOT NULL,
  description_zh   TEXT,
  first_seen_s     REAL NOT NULL,
  reveal_at_s      REAL NOT NULL,             -- ≥ first_seen_s; must be pushed later if the description includes attributes revealed later
  spoiler_critical INTEGER NOT NULL DEFAULT 0 CHECK (spoiler_critical IN (0, 1)),
  review_state     TEXT NOT NULL CHECK (review_state IN ('auto', 'prereviewed', 'flagged', 'reviewed', 'rejected')),
  source           TEXT NOT NULL
);

CREATE TABLE object_sightings (
  object_id TEXT NOT NULL REFERENCES objects(object_id),
  t_s       REAL NOT NULL,
  PRIMARY KEY (object_id, t_s)
);

-- Item–character relations: present = appears in the scene/event; mentioned = referred to in the text; speaker = speaker of a line (human-confirmed).
-- item_type ∈ scene / event / cue / character_fact / character_look / object.
CREATE TABLE item_characters (
  item_type    TEXT NOT NULL,
  item_id      TEXT NOT NULL,
  character_id TEXT NOT NULL REFERENCES characters(character_id),
  relation     TEXT NOT NULL CHECK (relation IN ('present', 'mentioned', 'speaker')),
  PRIMARY KEY (item_type, item_id, character_id, relation)
);

-- Supporting lines for an item: dialogue events and facts with basis = dialogue must link every supporting cue, and reveal_at_s ≥ max(cue.end_s).
CREATE TABLE item_cues (
  item_type TEXT NOT NULL,                    -- event / character_fact / scene
  item_id   TEXT NOT NULL,
  cue_id    TEXT NOT NULL REFERENCES cues(cue_id),
  PRIMARY KEY (item_type, item_id, cue_id)
);

-- ── Media and vectors ────────────────────────────────────────────────

-- Thumbnails: tNNNNN is the last frame with pts ≤ N (not "the frame closest to N"), 640 px wide, JPEG.
CREATE TABLE thumbnails (
  t_s    INTEGER PRIMARY KEY,
  path   TEXT NOT NULL,                       -- relative to the pack root, e.g. 'thumbs/t00452.jpg'
  width  INTEGER NOT NULL,
  height INTEGER NOT NULL,
  bytes  INTEGER NOT NULL
);

-- Vectors: float32 little-endian. item_type ∈ cue / scene / event / character_fact / object / segment
--   / shot_caption / recap / video_window (v1.1).
-- The gate looks up the source table by item_type to decide eligibility; segments are gated on end_s + gate_margin_scene_snapped_s.
CREATE TABLE embeddings (
  item_type TEXT NOT NULL,
  item_id   TEXT NOT NULL,
  model     TEXT NOT NULL,                    -- e.g. 'gemini-embedding-2'
  dim       INTEGER NOT NULL,
  vector    BLOB NOT NULL,
  PRIMARY KEY (item_type, item_id, model)
);

-- ── v1.1 additions (compatible: new tables only, spec §12) ───────────
-- Shot-level understanding (Shot-by-Shot), in-film character bank (AutoAD-II), speaker attribution, progressive recaps (MovieChat breakpoint mode), video retrieval windows.
-- Tables with review_state pass the gate like other content tables: time conditions are given in each table's comment; the review threshold is the same as scenes (prereviewed+).

-- In-film character bank: reference frames of a character taken from the film itself (no external headshots, no real-person data). Usable only when t_s ≤ P.
CREATE TABLE character_exemplars (
  exemplar_id  TEXT PRIMARY KEY,
  character_id TEXT NOT NULL REFERENCES characters(character_id),
  t_s          INTEGER NOT NULL,               -- thumbnail second, maps to thumbs/tNNNNN.jpg
  bbox         TEXT,                           -- JSON [ymin, xmin, ymax, xmax], normalized 0–1000 (Gemini's native box format)
  look_id      TEXT REFERENCES character_looks(look_id),
  source       TEXT NOT NULL,
  review_state TEXT NOT NULL CHECK (review_state IN ('auto', 'prereviewed', 'flagged', 'reviewed', 'rejected'))
);

-- Shot-level captions: one per shot (shots < 1 s may be merged into a neighbor). All information is visible by the end of the shot: reveal_at_s ≥ end_s.
CREATE TABLE shot_captions (
  shot_caption_id TEXT PRIMARY KEY,
  start_s         REAL NOT NULL,
  end_s           REAL NOT NULL CHECK (end_s > start_s),
  reveal_at_s     REAL NOT NULL,
  caption_en      TEXT NOT NULL,
  caption_zh      TEXT,
  source          TEXT NOT NULL,
  review_state    TEXT NOT NULL CHECK (review_state IN ('auto', 'prereviewed', 'flagged', 'reviewed', 'rejected'))
);

-- Entities in a shot: on-screen characters / objects and their screen position, for disambiguating "who is this / what is that" questions about the frame. Gated together with their shot.
CREATE TABLE shot_entities (
  shot_caption_id TEXT NOT NULL REFERENCES shot_captions(shot_caption_id),
  seq             INTEGER NOT NULL,
  entity_type     TEXT NOT NULL CHECK (entity_type IN ('character', 'object')),
  character_id    TEXT REFERENCES characters(character_id),   -- when entity_type = character and matched to a character
  object_id       TEXT REFERENCES objects(object_id),         -- when entity_type = object and matched to a known object
  label_en        TEXT NOT NULL,              -- character: pre-name label until the name is visible (§6); object: its name
  label_zh        TEXT,
  position        TEXT CHECK (position IN ('left', 'center', 'right', 'background')),
  bbox            TEXT,                       -- JSON [ymin, xmin, ymax, xmax], 0–1000, on the shot's representative frame
  bbox_t_s        INTEGER,                    -- thumbnail second the bbox refers to
  action_en       TEXT,
  action_zh       TEXT,
  PRIMARY KEY (shot_caption_id, seq)
);

-- Speaker attribution: at most one row per cue. NULL character_id = unknown / off-screen voice / narrator. Time condition follows the cue (end_s + m_cue ≤ P).
CREATE TABLE cue_speakers (
  cue_id       TEXT PRIMARY KEY REFERENCES cues(cue_id),
  character_id TEXT REFERENCES characters(character_id),
  diar_label   TEXT,                          -- diarization label from transcription, e.g. 'S1'
  confidence   REAL CHECK (confidence BETWEEN 0 AND 1),
  method       TEXT NOT NULL,                 -- e.g. 'diarization+visual'
  review_state TEXT NOT NULL CHECK (review_state IN ('auto', 'prereviewed', 'flagged', 'reviewed', 'rejected'))
);

-- Progressive recaps: story so far up to upto_s (the end of some scene), generated only from items visible at ≤ upto_s.
-- Gate: reveal_at_s + gate_margin_scene_snapped_s ≤ P; online, take the latest eligible recap, then append the eligible events after it.
CREATE TABLE recaps (
  recap_id      TEXT PRIMARY KEY,
  upto_scene_id TEXT REFERENCES scenes(scene_id),
  upto_s        REAL NOT NULL,
  reveal_at_s   REAL NOT NULL CHECK (reveal_at_s >= upto_s),
  text_en       TEXT NOT NULL,
  text_zh       TEXT,
  source        TEXT NOT NULL,
  review_state  TEXT NOT NULL CHECK (review_state IN ('auto', 'prereviewed', 'flagged', 'reviewed', 'rejected'))
);

-- Video retrieval windows: sub-windows of ≤ 30 s (may overlap) cut along segments; vector carriers only (embeddings.item_type = 'video_window').
-- Gate: end_s + gate_margin_scene_snapped_s ≤ P.
CREATE TABLE video_windows (
  window_id  TEXT PRIMARY KEY,
  segment_id TEXT NOT NULL REFERENCES segments(segment_id),
  start_s    REAL NOT NULL,
  end_s      REAL NOT NULL CHECK (end_s > start_s)
);

-- ── Output-side safeguards ───────────────────────────────────────────

-- Terms never to output (real people's names). Each entry is already expanded into concrete variants (spec §7.1); consumers normalize with NFKC + case folding + accent stripping + Traditional→Simplified, then match whole words.
CREATE TABLE blocklist (
  term       TEXT NOT NULL,
  lang       TEXT NOT NULL,                   -- 'en' / 'zh' / 'und'
  person_ref TEXT NOT NULL,                   -- all variants of one real person share an opaque reference, e.g. 'p03'
  source     TEXT NOT NULL,                   -- 'credits' / 'catalog' / 'review'
  PRIMARY KEY (term, lang)
);

-- Future canaries: while P < active_until_s, the term must not appear in output (spec §7.2).
CREATE TABLE canaries (
  term           TEXT NOT NULL,
  lang           TEXT NOT NULL,               -- 'en' / 'zh'
  match          TEXT NOT NULL CHECK (match IN ('word', 'phrase', 'regex')),
  case_sensitive INTEGER NOT NULL CHECK (case_sensitive IN (0, 1)),  -- 1 when a character name doubles as a common noun (e.g. 'Scales')
  active_until_s REAL NOT NULL,               -- = visibility time of the referenced item (reveal_at_s + the matching margin)
  kind           TEXT NOT NULL CHECK (kind IN ('name', 'alias', 'identity_clue', 'event', 'other')),
  ref_type       TEXT NOT NULL,               -- character / identity_link / event / character_fact / character_look / object
  ref_id         TEXT NOT NULL,
  PRIMARY KEY (term, lang, ref_type, ref_id)
);

-- ── Review (master pack only; stripped from the device pack) ─────────

CREATE TABLE review_log (
  log_id           INTEGER PRIMARY KEY,
  item_type        TEXT NOT NULL,
  item_id          TEXT NOT NULL,
  field            TEXT,
  old_value        TEXT,
  new_value        TEXT,
  decision         TEXT NOT NULL CHECK (decision IN ('approve', 'edit', 'reject', 'add')),
  spoiler_critical INTEGER NOT NULL DEFAULT 0 CHECK (spoiler_critical IN (0, 1)),
  reviewer         TEXT NOT NULL,             -- role-style identifier, e.g. 'claude-prereview', 'reviewer-1'; never a real person's name
  ts               TEXT NOT NULL,             -- RFC 3339
  seconds_spent    REAL,
  why              TEXT
);

-- ── Full-text search ─────────────────────────────────────────────────

-- Content is derived by the builder from the tables above; t_s is the item's visibility time (the gate must still check the source table).
CREATE VIRTUAL TABLE text_fts USING fts5(item_type UNINDEXED, item_id UNINDEXED, t_s UNINDEXED, lang UNINDEXED, text);

CREATE INDEX idx_cues_time ON cues(end_s);
CREATE INDEX idx_scenes_reveal ON scenes(reveal_at_s);
CREATE INDEX idx_events_reveal ON events(reveal_at_s);
CREATE INDEX idx_appearances_char ON appearances(character_id, start_s);
CREATE INDEX idx_facts_char ON character_facts(character_id, reveal_at_s);
CREATE INDEX idx_item_characters_char ON item_characters(character_id);
