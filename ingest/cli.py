"""CLI: `python3 -m ingest {run,build,check,export,apply-patch,status,cloud,library,...}`."""
from __future__ import annotations

import argparse
import json
import pathlib
import sys
import time

from ingest.ledger import DEFAULT_BUDGET_USD, BudgetExceeded, Ledger
from ingest.util import CACHE, CATALOG_PATH, TITLES_DIR, read_json, sha256_file, work_dir, write_json


def _catalog_item(catalog_id: str) -> dict:
    for it in read_json(CATALOG_PATH)["items"]:
        if it["id"] == catalog_id:
            return it
    raise SystemExit(f"catalog id {catalog_id} not in catalog/catalog.json")


def _ctx(args, sha: str | None = None):
    from ingest.stages import Ctx
    asset = pathlib.Path(args.asset).expanduser().resolve()
    if sha is None:
        sha = sha256_file(asset)
    title = read_json(TITLES_DIR / f"{args.catalog_id}.json")
    exp = title.get("expected_asset_sha256")
    if exp and sha not in exp:
        raise SystemExit(f"asset sha256 {sha} not in expected list of ingest/titles/{args.catalog_id}.json")
    subs_dir = pathlib.Path(args.subs_dir) if getattr(args, "subs_dir", None) else CACHE / "sources" / args.catalog_id / "subs"
    wd = work_dir(sha)
    wd.mkdir(parents=True, exist_ok=True)
    write_json(wd / "asset.json", {"asset_sha256": sha, "catalog_id": args.catalog_id, "asset_name": asset.name})
    return Ctx(sha=sha, asset=asset, catalog_id=args.catalog_id, title=title, catalog_item=_catalog_item(args.catalog_id),
               subs_dir=subs_dir, ledger=Ledger(sha, args.budget), force=frozenset(args.force or []))


def cmd_run(args) -> int:
    from ingest import build
    from ingest.stages import ORDER, load_all, run_stage
    load_all()
    ctx = _ctx(args)
    print(f"asset sha256 {ctx.sha}")
    want = args.stages.split(",") if args.stages else ORDER + ["build", "export"]
    try:
        for name in ORDER + ["build", "export"]:
            if name not in want:
                continue
            t0 = time.time()
            if name == "build":
                build.build_auto(ctx)
                ctx.ledger.log("build", "ran", time.time() - t0)
            elif name == "export":
                res = build.export_all(ctx.sha)
                ctx.ledger.log("export", "ran", time.time() - t0, pack_version=res["pack_version"])
                _print_export(res)
            else:
                run_stage(ctx, name)
            print(f"  {name}: ok ({time.time() - t0:.1f}s)", flush=True)
    except BudgetExceeded as e:
        print(f"STOP: {e}", file=sys.stderr)
        return 3
    print(f"spent so far: ${ctx.ledger.spent():.3f}")
    return 0


def _sha_arg(args) -> str:
    if args.sha:
        return args.sha
    d = CACHE / "ingest"
    shas = [p.name for p in d.iterdir() if (p / "auto" / "rows.json").exists()] if d.exists() else []
    if len(shas) != 1:
        raise SystemExit("pass --sha (found %d candidate packs)" % len(shas))
    return shas[0]


def _print_export(res: dict) -> None:
    print(json.dumps({k: res[k] for k in ("pack_version", "review_complete", "checks_failed", "flagged", "master", "device")},
                     ensure_ascii=False, indent=1))


def cmd_export(args) -> int:
    from ingest import build
    res = build.export_all(_sha_arg(args))
    _print_export(res)
    return 0


def cmd_check(args) -> int:
    from ingest import build
    res = build.export_all(_sha_arg(args), write=False)
    for c in res["checks"]["checks"]:
        print(f"  [{'PASS' if c['ok'] else 'FAIL'}] {c['id']:>2} {c['name']}: {len(c['issues'])} issue(s)")
    print(f"review_complete would be {res['review_complete']}; flagged {res['flagged']}")
    return 0 if all(c["ok"] for c in res["checks"]["checks"]) else 1


