"""tools/ops/jobs.py without the cloud: a fake `gcloud` on PATH that maps gs://<bucket>/… to a local directory."""
import copy
import importlib.util
import json
import os
import pathlib
import stat
import subprocess
import sys

import pytest

from ingest import packdb
from ingest.tests.conftest import mini_pack

ROOT = pathlib.Path(__file__).resolve().parents[3]
spec = importlib.util.spec_from_file_location("ops_jobs", ROOT / "tools" / "ops" / "jobs.py")
J = importlib.util.module_from_spec(spec)
spec.loader.exec_module(J)

FAKE_GCLOUD = r'''#!PYTHON
import os, pathlib, shutil, sys
root = pathlib.Path(os.environ["FAKE_GCS"])
args = [a for a in sys.argv[1:] if a != "--no-user-output-enabled"]
local = lambda url: root / url[5:]
with open(root / "calls.log", "a") as f:
    f.write(" ".join(args) + "\n")
if args[:2] == ["storage", "ls"]:
    p = local([a for a in args[2:] if not a.startswith("-")][0].rstrip("/"))
    files = [p] if p.is_file() else sorted(x for x in p.rglob("*") if x.is_file()) if p.is_dir() else []
    if not files:
        print("ERROR: One or more URLs matched no objects.", file=sys.stderr); sys.exit(1)
    for f in files:
        print(f"{f.stat().st_size:>10}  2026-11-20T10:00:00Z  gs://{f.relative_to(root)}")
    sys.exit(0)
if args[:3] == ["storage", "objects", "describe"]:
    import base64, hashlib, json
    f = local(args[3])
    if not f.is_file():
        print("ERROR: not found: 404", file=sys.stderr); sys.exit(1)
    print(json.dumps({"md5_hash": base64.b64encode(hashlib.md5(f.read_bytes()).digest()).decode()}))
    sys.exit(0)
if args[:2] == ["storage", "cp"] and "-I" in args:
    dst = local(args[-1].rstrip("/"))
    dst.mkdir(parents=True, exist_ok=True)
    for line in sys.stdin.read().split():
        shutil.copyfile(line, dst / pathlib.Path(line).name)
    sys.exit(0)
if args[:2] == ["storage", "cp"]:
    src, dst = args[2:4]
    s = local(src) if src.startswith("gs://") else pathlib.Path(src)
    d = local(dst) if dst.startswith("gs://") else pathlib.Path(dst)
    if not s.exists():
        print("ERROR: No URLs matched", file=sys.stderr); sys.exit(1)
    d.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(s, d)
    sys.exit(0)
sys.exit(3)
'''


