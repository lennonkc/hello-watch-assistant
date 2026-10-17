# Web demo: HTTP API and front-end contract

> Contract between the Python service (`server/`, FastAPI + ADK agent) and the static front end (`web/`).
> All times are seconds on the film timeline. `P` is the locked playback position at the moment a question is asked.

## Layout

| Path | Owner | What |
|---|---|---|
| `server/` | back end | FastAPI app, ADK agent, gate-aware tools, output guards, pack store |
| `web/index.html` | front end | App shell: header, two tabs (`#experience`, `#architecture`), footer with film attribution |
| `web/shared/tokens.css` | front end | Design tokens shared by both tabs (see below) |
| `web/shared/motion.css`, `motion.js` | front end | Motion layer: entrances, tab / theme transitions, count-ups, ask-flow choreography; honours `prefers-reduced-motion` |
| `web/experience/` | front end | Tab 1, "Live demo": film picker, player, ask panel |
| `web/architecture/` | front end | Tab 2, "How it works": interactive flow diagram + Google Cloud technology map |

The service serves `web/` as static files at `/`. No build step: plain HTML, CSS and ES modules, no frameworks, no
external scripts (Google Fonts stylesheets are fine). Everything must work at phone width (≥ 360 px) and in light / dark.

## Design tokens (`web/shared/tokens.css`)

The visual language is soft UI (neumorphism): surfaces share the page colour and get their depth from a light
shadow at the top left and a dark one at the bottom right. Both tabs use only these CSS custom properties (defined
on `:root`, overridden under `@media (prefers-color-scheme: dark)` and `:root[data-theme="dark"|"light"]`):

- Colour: `--bg`, `--surface`, `--surface-2` (recessed wells), `--surface-3` (highlights), `--border`, `--text`,
  `--text-muted`, `--text-faint`, `--accent`, `--accent-2`, `--accent-contrast`, `--ok` (gate passed), `--locked`
  (gate blocked / future), `--warn`, and the indicator lights `--led-ok`, `--led-locked`.
- Depth: `--neu-raise-lg`, `--neu-raise`, `--neu-raise-sm`, `--neu-raise-xs` (extruded), `--neu-press`,
  `--neu-press-sm`, `--neu-press-xs` (pressed in), `--neu-edge` (rim light), `--neu-convex` / `--neu-concave`
  (surface gradients), `--accent-convex`, `--accent-glow`; `--shadow` is kept as an alias of `--neu-raise`.
  Inside SVG, use the page-level filters `url(#neu-raise)`, `url(#neu-raise-lg)` and `url(#neu-inset)`.
- Shape and type: `--radius-lg`, `--radius`, `--radius-sm`, `--font-sans`, `--font-display` (headings),
  `--font-mono`.
- Motion: `--ease-out`, `--ease-spring`, `--dur-fast`, `--dur`, `--dur-slow`. Stylesheets animate hover and press
  with the individual `translate` / `scale` properties; `transform` is left to the motion layer.
- One colour per Google Cloud service family used in the diagram: `--gc-gemini`, `--gc-run`, `--gc-storage`,
  `--gc-secret`, `--gc-build`.

## Endpoints

### `GET /api/titles`

```json
[{"id": "c4c02a81-…", "title": "Sintel", "duration_s": 888, "story_start_s": 18.0, "story_end_s": 840.0,
  "poster_t": 120, "license": "CC-BY-3.0", "attribution": "Sintel (c) Blender Foundation | durian.blender.org",
  "synopsis": "A lonely young woman…"}]
```

### `GET /api/titles/{id}/video`

Plays the film. Use it directly as `<video src>`; it either streams the file (local dev, supports `Range`) or redirects
(302) to a short-lived signed Cloud Storage URL.

### `GET /api/titles/{id}/frames/{t}.jpg`

Thumbnail at integer second `t` (one per second, 640 px wide). For posters and for evidence frames returned in answers.

### `GET /api/titles/{id}/gate?p=123.4`

What the spoiler gate unlocks at `p`. Counts only, never content:

```json
{"p": 123.4, "eligible": {"scene": 4, "event": 11, "cue": 30, "recap": 1, "shot_caption": 20, "character_fact": 3,
 "object": 2}, "total": {"scene": 22, "event": 61, "…": 0}, "characters_known": 2, "characters_total": 7}
```

### `POST /api/ask` (Server-Sent Events)

Request body:

```json
{"title_id": "c4c02a81-…", "p": 431.2, "question": "Who is she?", "lang": "en",
 "tap": {"x": 0.42, "y": 0.55}, "session_id": "browser-generated-uuid"}
```

- `tap` is optional: normalised coordinates (0–1, origin top-left) of a click on the paused video frame.
- `lang` is `en` or `zh` (answer language).
- `session_id` keeps follow-up questions in one conversation. If the viewer seeks **backwards** past the previous
  question, the server starts a fresh conversation (earlier answers may contain things not yet seen).

The response is `text/event-stream` (read it with `fetch` + a stream reader; `EventSource` cannot POST). Events:

| `event:` | `data:` (JSON) | Meaning |
|---|---|---|
| `gate` | `{"p": 431.2, "eligible_total": 140, "locked_total": 380}` | Gate computed for this question |
| `tool` | `{"id": "t1", "name": "story_so_far", "label": "Reading the story so far", "args": {…}}` | Agent called a tool |
| `tool_result` | `{"id": "t1", "name": "story_so_far", "summary": "1 recap + 6 events, up to 06:58"}` | Tool finished |
| `answer` | see below | Final, guarded answer |
| `error` | `{"message": "…"}` | Something failed; show it politely |
| `done` | `{}` | Stream finished |

`answer` payload:

```json
{"text": "That's the young woman who …",
 "evidence": [{"type": "scene", "start_s": 398.0, "end_s": 420.0, "text": "…"}],
 "frames": [{"t": 402, "caption": "Earlier: 06:42"}],
 "highlight": {"bbox": [210, 380, 760, 560], "label": "the young woman"},
 "guard": {"checked": true, "rewritten": false},
 "latency_ms": 4210}
```

- `highlight.bbox` is `[ymin, xmin, ymax, xmax]` normalised to 0–1000 on the current frame; draw it over the video.
- `frames[].t` → `/api/titles/{id}/frames/{t}.jpg`; every frame is at or before `P`.
- `guard.rewritten = true` means the output guard caught something and the answer was regenerated or replaced.

### `GET /api/health` (also `/healthz` locally; Cloud Run reserves that path)

`{"ok": true, "titles": 3}`.

## Limits

Questions are rate limited per client IP (HTTP 429 with `{"message": …}`); the UI should show a friendly note.
