# Online Q&A Integration (Knowledge Pack v1.1, web demo)

> For developers building the Q&A service on Cloud Run (ADK agent + web player).
> The contract is [knowledge-pack.md](knowledge-pack.md) ("C1"); this document only explains how to use the v1.1 data,
> how to call retrieval, and how to organise the answering context.

## 1. What is in the pack (v1.1 additions)

| Table | Contents | Typical use | Gate (C1 §5.2) |
|---|---|---|---|
| `character_exemplars` | 1–3 in-film reference stills per character (`t_s` second + bbox), verified by drawing the box | "Who is this": show reference images to the model; on-frame highlighting | character eligible and `t_s ≤ floor(P)` |
| `shot_captions` | Per-shot descriptions (shots < 1 s are merged into a neighbour) | Fine-grained evidence for "what just happened" | `max(reveal_at_s, end_s) + 3 ≤ P` |
| `shot_entities` | Characters / objects on screen in a shot: screen position, bbox (on keyframe `bbox_t_s`), action | Tap-to-disambiguate ("who's the one on the left") | follows the shot; **the current shot has its own rule, see §3** |
| `cue_speakers` | Speaker for each subtitle line (diarization + visual attribution, with confidence) | "What did he just say / who said it" | follows the cue: `end_s + 1 ≤ P` |
| `recaps` | "Story so far" recap at the end of each scene (en / zh), generated only from earlier information, names re-checked | "What did I miss" | `max(reveal_at_s, upto_s) + 3 ≤ P` |
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

**Context first, tools second.** Measured on the live demo, most tool calls were
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
(movement across the last 30 s) stay callable, for one round. The step trace still shows the server-run lookups (`ctx-1`…`ctx-3`).

**Conversation memory.** Per browser session the server keeps (P, question, answer exactly as sent). A question at P gets
only the turns asked at or before P (at most `WATCH_HISTORY_TURNS`): eligibility is monotone in P, so all of it is still
eligible; after a seek backwards the later turns are hidden, and they come back once the viewer passes that point again.
A dropped sentence or a tool payload never enters this memory.

**Streaming through the guard.** The run streams (`StreamingMode.SSE`). Model text is cut into sentences (English and
Chinese punctuation, `server/answer.py`). Each finished sentence is checked by the §7 guards together with everything
already sent, so terms spanning a sentence boundary are caught too. It is sent as a `delta` only if it passes; a failing
sentence is dropped and logged. Besides spec §7 the guard applies the identity co-occurrence rule
(`server/guard.py`) while an identity link a = b is pending (a link a reviewer rejected never is):

- a *reference* to a character is a name or alias (visible or not), or its label with any determiner and adjectives
  in front of the label's head noun, plurals included: "the wolf" also gives "that wolf", "a grey wolf", "wolves";
  in Chinese the label (without a leading demonstrative or measure word such as "that one" / "a") and, loosely, the
  noun after its last possessive particle;
- a *passage* is a sentence plus the following sentences that continue it with a pronoun (one among their first five
  words, or a leading "This / That"; in Chinese a third-person pronoun among the first six characters, or a leading
  "this / that"; the patterns are in `server/guard.py`):
  "A grey wolf has appeared. It is Bob." is one passage;
- no passage may contain a reference to a and one to b;
- when the question is about a, b's specific references (names, aliases, its label: "the boy", "that boy") may not
  appear anywhere in the answer, not even in "the film hasn't said whether this is Bob"; the same the other way round
  for a question about b. The question is about a when the tap falls on a; otherwise when the question refers to a
  and not to b ("Is the wolf Bob?" names both, so only the passage rule applies); otherwise when it points at the
  screen ("who is this?", or its Chinese equivalents) while a is on screen and b is not. An a that is merely on
  screen ("What did I miss?") bars b only per passage, so story lines about b still get through.

The tool arguments the step trace shows go through the same check, whole, before they are shortened for display
(names not visible yet become labels; anything else the guard objects to is not shown). Regex canaries use the same
Latin word boundary as words: `\b` in a canary does not count Chinese characters as word characters.

