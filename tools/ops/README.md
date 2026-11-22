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
| `job` | runs `tools/ops/job.sh` of this commit, if present (one-off operations) | whatever it does |

Settings: the runner's git-ignored `.cache/ops.env` (`HOME`, `HELLO_WATCH_GEMINI_KEY_FILE`, `HELLO_WATCH_MOVIES_DIR`,
`HELLO_WATCH_DEVICE_PACKS_DIR`, `HELLO_WATCH_INGEST_CACHE`), then the committed `tools/ops/params.env` (all options
with their defaults). Project, bucket, packs prefix and URLs are discovered from `gcloud` and from the service's own
environment at run time. Secrets (the Gemini key, the bench token) are never printed.

`deploy/web/deploy.sh` is the full production deploy (service account, IAM, 100 % traffic); the agent prompts ship in
the image (`prompts/agent/`).
