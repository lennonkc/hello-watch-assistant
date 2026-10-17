# Knowledge Pack Specification v1.1

> Status: **v1 frozen**, extended by **v1.1** (tables only, backward compatible). DDL: [`../schema/knowledge_pack_v1.sql`](../schema/knowledge_pack_v1.sql)
>
> The knowledge pack is the output of the offline ingest pipeline and the **only** source of in-film knowledge for the
> online Q&A service (on-device or in the cloud). Together with the DDL, this document is the **authoritative contract**
> between the ingest pipeline and the online service. Code comments cite it as "spec §N" / "C1 §N".
> Related: [online-qa.md](online-qa.md) (how the online service consumes v1.1 data), [cloud-deploy.md](cloud-deploy.md) (running ingest on Google Cloud).

---

## 1. Principles

1. **Anchor on the asset file, not the title.** One knowledge pack corresponds to one asset file (`asset.asset_sha256`).
   Re-encoding the same `content_id` requires re-ingesting. If the sha256 of the asset being played does not match the pack,
   the online service must not use that pack to answer.
2. **All times are on the asset timeline**, in seconds.
3. **Every text that may be shown to a viewer carries a visibility time.** `reveal_at_s` is the latest moment at which
   **all** information in that text has appeared on screen or in dialogue — not the moment the content starts.
   Text without a visibility time must never be output. (In early experiments the top spoiler source was untimed
   "appearance descriptions".)
4. **Knowledge is stored in English**; `*_zh` columns hold reviewed Chinese. When the online service needs Chinese, it translates
   only items that have **already passed the gate**.
5. **Characters only, never actors.** Distributable content must not contain any real person's name; real names appear only in `blocklist`.
6. **Opaque ids.** Primary keys must not contain character names, aliases or canary terms (otherwise the id itself leaks a name or a twist).
7. **Packs are immutable.** Any change produces a new `pack_version`. Operational data that expires (cloud media registration)
   lives in a sidecar (§4).

## 2. Pack composition and distribution forms

```
<asset_sha256>/
  MANIFEST.json      # manifest
  pack.db            # SQLite, DDL in schema/knowledge_pack_v1.sql
  pack.json          # optional: JSON export of pack.db (§8), for review tools and diffs
  thumbs/tNNNNN.jpg  # thumbnails: one per second; tN = last frame with pts ≤ N; 640 px wide, JPEG; NNNNN zero-padded to 5 digits
```

| Form | `pack_meta.distribution` | Contents |
|---|---|---|
| master | `master` | All tables including `review_log`; stays on the ingest side for review and rebuilds |
| device | `device` | Derived from master: **`review_log` removed** (it contains the original spoiler text that was edited out); only rows with `review_state ∈ {prereviewed, reviewed}`; `title_info` (no `review_state` column) contains only fields approved by a human in `review_log` |

`MANIFEST.json`:

```json
{
  "schema_version": 1, "pack_version": 3, "distribution": "device",
  "asset_sha256": "…", "built_at": "2026-10-14T10:00:00Z", "review_complete": true,
  "files": [{ "path": "pack.db", "sha256": "…", "bytes": 262144 },
            { "path": "thumbs/t00000.jpg", "sha256": "…", "bytes": 16384 }]
}
```

Consumers verify integrity against `files[].sha256`. Packs with `pack_meta.fixture = '1'` (converted from evaluation
fixtures, margins 0) **must be refused by device loaders**. How packs are distributed (to which device, which path) is
outside this contract.

## 3. Tables

