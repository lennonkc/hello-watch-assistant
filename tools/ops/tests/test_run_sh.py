"""tools/ops/run.sh without the cloud: a copy of the script in a scratch git checkout, with fake gcloud, curl and
python first on PATH (OPS_TEST_BIN). Pins that `tests` fails whenever pytest or the lint fails, that the image builds
cannot swallow pytest's status, and what `promote` prints last (the rollback)."""
import json
import os
import pathlib
import re
import shutil
import stat
import subprocess
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[3]

FAKE_PY = r'''#!/bin/bash
# the .venv python: pytest, pip and the case lint are faked, the rest (gcloud JSON parsing) is the real interpreter
case "$1" in
  -m)
    case "$2" in
      pytest) if [ "${FAKE_PYTEST_RC:-0}" = 0 ]; then echo "3 passed"; else echo "1 failed, 2 passed"; fi
              exit "${FAKE_PYTEST_RC:-0}" ;;
      pip) exit 0 ;;
    esac ;;
  bench/judge.py) echo "lint: exit ${FAKE_LINT_RC:-0}"; exit "${FAKE_LINT_RC:-0}" ;;
  -c) case "$2" in *"version_info[0] * 100"*) echo 313; exit 0 ;; esac ;;
esac
exec REALPY "$@"
'''

FAKE_GCLOUD = r'''#!REALPY
import os, pathlib, shutil, sys
d = pathlib.Path(os.environ["FAKE_DIR"])
args = sys.argv[1:]
with open(d / "calls.log", "a") as f:
    f.write(" ".join(args) + "\n")
if args[:3] == ["run", "services", "describe"]:
    print((d / "service.json").read_text()); sys.exit(0)
if args[:3] == ["run", "revisions", "describe"]:
    p = d / f"rev-{args[3]}.json"
    if not p.is_file():
        sys.exit(1)
    print(p.read_text()); sys.exit(0)
if args[:3] == ["run", "services", "update-traffic"]:
    rc = int(os.environ.get("FAKE_TRAFFIC_RC", "0"))
    if rc == 0 and (d / "service-after.json").is_file():
        shutil.copyfile(d / "service-after.json", d / "service.json")
    print("Updating traffic... done" if rc == 0 else "ERROR: (gcloud.run.services.update-traffic) denied")
    sys.exit(rc)
if args[:1] == ["version"]:
    print("Google Cloud SDK (fake)"); sys.exit(0)
sys.exit(3)
'''

FAKE_CURL = r'''#!/bin/bash
cat "$FAKE_DIR/health.json"
'''


def _exe(p: pathlib.Path, text: str) -> None:
    p.write_text(text.replace("REALPY", sys.executable))
    p.chmod(p.stat().st_mode | stat.S_IEXEC)


@pytest.fixture
def ops(tmp_path):
    """A scratch checkout with run.sh, env.sh, the catalog and the requirements files, and the fakes."""
    repo, fake, home = tmp_path / "repo", tmp_path / "fake", tmp_path / "home"
    for rel in ("tools/ops/run.sh", "deploy/web/env.sh", "catalog/catalog.json",
                "requirements-ingest.txt", "requirements-web.txt"):
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / rel, repo / rel)
    (repo / ".venv" / "bin").mkdir(parents=True)
    fake.mkdir()
    home.mkdir()
    _exe(repo / ".venv" / "bin" / "python", FAKE_PY)
    _exe(fake / "python3.13", FAKE_PY)
    _exe(fake / "gcloud", FAKE_GCLOUD)
    _exe(fake / "curl", FAKE_CURL)
    git = ["git", "-C", str(repo), "-c", "user.name=ops-test", "-c", "user.email=ops-test", "-c", "commit.gpgsign=false"]
    subprocess.run(git[:3] + ["init", "-q"], check=True)
    subprocess.run(git + ["commit", "-q", "--allow-empty", "-m", "scratch"], check=True)
    commit = subprocess.run(git[:3] + ["rev-parse", "--short=12", "HEAD"], capture_output=True, text=True,
                            check=True).stdout.strip()

    def run(mode, **env):
        e = {"PATH": f"{fake}:/usr/bin:/bin", "HOME": str(home), "LANG": "C", "OPS_TEST_BIN": str(fake),
             "FAKE_DIR": str(fake), "PROJECT": "proj"}
        e.update({k: str(v) for k, v in env.items()})
        return subprocess.run(["/bin/bash", str(repo / "tools" / "ops" / "run.sh"), mode], capture_output=True,
                              text=True, env=e, timeout=120)

    run.repo, run.fake, run.commit = repo, fake, commit
    return run


@pytest.mark.parametrize("pytest_rc, lint_rc, ok", [(0, 0, True), (1, 0, False), (2, 0, False), (0, 1, False)])
def test_tests_mode_fails_when_pytest_or_the_lint_fails(ops, pytest_rc, lint_rc, ok):
    r = ops("tests", FAKE_PYTEST_RC=pytest_rc, FAKE_LINT_RC=lint_rc)
    assert (r.returncode == 0) is ok, r.stdout + r.stderr
    assert ("3 passed" if pytest_rc == 0 else "1 failed, 2 passed") in r.stdout          # the log is still shown
    if not ok:
        assert f"tests failed (pytest exit {pytest_rc}, lint exit {lint_rc})" in r.stderr


