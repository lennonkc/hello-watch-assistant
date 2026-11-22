"""deploy/web/env.sh: the service environment is complete, validated and never inherits or smuggles anything."""
import pathlib
import subprocess

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[3]


def service_env(**env) -> subprocess.CompletedProcess:
    base = {"PATH": "/usr/bin:/bin", "PROJECT": "proj-1", "BUCKET": "bkt", "PACKS_GCS": "gs://bkt/demo-packs-next"}
    base.update(env)
    return subprocess.run(["/bin/bash", "-c", f". '{ROOT}/deploy/web/env.sh' && service_env"], capture_output=True,
                          text=True, env=base)


def test_the_whole_environment_is_computed():
    r = service_env(PRIVATE_TITLES_GCS="gs://bkt/demo-private/titles.json", BENCH_TOKEN_VALUE="ab12",
                    EXTRA_ENV="WATCH_HISTORY_TURNS=4,WATCH_MODEL=gemini-3.8-flash")
    assert r.returncode == 0, r.stderr
    lines = r.stdout.splitlines()
    assert lines == ["CEDAR_GCP_PROJECT=proj-1", "CEDAR_GEMINI_BACKEND=enterprise", "CEDAR_INGEST_BUCKET=bkt",
                     "WATCH_PACKS_GCS=gs://bkt/demo-packs-next", "WATCH_PRIVATE_TITLES=gs://bkt/demo-private/titles.json",
                     "WATCH_ASK_PER_MINUTE=6", "WATCH_ASK_PER_DAY_PER_IP=80", "WATCH_ASK_PER_DAY_TOTAL=1500",
                     "WATCH_BENCH_TOKEN=ab12", "WATCH_HISTORY_TURNS=4", "WATCH_MODEL=gemini-3.8-flash"]
    release = service_env().stdout                       # RELEASE: no token, no private titles unless asked
    assert "WATCH_BENCH_TOKEN" not in release and "WATCH_PRIVATE_TITLES" not in release


@pytest.mark.parametrize("env, why", [
    ({"EXTRA_ENV": "WATCH_BENCH_TOKEN=guess"}, "must not set WATCH_BENCH_TOKEN"),
    ({"EXTRA_ENV": "WATCH_PACKS_GCS=gs://other/x"}, "must not set WATCH_PACKS_GCS"),
    ({"EXTRA_ENV": "WATCH_HISTORY_TURNS=4,WATCH_HISTORY_TURNS=6"}, "twice"),
    ({"EXTRA_ENV": "WATCH_HISTORY_TURNS"}, "not KEY=VALUE"),
    ({"EXTRA_ENV": "WATCH_HISTORY_TURNS=a b"}, "the value must"),
    ({"EXTRA_ENV": "lower=x"}, "bad env name"),
    ({"PRIVATE_TITLES_GCS": "gs://bkt/p,WATCH_BENCH_TOKEN=x"}, "the value must"),
    ({"PACKS_GCS": "gs://bkt/a b"}, "the value must"),
    ({"PACKS_GCS": "/local/dir"}, "must be gs://"),
    ({"PACKS_GCS": ""}, "needs PROJECT, BUCKET and PACKS_GCS"),
])
def test_bad_values_and_overrides_are_refused(env, why):
    r = service_env(**env)
    assert r.returncode != 0 and why in r.stderr, (r.stdout, r.stderr)
    assert "WATCH_BENCH_TOKEN=guess" not in r.stdout


def test_env_names_print_no_values():
    r = subprocess.run(["/bin/bash", "-c", f". '{ROOT}/deploy/web/env.sh' && env_names \"$(printf 'A=1\\nB=secret\\n')\""],
                       capture_output=True, text=True)
    assert r.stdout.strip() == "A B"
