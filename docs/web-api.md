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
external scripts, fonts or stylesheets (system fonts only). Everything must work at phone width (≥ 360 px) and in light / dark.

Page links: `/?title=<id>&t=<seconds>&q=<text>&lang=en|zh` opens that film paused at `t` with the question filled
in; `&ask=1` also sends it. An unknown id opens the first film. `/#code=…` unlocks private preview titles (see
`POST /api/unlock`).

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
  "synopsis": "A lonely young woman…", "playback": "standard"}]
```

- `playback`: `standard` (the film as ingested) or `preview` (a low-bitrate copy, 640 px wide: the UI may say so).
- Private preview titles are listed only while the browser holds a valid access code (see `POST /api/unlock`), after
  the public films, with `"private": true`; the response is then `Cache-Control: private, no-store`.

### `POST /api/unlock`

Access code for the private preview titles, as a JSON body `{"code": "…"}` (never in a URL, so it reaches no log).
`204` with the cookie `watch_access` (HttpOnly, Secure, SameSite=Lax, Path=/, 30 days) when the code is valid, else
`404`; at most 10 attempts per minute and IP, malformed ones included (`429`, `Retry-After`). A shared link carries the
code in the URL fragment (`/#code=…`, which browsers never send): the page reads it, POSTs it, removes it from the
address bar and reloads `/api/titles`. Without the cookie every endpoint answers `404 unknown title` for a private
title, the question endpoint included; with it, frames and the video redirect of a private title are
`Cache-Control: private, no-store`. A request carrying a valid bench token (`X-Watch-Bench-Token`, see Limits) counts
as unlocked too, so the benchmark host can test a private preview title without any access code.

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
- `session_id` keeps follow-up questions in one conversation. After a seek **backwards**, the turns asked later than
  the new `p` are left out of what the model sees (they may contain things not yet seen) and come back once the viewer
  passes that point again. Without a `session_id` nothing is kept.

The response is `text/event-stream` (read it with `fetch` + a stream reader; `EventSource` cannot POST). Events:

| `event:` | `data:` (JSON) | Meaning |
|---|---|---|
| `gate` | `{"p": 431.2, "eligible_total": 140, "locked_total": 380}` | Gate computed for this question |
| `tool` | `{"id": "t1", "name": "story_so_far", "label": "Reading the story so far", "args": {…}}` | A lookup started: a tool the agent called, or (ids `ctx-1`…`ctx-3`) one the server ran to build the context. `args` are model-written, so they pass the output guard first (names not visible yet become labels; `{}` if anything else is caught) |
| `tool_result` | `{"id": "t1", "name": "story_so_far", "summary": "1 recap + 6 events, up to 06:58"}` | That lookup finished |
| `highlight` | `{"bbox": [210, 380, 760, 560], "label": "the young woman", "frame_t": 430}` | The tap matched an entity whose box was measured within 1 s of the paused frame; draw it right away |
| `delta` | `{"text": " She is looking for her dragon."}` | Next piece of the answer: one sentence that already passed the output guard, with its leading space if any. Append as is |
| `answer` | see below | Final, guarded answer. Normally `text` equals the concatenation of all `delta` texts; when it differs (text written before a tool call, a fallback) it replaces what was streamed |
| `error` | `{"message": "…"}` | Something failed before any answer text; show it politely |
| `done` | `{}` | Stream finished |

Order: `gate`, then the trace (`tool` / `tool_result`, possibly `highlight`), then `delta`s interleaved with further
`tool` / `tool_result` pairs, then `answer` (or `error`), then `done`. With `WATCH_FLOW=tools` there is exactly one
`delta`, holding the whole answer, just before `answer`.

`answer` payload:

```json
{"text": "That's the young woman who …",
 "evidence": [{"type": "scene", "start_s": 398.0, "end_s": 420.0, "text": "…"}],
 "frames": [{"t": 402, "caption": "Earlier: 06:42"}],
 "highlight": {"bbox": [210, 380, 760, 560], "label": "the young woman", "frame_t": 430},
 "guard": {"checked": true, "rewritten": false, "dropped": 0},
 "cut_off": false,
 "latency_ms": 4210,
 "usage": {"input_tokens": 2950, "output_tokens": 84, "thought_tokens": 40, "model_calls": 1},
 "timing": {"first_text_ms": 1380, "total_ms": 2210},
 "flow": "context"}
```

- `highlight.bbox` is `[ymin, xmin, ymax, xmax]` normalised to 0–1000 on frame `frame_t` (≤ `P`, from the shot's
  keyframe). In `answer` it is `null` unless the answer text mentions the tapped entity, so the box never contradicts
  the text; the early `highlight` event is sent only when the box is fresh (within 1 s of `floor(P)`).
- `evidence`: only what the answer rests on (at most four rows, a recap as the matching sentence), each checked by the
  output guard; empty for a refusal or a fallback.
- `frames[].t` → `/api/titles/{id}/frames/{t}.jpg`; every frame is at or before `P`.
- `guard.rewritten = true` means the output guard caught something: `dropped` sentences were never sent, or the
  answer was regenerated (one retry) or replaced by a fixed reply.
- `cut_off: true`: the answer ran out of time (or the model failed) partway; the sentences sent are complete and
  guarded, but the answer may be unfinished. The page says so.
- `usage` counts every model call of the question (the nested `look_closer` call included); `output_tokens` includes
  `thought_tokens`. `timing.first_text_ms` is the time to the first `delta`.

### `GET /api/health` (also `/healthz` locally; Cloud Run reserves that path)

`{"ok": true, "titles": 3, "revision": "<Cloud Run revision>", "packs": [{"sha": "…", "dist": "master", "version": "11"}]}`
— public films only: their count and the pack each one serves (a benchmark grades against the same packs and does
not resume a run against another revision).

## Limits

Questions are rate limited per client IP (HTTP 429, `Retry-After` header) with the body
`{"message": "…", "limit": "per_minute" | "daily", "scope": "ip" | "total", "retry_after_s": 42}`; the UI should show a
friendly note worded for the limit that was hit. Benchmark runs against a deployed revision skip the limiter by sending
`X-Watch-Bench-Token: <WATCH_BENCH_TOKEN>`; such requests may also choose the flow with `X-Watch-Flow: context|tools`
and see private preview titles.