def _instructions(dockerfile: pathlib.Path) -> list[str]:
    lines, cur = [], ""
    for raw in dockerfile.read_text().splitlines():
        if raw.lstrip().startswith("#") and not cur:
            continue
        cur += raw.rstrip("\\").strip() + " "
        if not raw.rstrip().endswith("\\"):
            lines.append(cur.strip())
            cur = ""
    return [x for x in lines if x]


def test_image_builds_cannot_swallow_a_test_failure():
    dockerfiles = sorted((ROOT / "deploy").rglob("Dockerfile*"))
    runs_pytest = 0
    for df in dockerfiles:
        ins = _instructions(df)
        assert not [i for i in ins if i.upper().startswith("SHELL")], f"{df}: a SHELL override changes RUN's errors"
        for i in ins:
            if not i.upper().startswith("RUN "):
                continue
            body = i[4:]
            if "pytest" in body:
                # /bin/sh -c has no pipefail (a pipe's status is its last command's); `;` and `||` drop a status
                runs_pytest += 1
                assert not re.search(r"[|;]", body), f"{df}: pytest's status can be lost: {i}"
    assert runs_pytest == 2                                          # the web and the ingest image test themselves
    for cb in sorted((ROOT / "deploy").rglob("cloudbuild*.yaml")):
        text = cb.read_text()
        assert "allowFailure" not in text and "allow_failure" not in text and "|| true" not in text, cb
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text()
    assert [ln for ln in ci.splitlines() if "pytest" in ln and "|" not in ln and "run:" in ln], "CI runs pytest"
    assert "\nset -euo pipefail\n" in (ROOT / "tools" / "ops" / "run.sh").read_text()   # mode_tests: test above


# ── promote ─────────────────────────────────────────────────────────

def _service(traffic, latest="hello-watch-00011-new"):
    return {"spec": {"template": {"spec": {"containers": [{"env": [{"name": "CEDAR_INGEST_BUCKET", "value": "bkt"}]}]}}},
            "status": {"url": "https://svc", "latestReadyRevisionName": latest, "traffic": traffic}}


def _setup_promote(ops, before, image_commit=None):
    tagged = {"revisionName": "hello-watch-00011-new", "percent": 0, "tag": "next", "url": "https://next---svc"}
    after = [{"revisionName": "hello-watch-00011-new", "percent": 100, "tag": "next", "url": "https://next---svc"}]
    (ops.fake / "service.json").write_text(json.dumps(_service(before + [tagged], latest="hello-watch-00010-mid")))
    (ops.fake / "service-after.json").write_text(json.dumps(_service(after)))
    rev = {"spec": {"containers": [{"image": f"r/p/web:{image_commit or ops.commit}",
                                    "env": [{"name": "WATCH_PACKS_GCS", "value": "gs://bkt/demo-packs"}]}]},
           "status": {"conditions": [{"type": "Ready", "status": "True"}]}}
    (ops.fake / "rev-hello-watch-00011-new.json").write_text(json.dumps(rev))
    n = len(json.loads((ROOT / "catalog" / "catalog.json").read_text())["items"])
    (ops.fake / "health.json").write_text(json.dumps({"ok": True, "titles": n, "revision": "hello-watch-00011-new"}))


def _calls(ops):
    p = ops.fake / "calls.log"
    return p.read_text().splitlines() if p.is_file() else []


@pytest.mark.parametrize("before, prev, spec", [
    ([{"revisionName": "hello-watch-00009-old", "percent": 100}], "hello-watch-00009-old", "hello-watch-00009-old=100"),
    ([{"latestRevision": True, "percent": 90}, {"revisionName": "hello-watch-00008-b", "percent": 10, "tag": "b"}],
     "hello-watch-00010-mid (traffic was split: hello-watch-00010-mid=90,hello-watch-00008-b=10)",
     "hello-watch-00010-mid=90,hello-watch-00008-b=10"),
])
def test_promote_ends_with_the_previous_revision_and_its_rollback(ops, before, prev, spec):
    _setup_promote(ops, before)
    r = ops("promote")
    assert r.returncode == 0, r.stdout + r.stderr
    assert r.stdout.rstrip("\n").splitlines()[-2:] == [
        f"previous production revision: {prev}",
        f"rollback: gcloud run services update-traffic hello-watch --project proj --region us-west1 --to-revisions {spec}"]
    assert [c for c in _calls(ops) if "update-traffic" in c] == [
        "run services update-traffic hello-watch --project proj --region us-west1 --to-tags next=100 --quiet"]


def test_a_failed_traffic_move_still_prints_the_rollback(ops):
    _setup_promote(ops, [{"revisionName": "hello-watch-00009-old", "percent": 100}])
    r = ops("promote", FAKE_TRAFFIC_RC=1)
    assert r.returncode != 0 and "update-traffic failed" in r.stderr
    assert "--to-revisions hello-watch-00009-old=100" in r.stdout.splitlines()[-1]


def test_promote_refuses_an_image_of_another_commit_without_moving_traffic(ops):
    _setup_promote(ops, [{"revisionName": "hello-watch-00009-old", "percent": 100}], image_commit="0123456789ab")
    r = ops("promote")
    assert r.returncode != 0 and "is not the checked-out commit" in r.stderr
    assert not [c for c in _calls(ops) if "update-traffic" in c]
