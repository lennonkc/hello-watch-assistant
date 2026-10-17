"""Prompt texts are loaded at runtime, not shipped with the code or the container image.

Sources (in priority order):
  1. `CEDAR_PROMPTS_SECRET`: a Secret Manager version resource name `projects/<p>/secrets/<name>/versions/<v>`
     whose payload is the JSON produced by `bundle_json()` (used by cloud jobs / the online service; credentials via ADC);
  2. `CEDAR_PROMPTS_DIR`: a directory with one `<name>.txt` per prompt;
  3. the repo's `prompts/` directory (local development).

The public repo's `prompts/` holds simplified reference prompts; the production prompts live in Secret Manager.

Templates use `str.format` placeholders; `NAMES` registers every prompt and its placeholders, and loading validates
each one: a missing prompt or mismatched placeholders is an error. One trailing newline is stripped on read (editors
always add it). Changing a prompt changes the stage fingerprint (triggers a re-run).
"""
from __future__ import annotations

import base64
import functools
import json
import os
import pathlib
import string
import sys

from ingest.util import REPO

FORMAT = "cedar-prompts/1"
SECRET_ENV = "CEDAR_PROMPTS_SECRET"
DIR_ENV = "CEDAR_PROMPTS_DIR"
DEFAULT_DIR = REPO / "prompts"

NAMES: dict[str, tuple[str, ...]] = {
    "common_rules": (), "full_rules_tail": (), "full_rules_tail_v2": (), "seg_task": ("start", "end", "roster"),
    "credits": (), "public_names_rule": ("synopsis",), "present_rule": (), "trailer_rule": (), "lang_rule": ("lang",),
    "identify": ("title", "title_zh", "year", "type", "synopsis"),
    "no_people": (), "exemplar": ("ref", "label", "kind", "looks", "others", "no_people"),
    "shot": ("seg_range", "characters", "others", "objects", "shot_table", "no_people"),
    "speaker": ("characters", "lines", "no_people"), "verify": ("label", "kind", "looks", "others", "no_people"),
    "recap_rules": ("at", "words"), "recap_retry": ("terms",),
}


class PromptsUnavailable(RuntimeError):
    pass


def fields(text: str) -> set[str]:
    return {f for _, f, _, _ in string.Formatter().parse(text) if f is not None}


def validate(texts: dict[str, str]) -> dict[str, str]:
    errs = [f"missing {n}" for n in NAMES if n not in texts]
    errs += [f"{n}: placeholders {sorted(fields(t))} != {sorted(NAMES[n])}"
             for n, t in texts.items() if n in NAMES and fields(t) != set(NAMES[n])]
    if errs:
        raise PromptsUnavailable("prompt bundle invalid: " + "; ".join(errs))
    return texts


def read_dir(d: pathlib.Path) -> dict[str, str]:
    out = {}
    for f in sorted(d.glob("*.txt")):
        t = f.read_text(encoding="utf-8")
        out[f.stem] = t[:-1] if t.endswith("\n") else t
    return out


def bundle_json(texts: dict[str, str]) -> str:
    return json.dumps({"format": FORMAT, "prompts": validate(texts)}, ensure_ascii=False, indent=1, sort_keys=True)


def read_secret(name: str) -> dict[str, str]:
    import google.auth
    from google.auth.transport.requests import AuthorizedSession

    creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
    r = AuthorizedSession(creds).get(f"https://secretmanager.googleapis.com/v1/{name}:access", timeout=30)
    if r.status_code != 200:
        raise PromptsUnavailable(f"Secret Manager {name}: HTTP {r.status_code} {r.text[:200]}")
    doc = json.loads(base64.b64decode(r.json()["payload"]["data"]))
    if doc.get("format") != FORMAT:
        raise PromptsUnavailable(f"Secret Manager {name}: format must be {FORMAT}")
    return doc["prompts"]


@functools.cache
def _load() -> tuple[str, dict[str, str]]:
    if secret := os.environ.get(SECRET_ENV):
        return f"secret:{secret}", validate(read_secret(secret))
    d = pathlib.Path(os.environ.get(DIR_ENV) or DEFAULT_DIR)
    if d.is_dir():
        return f"dir:{d}", validate(read_dir(d))
    raise PromptsUnavailable(f"no prompts: set {SECRET_ENV} (Secret Manager) or {DIR_ENV}, or create {DEFAULT_DIR}")


_override: dict[str, str] | None = None


def use(texts: dict[str, str] | None) -> None:
    """For tests: inject a prompt set directly (None restores normal loading)."""
    global _override
    _override = validate(texts) if texts is not None else None


def source() -> str:
    return "override" if _override is not None else _load()[0]


def text(name: str) -> str:
    texts = _override if _override is not None else _load()[1]
    return texts[name]


def render(name: str, **kw) -> str:
    return text(name).format(**kw)


def placeholder_bundle() -> dict[str, str]:
    """Each prompt = `[name]` + all its placeholders; for tests and smoke runs without real prompts."""
    return {n: f"[{n}]" + "".join(f" {{{f}}}" for f in fs) for n, fs in NAMES.items()}


if __name__ == "__main__":
    # python -m ingest.promptbook [DIR] > bundle.json   (then `gcloud secrets versions add … --data-file=bundle.json`)
    print(bundle_json(read_dir(pathlib.Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_DIR)))