@pytest.fixture
def gcs(tmp_path, monkeypatch):
    bin_dir, root = tmp_path / "bin", tmp_path / "gcs"
    bin_dir.mkdir()
    (root / "bkt").mkdir(parents=True)
    g = bin_dir / "gcloud"
    g.write_text(FAKE_GCLOUD.replace("PYTHON", sys.executable))
    g.chmod(g.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_GCS", str(root))
    monkeypatch.setenv("BUCKET", "bkt")
    return root


def test_short_synopsis_cuts_at_a_sentence_end():
    long = "First sentence here, with some words. Second one follows! " + "x" * 300
    assert J.short_synopsis("Short. Done.") == "Short. Done."
    assert J.short_synopsis(long) == "First sentence here, with some words. Second one follows!"
    assert J.short_synopsis("一个女孩出发去找她的弟弟。" * 30).endswith("。")
    words = J.short_synopsis("word " * 100)
    assert len(words) <= 260 and words.endswith("…")


def test_upload_is_resumable_and_puts_the_pack_last(gcs, tmp_path):
    d = tmp_path / "pack"
    (d / "thumbs").mkdir(parents=True)
    for name in ("pack.db", "MANIFEST.json", "pack.json"):
        (d / name).write_text(name)
    for t in range(3):
        (d / "thumbs" / f"t{t:05d}.jpg").write_bytes(b"j%d" % t)
    rels = J.pack_files(d)
    u = J.upload_tree(d, "gs://bkt/next/abc/master", rels)
    assert u["uploaded"] == 6 and u["skipped"] == 0 and not u["mismatch"] and u["dest_files"] == 6
    cps = [ln for ln in (gcs / "calls.log").read_text().splitlines() if ln.startswith("storage cp")]
    assert cps[0].endswith("master/thumbs/") and [c.split()[-1][-8:] for c in cps[1:]] == ["/master/"] * 3
    (d / "thumbs" / "t00001.jpg").write_bytes(b"changed!")
    u2 = J.upload_tree(d, "gs://bkt/next/abc/master", rels)
    assert u2["uploaded"] == 1 and u2["skipped"] == 5 and not u2["mismatch"]


def test_prefix_params_refuse_the_live_prefix(monkeypatch):
    monkeypatch.delenv("LIVE_PACKS_GCS", raising=False)
    for bad in ("demo-packs", "demo-packs/x", "./demo-packs", "//demo-packs/", "gs://b/x", "../x", "a b", "a,b", ""):
        monkeypatch.setenv("PACKS_PREFIX", bad)
        with pytest.raises(J.OpsError):
            J.prefix_param("PACKS_PREFIX")
    monkeypatch.setenv("PACKS_PREFIX", "/demo-packs-next/")
    assert J.prefix_param("PACKS_PREFIX") == "demo-packs-next"
    # after a promote production serves demo-packs-next: then that is the prefix no job may write
    monkeypatch.setenv("BUCKET", "bkt")
    monkeypatch.setenv("LIVE_PACKS_GCS", "gs://bkt/demo-packs-next")
    for bad in ("demo-packs-next", "./demo-packs-next/abc", "demo-packs"):
        monkeypatch.setenv("PACKS_PREFIX", bad)
        with pytest.raises(J.OpsError, match="production serves"):
            J.prefix_param("PACKS_PREFIX")


def test_a_reexported_pack_of_the_same_size_is_uploaded_again(gcs, tmp_path):
    d = tmp_path / "pack"
    (d / "thumbs").mkdir(parents=True)
    (d / "pack.db").write_bytes(b"version-1")
    (d / "thumbs" / "t00000.jpg").write_bytes(b"j")
    J.upload_tree(d, "gs://bkt/next/abc/master", J.pack_files(d))
    (d / "pack.db").write_bytes(b"version-2")                          # same size, other content
    u = J.upload_tree(d, "gs://bkt/next/abc/master", J.pack_files(d))
    assert u["uploaded"] == 1 and not u["mismatch"] and (gcs / "bkt/next/abc/master/pack.db").read_bytes() == b"version-2"
    assert J.upload_tree(d, "gs://bkt/next/abc/master", J.pack_files(d))["uploaded"] == 0


def test_an_unreadable_titles_file_is_never_overwritten(gcs, tmp_path, monkeypatch):
    monkeypatch.setattr(J, "WORK", tmp_path / "work")
    entry = {"id": "pv-1", "sha": "cd" * 32, "title": "T"}
    (gcs / "bkt" / "priv").mkdir()
    f = gcs / "bkt" / "priv" / "titles.json"
    doc = J.write_titles_file("bkt", "priv", [entry], ["00" * 32])         # no file yet: written
    assert json.loads(f.read_text())["titles"] == [entry] and doc["access_sha256"] == ["00" * 32]
    J.write_titles_file("bkt", "priv", [{**entry, "sha": "ef" * 32}], ["00" * 32])
    assert len(json.loads(f.read_text())["titles"]) == 2                 # merged by sha
    f.write_text("{broken")
    with pytest.raises(J.OpsError, match="unreadable"):
        J.write_titles_file("bkt", "priv", [entry], ["00" * 32])
    assert f.read_text() == "{broken"


def test_health_finds_placeholders_and_says_whether_braces_are_visible(tmp_path):
    p = copy.deepcopy(mini_pack())
    p["scenes"][0]["summary_en"] = "The {o01} falls while {other} watches."
    p["scenes"][1]["source"] = "model {run-7}"
    p["events"][0]["text_en"] = "Alice reads {the sign}."
    packdb.write_db(packdb.normalize_pack(p), tmp_path / "pack.db")
    h = J.health(tmp_path / "pack.db")
    assert h["ph"]["{oNN}"] == 1 and h["ph"]["{other}"] == 1 and h["ph"]["{…}"] == 1
    lines = "\n".join(J.brace_lines(h))
    assert "{run-7} ×1 in scenes.source (internal column, never shown)" in lines
    assert "{the sign}" not in lines                    # has a space: not a token, just prose in braces


def test_local_catalog_entry_reads_title_and_synopsis_only(tmp_path, monkeypatch):
    repo = tmp_path / "priv"
    (repo / ".git").mkdir(parents=True)
    (repo / ".cache" / "ingest").mkdir(parents=True)
    (repo / "ingest" / "titles").mkdir(parents=True)
    (repo / "catalog").mkdir()
    sha = "ab" * 32
    (repo / "ingest" / "titles" / "cid-1.json").write_text(json.dumps(
        {"catalog_id": "cid-1", "title": "A Title", "license_note": "SECRET NOTE", "expected_asset_sha256": [sha]}))
    (repo / "catalog" / "catalog.json").write_text(json.dumps(
        {"items": [{"id": "cid-1", "synopsis_full": "This is the first sentence of it. And a second one. " + "y" * 300}]}))
    monkeypatch.setenv("HELLO_WATCH_INGEST_CACHE", str(repo / ".cache"))
    monkeypatch.delenv("HELLO_WATCH_DEVICE_PACKS_DIR", raising=False)
    e = J.local_catalog_entry(sha, None)
    assert e["title"] == "A Title" and e["synopsis"] == "This is the first sentence of it. And a second one." and "SECRET" not in json.dumps(e, default=str)
    with pytest.raises(J.OpsError):
        J.local_catalog_entry("cd" * 32, None)


def test_preview_command_drops_metadata_and_sets_gop():
    cmd = J.preview_cmd(pathlib.Path("in.mp4"), pathlib.Path("out.mp4"), 23.976)
    s = " ".join(cmd)
    for part in ("-map_metadata -1", "-map_chapters -1", "-sn -dn", "scale=640:-2", "-b:v 400k", "-g 48",
                 "-keyint_min 48", "-sc_threshold 0", "-b:a 64k -ac 2", "-movflags +faststart"):
        assert part in s, part


def test_job_sh_rejects_unknown_jobs(tmp_path):
    r = subprocess.run(["/bin/bash", str(ROOT / "tools" / "ops" / "job.sh")], capture_output=True, text=True,
                       env={**os.environ, "OPS_ROOT": str(ROOT), "JOB": "inspect-packs,nope",
                            "OPS_VENV_PY": sys.executable})
    assert r.returncode == 1 and "unknown JOB nope" in r.stdout


def test_bench_prep_fetches_served_packs_compares_versions_and_generates(gcs, tmp_path, monkeypatch):
    from bench.tests.test_gen_cases import long_pack
    sha_pub, sha_pv = "ab" * 32, "cd" * 32
    pub = long_pack()
    pv = copy.deepcopy(pub)
    for p_, sha in ((pub, sha_pub), (pv, sha_pv)):
        p_["asset"]["asset_sha256"] = p_["pack_meta"]["asset_sha256"] = sha
    packdb.write_db(pub, gcs / "bkt" / "packs" / sha_pub / "master" / "pack.db")
    packdb.write_db(pv, gcs / "bkt" / "packs" / sha_pv / "device-auto" / "pack.db")
    (gcs / "bkt" / "priv").mkdir()
    (gcs / "bkt" / "priv" / "titles.json").write_text(json.dumps(
        {"access_sha256": ["00" * 32], "titles": [{"id": "pv-cdcdcdcdcdcd", "sha": sha_pv, "title": "Secret Film",
                                                   "dist": "device-auto"}]}))
    monkeypatch.setattr(J, "ROOT", tmp_path)
    monkeypatch.setattr(J, "public_films", lambda: [{"id": "mini", "title": "Mini", "sha": sha_pub}])
    monkeypatch.setattr(J, "_health", lambda base: {"ok": True, "revision": "r-9", "packs": [
        {"sha": sha_pub, "dist": "master", "version": pub["pack_meta"]["pack_version"]}]})
    for k, v in {"PACKS_GCS": "gs://bkt/packs", "PRIVATE_TITLES_GCS": "gs://bkt/priv/titles.json",
                 "BENCH_BASE": "http://x", "BENCH_GENERATE": "20", "BENCH_TITLES": "all",
                 "BENCH_GEN_DIR": str(tmp_path / "gen")}.items():
        monkeypatch.setenv(k, v)
    lines = []
    monkeypatch.setattr(J, "say", lambda s="": lines.append(s))
    assert J.bench_prep() == 0
    log = "\n".join(lines)
    assert "server r-9 serves the same pack versions" in log and "private preview title #1: 20 cases" in log
    assert "Secret" not in log and sha_pv not in log
    served = json.loads((tmp_path / ".cache" / "bench" / "served.json").read_text())
    assert [t["key"] for t in served] == ["abababab", "pv1"] and served[1]["dist"] == "device-auto"
    assert sorted(p.name for p in (tmp_path / "gen").glob("*.jsonl")) == ["gen-abababab.jsonl", "gen-pv1.jsonl"]
    monkeypatch.setattr(J, "_health", lambda base: {"ok": True, "packs": [{"sha": sha_pub, "version": "99"}]})
    with pytest.raises(J.OpsError, match="serves pack v99"):
        J.bench_prep()


def test_rejudge_grades_stored_results_without_asking(tmp_path, monkeypatch, capsys):
    sha = "ab" * 32
    packdb.write_db(mini_pack(), tmp_path / ".cache" / "packs" / sha / "master" / "pack.db")
    run = tmp_path / ".cache" / "bench" / "runs" / "r1"
    gen = tmp_path / ".cache" / "bench" / "generated" / "r1"
    run.mkdir(parents=True)
    gen.mkdir(parents=True)
    case = {"id": "gen:abab:a01", "content_id": "mini", "asset_sha256": sha, "title": "Mini", "P": 5.0, "lang": "en",
            "question": "Who dies?", "category": "spoiler_probe", "expect": "decline", "set": "generated",
            "notes": "Generated spoiler probe."}
    (gen / "gen-abab.jsonl").write_text(json.dumps(case) + "\n")
    (run / "results.jsonl").write_text(json.dumps({"id": case["id"], "status": "ok", "answer": "That is Bob.",
                                                   "evidence_text": [], "t_total_ms": 10, "flow": "tools"}) + "\n")
    (run / "run.json").write_text(json.dumps({"run_id": "r1", "base": "http://x", "flow": "tools"}))  # an older run
    monkeypatch.setattr(J, "ROOT", tmp_path)
    monkeypatch.setenv("BENCH_REJUDGE_RUNS", "r1")
    monkeypatch.setenv("BENCH_LLM", "0")
    monkeypatch.delenv("BENCH_COMPARE", raising=False)
    assert J.rejudge() == 0
    judged = [json.loads(x) for x in (run / "judged.jsonl").read_text().splitlines()]
    assert judged[0]["rule_leaks"] == ["canary:Bob", "hidden_name:Bob"]
    out = capsys.readouterr().out
    assert "Benchmark scorecard" in out and "answer flow `tools`" in out
    monkeypatch.setenv("BENCH_REJUDGE_RUNS", "nope")
    with pytest.raises(J.OpsError, match="no stored run"):
        J.rejudge()


class FakeRun:
    """`gcloud run services describe / update-traffic` over an in-memory traffic list (status.traffic shape)."""

    def __init__(self, traffic, drop_tagged_traffic=False):
        self.traffic, self.drop, self.calls = copy.deepcopy(traffic), drop_tagged_traffic, []

    def __call__(self, *args, timeout=3600):
        self.calls.append(list(args))
        ok = lambda out="": subprocess.CompletedProcess(args, 0, out, "")
        if args[:3] == ("run", "services", "describe"):
            return ok(json.dumps({"metadata": {"name": args[3]}, "status": {"traffic": self.traffic}}))
        if args[:3] == ("run", "services", "update-traffic") and "--remove-tags" in args:
            tag = args[args.index("--remove-tags") + 1]
            kept = []
            for t in self.traffic:
                if t.get("tag") == tag:
                    if self.drop or not t.get("percent"):
                        continue
                    t = {k: v for k, v in t.items() if k not in ("tag", "url")}
                kept.append(t)
            self.traffic = kept
            return ok()
        if args[:3] == ("run", "services", "update-traffic") and "--to-revisions" in args:
            self.traffic = [{"revisionName": r, "percent": int(p)}
                            for r, p in (x.split("=") for x in args[args.index("--to-revisions") + 1].split(","))]
            return ok()
        return subprocess.CompletedProcess(args, 3, "", "unexpected call")


@pytest.fixture
def run_env(monkeypatch):
    for k, v in {"PROJECT": "proj", "REGION": "us-west1", "SERVICE": "svc", "TAG": "next"}.items():
        monkeypatch.setenv(k, v)


def test_untag_removes_the_tag_of_the_serving_revision_and_keeps_its_traffic(run_env, monkeypatch, capsys):
    fake = FakeRun([{"revisionName": "svc-00018-lut", "percent": 100, "tag": "next", "url": "https://next---svc"}])
    monkeypatch.setattr(J, "gcloud", fake)
    assert J.untag() == 0
    assert fake.traffic == [{"revisionName": "svc-00018-lut", "percent": 100}]
    assert ["--remove-tags", "next"] == [a for c in fake.calls for a in c if a in ("--remove-tags", "next")]
    out = capsys.readouterr().out
    assert "tag next removed from svc-00018-lut; traffic unchanged: svc-00018-lut 100%" in out
    assert out.rstrip().endswith("--update-tags next=svc-00018-lut")


def test_untag_of_a_staged_revision_leaves_production_alone(run_env, monkeypatch):
    fake = FakeRun([{"revisionName": "svc-00017-old", "percent": 100},
                    {"revisionName": "svc-00018-lut", "percent": 0, "tag": "next", "url": "https://next---svc"}])
    monkeypatch.setattr(J, "gcloud", fake)
    assert J.untag() == 0
    assert fake.traffic == [{"revisionName": "svc-00017-old", "percent": 100}]


def test_untag_without_the_tag_changes_nothing(run_env, monkeypatch, capsys):
    fake = FakeRun([{"revisionName": "svc-00018-lut", "percent": 100}])
    monkeypatch.setattr(J, "gcloud", fake)
    assert J.untag() == 0
    assert [c[:3] for c in fake.calls] == [["run", "services", "describe"]]
    assert "no tag next: nothing to remove" in capsys.readouterr().out


def test_untag_puts_the_split_back_when_gcloud_changed_it(run_env, monkeypatch):
    fake = FakeRun([{"revisionName": "svc-00015-zec", "percent": 10},
                    {"revisionName": "svc-00018-lut", "percent": 90, "tag": "next", "url": "https://next---svc"}],
                   drop_tagged_traffic=True)
    monkeypatch.setattr(J, "gcloud", fake)
    with pytest.raises(J.OpsError, match="was put back to svc-00015-zec=10,svc-00018-lut=90"):
        J.untag()
    assert J._split(fake.traffic) == [("svc-00015-zec", 10), ("svc-00018-lut", 90)]
