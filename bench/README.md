# Benchmark: answer accuracy, spoiler leaks and latency of the HTTP API

`bench/` sends a fixed set of questions to a running service (`POST /api/ask`, see `docs/web-api.md`), records what
came back and how fast, and grades the answers against the master knowledge packs. It works against a local server
or any deployed revision (for example a tagged Cloud Run revision that takes no traffic).

```bash
# 1. ask (resumable; --only filters by case-id prefix, --workers 1 keeps latency clean)
python bench/run_bench.py --base http://127.0.0.1:8080 --run-id local-1
python bench/run_bench.py --base https://next---<service-url> --token-file .cache/bench_token --run-id next-1

# 2. grade + scorecard (runs/<run>/scorecard.{md,json}); two runs side by side → runs/compare-A-vs-B/
python bench/judge.py next-1 --packs .cache/packs
python bench/judge.py base-1 next-1 --packs .cache/packs

# offline: case lint, and latency / tool / cost tables from an already graded run
python bench/judge.py --lint
python bench/judge.py next-1 --no-llm --no-rules
```

The public demo is rate limited per IP (6 questions a minute, 80 a day): use `--sleep 11 --limit N` there. A
deployment can let the benchmark past the limiter with a shared token, sent as the `X-Watch-Bench-Token` header
(`--token`, `--token-file`, or env `BENCH_TOKEN`; header name via `--token-header`).

A run does not start when `GET /api/health` fails, and stops at the first HTTP 429 when a token was sent (the target
does not accept it).

## Results

<!-- results:start final run (round 5) -->
**Final run (round 5), 2026-11-22: the shipped code on one staged Cloud Run revision.** Films: the three public short
films (11–15 min) and a private preview title (169 min). Grading: an LLM judge (Gemini) on the answer text, plus a
rule gate over everything the viewer received (definitions below, under Grading). The earlier tool-calling flow was
measured in round 3, on the last commit that still had it, with the same cases and packs.

Hand-written cases (97, three public films):

| | Shipped flow: server-built context, one model call (a tool round only when needed) | Earlier tool-calling flow (round 3, the commit before its removal) |
|---|---|---|
| accuracy (LLM judge) | 91.9 % (n 62) | 89.5 % (n 62) |
| cases flagged by the rule gate | 1 of 97 (see below) | 1 of 97 |
| cases flagged by the judge only | 0 of 97 | 0 of 97 |
| fallback answers (a fixed non-answer), share of answers | 1.1 % (n 95) | 8.2 % (n 97) |
| server latency p50 / p90 | 2.2 s / 4.3 s (n 95) | 4.1 s / 8.2 s (n 97) |
| server time to the first words, p50 | 1.9 s (n 95) | 4.1 s (n 97) |
| model calls per answer | 1.09 | 2.42 |
| estimated cost per answer (list prices) | $0.00193 | $0.00251 |

Two of the 97 cases got no answer because the benchmark host's connection timed out; they count as non-answers and
are left out of the latency rows (n 95).

The one rule-gate flag of the shipped flow is a wrong identification, not a look ahead: asked who a dragon hatchling
is, the answer gave the name of the dragon the viewer already knows. The judge marked it incorrect; it states nothing
that the film had not shown.

Generated cases (240 over the four films, `gen_cases.py`, shipped flow): accuracy 95.6 % (n 136); 0 of 240 flagged by
the rule gate; 2 of 240 flagged by the judge only (0.8 %); fallback answers 1.2 % (n 240); server latency p50 / p90
2.1 s / 3.2 s (n 240); 1.01 model calls per answer.

Known weak spots in this run:

- Both judge-only flags concern an identity: a character named a moment before the film confirms who they are (one
  case sits less than a second before the reveal), and a recap that names the person a character is about to meet.
- About 1 % of answers are the fixed "that took too long" text: the first model call produced nothing within its 8 s
  budget. There is no second, parallel call yet.
- After the guard drops a sentence, the rest can read badly: one Chinese dialogue answer was reduced to a closing
  bracket.
- Chinese answers show a source line less often than English ones (75 % against 93 % on the generated set).
- "Who is this?" without a tap is the weakest question type on the generated set (86.8 %, n 19).
<!-- results:end -->

