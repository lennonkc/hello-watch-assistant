# Prompts

One file per prompt template (`<name>.txt`, Python `str.format` placeholders; `{{` / `}}` are literal braces).

## Agent prompts (`agent/`): the production prompts

[`agent/`](agent/) holds the prompts the online assistant uses: the agent instruction, the nested `look_closer`
request and the guard's retry note. **These are the prompts the live demo runs**, published as they are. Their names
and placeholders are defined by `NAMES` in [`server/prompts.py`](../server/prompts.py) and validated on load. The
service can still read them from Secret Manager (`WATCH_AGENT_PROMPTS_SECRET`) or another directory
(`WATCH_AGENT_PROMPTS_DIR`); `deploy/web/deploy.sh` publishes a directory as a new secret version.

## Ingest prompts (this directory): simplified reference versions

The ingest prompts here are **simplified reference prompts**, enough to run the pipeline end to end. Their set of
names and placeholders is defined by `NAMES` in [`ingest/promptbook.py`](../ingest/promptbook.py). The production
ingest prompts are kept in Secret Manager and selected with `CEDAR_PROMPTS_SECRET`; see the main
[README](../README.md#prompts-open-code-private-know-how). To publish a prompt directory as a new secret version:
`PROJECT=<project> DIR=<dir> deploy/prompts/push_prompts.sh`.
