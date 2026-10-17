"""Agent prompts, loaded at runtime like the ingest prompts (`ingest/promptbook.py`), but from their own registry and
secret so the online service and the ingest pipeline can change independently.

Sources, in priority order:
  1. `WATCH_AGENT_PROMPTS_SECRET`: Secret Manager version `projects/<p>/secrets/<name>/versions/<v>` holding the JSON
     bundle (`python -m server.prompts DIR > bundle.json`);
  2. `WATCH_AGENT_PROMPTS_DIR`: a directory with one `<name>.txt` per prompt;
  3. `prompts/agent/` in this repo: simplified reference prompts.
"""
from __future__ import annotations

import functools
import json
import os
import pathlib
import sys

from ingest.promptbook import FORMAT, PromptsUnavailable, fields, read_dir, read_secret
from server.config import REPO

SECRET_ENV = "WATCH_AGENT_PROMPTS_SECRET"
DIR_ENV = "WATCH_AGENT_PROMPTS_DIR"
DEFAULT_DIR = REPO / "prompts" / "agent"

NAMES: dict[str, tuple[str, ...]] = {
    "agent_instruction": ("title", "position", "phase", "lang", "characters", "tap"),
    "look_closer": ("position", "characters", "question", "lang"),
    "guard_retry": ("terms",),
}


def validate(texts: dict[str, str]) -> dict[str, str]:
    errs = [f"missing {n}" for n in NAMES if n not in texts]
    errs += [f"{n}: placeholders {sorted(fields(t))} != {sorted(NAMES[n])}"
             for n, t in texts.items() if n in NAMES and fields(t) != set(NAMES[n])]
    if errs:
        raise PromptsUnavailable("agent prompt bundle invalid: " + "; ".join(errs))
    return {n: texts[n] for n in NAMES}


@functools.cache
def _load() -> tuple[str, dict[str, str]]:
    if secret := os.environ.get(SECRET_ENV):
        return "secret", validate(read_secret(secret))
    d = pathlib.Path(os.environ.get(DIR_ENV) or DEFAULT_DIR)
    if d.is_dir():
        return f"dir:{d.name}", validate(read_dir(d))
    raise PromptsUnavailable(f"no agent prompts: set {SECRET_ENV} or {DIR_ENV}, or create {DEFAULT_DIR}")


def source() -> str:
    return _load()[0]


def render(name: str, **kw) -> str:
    return _load()[1][name].format(**kw)


if __name__ == "__main__":
    # python -m server.prompts DIR > bundle.json   (then `gcloud secrets versions add … --data-file=bundle.json`)
    texts = validate(read_dir(pathlib.Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_DIR))
    print(json.dumps({"format": FORMAT, "prompts": texts}, ensure_ascii=False, indent=1, sort_keys=True))
