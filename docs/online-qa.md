# Online Q&A Integration (Knowledge Pack v1.1, web demo)

> For developers building the Q&A service on Cloud Run (ADK agent + web player).
> The contract is [knowledge-pack.md](knowledge-pack.md) ("C1"); this document only explains how to use the v1.1 data,
> how to call retrieval, and how to organise the answering context.

## 1. What is in the pack (v1.1 additions)

| Table | Contents | Typical use | Gate (C1 §5.2) |
|---|---|---|---|
| `character_exemplars` | 1–3 in-film reference stills per character (`t_s` second + bbox), verified by drawing the box | "Who is this": show reference images to the model; on-frame highlighting | character eligible and `t_s ≤ floor(P)` |
| `shot_captions` | Per-shot descriptions (shots < 1 s are merged into a neighbour) | Fine-grained evidence for "what just happened" | `reveal_at_s + 3 ≤ P` |
| `shot_entities` | Characters / objects on screen in a shot: screen position, bbox (on keyframe `bbox_t_s`), action | Tap-to-disambiguate ("who's the one on the left") | follows the shot; **the current shot has its own rule, see §3** |
| `cue_speakers` | Speaker for each subtitle line (diarization + visual attribution, with confidence) | "What did he just say / who said it" | follows the cue: `end_s + 1 ≤ P` |
| `recaps` | "Story so far" recap at the end of each scene (en / zh), generated only from earlier information, names re-checked | "What did I miss" | `reveal_at_s + 3 ≤ P` |
| `video_windows` | Sub-windows of ≤ 30 s cut along segments (≈ 5 s overlap) | Carrier for video embeddings; on a hit, clip and read closely | `end_s + 3 ≤ P` |
| `embeddings` | gemini-embedding-2, 1536 dims: text items + video windows (film with audio track on GCS) | Semantic localisation | follows the owning item |

**All new rows are `review_state = auto` and exist only in the master pack.** The device pack, by rule, only contains
pre-reviewed / human-reviewed rows, so the demo must read the master pack and enable `allow_auto` (the development
switch permitted by C1 §5.2 supplementary rule 1). The master pack contains `review_log`; it must stay on the server and
must never be sent to the browser.

Output location: `.cache/ingest/<asset_sha256>/master/` (`pack.db`, `thumbs/tNNNNN.jpg`). The three demo films are
Sintel, Tears of Steel and Elephants Dream; their assets are in the bucket at `assets/<sha>.mp4`.

## 2. Retrieval: `ingest/retrieval.py`

```python
from ingest import retrieval
hits = retrieval.search(db_path, P, "what happened to the baby dragon", k=5,
                        mode="vector", allow_auto=True,
                        types={"scene", "event", "recap", "shot_caption", "character_fact", "object", "cue"})
for h in hits:   # Hit(item_type, item_id, start_s, end_s, eligible_from, text, score, ...)
    assert h.eligible_from <= P
```

- Ordering is a hard constraint: first compute the eligible set at P, then rank only within that set (vector / full-text /
  three-way RRF). Do not bypass it by querying `embeddings` yourself.
- `mode`: for evidence retrieval use `"vector"` with text types (`"fts"` and `"hybrid"` also exist). To find "the clip that
  can be played", make a separate call with `types={"video_window"}, k=1`. Cross-modal cosine (≈ 0.6) is lower than
  text-to-text (≈ 0.8), so in a single mixed ranking video windows never reach the top.
- Query embeddings use `CEDAR_GEMINI_BACKEND` (`developer` / `enterprise`) and are cached in-process; offline tests can
  pass `qvec=` directly.
- Sintel PoC (20 questions × 2 positions P, top-3 time overlap): vector 36/40, full-text only 22/40, hybrid 33/40;
  full-text scores 0 on Chinese questions.
- **Known issue: very short dialogue lines can hijack the ranking** (e.g. asking "what happened to that little dragon" at
  760 s returns "a dragon" lines first). For "what happened" questions, prefer the latest eligible `recap` plus the events
  after it, and use retrieval only as a supplement; or exclude / down-weight `cue`.

## 3. Current shot (tap the frame: "who is this / what is that")

The shot containing P only becomes eligible at shot end + 3 s, yet the viewer has already seen the frame. C1 §5.2 has a
dedicated rule for this:

- Use only `shot_entities` with `bbox_t_s ≤ floor(P)`, and only their `character_id` / `object_id`, `position` and `bbox`;
- The character must be eligible, and its label is re-decided per C1 §6 **at time P** (do not use the pre-rendered
  `label_*`; it is rendered for the shot's end time);
- The shot's `caption_*` and `action_*` are not usable (they may contain information from later in the shot).

Disambiguate by testing the tap coordinate against the bbox (`[ymin, xmin, ymax, xmax]`, normalised 0–1000) for
containment / nearest. Then give the model eligible reference images from `character_exemplars` for comparison, or draw
a circle around each character on the current frame (the AutoAD-Zero approach).

## 4. Breakpoint context (MovieChat breakpoint mode)

The context for one answer has three layers. When the budget is tight, **keep the near window first** and drop long-range memory:

1. **Near window**: Gemini static video, `gs://…/assets/<sha>.mp4` clipped to `[max(story_start, P − W), floor(P) − 1]`,
   with W = 30–60 s; plus eligible thumbnails in that window (`t_s ≤ floor(P)`) and dialogue (including `cue_speakers`).
2. **Long-range memory**: the latest eligible `recap` + eligible events / scenes after it; add a few items from §2
   retrieval depending on the question.
3. **Current frame**: `thumbs/t{floor(P):05d}.jpg` (+ the on-screen entities from §3).

Fallback (look-back): only when text evidence is judged insufficient, escalate to re-answering with the near-window video;
the window only ever covers time before P, never after. Agentic video mode cannot clip, so it is not used online.

Output must still pass C1 §7: blocklist (real names), future canaries, name visibility. The web demo implements these
checks in `server/guard.py`.

## 4a. The web demo service (`server/`)

The demo in this repository follows this guide: an ADK `LlmAgent` on Gemini 3.8 Flash (Agent Platform, `WATCH_MODEL`)
over `server/knowledge.py` (gate-first views at P). P, title, language and tap come only from the request, on the
server; tools read them from `ToolContext.state`, so the model has no parameter that could move P. HTTP contract:
[web-api.md](web-api.md).

**Context first, tools second** (`WATCH_FLOW=context`, the default). Measured on the live demo, most tool calls were
local lookups that cost nothing to compute but one model round trip (≈1.5 s) each. So the server runs them itself for
every question, `server/context.py`:

- the current shot's entities (labels at P, looks, facts, boxes with the keyframe time) and, for a tap, the point on
  the 0–1000 grid and the entity it geometrically falls on;
- the lines of the last 60 s with speakers, or the last few lines before that if the last minute was silent;
- the story so far: the latest recap and the events / scenes after it;
- the paused frame `thumb(floor(P))` as an image, with a ring drawn at the tap. Only the ring is drawn: the boxes come
  from a keyframe that can be seconds older than the paused frame, so the prompt tells the model the ring is
  authoritative.

An ADK `before_model_callback` puts this context, plus the conversation so far, in front of the question in every
model request of the question. Nothing of it is written to the ADK session (each question runs in a throwaway session),
so stale context cannot pile up. Only `search_moments` (anything earlier the summary does not cover) and `look_closer`
(movement across the last 30 s) stay callable. The step trace still shows the server-run lookups (`ctx-1`…`ctx-3`).

**Conversation memory.** Per browser session the server keeps (P, question, answer exactly as sent). A question at P gets
only the turns asked at or before P (at most `WATCH_HISTORY_TURNS`): eligibility is monotone in P, so all of it is still
eligible; after a seek backwards the later turns are hidden, and they come back once the viewer passes that point again.
A dropped sentence or a tool payload never enters this memory.

**Streaming through the guard.** The run streams (`StreamingMode.SSE`). Model text is cut into sentences (English and
Chinese punctuation, `server/answer.py`). Each finished sentence is checked by the §7 guards together with everything
already sent, so terms spanning a sentence boundary are caught too. It is sent as a `delta` only if it passes; a failing
sentence is dropped and logged. A model call's first sentence is held back until a second one starts or the call ends
without a function call, so a "let me check" written before a tool call never reaches the viewer (counted as
`held_preamble`). If nothing survives, the question is asked once more, not streamed, with a note listing the terms to
avoid; if that fails too, a fixed reply is sent. No unguarded text leaves the server.

**Budgets and deadlines** (`server/config.py`, all overridable):

| What | Default | On expiry |
|---|---|---|
| Query embedding (`search_moments`) | 1.5 s, one attempt, no retry loop | full-text ranking; embeddings paused for 60 s |
| `look_closer` nested call | 10 s | the tool returns a note; the model answers without it |
| Tool rounds per question | 2 (3 in the `tools` flow) | function calls are switched off (`FunctionCallingConfig` `NONE`) and the model is told to answer |
| Soft deadline | 9 s | same as above |
| Silence from the agent run | 12 s | the run is stopped |
| Whole question | 25 s | the run is stopped; complete sentences already sent stand, else a "took too long" reply |

**Observability.** The `answer` event carries `usage` (input / output / thought tokens, model calls) and `timing`
(time to first text, total). Each question also writes one JSON log line (`ask {…}`) with the flow, P, tools called,
search mode, usage, timing, sentences dropped and their guard kinds, held preambles, retry, fallback and whether the
highlight was kept. The log line never contains the question text or the bench token.

**The earlier flow** (`WATCH_FLOW=tools`) stays available for benchmarking: five tools (`story_so_far`,
`search_moments`, `recent_dialogue`, `on_screen`, `look_closer`) and a routing-table instruction. Its ADK session
carries the conversation and is replaced after a seek backwards or a guard rewrite. The whole answer is guarded at
once and sent as a single `delta`.

`look_closer` sends the current frame plus stills from the 30 s before P (every 3 s, all `≤ floor(P)`). The `gs://` clip
mode is optional (`WATCH_LOOK_VIDEO=1`) because clipping a whole-film URI adds tens of seconds.

## 5. Gemini backends

- `CEDAR_GEMINI_BACKEND=enterprise`: Gemini Enterprise Agent Platform (formerly Vertex AI),
  `genai.Client(enterprise=True, project=…, location="global")`; **location must be `global`** (regional endpoints return 404).
  Video is referenced directly by `gs://` URI, with no 48 h expiry.
- gemini-3.5-transcribe does not support the Interactions API on Agent Platform, so transcription still goes through the
  Developer API (key in Secret Manager).
- Details and IAM: [cloud-deploy.md](cloud-deploy.md).
