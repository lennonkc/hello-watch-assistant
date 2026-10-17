"""Gemini Files registration and renewal → spec §4 sidecar `registry/<sha>.json`.

Registration modes (`--mode`):
- `gcs`: `files.register_files` registers bucket objects directly (no copy; the Gemini API service agent needs read access to the bucket);
- `upload`: download from the bucket, then upload via the Files API (48 h).
- `auto` (default): try `gcs`, fall back to `upload` when rejected (permissions / unsupported).

`expires_at` always comes from the API's `expirationTime`, never estimated; if the API omits it, store null (the online service treats it as missing).
`renew` scans every sidecar and re-registers entries whose `expires_at` is missing or < 12 h away.
"""
from __future__ import annotations

import datetime as dt
import json
import pathlib
import tempfile
import time

from ingest.cloud import gcs

SCOPES = ["https://www.googleapis.com/auth/devstorage.read_only", "https://www.googleapis.com/auth/cloud-platform"]
RENEW_WINDOW = dt.timedelta(hours=12)
MODES = ("auto", "gcs", "upload")


def iso(t: dt.datetime | None) -> str | None:
    return None if t is None else t.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(s: str | None) -> dt.datetime | None:
    return None if not s else dt.datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc)


def entry_due(e: dict, now: dt.datetime, window: dt.timedelta = RENEW_WINDOW) -> bool:
    exp = parse_iso(e.get("expires_at"))
    return not e.get("gemini_file") or exp is None or exp - now < window


def due_entries(sidecar: dict, now: dt.datetime, window: dt.timedelta = RENEW_WINDOW) -> list[str]:
    """storage_uris that need renewal (full asset first, then segments in sidecar order)."""
    out = []
    a = sidecar.get("asset_file")
    if a and entry_due(a, now, window):
        out.append(a["storage_uri"])
    out += [s["storage_uri"] for s in sidecar.get("segments", []) if entry_due(s, now, window)]
    return out


def apply_registrations(sidecar: dict, regs: dict[str, dict]) -> dict:
    """Write {storage_uri: {gemini_file, expires_at}} back into the matching sidecar entries (only these two fields change)."""
    out = json.loads(json.dumps(sidecar))
    for e in [out.get("asset_file")] + out.get("segments", []):
        if e and e["storage_uri"] in regs:
            e.update({k: regs[e["storage_uri"]][k] for k in ("gemini_file", "expires_at")})
    return out


def _state(f) -> str:
    return getattr(f.state, "name", str(f.state))


def _wait_active(c, f, timeout_s: float = 900):
    t0 = time.time()
    while _state(f) == "PROCESSING":
        if time.time() - t0 > timeout_s:
            raise TimeoutError(f"{f.name} still PROCESSING after {timeout_s}s")
        time.sleep(5)
        f = c.files.get(name=f.name)
    if _state(f) != "ACTIVE":
        raise RuntimeError(f"{f.name}: state {_state(f)}")
    return f


REGISTER_URL = "https://generativelanguage.googleapis.com/v1beta/files:register"


def _parse_time(s: str | None) -> dt.datetime | None:
    if not s:
        return None
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))


def register_gcs(c, uris: list[str]) -> dict[str, dict]:
    """`files:register` goes over raw REST + OAuth: the endpoint rejects an API key and OAuth together (401 OVERLOADED_CREDENTIALS),
    and the google-genai 2.30.1 Developer API client always sends the key. Files belong to the OAuth credentials' quota project,
    so the API key must belong to the same project; we then poll `files.get` with the key client until ACTIVE, which also verifies key-side visibility."""
    import google.auth
    from google.auth.transport.requests import AuthorizedSession
    creds, _ = google.auth.default(scopes=SCOPES)
    sess = AuthorizedSession(creds)
    headers = {}
    qp = getattr(creds, "quota_project_id", None)     # service accounts lack this field: billed to their own project
    if qp:
        headers["x-goog-user-project"] = qp
    r = sess.post(REGISTER_URL, json={"uris": uris}, headers=headers, timeout=120)
    if r.status_code != 200:
        raise RuntimeError(f"files:register HTTP {r.status_code}: {r.text[:500]}")
    files = r.json().get("files", [])
    if len(files) != len(uris):
        raise RuntimeError(f"files:register returned {len(files)} files for {len(uris)} uris")
    out = {}
    for u, f in zip(uris, files):
        g = _wait_active(c, c.files.get(name=f["name"]))
        exp = g.expiration_time or _parse_time(f.get("expirationTime"))
        out[u] = {"gemini_file": g.name, "expires_at": iso(exp), "how": "gcs"}
    return out


