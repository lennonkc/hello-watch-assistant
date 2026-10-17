"""`python3 -m ingest cloud {run,pull,push-work,register,renew}` subcommands. Needs google-cloud-storage, imported lazily."""
from __future__ import annotations

import argparse

from ingest.ledger import DEFAULT_BUDGET_USD


def add_subcommands(sub) -> None:
    cloud = sub.add_parser("cloud", help="cloud ingest: GCS-backed job, pack publish, Gemini file registration")
    csub = cloud.add_subparsers(dest="cloud_cmd", required=True)

    def p(name, fn, hlp):
        x = csub.add_parser(name, help=hlp)
        x.add_argument("--bucket", help="GCS bucket (default: $CEDAR_INGEST_BUCKET)")
        x.set_defaults(fn=fn)
        return x

    r = p("run", _job("cmd_run"), "ingest assets from gs://<bucket>/assets/ and publish packs (Cloud Run Job entry)")
    # gcloud run jobs execute --args rejects repeated flag names, so multi-value flags use nargs="+" (commas also accepted)
    r.add_argument("--sha", nargs="+", action="extend", help="asset sha256 … (space or comma separated)")
    r.add_argument("--all", action="store_true", help="every asset in assets/index.json")
    r.add_argument("--stages", help="comma list (same as `run --stages`)")
    r.add_argument("--force", nargs="+", action="extend", help="re-run these stages even if cached")
    r.add_argument("--budget", type=float, default=DEFAULT_BUDGET_USD, help="per-asset paid-stage budget, USD")

    q = p("pull", _job("cmd_pull"), "download a published pack (verified against MANIFEST) or the work dir")
    q.add_argument("--sha", required=True)
    q.add_argument("--version", default="latest", help="latest | N | vN")
    q.add_argument("--dist", choices=("device", "master"), default="device")
    q.add_argument("--dest", default="packs")
    q.add_argument("--work", action="store_true", help="pull work/<sha>/ into .cache/ingest/<sha>/ instead")

    w = p("push-work", _job("cmd_push_work"), "mirror a local work dir (e.g. after apply-patch) to work/<sha>/")
    w.add_argument("--sha", required=True)
    w.add_argument("--src", help="work dir (default .cache/ingest/<sha>)")

    g = p("register", _reg("cmd_register"), "register assets with Gemini Files → registry/<sha>.json")
    g.add_argument("--sha", nargs="+", action="extend", required=True, help="asset sha256 … (space or comma separated)")
    g.add_argument("--segments", action="store_true", help="also register segment files (work/<sha>/segments/)")
    g.add_argument("--mode", choices=("auto", "gcs", "upload"), default="auto")

    n = p("renew", _reg("cmd_renew"), "re-register sidecar entries expiring within the window")
    n.add_argument("--window-hours", type=float, default=12.0)
    n.add_argument("--mode", choices=("auto", "gcs", "upload"), default="auto")


def _job(name):
    def run(args: argparse.Namespace) -> int:
        from ingest.cloud import job
        return getattr(job, name)(args)
    return run


def _reg(name):
    def run(args: argparse.Namespace) -> int:
        from ingest.cloud import register
        return getattr(register, name)(args)
    return run