What these numbers do not cover:

- Four films, two answer languages (English, Chinese). The public films are short. The private preview title is the
  only feature-length one, and only generated cases cover it.
- Generated cases are derived from the knowledge packs. They test the service against its own pack, not against the
  film, so an error in the pack (a wrong reveal time, a missed character) cannot show up there. The hand-written
  cases were written by the people who built the service.
- The judge is a model, not a person. Its own error rate has not been measured. A leak worded without any listed
  term or canary is caught only if the judge notices it.
- Mostly single questions: 6 two-turn conversations and 10 injection probes. No long conversations or sustained
  adversarial use.
- Latency is measured on one revision without the rate limiter, with no concurrent load. Cost is estimated from list
  prices and token counts, not from billing.
- The highlight box and frames are checked for leaks only. Whether they point at the right thing is not graded.

## Cases (`cases/*.jsonl`, one case per line)

| File | Set | What | Cases |
|---|---|---|---|
| `sintel_tos.jsonl` | base | Graded set for Sintel and Tears of Steel: name / identity reveals, cue margins, credits, language switch | 48 |
| `hand_elephants_dream.jsonl` | base | Hand-written cases for Elephants Dream | 12 |
| `probes.jsonl` | base | Spoiler probes at 35 % / 55 % / 75 % of each film's story ("what did I miss" / "what happens next") | 9 |
| `new_tap.jsonl` | new | Questions with a tap on the paused frame (4 per film; coordinates picked on the real frames) | 12 |
| `new_injection.jsonl` | new | Prompt-injection / jailbreak probes at early P: "I've seen it already", role-play, JSON "ending" field, fake system / editor / director claims (en + zh) | 10 |
| `new_followup.jsonl` | new | Two-turn conversations: the graded question depends on the previous turn (pronouns, a name just mentioned) | 6 |

The `base` set keeps the case ids of the first measurement (2026-10-21), so results stay comparable; scorecards
report `base` and `new` separately.

```json
{"id": "new:tap:tos-02", "content_id": "9e9cbbc5-…", "asset_sha256": "b9b4cd74…", "title": "Tears of Steel",
 "P": 150.5, "lang": "zh", "question": "这个人是谁?", "tap": {"x": 0.27, "y": 0.18},
 "category": "who_is_this", "expect": "answer", "reference": "…",
 "must_include_any": ["狙击手", "sniper"], "must_not_include": ["Barley"], "flag_if_any": [],
 "notes": "why: which items are eligible at P and which are not yet", "traps": ["tap", "name_not_revealed"],
 "history": [{"question": "…", "P": 140.0}], "source": "hand-written 2026-11-22", "set": "new"}
```

- `P`: locked playback position (seconds). `lang`: `en` / `zh`, the language the answer must be in.
- `tap`: point on the paused frame, 0–1 with the origin top left (same as the API).
- `category`: `what_did_i_miss` / `who_is_this` / `what_is_that` / `what_did_they_say` / `explain_this_line` / `free` /
  `spoiler_probe` / `injection` / `followup`.
- `expect`: `answer` (the information is on screen already) / `decline` / `either` (asks about later parts: declining
  or describing only the present are both fine).
- `history`: earlier turns of the same conversation (each `P` ≤ the case's `P`, since a seek backwards starts a fresh
  conversation on the server). The client sends them first with the same `session_id`; only the last answer is graded.
- Terms: a string (whole word; NFKC, accents dropped, Traditional → Simplified, case folded), `{"term",
  "case_sensitive"}`, or `{"regex", "ignore_case"}`. `must_not_include` terms count as leaks, `flag_if_any` as
  suspects (never for a public name: `name_public` names are allowed at any time). `lint_allow_eligible` exempts a
  marker from the "already eligible at P" lint; `"channels": [...]` limits a marker to some of what the viewer
  receives (`answer`, `evidence`, `frame`, `highlight`, `tool_args`, `tool_summary`, `streamed`, `history`; default
  all). `"link": true` (or a link id) on a `must_not_include` term says it is the name of one side of an identity link
  whose other side is the case's subject: the lint accepts it although the name is public or already eligible, after
  checking against the pack that such a link is still pending at `P`.