What this cannot catch: a pronoun or paraphrase that links a and b across sentences without a lexical cue ("Nobody
has seen Bob since then." after a passage about the wolf) when the answer's subject is not known from the tap or the
question. The time gate keeps the reveal out of the model's context, and the judge-measured leak rate of the
benchmark covers what is left.

**Sources shown with the answer** (`server/evidence.py`). The context is attached whether or not the
answer uses it, so it is not the evidence list. The lookups and tools only offer candidates (recaps sentence by
sentence, story items, lines, the tapped entity's facts, search hits); once the answer is final the server keeps those
that share content words with the answer's *assertive* sentences (two, or one for a fact about the tapped entity the
answer talks about), at most four, with one frame each, the tapped entity's reference stills and the paused frame. A
sentence that only says the film has not shown, said or made clear something yet ("We don't know yet why…", and the
Chinese equivalents in `server/evidence.py`) rests on nothing, so "Alice met Bob in a meadow. The film hasn't said
why." keeps the sources of its first sentence and a pure refusal or a fallback shows none and no frames. For a
Chinese answer, each character it names in Chinese counts as that character's English label / name, and a candidate
also matches through its own Chinese text when the pack has one (shown to the Chinese viewer instead of the English).
Every line, frame caption and the highlight label passes the guard relative to the answer's subject, with the same
references as above: next to an answer about a (asked about, referred to, or on screen while the answer does not refer
to b) nothing may refer to b, and the reverse. The sources run after the answer with their own budget
(`WATCH_SOURCES_TIMEOUT_S`, 0.8 s): slow frames or an error cost the sources, never the answer.

The prompt forbids text before a tool call. The hold rule (`server/main.py`, `Streamer`): a model call's text is held
until its first sentence is complete and a second one has started, or the call ends without a function call; if that
first sentence looks like a preamble ("Let me look at the last scene.", or a Chinese "one moment, let me check"), the
whole call's text is held until the call ends — sent if no function call follows, dropped if one does
(`held_preamble`). If the run stops early (deadline, stall, error), held text is sent only when it is a complete
sentence that does not look like a preamble, so
a stall between "Let me look…" and the function call ends in the "took too long" reply, not in the preamble. Text that
did go out in a call that then called a tool (`leaked_preamble`: a first sentence that did not look like a preamble)
is guarded, but never counts as the answer: the final `answer` event leaves it out (the browser replaces what was
streamed), it is not stored in the conversation memory, and if nothing else was written the question gets a fallback
(`preamble_only` in the log line). An answer cut short (`cut_off`) is not stored either. If nothing survives the guard,
the question is asked once more, not streamed and without tools, with a note listing the terms to avoid, inside its own
budget; if that fails too, a fixed reply is sent — the "can't answer without giving something away" wording only when
the guard held something back, the neutral "took too long" one otherwise (deadline, tool rounds used up, nothing
written). There is no retry when less than 1.5 s is left, and none for a *spoiler probe* whose draft tripped the guard
(`declined` in the log line): a second draft of a decline would only arrive later, so the short decline goes out at
once. No unguarded text leaves the server.

**Spoiler probes** (`context.spoiler_probe`): questions about the ending, what happens next or later, who dies or
survives, who someone really is, a twist (English and Chinese patterns). The context of such a question carries a note
that the whole reply is one short sentence saying it hasn't been shown yet (optionally one about what is on screen),
with nothing connected to the future in it; prompt rule 4 says the same for every question. The log line marks them
(`probe`), so the rewrite rate of probes can be followed separately.

**Budgets and deadlines** (`server/config.py`, all overridable):

| What | Default | On expiry |
|---|---|---|
| Query embedding (`search_moments`) | 1.5 s, one attempt, no retry loop | full-text ranking; embeddings paused for 60 s |
| `look_closer` (stills + nested call) | 6 s, the paused frame + at most 8 stills (read concurrently, at most half the budget), and never more than what is left of the question minus 3 s for the answer step (`WATCH_ANSWER_RESERVE_S`); with less than 1 s it does not start | the tool returns a note; the model answers without it (slow stills: only the paused frame is looked at) |
| Paused frame for the context | 1.5 s (`WATCH_FRAME_WAIT_S`; on-demand titles fetch it from the bucket) | the context goes without the image |
| Model-called tool rounds per question | 1 | function calls switched off (`FunctionCallingConfig` `NONE`), the model is told to answer now |
| Soft deadline | 6 s | same as above |
| Silence from the agent run | 8 s | the run is stopped |
| Whole question | 10 s, retry included | the run is stopped; complete sentences stand (a last one that ends with sentence punctuation counts, unless it looks like a preamble), else a "took too long" reply |
| Guard retry | 4 s (`WATCH_RETRY_TIMEOUT_S`) inside what is left; skipped below 1.5 s (`WATCH_RETRY_MIN_S`) or for a spoiler probe | the fixed reply |
| Sources after the answer | 0.8 s (`WATCH_SOURCES_TIMEOUT_S`) | the answer is sent without frames, or without sources |

**Observability.** The `answer` event carries `usage` (input / output / thought tokens, model calls), `timing`
(time to first text, total) and a `request_id` (8 hex characters; also in `error`). Each question also writes one JSON
log line (`ask {…}`, with the same id as `rid`) with the flow, P, whether it reads as a spoiler probe, tools called,
search mode, usage, timing, sentences dropped and their guard kinds, held preambles, retry or decline, fallback and
whether the highlight was kept; every other line written while answering it (guard drops with the dropped draft
sentence, timeouts, skipped frames or sources) ends in `[rid=…]`. The log never contains the question text or the
bench token.

**Long films.** Timestamps are `mm:ss`, and `h:mm:ss` from one hour on. Everything about a pack that does not depend on
P (the gate's candidate list and lookups, dialogue tracks, shots, speakers) is built once per pack and reused, and the
per-question build runs in a worker thread: on a synthetic 10,000-cue / 3,000-shot pack it takes 30–50 ms (about 120 ms
for the first question, which builds the indexes).

**The earlier flow, removed.** Until round 3 the service could also answer the earlier way (`WATCH_FLOW=tools`): one
agent choosing among five tools (`story_so_far`, `search_moments`, `recent_dialogue`, `on_screen`, `look_closer`) by a
routing-table instruction, the whole answer guarded at once and sent as a single `delta`. It ran on the same plumbing
(budget callback, deadlines, guard, source selection), so the comparison measured the server-built context and the
one-round tool cap. Three benchmark rounds in a row gave the same picture, so it was deleted; `X-Watch-Flow` is
ignored since. Last comparison, hand-written set (97 cases), staged revision 00010 = finale `646e7ad`:

| | context flow | tools flow (removed) |
|---|---|---|
| Accuracy | 91.9 % | 89.5 % |
| Fallback replies | 1.0 % | 8.2 % |
| Guard rewrites | 2.1 % | 9.3 % |
| Server time p50 / p90 | 2.2 s / 3.9 s | 4.1 s / 8.2 s |
| Model calls per answer | 1.15 | 2.42 |
| Rule-gate leaks | 0 | 1 (in evidence) |

`look_closer` sends the current frame plus stills from the 30 s before P (every 3 s, all `≤ floor(P)`). The `gs://` clip
mode is optional (`WATCH_LOOK_VIDEO=1`) because clipping a whole-film URI adds tens of seconds.

## 5. Gemini backends

- `CEDAR_GEMINI_BACKEND=enterprise`: Gemini Enterprise Agent Platform (formerly Vertex AI),
  `genai.Client(enterprise=True, project=…, location="global")`; **location must be `global`** (regional endpoints return 404).
  Video is referenced directly by `gs://` URI, with no 48 h expiry.
- gemini-3.5-transcribe does not support the Interactions API on Agent Platform, so transcription still goes through the
  Developer API (key in Secret Manager).
- Details and IAM: [cloud-deploy.md](cloud-deploy.md).
