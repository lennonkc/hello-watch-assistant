"""Private preview titles: loading the titles file, on-demand thumbnails, access codes, hidden everywhere unless
unlocked (no network: a fake bucket on local disk)."""
import collections
import copy
import hashlib
import json
import shutil
import threading

import pytest
from fastapi.testclient import TestClient

from ingest import packdb
from ingest.build import AUTO_OK, device_view
from ingest.tests.conftest import SHA, mini_pack
from server import config, packs

PSHA = "cd" * 32
CODE = "open-sesame-42"


class FakeBucket:
    """The bucket as a directory: object name = relative path."""

    def __init__(self, root):
        self.root = root
        self.reads = collections.Counter()
        self.lock = threading.Lock()
        self.gate = None                      # an Event: read() holds until it is set (no real sleeps in tests)
        self.reading = threading.Event()

    def names(self, prefix):
        return sorted(n for n in (str(p.relative_to(self.root)) for p in self.root.rglob("*") if p.is_file())
                      if n.startswith(prefix))

    def download_many(self, prefix, rel, dest):
        for r in rel:
            (dest / r).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(self.root / (prefix + r), dest / r)
            with self.lock:
                self.reads[prefix + r] += 1

    def read(self, name, timeout=None):
        self.reading.set()
        if self.gate is not None:
            assert self.gate.wait(5)
        with self.lock:
            self.reads[name] += 1
        p = self.root / name
        return p.read_bytes() if p.is_file() else None


def _write_pack(d, pack, thumbs=range(0, 5)):
    d.mkdir(parents=True, exist_ok=True)
    packdb.write_db(pack, d / "pack.db")
    (d / "MANIFEST.json").write_text(json.dumps({"distribution": pack["pack_meta"]["distribution"]}))
    (d / "thumbs").mkdir(exist_ok=True)
    for t in thumbs:
        (d / "thumbs" / f"t{t:05d}.jpg").write_bytes(b"\xff\xd8jpeg%d" % t)


def _device_auto(sha):
    p = copy.deepcopy(mini_pack())
    p["asset"]["asset_sha256"] = sha
    p["asset"]["content_id"] = "secret"
    d = device_view(p, AUTO_OK)
    d["pack_meta"] = {**p["pack_meta"], "asset_sha256": sha, "distribution": "device-auto"}
    return d


