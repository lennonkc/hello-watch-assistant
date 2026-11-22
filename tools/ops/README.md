# Ops jobs

`tools/ops/run.sh <mode>` is the single entry point for operations that need cloud credentials, the master packs or
the films. It is written to run from a fresh checkout of an exact commit on a Mac runner with an almost empty
environment (macOS bash 3.2), but works on Linux too.

| Mode | What it does | Changes anything? |
|---|---|---|
| `info` | tool versions, settings paths, gcloud auth, the service URL, traffic tags, recent revisions, what is under the packs prefix, free disk | no |
| `tests` | unit tests + benchmark case lint, in the checkout's `.venv` | no |
| `deploy-staging` | builds the image of this commit (`deploy/web/build.sh`) and deploys it as a new revision with **no traffic**, tagged `TAG` (default `next`), with session affinity and its WHOLE environment computed from the parameters (`deploy/web/env.sh`, `--set-env-vars`: project, backend, bucket, packs `PACKS_PREFIX` or what production serves, private titles `PRIVATE_PREFIX` or none, limits, `STAGING_ENV`) plus a fresh `WATCH_BENCH_TOKEN` (written to `.cache/bench_token` only after a successful deploy) unless `RELEASE=1`; prints the env names and the tagged URL, and fails unless `/api/health` is ok with every public film | new revision only |
| `bench` | downloads the packs the URL serves (refused when its `/api/health` reports other pack versions), lints the hand-written cases against them (mismatches printed, bounded), runs `bench/run_bench.py` against `BENCH_URL` (default: the tagged revision), grades with `bench/judge.py`, prints the scorecard summary. `BENCH_GENERATE=<n>` runs n generated cases per film instead (`BENCH_TITLES` public / private / all), as a run of its own; a private preview title's cases, results and scorecards stay in `.cache/`, and the log shows their ids and numbers only (`BENCH_SHOW_PRIVATE=1`: everything) | no |
| `promote` | checks the tagged revision first (Ready, image built from the checked-out commit, `/api/health` ok with every public film, no `WATCH_BENCH_TOKEN` unless `PROMOTE_WITH_BENCH_TOKEN=1`; prints its env names), then moves 100 % of traffic to it; prints traffic before and after, and as its last lines the revision production served before and the one-line `gcloud run services update-traffic … --to-revisions REV=100` that rolls back to it (the whole split if traffic was split) | production traffic |
| `job` | runs the one-off job `JOB` of this commit (`tools/ops/job.sh`, below) | whatever it does |

Settings: the runner's git-ignored `.cache/ops.env` (`HOME`, `HELLO_WATCH_GEMINI_KEY_FILE`, `HELLO_WATCH_MOVIES_DIR`,
`HELLO_WATCH_DEVICE_PACKS_DIR`, `HELLO_WATCH_INGEST_CACHE`), then the committed `tools/ops/params.env` (all options
with their defaults). Project, bucket, packs prefix and URLs are discovered from `gcloud` and from the service's own
environment at run time. Secrets (the Gemini key, the bench token) are never printed.

Python: the `.venv` is built with 3.13 (what the image runs), else 3.12, else 3.14, else a uv-managed 3.13; never
3.15+. Packages are installed as wheels only (`--only-binary=:all:`), so a missing wheel fails at once with its name.
Job logs stay short: listings are summarised and capped, full tool output goes to `.cache/ops-logs/`, and the bench
prints the scorecard summary and failing case ids (the full scorecard stays in `.cache/bench/runs/<run>/`).

`deploy/web/deploy.sh` is the full production deploy (service account, IAM, 100 % traffic); the agent prompts ship in
the image (`prompts/agent/`).

## Jobs (`JOB=<name>[,<name>…]` in `params.env`, run with `tools/ops/run.sh job`; several run in order and stop at the first failure)

| Job | What it does | Writes |
|---|---|---|
| `inspect-packs` | per public film: pack versions per distribution in the ingest cache and under the bucket prefixes, raw `{oNN}` / `{other}` placeholders, blank and out-of-order cues; per `PRIVATE_TITLE_SHAS` entry: where its pack lives, manifest flags, row counts, vectors, story bounds, recaps, speakers, characters, safeguards, thumbnails, what the server would miss (`server.packs.check_pack`), whether the film file is under `HELLO_WATCH_MOVIES_DIR` (matched by sha256) and which local catalog JSON describes it (path and field names only) | nothing |

| `publish-public-packs` | the ingest cache's master pack of each public film (pack.db, MANIFEST.json, pack.json, thumbs/) → `<PACKS_PREFIX>/<sha>/master/`, only if it has no raw `{oNN}` / `{other}` / `{cNN}` placeholders or blank cues and every file of its manifest is there; says where any other `{…}` token sits and whether a viewer could see it; uploads only files that are missing or differ in size, pack.db last; checks the service account can read the bucket. Never writes under the live `demo-packs` | the bucket (new prefix) |
| `publish-private-title` | for each `PRIVATE_TITLE_SHAS` entry: the newest device-auto / master pack (device packs dir or ingest cache) with the thumbnails next to it → `<PACKS_PREFIX>/<sha>/<dist>/`; a 640 px H.264 preview (about 400 kbps + 64 kbps stereo AAC, a keyframe every 2 s, faststart, no container metadata) of the local film file (matched by sha256) → `previews/<sha>-640.mp4`; then `<PRIVATE_PREFIX>/titles.json` with the title (local `ingest/titles/<id>.json`) and a synopsis (local `catalog/catalog.json` `synopsis_full`, cut at a sentence end within 260 characters, printed for review) and `PRIVATE_ACCESS_SHA256`. Resumable; `PREVIEW_REBUILD=1` re-encodes | the bucket (new prefixes) |
| `bench-show` | per case of a graded run (`BENCH_SHOW_RUN`, default the latest; `BENCH_SHOW_CASES`, default every failing case): P, language, question, answer (≤ 400 characters), rule hits with the matched text (re-checked with this commit's rules when the bench's packs are in `.cache/packs`), the judge's reason, timings, tools (a private preview title's cases: id, kind and counts only, unless `BENCH_SHOW_PRIVATE=1`); at most 120 lines | nothing |
| `rejudge` | grades stored runs again (`BENCH_REJUDGE_RUNS=<id>[,<id>]`, optional `BENCH_COMPARE`) with this commit's judge and cases against the packs of the last bench, without asking the service; prints the summary (at most 120 lines) | `.cache/bench/runs/<id>/` (grades) |
| `logs` | Cloud Logging of the `TAG` revision for the last `LOGS_SINCE` (default 2h): severity ≥ WARNING plus the question records flagged error / cut_off / retried, optionally narrowed by `LOGS_FILTER`; time, severity and the first 300 characters | nothing |

The job's full output is kept in `.cache/ops-logs/job-<name>.log`; the job log shows at most 80 lines. A private
preview title appears in job logs as its sha256 (or "private preview title #n") and as counts, never with its title
or file names, with two deliberate exceptions for review: `publish-private-title` prints the synopsis it writes, and
`inspect-packs` prints the speaker names of its pack. Benchmark logs (`bench`, `bench-show`) show a private preview
title's cases by id and numbers only unless `BENCH_SHOW_PRIVATE=1`.
