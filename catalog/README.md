# Catalog

`catalog.json` is a small demo catalog with the three open-licensed (CC BY) Blender Foundation films used by the demo:
**Sintel** (2010), **Tears of Steel** (2012) and **Elephants Dream** (2006). It follows the Cedar MediaItem schema v1.1
(`schema` / `schema_version` at the top of the file).

## How the ingest pipeline uses it

| Where | How |
|---|---|
| Asset matching (`ingest/library.py`, `deploy/ingest/upload_assets.py`) | matches asset file names to items: exact match on `title_zh` first, then an English `title` (prefix) match |
| Title configs (`ingest/titles/<id>.json`) | one per item, named after the item `id` (license, asset kind, expected asset sha256, subtitle sources) |
| Knowledge pack `content_id` | equals the item `id` |
| Content check (`identify` stage) | `title` / `title_zh` / `year` / `type` let the model judge whether the asset really is this work; mismatches are listed in `library.json` |
| Vision prompts | `synopsis_full` is passed to the model as "the synopsis the viewer read before pressing play"; names in it are treated as `name_source = catalog` |
| Knowledge pack `title_info` | `title_zh` and `year` are written as title-level facts |

## Fields

Top level: `schema`, `schema_version`, `generated_at`, `count`, `notes`, `items`.

Each item (snake_case; durations in seconds; ratings on a 0–10 scale):

| Field | Type | Description |
|---|---|---|
| `id` | string | UUIDv4; stable identifier (also the `catalog_id` used by ingest) |
| `type` | enum | `movie` for all three items |
| `title` / `title_zh` | string | original English title / Chinese title |
| `year` | int | release year |
| `genres` | [enum] | from a closed set: `action adventure animation comedy crime documentary drama family fantasy musical mystery scifi war` |
| `mood_tags` | [{tag, weight}] | weight ∈ {9 = primary, 7 = secondary, 5 = minor} |
| `duration_s` | int | actual length of the playable asset in seconds |
| `age_rating` | string | rating label in its original system (e.g. `TV-PG`, `TV-14`) |
| `min_age` | int | minimum age (0 / 7 / 13 / 17) mapped from the rating |
| `rating` / `rating_count` / `rating_source` | float / int / string | main 0–10 rating, sample size and source (`tmdb` / `imdb`); all three keys are always present, `null` when unverified |
| `rating_imdb` / `rating_douban` | float | supporting ratings (optional keys) |
| `synopsis_full` | string | synopsis (treated as information the viewer already has at ingest time) |
| `synopsis_short_zh` | string | short spoiler-free one-liner in Chinese |