@pytest.fixture
def world(tmp_path, monkeypatch):
    """A bucket with one public film (master) and one private preview title (device-auto) under `packs/`, the private
    titles file under `priv/`, and a one-film catalog."""
    gcs = tmp_path / "gcs"
    _write_pack(gcs / "packs" / SHA / "master", mini_pack())
    _write_pack(gcs / "packs" / PSHA / "device-auto", _device_auto(PSHA), thumbs=range(0, 101))
    doc = {"access_sha256": [hashlib.sha256(CODE.encode()).hexdigest(), "not-hex"],
           "titles": [{"id": "pv1", "sha": PSHA, "title": "Preview One", "synopsis": "s", "license": "private",
                       "attribution": "Private preview for evaluation. Not for redistribution.", "dist": "device-auto",
                       "video_object": f"previews/{PSHA}-640.mp4"},
                      {"id": "bad", "sha": "xyz", "title": "t"},
                      {"id": "nopack", "sha": "ef" * 32, "title": "t"}]}
    (gcs / "priv").mkdir()
    (gcs / "priv" / "titles.json").write_text(json.dumps(doc))
    cat = tmp_path / "cat"
    (cat / "catalog").mkdir(parents=True)
    (cat / "ingest" / "titles").mkdir(parents=True)
    (cat / "catalog" / "catalog.json").write_text(json.dumps({"items": [{"id": "mini", "title": "Mini"}]}))
    (cat / "ingest" / "titles" / "mini.json").write_text(json.dumps({"expected_asset_sha256": [SHA],
                                                                     "license": "CC-BY-3.0"}))
    bucket = FakeBucket(gcs)
    monkeypatch.setattr(packs, "bucket_for", lambda name: bucket)
    monkeypatch.setattr(config, "CATALOG_ROOT", cat)
    monkeypatch.setattr(config, "PACKS_DIR", None)
    monkeypatch.setattr(config, "PACKS_GCS", "gs://bkt/packs")
    monkeypatch.setattr(config, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(config, "PRIVATE_TITLES", "gs://bkt/priv/titles.json")
    return bucket


def test_store_loads_private_titles_hidden_with_pack_only(world, caplog):
    caplog.set_level("INFO")
    store = packs.Store().load()
    pub, pv = store.get("mini"), store.get("pv1")
    assert not pub.hidden and pub.playback == "standard" and pub.video_object == f"assets/{SHA}.mp4"
    assert pv.hidden and pv.dist == "device-auto" and pv.playback == "preview"
    assert pv.video_object == f"previews/{PSHA}-640.mp4" and pv.root.parts[-2:] == (PSHA, "device-auto")
    assert sorted(store.titles) == ["mini", "pv1"]              # bad sha and missing pack skipped
    # public: everything copied at startup; private: only pack.db + MANIFEST.json, thumbnails on demand
    assert (pub.root / "thumbs" / "t00003.jpg").exists()
    assert not (pv.root / "thumbs" / "t00003.jpg").exists()
    assert world.reads[f"packs/{PSHA}/device-auto/pack.db"] == 1
    assert world.reads[f"packs/{PSHA}/device-auto/MANIFEST.json"] == 1
    assert pv.pack["pack_meta"]["distribution"] == "device-auto"
    assert store.unlocked(CODE) and not store.unlocked("open-sesame-43") and not store.unlocked("")
    text = caplog.text
    assert "Preview One" not in text and PSHA[:12] not in text and "private preview titles: 1 loaded" in text


@pytest.mark.parametrize("content", [None, "{not json", json.dumps({"titles": "x"})])
def test_missing_or_broken_private_file_is_a_warning(world, content, caplog):
    f = world.root / "priv" / "titles.json"
    if content is None:
        f.unlink()
    else:
        f.write_text(content)
    store = packs.Store().load()
    assert sorted(store.titles) == ["mini"] and not store.access
    assert "private titles: not loaded" in caplog.text


def _private_doc(world, **entry):
    doc = json.loads((world.root / "priv" / "titles.json").read_text())
    doc["titles"][0].update(entry)
    return doc


@pytest.mark.parametrize("damage", ["access not a list", "access entries", "truncated db", "not sqlite",
                                    "missing tables", "empty asset", "public sha", "same sha twice", "titles entry"])
def test_bad_private_data_never_stops_the_public_films(world, damage, caplog):
    """Every private-title failure is a warning and that title is skipped; the public film always loads and serves."""
    from server import main
    doc = json.loads((world.root / "priv" / "titles.json").read_text())
    db = world.root / "packs" / PSHA / "device-auto" / "pack.db"
    if damage == "access not a list":
        doc["access_sha256"] = 5
    elif damage == "access entries":
        doc["access_sha256"] = [None, 7, {"x": 1}, hashlib.sha256(CODE.encode()).hexdigest()]
    elif damage == "truncated db":
        db.write_bytes(db.read_bytes()[:3000])
    elif damage == "not sqlite":
        db.write_bytes(b"this is not a database" * 100)
    elif damage == "missing tables":
        import sqlite3
        con = sqlite3.connect(db)
        for t in ("cues", "characters", "asset", "pack_meta"):
            con.execute(f"DROP TABLE IF EXISTS {t}")
        con.commit()
        con.close()
    elif damage == "empty asset":
        import sqlite3
        con = sqlite3.connect(db)
        con.execute("DELETE FROM asset")
        con.commit()
        con.close()
    elif damage == "public sha":
        doc["titles"][0]["sha"] = SHA                  # would put the on-demand cache on the public thumbs dir
    elif damage == "same sha twice":
        doc["titles"].append({**doc["titles"][0], "id": "pv2"})
    elif damage == "titles entry":
        doc["titles"].insert(0, "not an object")
    (world.root / "priv" / "titles.json").write_text(json.dumps(doc))
    store = packs.Store().load()
    assert "mini" in store.titles
    ok = damage in ("access not a list", "access entries", "same sha twice", "titles entry")
    assert ("pv1" in store.titles) == ok, damage
    assert "pv2" not in store.titles
    if damage == "access entries":
        assert store.unlocked(CODE)
    if damage == "access not a list":                          # loaded, but no code opens it (the bench token does)
        assert not store.access and "access_sha256 is not a list" in caplog.text
    if not ok:
        assert "private" in caplog.text and "skipped" in caplog.text or "not loaded" in caplog.text
    pub_thumbs = store.get("mini").root / "thumbs"
    assert (pub_thumbs / "t00003.jpg").exists()                # public frames untouched
    m = TestClient(main.app)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(packs, "STORE", store)
        mp.setattr(main, "STORE", store)
        r = m.get("/api/titles", headers=COOKIE)
        assert r.status_code == 200 and "mini" in [t["id"] for t in r.json()]
        assert m.get("/api/titles/mini/gate?p=20").status_code == 200


def test_thumbnails_on_demand_negative_cache_eviction_and_prefetch(tmp_path):
    src = tmp_path / "gcs"
    (src / "p" / "thumbs").mkdir(parents=True)
    for t in range(10):
        (src / "p" / "thumbs" / f"t{t:05d}.jpg").write_bytes(b"j%d" % t)
    b = FakeBucket(src)
    now = [1000.0]
    rt = packs.RemoteThumbs(b, "p/thumbs", tmp_path / "local", max_files=3, clock=lambda: now[0])
    assert rt.ensure(1).read_bytes() == b"j1" and rt.ensure(1).exists()
    assert b.reads["p/thumbs/t00001.jpg"] == 1                       # second call served locally
    assert not rt.ensure(50).exists() and not rt.ensure(50).exists()
    assert b.reads["p/thumbs/t00050.jpg"] == 1                       # a miss is remembered
    now[0] += 599
    assert not rt.ensure(50).exists() and b.reads["p/thumbs/t00050.jpg"] == 1
    now[0] += 2                                                      # past miss_ttl_s (600): asked again
    assert not rt.ensure(50).exists() and b.reads["p/thumbs/t00050.jpg"] == 2
    for t in (2, 3, 4):
        rt.ensure(t)
    assert sorted(int(p.stem[1:]) for p in (tmp_path / "local").glob("t*.jpg")) == [2, 3, 4]   # 1 evicted
    rt.ensure(2)                                                     # touch 2: 3 is now the oldest
    rt.ensure(5)
    assert sorted(int(p.stem[1:]) for p in (tmp_path / "local").glob("t*.jpg")) == [2, 4, 5]
    rt2 = packs.RemoteThumbs(b, "p/thumbs", tmp_path / "local2", max_files=100)
    assert rt2.prefetch([6, 7, 8, 99, -1]) == [6, 7, 8]


def test_concurrent_requests_share_one_download(tmp_path):
    src = tmp_path / "gcs"
    (src / "thumbs").mkdir(parents=True)
    (src / "thumbs" / "t00007.jpg").write_bytes(b"seven")
    b = FakeBucket(src)
    b.gate = threading.Event()                                       # the download holds until all six have asked
    rt = packs.RemoteThumbs(b, "thumbs", tmp_path / "local")
    got, asked = [], threading.Barrier(7)

    def ask():
        asked.wait(5)
        got.append(rt.ensure(7).read_bytes())

    threads = [threading.Thread(target=ask) for _ in range(6)]
    for th in threads:
        th.start()
    asked.wait(5)
    assert b.reading.wait(5)                                         # one download in flight
    b.gate.set()
    for th in threads:
        th.join(10)
    assert got == [b"seven"] * 6 and b.reads["thumbs/t00007.jpg"] == 1


def test_fetch_errors_do_not_fail_and_retry_later(tmp_path):
    class Broken:
        calls, timeouts = 0, []

        def read(self, name, timeout=None):
            Broken.calls += 1
            Broken.timeouts.append(timeout)
            raise OSError("network")

    now = [1000.0]
    rt = packs.RemoteThumbs(Broken(), "x", tmp_path / "l", clock=lambda: now[0])
    assert rt.error_ttl_s == 30.0
    assert not rt.ensure(3).exists() and Broken.calls == 2          # one retry
    assert set(Broken.timeouts) == {config.THUMB_FETCH_TIMEOUT_S} == {4.0}
    now[0] += 29
    assert not rt.ensure(3).exists() and Broken.calls == 2          # within the error TTL: not asked again
    now[0] += 2
    assert not rt.ensure(3).exists() and Broken.calls == 4          # past it: asked again (twice)


def test_one_slow_attempt_is_retried(tmp_path):
    class Flaky:
        calls = 0

        def read(self, name, timeout=None):
            Flaky.calls += 1
            if Flaky.calls == 1:
                raise TimeoutError("read timed out")
            return b"jpeg"

    rt = packs.RemoteThumbs(Flaky(), "x", tmp_path / "l")
    assert rt.ensure(7).read_bytes() == b"jpeg" and Flaky.calls == 2


@pytest.fixture
def client(world, monkeypatch):
    from server import main
    store = packs.Store().load()
    monkeypatch.setattr(packs, "STORE", store)
    monkeypatch.setattr(main, "STORE", store)
    monkeypatch.setattr(main, "UNLOCKS", main.Attempts(config.UNLOCK_FAILURES_PER_MINUTE, clock=lambda: 1000.0))
    monkeypatch.setattr(main, "_signed_url", lambda obj, _e: f"https://signed.example/{obj}")
    monkeypatch.setattr(config, "ASSET_BUCKET", "bkt")
    monkeypatch.setattr(config, "VIDEO_DIR", None)
    return TestClient(main.app)


COOKIE = {"Cookie": f"watch_access={CODE}"}


def test_hidden_everywhere_without_the_cookie(client):
    assert [t["id"] for t in client.get("/api/titles").json()] == ["mini"]
    assert client.get("/api/titles").json()[0]["playback"] == "standard"
    h = client.get("/api/health").json()
    assert h["ok"] is True and h["titles"] == 1 and [x["sha"] for x in h["packs"]] == [SHA]
    assert PSHA not in str(h) and "pv1" not in str(h)                     # nothing about private preview titles
    for url in ("/api/titles/pv1/frames/3.jpg", "/api/titles/pv1/video", "/api/titles/pv1/gate?p=10"):
        r = client.get(url, follow_redirects=False)
        assert r.status_code == 404 and r.json() == {"detail": "unknown title"}, url
    r = client.post("/api/ask", json={"title_id": "pv1", "p": 10, "question": "Who is that?"})
    assert r.status_code == 404
    bad = {"Cookie": "watch_access=wrong"}
    assert client.get("/api/titles/pv1/gate?p=10", headers=bad).status_code == 404


def test_unlocked_titles_frames_and_video(client, world):
    items = client.get("/api/titles", headers=COOKIE)
    assert items.headers["cache-control"] == "private, no-store"
    pv = [t for t in items.json() if t["id"] == "pv1"][0]
    assert pv["private"] is True and pv["playback"] == "preview" and items.json()[0]["id"] == "mini"
    r = client.get("/api/titles/pv1/frames/3.jpg", headers=COOKIE)
    assert r.status_code == 200 and r.content == b"\xff\xd8jpeg3" and r.headers["cache-control"] == "private, no-store"
    assert world.reads[f"packs/{PSHA}/device-auto/thumbs/t00003.jpg"] == 1
    assert client.get("/api/titles/pv1/frames/999.jpg", headers=COOKIE).status_code == 404
    v = client.get("/api/titles/pv1/video", headers=COOKIE, follow_redirects=False)
    assert v.status_code == 302 and v.headers["location"] == f"https://signed.example/previews/{PSHA}-640.mp4"
    assert v.headers["cache-control"] == "private, no-store"
    pub = client.get("/api/titles/mini/video", follow_redirects=False)
    assert pub.headers["location"].endswith(f"assets/{SHA}.mp4") and "cache-control" not in pub.headers
    assert client.get("/api/titles/mini/frames/3.jpg").headers["cache-control"] == "public, max-age=86400"
    g = client.get("/api/titles/pv1/gate?p=53", headers=COOKIE).json()
    assert g["eligible"]["scene"] == 1                               # the device-auto pack gates like master


def test_the_bench_token_unlocks_without_an_access_code(client, monkeypatch):
    """The benchmark host tests a private preview title with its bench token only; a wrong or unset token does not."""
    from server import main
    monkeypatch.setattr(config, "BENCH_TOKEN", "t0ken")
    ok = {main.BENCH_HEADER: "t0ken"}
    assert client.get("/api/titles/pv1/gate?p=53", headers=ok).status_code == 200
    assert "pv1" in [t["id"] for t in client.get("/api/titles", headers=ok).json()]
    assert client.get("/api/titles/pv1/gate?p=53", headers={main.BENCH_HEADER: "wrong"}).status_code == 404
    monkeypatch.setattr(config, "BENCH_TOKEN", "")
    assert client.get("/api/titles/pv1/gate?p=53", headers={main.BENCH_HEADER: ""}).status_code == 404
    assert client.get("/api/health", headers=ok).json()["titles"] == 1          # still only public films counted


def test_unlock_sets_the_cookie_and_limits_attempts(client):
    r = client.post("/api/unlock", json={"code": CODE})
    assert r.status_code == 204
    c = r.headers["set-cookie"]
    for part in (f"watch_access={CODE}", "HttpOnly", "Max-Age=2592000", "Path=/", "SameSite=lax", "Secure"):
        assert part in c
    assert r.headers["cache-control"] == "private, no-store"
    g = client.get("/api/unlock", params={"code": CODE})                # never in a URL: no GET form
    assert g.status_code in (404, 405) and "set-cookie" not in g.headers
    assert client.post("/api/unlock", json={"code": "nope"}).status_code == 404
    assert client.post("/api/unlock", content=b"not json").status_code == 404
    assert client.post("/api/unlock", json={"code": 42}).status_code == 404
    assert client.post("/api/unlock", json={"code": "ünlöck-ça"}).status_code == 404     # not printable ASCII: no 500
    assert [client.post("/api/unlock", json={"code": CODE}).status_code for _ in range(40)] == [204] * 40  # not counted
    codes = [client.post("/api/unlock", json={"code": f"guess{i}"}).status_code for i in range(30)]
    assert codes[:26] == [404] * 26 and codes[26:] == [429] * 4     # 30 failures a minute (4 above were counted)
    assert client.post("/api/unlock", json={"code": CODE}).status_code == 429   # blocked, even for the right code


def test_invalid_cookies_count_as_failures_so_no_endpoint_is_an_oracle(client):
    """Guessing through the cookie: each distinct wrong value is a failure; past the limit no cookie counts."""
    stale = {"Cookie": "watch_access=an-old-code"}
    for _ in range(50):                                                 # one stale cookie, many requests: one failure
        assert "pv1" not in [t["id"] for t in client.get("/api/titles", headers=stale).json()]
    assert "pv1" in [t["id"] for t in client.get("/api/titles", headers=COOKIE).json()]
    for i in range(29):
        client.get("/api/titles/pv1/gate?p=10", headers={"Cookie": f"watch_access=guess{i}"})
    assert client.get("/api/titles/pv1/gate?p=10", headers=COOKIE).status_code == 404    # over the limit: no oracle
    assert client.post("/api/unlock", json={"code": CODE}).status_code == 429
    assert client.get("/api/titles/mini/gate?p=10").status_code == 200                  # public films unaffected


def test_the_unlock_window_slides_on_the_injected_clock():
    from server import main
    now = [1000.0]
    a = main.Attempts(3, clock=lambda: now[0])
    a.fail("1.2.3.4")
    a.fail("1.2.3.4")
    a.fail("1.2.3.4", key="c1")
    a.fail("1.2.3.4", key="c1")                                        # the same wrong cookie again: counted once
    assert a.blocked("1.2.3.4") == 60 and not a.blocked("5.6.7.8")
    now[0] += 59.5
    assert a.blocked("1.2.3.4") == 1                                   # seconds until the oldest failure leaves
    now[0] += 1
    assert not a.blocked("1.2.3.4")                                    # the window has slid past all three


def test_check_pack_reports_what_the_service_would_miss(tmp_path):
    p = mini_pack()
    packdb.write_db(p, tmp_path / "pack.db")
    issues = packs.check_pack(tmp_path / "pack.db")
    assert any(i.startswith("no rows in:") and "embeddings" in i for i in issues)
    d = _device_auto(PSHA)
    packdb.write_db(d, tmp_path / "d.db")
    assert not any("review_log" in i or "missing tables" in i for i in packs.check_pack(tmp_path / "d.db"))


def test_private_spec_validation():
    ok = packs.private_spec({"id": "a", "sha": "AB" * 32, "title": "T"})
    assert ok["sha"] == "ab" * 32 and ok["dist"] == "master" and ok["video_object"] == f"assets/{'ab' * 32}.mp4"
    for bad in ({"id": "a", "sha": "ab" * 32, "title": "T", "dist": "device"},
                {"id": "a", "sha": "ab" * 32, "title": "T", "video_object": "../x.mp4"},
                {"id": "a/b", "sha": "ab" * 32, "title": "T"}, {"id": "a", "sha": "ab" * 32}):
        with pytest.raises(ValueError):
            packs.private_spec(bad)
