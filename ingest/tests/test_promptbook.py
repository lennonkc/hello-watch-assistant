"""Runtime prompt loading: source precedence, directory reading, placeholder validation, Secret Manager bundle format."""
import base64
import json

import pytest

from ingest import promptbook as pb


@pytest.fixture
def fresh(monkeypatch):
    pb.use(None)
    pb._load.cache_clear()
    monkeypatch.delenv(pb.SECRET_ENV, raising=False)
    monkeypatch.delenv(pb.DIR_ENV, raising=False)
    yield
    pb._load.cache_clear()


def _write(d, texts):
    d.mkdir(exist_ok=True)
    for n, t in texts.items():
        (d / f"{n}.txt").write_text(t + "\n", encoding="utf-8")


def test_dir_strips_one_trailing_newline(tmp_path, monkeypatch, fresh):
    texts = pb.placeholder_bundle() | {"recap_rules": "at {at}, {words} words\n", "present_rule": "\nPRESENCE"}
    _write(tmp_path / "p", texts)
    monkeypatch.setenv(pb.DIR_ENV, str(tmp_path / "p"))
    assert pb.text("recap_rules") == "at {at}, {words} words\n"
    assert pb.text("present_rule") == "\nPRESENCE"
    assert pb.render("recap_rules", at="01:00", words=50) == "at 01:00, 50 words\n"
    assert pb.source().startswith("dir:")


def test_validate_rejects_missing_and_wrong_placeholders():
    texts = pb.placeholder_bundle()
    with pytest.raises(pb.PromptsUnavailable, match="missing speaker"):
        pb.validate({k: v for k, v in texts.items() if k != "speaker"})
    with pytest.raises(pb.PromptsUnavailable, match="recap_rules"):
        pb.validate(texts | {"recap_rules": "no placeholders"})
    assert pb.validate(texts | {"shot": texts["shot"] + " {{c03}} literal braces"})


def test_secret_wins_over_dir(tmp_path, monkeypatch, fresh):
    _write(tmp_path / "p", pb.placeholder_bundle())
    monkeypatch.setenv(pb.DIR_ENV, str(tmp_path / "p"))
    monkeypatch.setenv(pb.SECRET_ENV, "projects/p/secrets/s/versions/latest")
    from_secret = pb.placeholder_bundle() | {"no_people": "FROM SECRET"}
    monkeypatch.setattr(pb, "read_secret", lambda name: from_secret)
    assert pb.text("no_people") == "FROM SECRET"
    assert pb.source() == "secret:projects/p/secrets/s/versions/latest"


def test_secret_payload_format(monkeypatch):
    payload = pb.bundle_json(pb.placeholder_bundle()).encode()

    class Resp:
        status_code = 200
        text = ""

        def json(self):
            return {"payload": {"data": base64.b64encode(payload).decode()}}

    class Session:
        def __init__(self, creds):
            pass

        def get(self, url, timeout):
            assert url == "https://secretmanager.googleapis.com/v1/projects/p/secrets/s/versions/3:access"
            return Resp()

    import google.auth
    import google.auth.transport.requests as gtr
    monkeypatch.setattr(google.auth, "default", lambda scopes: (None, "p"))
    monkeypatch.setattr(gtr, "AuthorizedSession", Session)
    assert pb.read_secret("projects/p/secrets/s/versions/3") == pb.placeholder_bundle()
    assert json.loads(payload)["format"] == pb.FORMAT


def test_nothing_configured_is_a_clear_error(tmp_path, monkeypatch, fresh):
    monkeypatch.setattr(pb, "DEFAULT_DIR", tmp_path / "absent")
    with pytest.raises(pb.PromptsUnavailable, match="CEDAR_PROMPTS_SECRET"):
        pb.text("common_rules")


@pytest.mark.real_prompts
def test_repo_bundle_is_valid():
    assert set(pb.NAMES) <= set(pb._load()[1])