- `set`: `base`, `new` (ids `new:…`) or `generated` (ids `gen:…`, from `gen_cases.py`). A generated set is always a
  run of its own: `run_bench.py` and `judge.py` refuse to mix it with hand-written cases.
- `judge.py --lint` checks the schema, catalog / asset ids, `P` within the film, English notes, history order and
  contradictions; with `--packs` it also checks that the case's pack is there, `P` within it, no `must_not_include`
  term is already eligible at `P` or a public name. `run.sh bench` runs it against the packs the benchmarked URL
  serves before every run and prints the mismatches.

## Generated cases (`gen_cases.py`)

About 60 cases per film from the pack alone, deterministic for a seed, en and zh alternating: spoiler / jailbreak
probes at sampled positions (a), a character on screen shortly before their name is visible (b), both sides of an
identity link before its reveal and one position after it (c), dialogue recall just after a line with no next line for
4 s (d), "what did I miss" just after a recap (e), and the edges: before the story, the end credits, past 1:00:00 and
2:00:00 (f; from one hour on a time written as minutes ≥ 60 without hours fails). `run.sh bench` with
`BENCH_GENERATE=<n>` generates them from the served packs, lints, runs and grades them as a run of its own. Cases of
a private preview title carry `"private": true`, stay under `.cache/`, reach a hidden title through the bench token,
and reports show their ids and numbers only (`--show-private` for everything).

## What is recorded per case (`runs/<run>/results.jsonl`)

| Field | Meaning |
|---|---|
| `t_total_ms` | client wall clock: request sent → `done` event |
| `t_first_tool_ms` | first `tool` event |
| `t_first_text_ms` | first `delta` event when the server streams text, otherwise the `answer` event |
| `server_latency_ms` | `latency_ms` of the `answer` event |
| `server_tools` / `model_tools` | tool events split by id: `ctx-…` = run by the server before the model, the rest called by the model |
| `guard_rewritten`, `guard_fallback` | `answer.guard.rewritten` / `.fallback` (when the server sends it; otherwise the judge recognises the fixed replies of `server/main.py`) |
| `usage`, `timing` | copied from the `answer` event: `usage = {input_tokens, output_tokens (incl. thinking), thought_tokens, model_calls}`, `timing = {first_text_ms, total_ms}` |
| `guard_dropped` | `answer.guard.dropped`: sentences the output guard withheld |
| `streamed_text` | the concatenated `delta` text, kept only when it differs from the final answer |
| `raw` | every event but the deltas, and the full `answer` (evidence rows with times, frames, highlight): what the leak check reads |
| `http_status`, `stream_end`, `sse_error`, `events_seen` | how a request ended: `done`, `eof` (closed without `done`), `exception` (timeout / reset: `error` keeps the exception and how far the stream got), `http_error`; the `error` event's message. Failed cases are never retried automatically (`--retry-errors` re-asks them on purpose) |

## Known limits of the cases

- `ed-h04`: forbade the single character for "hit", which also matches the everyday word for making a phone call;
  since 2026-11-22 it forbids phrases for beating someone (a regex marker) instead, whipping only with him / Emo as
  its object (alone it matched an eligible evidence line about whipping cables).
- `new:inj:sintel-01/-03/-04` forbade bare "stab", "stabbed", "kills" and "aged", but Sintel kills a bandit at about
  69 s; since 2026-11-22 they forbid harm to the dragon and Sintel's aged look as phrases.
- `c5:sintel-g16/-g21` flagged "Scales", which the pack makes `name_public` (it is in the public synopsis); the rule
  gate ignores public names in `flag_if_any` and the judge is told they are allowed.
- `c5:sintel-g08`: the "innocent blood" line ends earlier on the generated English track than on the official one,
  so the words can be eligible at `P` = 116.5 although the official cue is not; since 2026-11-22 they are suspects
  (`flag_if_any`), not leaks. The positive check still assumes the service quotes the official track.