| Group | Tables | Notes |
|---|---|---|
| Metadata | `pack_meta`, `asset`, `title_info` | `pack_meta` holds versions, build info and gate margins; `asset.story_start_s / story_end_s` bound the feature proper; `title_info` is treated as visible at `story_start_s` and must not contain the full plot |
| Time structure | `shots`, `segments` | Shot cuts (deterministic detection); media segments of ≤ 120 s |
| Dialogue | `subtitle_tracks`, `cues` | Multiple language tracks with priority; source, license and applied alignment; `kind = lyrics` not output by default |
| Characters | `characters`, `character_aliases`, `character_looks`, `character_facts`, `identity_links`, `appearances` | Name visibility (§6), appearance timeline, identity twists kept separately (§5.3) |
| Content | `scenes`, `events`, `objects`, `object_sightings` | Scene boundaries keep both the model's raw value and the snapped value |
| Links | `item_characters`, `item_cues` | Item ↔ character relations (present / mentioned / speaker); supporting cues for an item |
| Media / vectors | `thumbnails`, `embeddings` | Eligibility is always decided by looking back at the source table |
| Output guards | `blocklist`, `canaries` | Real-name variants; future canaries (§7) |
| Review | `review_log` | Master pack only |
| Search | `text_fts` | FTS5, derived by the builder |
| v1.1 additions | `character_exemplars`, `shot_captions`, `shot_entities`, `cue_speakers`, `recaps`, `video_windows` | In-film character references, per-shot captions and on-screen entities, speaker attribution, progressive recaps, video retrieval windows (§5.2) |

`review_state`: `auto` (model output) → `prereviewed` (passed automated pre-review, possibly with edits) → `reviewed`
(approved by a human); also `flagged` (pending) and `rejected`.

## 4. Sidecar: cloud registration (not inside the pack)

Online "look at the clip" requires the media file to be available on the Gemini side. Registrations expire, so they live
outside the pack in `registry.json`:

```json
{ "asset_sha256": "…",
  "asset_file": { "storage_uri": "gs://…/asset.mp4", "gemini_file": "files/…", "expires_at": "2026-11-13T00:00:00Z" },
  "segments": [ { "segment_id": "seg03", "file_sha256": "…", "storage_uri": "gs://…/seg03.mp4",
                  "gemini_file": "files/…", "expires_at": "2026-11-13T00:00:00Z" } ] }
```

- `expires_at` is taken from the registration API's response, never estimated (Files API direct upload: 48 h;
  object-storage registration is longer — use the actual returned value).
- The online service checks `expires_at` before use; if expired or missing, media-dependent tools degrade to "text knowledge only".
- The online service currently uses only `asset_file` (the whole film is registered once and clipped per request);
  `segments` serve embeddings and future long-film retrieval.
- With the Agent Platform backend, media is read directly from `gs://` and does not expire (see [cloud-deploy.md](cloud-deploy.md)).

## 5. Eligibility rules (spoiler gate, normative)

### 5.1 Definitions

- `P`: the **locked playback position** (seconds). Each request has exactly one `P`, locked at the moment the question is
  asked (touch = instant of the tap; voice = instant of the wake word or push-to-talk). Generation, cancellation or switching
  display surfaces never change it afterwards.
- An item is **eligible** if it may enter retrieval results, the answering context or the evidence list. Ineligible items
  are invisible to the answering model.
- `m_*`: margin, equal to `max(value in pack_meta, server-side floor)`. For non-evaluation builds the server-side floor
  **defaults to the defaults in this section**; only evaluation fixtures may use 0.

### 5.2 Rules