def cmd_apply_patch(args) -> int:
    from ingest import build
    res = build.apply_patch(_sha_arg(args), pathlib.Path(args.patch))
    _print_export(res)
    return 0


def cmd_status(args) -> int:
    sha = _sha_arg(args)
    led = Ledger(sha)
    ran = [e for e in led.entries() if e["status"] == "ran"]
    print(f"sha {sha}: spent ${led.spent():.3f} over {len(ran)} runs")
    for e in ran:
        print(f"  {e['ts']} {e['stage']:<22} {e['elapsed_s']:>7.1f}s ${e['cost_usd']:.4f}")
    return 0



def cmd_library(args) -> int:
    from ingest import library
    if args.cast_source:
        n = library.import_cast(pathlib.Path(args.cast_source))
        print(f"cast names cached for {len(n)} catalog items")
    lib = library.register_assets(pathlib.Path(args.asset_dir))
    for a in lib["assets"]:
        ms = "; ".join(f"{m['kind']}: {m['detail']}" for m in a["mismatches"]) or "-"
        print(f"  {a['asset_sha256'][:12]} {a['catalog_title'] or '?':<34} {a['duration_s']:>7.1f}s  {ms}")
    print(f"wrote {library.LIBRARY_PATH}")
    return 0


def cmd_library_status(args) -> int:
    from ingest import library
    st = library.status()
    for r in st["assets"]:
        print(f"  {r['asset_sha256'][:12]} v{r['pack_version'] or '-':<3} complete={int(bool(r['review_complete']))} "
              f"human={r.get('needs_human') or 0:<4} ${r['cost_usd']:.3f}  {r['file']}")
    print(f"total ${st['total_cost_usd']:.3f}; needs human {st['needs_human_total']}; wrote {library.STATUS_PATH}")
    return 0



def _add_cloud(sub) -> None:
    from ingest.cloud.cli import add_subcommands
    add_subcommands(sub)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python3 -m ingest")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="run pipeline stages for one asset")
    r.add_argument("--asset", required=True)
    r.add_argument("--catalog-id", required=True)
    r.add_argument("--stages", help="comma list; default all: register,shots,segments,subtitles,thumbs,upload,"
                                    "identify,transcribe,transcribe_plain,align,vision_full,vision_seg,credits,build,export")
    r.add_argument("--force", action="append", help="re-run this stage even if cached (repeatable)")
    r.add_argument("--subs-dir")
    r.add_argument("--budget", type=float, default=DEFAULT_BUDGET_USD)
    r.set_defaults(fn=cmd_run)
    for name, fn, hlp in (("check", cmd_check, "run spec §10 builder checks on master (no write)"),
                          ("export", cmd_export, "rebuild master + device packs from auto rows + patches"),
                          ("status", cmd_status, "show ledger")):
        p = sub.add_parser(name, help=hlp)
        p.add_argument("--sha")
        p.set_defaults(fn=fn)
    p = sub.add_parser("library", help="register all assets of a directory, detect catalog mismatches → library.json")
    p.add_argument("--asset-dir", required=True)
    p.add_argument("--cast-source", help="MediaItem catalog with cast / directors (blocklist only; never committed)")
    p.set_defaults(fn=cmd_library)
    p = sub.add_parser("library-status", help="summarise every asset's pack → library_status.json")
    p.set_defaults(fn=cmd_library_status)
    p = sub.add_parser("apply-patch", help="apply a review patch, re-export, re-check")
    p.add_argument("patch")
    p.add_argument("--sha")
    p.set_defaults(fn=cmd_apply_patch)
    _add_cloud(sub)
    args = ap.parse_args(argv)
    if not hasattr(args, "budget"):
        args.budget = DEFAULT_BUDGET_USD
    return args.fn(args)