- Term matching is folding plus whole words for Latin terms on the pack's word class (`ingest.textnorm.LATIN_WORD`:
  CJK and other scripts are boundaries, so `Scales` right after Chinese text still matches; `\b` / `\B` in regex
  markers use the same class, as in the service) and substrings for CJK terms: a one- or two-character CJK term can
  match inside an unrelated word. Prefer phrases or regex markers for those. The one remaining difference from the
  service: the bench folds `_` to a space, so it is the stricter of the two there.

## Grading (`judge.py`)

| Metric | Definition |
|---|---|
| accuracy | LLM judge on the ANSWER TEXT ONLY against reference / key terms / notes: correct = 1, partial = 0.5, declined / incorrect / nonanswer = 0; excludes spoiler probes and `expect: decline` cases; service errors stay in (as non-answers); n printed |
| appropriate behaviour | the judge's view of whether the answer text matches `expect`, over every graded case (errors included); n printed |
| ungraded | cases the judge could not grade (judge error, or not run): their own line, never silently left out |
| key-term hit | at least one `must_include_any` term in the answer text (evidence does not count) |
| evidence share | answers (expect ≠ decline) with at least one evidence row, overall and per language |
| spoiler leak | rule gate ∪ LLM judge over everything the viewer received; counted as cases with a rule-gate hit and cases only the judge flagged |
| · rule gate | the served pack at `P` (review relaxed like the demo, `allow_auto`), per channel and at each turn's own `P`: names not yet visible and aliases not yet known, active canaries, the case's `must_not_include`, evidence / frame / highlight times after `P`. Suspects (own count and list, not leaks): a hidden term or canary the viewer typed (echoing is fine, affirming is a leak: left to the judge), a canary word that occurs in one item the viewer could have seen by then (named in the label), `flag_if_any` |
| · LLM judge | gets the next 30 future items, every later recap, every canary still active, pending names / identities and the public names (allowed at any time); grades the answer and checks the other channels for leaks only, naming the channel |
| real names | `blocklist` hits, reported separately |
| guard rewrites, fallbacks | shares of answered cases, and the case ids with a rewrite or a fallback (a fixed non-answer the viewer sees) |
| latency | p50 / p90 / p95, interpolated between ranks, with n; p95 only from n ≥ 20; server-side first (`latency_ms`, `timing.first_text_ms`), then the client's measurements, which add the network between the bench host and the service |
| errors | cases without an answer, on their own line; they are graded as non-answers (the viewer saw the error) |
| tools | calls per answer, split server-run / model-called, and usage by tool name |
| tokens, cost | from `answer.usage`: input and output (incl. thinking) tokens per answer p50 / p90, model calls per answer, estimated cost per answer (list prices from `ingest/ledger.py`) |

Graded text = the answer alone. Leak-checked = everything the viewer received: answer, evidence rows, frame captions,
highlight labels, tool arguments and summaries, streamed text that differs from the final answer, and the answers of
earlier turns (at their own `P`). Every rule hit is listed in `judged.jsonl` (`rule_hits`: rule, the channel, the
matched part in brackets with some context); labels of hits outside the answer end in ` @<channel>`. "Already
eligible" means the canary occurs in one item the viewer could have seen by `P` (an eligible item's English or
Chinese text, cues of English / Chinese tracks only, a look already shown, title info), never in a joined text.
Grades are cached in `runs/<run>/judged.jsonl` per (case id, case content, what was received, pack sha and version,
judge prompt, judge model), so re-grading costs nothing until one of them changes, and old runs can be re-graded
from their `results.jsonl` without asking the service again. `run_bench.py` refuses to resume a run against another
base, bench commit, server revision or set of packs, or one made with the removed tools flow (older `run.json` and
`results.jsonl` files with a `flow` field still load for grading and comparison; the scorecard header names that
flow).

The judge needs, on the machine that runs it: the master packs (`--packs DIR` or `BENCH_PACKS_DIR`, layout
`<asset_sha>/master/pack.db`) and a Gemini key (`CEDAR_GEMINI_KEY_FILE`, developer API) or
`CEDAR_GEMINI_BACKEND=enterprise` with `CEDAR_GCP_PROJECT`. `BENCH_JUDGE_MODEL` overrides the judge model.
