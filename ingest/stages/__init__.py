"""Pipeline stages: each stage writes `.cache/ingest/<sha>/stages/<name>.json`, cached by input fingerprint, rerunnable on its own."""
from __future__ import annotations

import dataclasses
import pathlib
import time
from typing import Callable

from ingest.ledger import Ledger
from ingest.util import fingerprint, read_json, work_dir, write_json


@dataclasses.dataclass
class Ctx:
    sha: str
    asset: pathlib.Path
    catalog_id: str
    title: dict            # ingest/titles/<catalog_id>.json
    catalog_item: dict
    subs_dir: pathlib.Path
    ledger: Ledger
    force: frozenset = frozenset()

    @property
    def wd(self) -> pathlib.Path:
        return work_dir(self.sha)

    def stage_path(self, name: str) -> pathlib.Path:
        return self.wd / "stages" / f"{name}.json"

    def load(self, name: str) -> dict:
        return read_json(self.stage_path(name))


@dataclasses.dataclass
class Stage:
    name: str
    deps: tuple[str, ...]
    fn: Callable[[Ctx], dict]
    inputs: Callable[[Ctx], dict]   # fingerprint inputs (besides the dependency stages' fingerprints)
    paid: bool = False


REGISTRY: dict[str, Stage] = {}


def stage(name: str, deps=(), paid=False, inputs=lambda ctx: {}):
    def deco(fn):
        REGISTRY[name] = Stage(name, tuple(deps), fn, inputs, paid)
        return fn
    return deco


def _fp(ctx: Ctx, st: Stage) -> str:
    dep_fps = {d: read_json(ctx.stage_path(d))["_fingerprint"] for d in st.deps}
    return fingerprint({"stage": st.name, "deps": dep_fps, "inputs": st.inputs(ctx)})


def run_stage(ctx: Ctx, name: str) -> dict:
    st = REGISTRY[name]
    for d in st.deps:
        if not ctx.stage_path(d).exists():
            raise RuntimeError(f"stage {name} needs {d}; run it first")
    fp = _fp(ctx, st)
    p = ctx.stage_path(name)
    if p.exists() and name not in ctx.force:
        old = read_json(p)
        if old.get("_fingerprint") == fp:
            ctx.ledger.log(name, "cached", 0.0)
            return old
    t0 = time.time()
    out = st.fn(ctx)
    cost = out.pop("_cost_usd", 0.0)
    usage = out.pop("_usage", None)
    out["_fingerprint"] = fp
    write_json(p, out)
    ctx.ledger.log(name, "ran", time.time() - t0, cost, **({"usage": usage} if usage else {}))
    return out


ORDER = ["register", "shots", "segments", "subtitles", "thumbs", "upload", "identify", "transcribe", "transcribe_plain",
         "align", "vision_full", "vision_seg", "vision_shots", "speakers", "credits"]


def load_all():
    # importing registers the stages
    from ingest.stages import register, shots, segments, subtitles, gemini_stages, thumbs, vision_shots, speakers  # noqa: F401
