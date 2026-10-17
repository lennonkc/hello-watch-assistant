# Reference prompts

One file per prompt template (`<name>.txt`, Python `str.format` placeholders; `{{` / `}}` are literal braces).
The set of names and placeholders is defined by `NAMES` in [`ingest/promptbook.py`](../ingest/promptbook.py) and
validated on load.

These are **simplified reference prompts**, enough to run the pipeline end to end. The production prompts are kept
in Secret Manager and selected with `CEDAR_PROMPTS_SECRET`; see the main [README](../README.md#prompts-open-code-private-know-how).
To publish a prompt directory as a new secret version: `PROJECT=<project> DIR=<dir> deploy/prompts/push_prompts.sh`.
