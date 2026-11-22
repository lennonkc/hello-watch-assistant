# Ops jobs

`tools/ops/run.sh <mode>` is the single entry point for operations that need cloud credentials, the master packs or
the films. It is written to run from a fresh checkout of an exact commit on a Mac runner with an almost empty
environment (macOS bash 3.2), but works on Linux too.

| Mode | What it does | Changes anything? |
|---|---|---|
| `info` | tool versions, settings paths, gcloud auth, the service URL, traffic tags, recent revisions, what is under the packs prefix, free disk | no |
| `tests` | unit tests + benchmark case lint, in the checkout's `.venv` | no |
| `deploy-staging` | builds the image of this commit (`deploy/web/build.sh`) and deploys it as a new revision with **no traffic**, tagged `TAG` (default `next`), with session affinity, a fresh `WATCH_BENCH_TOKEN` (kept in `.cache/bench_token`) and `STAGING_ENV`; prints the tagged URL | new revision only |
| `bench` | downloads the master packs, runs `bench/run_bench.py` against `BENCH_URL` (default: the tagged revision), grades with `bench/judge.py`, prints the scorecard | no |
| `promote` | moves 100 % of traffic to the tagged revision; prints traffic before and after | production traffic |
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

## Jobs (`JOB=<name>` in `params.env`, run with `tools/ops/run.sh job`)

| Job | What it does | Writes |
|---|---|---|
| `inspect-packs` | per public film: pack versions per distribution in the ingest cache and under the bucket prefixes, raw `{oNN}` / `{other}` placeholders, blank and out-of-order cues; per `PRIVATE_TITLE_SHAS` entry: where its pack lives, manifest flags, row counts, vectors, story bounds, recaps, speakers, characters, safeguards, thumbnails, what the server would miss (`server.packs.check_pack`), whether the film file is under `HELLO_WATCH_MOVIES_DIR` (matched by sha256) and which local catalog JSON describes it (path and field names only) | nothing |

The job's full output is kept in `.cache/ops-logs/job-<name>.log`; the job log shows at most 80 lines. A private
preview title appears in job logs only as its sha256 and as counts, never with its title, synopsis or file names.