def register_upload(c, bkt, uris: list[str]) -> dict[str, dict]:
    out = {}
    with tempfile.TemporaryDirectory(prefix="cedar-reg-") as td:
        for u in uris:
            name = u.removeprefix(f"gs://{bkt.name}/")
            p = pathlib.Path(td) / pathlib.Path(name).name
            bkt.blob(name).download_to_filename(str(p), timeout=gcs.TIMEOUT)
            f = c.files.upload(file=str(p), config={"display_name": p.name, "mime_type": "video/mp4"})
            f = _wait_active(c, f)
            out[u] = {"gemini_file": f.name, "expires_at": iso(f.expiration_time), "how": "upload"}
            p.unlink()
    return out


def register_uris(c, bkt, uris: list[str], mode: str) -> dict[str, dict]:
    if not uris:
        return {}
    if mode == "upload":
        return register_upload(c, bkt, uris)
    try:
        return register_gcs(c, uris)
    except Exception as e:
        if mode == "gcs":
            raise
        print(f"WARN register_files failed ({type(e).__name__}: {str(e)[:300]}); falling back to Files upload", flush=True)
        return register_upload(c, bkt, uris)


def new_sidecar(bkt, sha: str, with_segments: bool) -> dict:
    side = {"asset_sha256": sha, "asset_file": {"storage_uri": gcs.uri(bkt, f"assets/{sha}.mp4"),
                                                "gemini_file": None, "expires_at": None}, "segments": []}
    if with_segments:
        segs = gcs.read_json(bkt, f"work/{sha}/stages/segments.json")
        if segs is None:
            raise SystemExit(f"work/{sha}/stages/segments.json missing: run the ingest job first")
        for s in segs["segments"]:
            side["segments"].append({"segment_id": s["segment_id"], "file_sha256": s["file_sha256"],
                                     "storage_uri": gcs.uri(bkt, f"work/{sha}/{s['file']}"),
                                     "gemini_file": None, "expires_at": None})
    return side


def _save(bkt, sha: str, side: dict, generation: int | None) -> None:
    gcs.write_json(bkt, f"registry/{sha}.json", side, if_generation_match=generation or 0)


def cmd_register(args) -> int:
    from ingest import gemini
    from ingest.cloud.job import install_key_from_env
    install_key_from_env()
    bkt = gcs.bucket(gcs.bucket_name(args.bucket))
    c = gemini.client()
    for sha in [s for chunk in args.sha for s in chunk.split(",") if s]:
        if bkt.get_blob(f"assets/{sha}.mp4") is None:
            raise SystemExit(f"assets/{sha}.mp4 not in bucket")
        blob = bkt.get_blob(f"registry/{sha}.json")
        side = new_sidecar(bkt, sha, args.segments)
        regs = register_uris(c, bkt, due_entries(side, dt.datetime.now(dt.timezone.utc)), args.mode)
        side = apply_registrations(side, regs)
        _save(bkt, sha, side, blob.generation if blob else None)
        print("REGISTERED " + json.dumps({"asset_sha256": sha, "how": sorted({r["how"] for r in regs.values()}),
                                          "asset_file": side["asset_file"], "segments": len(side["segments"])}),
              flush=True)
    return 0


def cmd_renew(args) -> int:
    from ingest import gemini
    from ingest.cloud.job import install_key_from_env
    install_key_from_env()
    bkt = gcs.bucket(gcs.bucket_name(args.bucket))
    c = gemini.client()
    now = dt.datetime.now(dt.timezone.utc)
    window = dt.timedelta(hours=args.window_hours)
    renewed = failed = 0
    for b in bkt.client.list_blobs(bkt, prefix="registry/"):
        if not b.name.endswith(".json"):
            continue
        side = json.loads(b.download_as_bytes())
        uris = due_entries(side, now, window)
        if not uris:
            continue
        try:
            regs = register_uris(c, bkt, uris, args.mode)
            _save(bkt, side["asset_sha256"], apply_registrations(side, regs), b.generation)
            renewed += len(regs)
            print("RENEWED " + json.dumps({"asset_sha256": side["asset_sha256"], "entries": len(regs)}), flush=True)
        except Exception as e:  # keep going with the remaining sidecars
            failed += 1
            print(f"ERROR renew {b.name}: {type(e).__name__}: {str(e)[:300]}", flush=True)
    print(json.dumps({"renewed_entries": renewed, "failed_sidecars": failed, "window_hours": args.window_hours}))
    return 1 if failed else 0