| Object | Review requirement | Time condition | Default margin |
|---|---|---|---|
| Subtitles `cues` | — | `end_s + m_cue ≤ P`; a cue spanning `P` is **excluded entirely**; `kind = lyrics` excluded by default | `gate_margin_cue_s` = 1 |
| Scenes `scenes` | prereviewed+ | `reveal_at_s + m ≤ P`, `m` = `snapped` ? 3 : 5; a scene in progress is not eligible | `gate_margin_scene_*` |
| Events `events` | prereviewed+; `kind = reveal` or `spoiler_critical = 1` requires reviewed | `reveal_at_s + m_event ≤ P` | `gate_margin_event_s` = 3 |
| Objects `objects` | prereviewed+; `spoiler_critical = 1` requires reviewed | `reveal_at_s + m_object ≤ P`; `object_sightings` only where `t_s ≤ P` | `gate_margin_object_s` = 2 |
| Appearances `appearances` | prereviewed+; characters on either side of any `identity_links` require reviewed | `start_s ≤ P`; interval truncated to `[start_s, min(end_s, P)]` | — |
| Characters `characters` | **reviewed** | `first_appears_s + m_first ≤ P` | `gate_margin_first_appears_s` = 2 |
| Aliases `character_aliases` | follows the character | `alias_known_at_s + m_rev ≤ P` | `gate_margin_reviewed_reveal_s` = 1 |
| Character facts `character_facts` | **reviewed** | owning character eligible, and `reveal_at_s + m_rev ≤ P` | same as above |
| Looks `character_looks` | **reviewed** | owning character eligible, `from_s ≤ P` and `reveal_at_s + m_rev ≤ P`; if several are eligible for one character, take the one with the largest `from_s`; `to_s` is never output | same as above |
| Identity links `identity_links` | **reviewed** | `reveal_at_s + m_rev ≤ P` | same as above |
| Title info `title_info` | reviewed | treated as `reveal_at_s = story_start_s` | — |
| Thumbnails | — | `t_s ≤ floor(P)` (tN itself is the frame with pts ≤ N, so it never passes P) | — |
| Media | — | whole-film clipping: `endOffset ≤ floor(P) − 1` (clip precision ≤ 1 s); segments: `end_s + gate_margin_scene_snapped_s ≤ P` | — |
| Shot captions `shot_captions` (v1.1) | prereviewed+ | `reveal_at_s + m ≤ P`; `shot_entities` follow their shot; character links as in §5.3 | `gate_margin_scene_snapped_s` |
| On-screen entities of the current shot `shot_entities` (v1.1) | same as above | While the shot is not yet eligible (`start_s ≤ P` but `reveal_at_s + m > P`), only entities with `bbox_t_s ≤ floor(P)` may be used for **visual disambiguation** of "who is this / what is that": only `character_id` / `object_id`, `position`, `bbox`; the character must be eligible and its label is re-decided per §6 **at time P** (not the pre-rendered `label_*`); `action_*` and the shot's `caption_*` are not usable | — |
| Character exemplars `character_exemplars` (v1.1) | follows the character | owning character eligible, and `t_s ≤ floor(P)` | — |
| Cue speakers `cue_speakers` (v1.1) | prereviewed+ | follows the cue: `end_s + m_cue ≤ P`; the character must be eligible, otherwise treat the speaker as unknown | `gate_margin_cue_s` |
| Recaps `recaps` (v1.1) | prereviewed+ | `reveal_at_s + m ≤ P`; online takes the latest eligible recap, then adds eligible events after it | `gate_margin_scene_snapped_s` |
| Video windows `video_windows` (v1.1) | — | `end_s + m ≤ P` (retrieval carrier only) | `gate_margin_scene_snapped_s` |
| Embeddings `embeddings` | — | follow the owning item; **compute the eligible set first, then rank within it** — never search the whole film and filter afterwards | — |

Supplementary rules:

1. `auto`, `flagged` and `rejected` are never eligible. The online service **may** offer a development-only switch that lets
   `auto` rows answer; demo and evaluation builds must turn it off.
2. Items marked "requires reviewed" are the parts that require human review: character names, character information,
   identity twists, and spoiler-critical content. Everything else becomes eligible after automated pre-review.
3. Multilingual: eligibility depends only on time and review state, never on language.

### 5.3 Identity isolation (before the reveal, a and b are two characters)

For every `identity_links(a, b, reveal_at_s = R)` that is not yet eligible:

1. **Links must not cross over**: any item whose time falls in `[first_appears_s(a), R)` (`item_characters` present /
   mentioned / speaker, `character_facts`, `character_looks`, `appearances`) may be attached only to a; it **must not be
   attached to b** unless b is on screen at that moment as "b itself" (covered by one of b's appearance intervals).
2. **Clue terms**: every identity link must carry an `identity_clue` canary (§7.2), filled in by the reviewer with the clue
   viewers use to recognise the identity (for example a distinctive mark: `mark*` / `印记`), at least one per output
   language.
3. **Cross-table consistency**: no text whose eligibility time is earlier than `R` may contain the link's clue terms. This
   applies to appearance descriptions too (in one demo film, a look description mentioned the very mark the reveal
   relies on — that detail must be split into a separate look whose `reveal_at_s` is no
   earlier than the reveal).
4. The online co-occurrence rule (drop any sentence containing both a's label and b's name) is specified in the online
   service's tool contract.

### 5.4 Opening and end credits

- `P < story_start_s`: answer no content questions except the title.
- `P ≥ story_end_s` (end credits): "what did I miss" stops at `story_end_s`; "who is this / what is that" is declined and
  the frame is not inspected (the frame is the credits).
- Scenes and events at or after `story_end_s` never enter recaps.

### 5.5 Margin rationale

Margins were calibrated in early experiments on Sintel (subtitle alignment error, model scene-boundary drift relative to
shot cuts, systematically early dialogue events and first appearances, online clip precision). Reveal-type facts can be
several seconds early and must be snapped by a human — margins cannot cover them. Margins are versioned with the pack;
when evaluation finds a leak, raise the margin and rebuild the pack.

## 6. Name visibility

```
usable name  = canonical_name / name_zh                              when name_public = 1, or name_known_at_s is set and name_known_at_s + m_rev ≤ P
             = display_label_pre_name / display_label_pre_name_zh    otherwise
alias usable                                                         when alias_known_at_s + m_rev ≤ P; an alias with generic = 1 must not by itself establish identity
```

- `name_public = 1` is reserved for names that are not spoilers and that viewers know before watching (title characters,
  names on the poster and synopsis). Labels that appear only in the end credits are judged one by one by the reviewer
  ("is it a spoiler?"). The decision is recorded in `review_log`.
- **The pipeline must obey this when writing text too**: any text whose `reveal_at_s` is earlier than the moment a
  character's name becomes visible must not contain that character's name or non-generic aliases; use the label instead.
- All Chinese spellings of a name (`name_zh`, renderings in each Chinese subtitle track — e.g. Sintel's "史科尔斯 / 小鳞 / 小鱗")
  must be added as canaries (§7.2).

## 7. Output guards

### 7.1 `blocklist` (real people's names)

The builder expands each real name into variants; variants of the same person share a `person_ref`:

1. Full name; accent-stripped form; possessives (`X's`) are handled by the matcher and not listed separately.
2. Surname / given name alone: only when ≥ 5 characters long **and** not in the common-English wordlist (to avoid
   "Sam", "Lee", "Ton" killing dialogue and character names).
3. Chinese: each real name gets its Chinese rendering (from the catalog or added in review), `lang = zh`.
4. Sources: end-credit extraction, catalog cast, review additions. Trailers have no end credits and rely only on catalog
   cast and review additions — so the online service must also apply a "proper-noun allowlist + character–actor relation
   words" rule (see the online service's tool contract) and cannot rely on this table alone.

### 7.2 `canaries` (future canaries)

While `P < active_until_s`, any output containing `term` is treated as a spoiler.

| kind | Generated by | term | active_until_s |
|---|---|---|---|
| `name` | builder | `canonical_name`, `name_zh`, renderings in each Chinese subtitle track | `name_known_at_s + m_rev` (not generated when `name_public = 1`) |
| `alias` | builder | non-generic aliases (all languages) | `alias_known_at_s + m_rev` |
| `identity_clue` | **reviewer** (at least one per output language) | the clue viewers use to recognise the identity, e.g. `mark*`, `印记` | the identity link's `reveal_at_s + m_rev` |
| `event` | reviewer (mandatory for events / facts / objects with `spoiler_critical = 1`) | the key term of the reveal, e.g. `disguised` / `伪装` | the item's `reveal_at_s + its margin` |
| `other` | reviewer | anything | anything |

- **Align synonymous items**: margins differ by type (events 3 s; facts / identity links / looks 1 s). If a spoiler-critical
  event's canary term also appears in a synonymous fact or look, that text's `reveal_at_s` must be no earlier than
  `event reveal_at_s + m_event − m_rev`; otherwise the synonymous text becomes eligible while the canary is still active
  (builder check 11 reports this).
- **No whole-sentence terms** (e.g. `"the stranger = the lost brother"`): real output never contains them verbatim, so they guard nothing.
- `match`: `word` (whole word; English by word boundaries, Chinese by substring), `phrase` (whole phrase), `regex`
  (written explicitly by the reviewer, e.g. `mark(s|ed)?`). A `word` / `phrase` term ending in `*` means **prefix match**
  (`mark*` matches mark, marks, marked, but not Denmark); `*` anywhere else is literal.
- `case_sensitive = 1`: use when a character name coincides with a common noun (`Scales` vs "tan scales").
- Before matching, both sides are normalised with **NFKC + accent stripping + Traditional→Simplified Chinese**; with
  `case_sensitive = 0`, case folding as well.

### 7.3 Transliteration variants

Chinese speech transcription produces unconventional transliterations (e.g. Scales → "斯凯尔斯"). During automated
pre-review, common transliteration variants are generated for every `name` / `alias` canary and written after human
confirmation (`lang = zh`). Variants not listed can only be found by evaluation's "suspected" channel; once found they are
added and the pack is rebuilt.

### 7.4 Shared wordlists

The common-English wordlist (for §7.1 name-variant decisions and the online proper-noun allowlist) and the
Traditional→Simplified mapping live in `schema/wordlists/`, versioned; the pack records the version used at build time in
`pack_meta.wordlists_version`. The builder, the online service and evaluation must use the same version.

### 7.5 Role

Canaries and the blocklist are a safety net only: in early experiments one spoiler fact used different wording and the
canary did not fire. The eligibility rules (§5) and human review are the primary defence.

## 8. JSON export (`pack.json`)

A complete mirror of `pack.db` with keys equal to column names: `pack_meta` as a key–value object, `asset` as a single-row
object, all other tables as arrays of row objects. `embeddings` and `text_fts` are not exported. `pack.json` and `pack.db`
must each be rebuildable from the other (covered by pipeline tests).

## 9. Mapping from the demo fixture schema

Omitted in the public edition (internal evaluation fixtures).

## 10. Checks the builder must run

If any check fails, `review_complete` must not be set to 1; problems that can be located to an item mark that item `flagged`.

1. **Structure**: DDL constraints and foreign keys; `pack.json` round-trips to `pack.db`; all times within `[0, duration_s]`;
   `segments` are contiguous and cover `[0, duration_s]` (endpoint tolerance 0.5 s).
2. **Opaque ids**: primary keys contain no character name, alias or canary token (substring match after normalisation).
3. **Name visibility**: scan every timed text per §6 (including `title_info`, treated as visible at `story_start_s`).
4. **Supporting dialogue**: events with `kind = dialogue` and facts with `basis = dialogue` link at least one cue in
   `item_cues`, and `reveal_at_s ≥ max(end_s of linked cues)`.
5. **Mention order**: for every character in `item_characters(relation = mentioned)`, the item's `reveal_at_s ≥` that
   character's `first_appears_s`.
6. **Identity isolation**: check the timing of b-side links per §5.3 rule 1; run the cross-table clue-term scan per rule 3.
7. **Twist timing**: for every identity link and every event with `kind = reveal`, let C = cues containing b's name or alias
   (or the reveal's key term) whose start lies in `[R − 20, R + 20]`; if C is non-empty, require `R ≥ min(C.start_s)`.
   A reveal without dialogue (e.g. a silent visual transformation) must have a human-confirmed record with `spoiler_critical = 1`.
8. **Review coverage**: every "requires reviewed" item in §5.2 is `reviewed`; every identity link and every
   `spoiler_critical = 1` item has canaries in each output language; every `name` / `alias` canary has a `zh` counterpart.
9. **Real names**: every text column (including `title_info` and `*_zh`) is matched against `blocklist` with zero hits.
10. **Complete character cards**: every character with `role ≠ mentioned_only` has `first_appears_s`, at least one
    `appearances` row and at least one `character_looks` row.
11. **Canary scan**: at `active_until_s − 0.01` of every canary, and at the visibility time of every scene, scan all
    outputtable text in the §5 eligible set; zero hits.

## 11. Review responsibilities

| Content | Review |
|---|---|
| Character names, labels, `name_public` decisions, character facts, looks, identity links, clue canaries | Automated pre-review + **human review of every row** (100%) |
| Events and objects with `spoiler_critical = 1`; events with `kind = reveal` | Automated pre-review + **human review** |
| Scene summaries, ordinary events, objects, appearance intervals | Automated pre-review; human spot checks |
| Subtitles (official tracks) | Alignment check (automatic) |

Rows start as `auto`, move to `prereviewed` after automated pre-review and to `reviewed` after human review (§3).

## 12. Change process

- Incompatible changes (removing columns, changing semantics, changing eligibility rules) require a `schema_version` bump
  and a coordinated release of the ingest pipeline and the online service.
- Adding nullable columns or new tables is a compatible change; `schema_version` stays the same.
- Versioning: v1.1 adds tables only (backward compatible); it also starts populating `embeddings` (gemini-embedding-2, 1536 dimensions).
