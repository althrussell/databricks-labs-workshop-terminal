"""Download my code: the verified ZIP streams from this server, whatever its size.

The browser never reads the Volume, so these tests pin the server half of the
contract: the bytes that leave are the bytes the helper verified, they arrive
with their length and an attachment disposition, large archives are not capped,
and a missing or unreadable cache file is a clear 503 rather than a 500 after the
response has started. The expired-sign-in redirect happens in the Apps proxy, so
its handling is tested on the client (frontend/src/exportDownload.test.ts).
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import uuid

import pytest

NAME = "workshop-code-20261113T001122Z-0a1b2c3d.zip"


@pytest.fixture
def person():
    return {"X-Forwarded-Email": f"export-{uuid.uuid4().hex[:8]}@example.com"}


def _cache(person) -> Path:
    """The helper writes its ZIPs to the attendee's private cache, and only there are they served."""
    from server.users import user_manager

    cache = Path(user_manager.get(person["X-Forwarded-Email"]).home, ".cache", "workshop", "exports")
    cache.mkdir(parents=True, exist_ok=True)
    return cache


@pytest.fixture
def export_as(monkeypatch, person):
    """Stand in for the helper: return a receipt pointing at a file the test made in the cache."""
    import server.journey as journey

    def use(data: bytes | None, name: str = NAME) -> Path:
        path = _cache(person) / name
        if data is not None:
            path.write_bytes(data)
        receipt = {"local_zip": str(path), "filename": name, "path": f"/Volumes/c/s/workshop_exports/code/{name}",
                   "files": 3, "reused": False}
        monkeypatch.setattr(journey, "export", lambda user, **kwargs: receipt)
        return path

    return use


def _payload(megabytes: float) -> bytes:
    """Deterministic, incompressible bytes of an exact size."""
    size = int(megabytes * 1024 * 1024)
    block = hashlib.sha256(b"seed").digest()
    return (block * (size // len(block) + 1))[:size]


@pytest.mark.parametrize("megabytes", [0.001, 12, 40])
def test_the_verified_zip_streams_unchanged_with_its_length_and_an_attachment_name(client, export_as, person, megabytes):
    data = _payload(megabytes)
    export_as(data)

    response = client.post("/api/export", headers=person)

    assert response.status_code == 200, response.text
    assert response.content == data
    assert response.headers["content-length"] == str(len(data))
    assert response.headers["content-type"] == "application/zip"
    disposition = response.headers["content-disposition"]
    assert disposition.startswith("attachment")
    assert NAME in disposition
    assert response.headers["x-workshop-export-path"].endswith(f"/workshop_exports/code/{NAME}")
    assert response.headers["x-workshop-export-files"] == "3"
    assert response.headers["cache-control"] == "no-store"


def test_a_missing_cache_file_is_a_clear_503_not_a_server_error(client, export_as, person):
    export_as(None)

    response = client.post("/api/export", headers=person)

    assert response.status_code == 503
    assert "Click Download my code again" in response.json()["detail"]


@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0, reason="root can read a mode-000 file")
def test_an_unreadable_cache_file_is_a_clear_503(client, export_as, person):
    path = export_as(_payload(0.01))
    path.chmod(0)
    try:
        response = client.post("/api/export", headers=person)
    finally:
        path.chmod(0o600)

    assert response.status_code == 503
    assert "Click Download my code again" in response.json()["detail"]


def test_a_zip_outside_the_attendees_cache_is_refused(client, monkeypatch, tmp_path, person):
    import server.journey as journey

    elsewhere = tmp_path / NAME
    elsewhere.write_bytes(b"not-the-helper-output")
    monkeypatch.setattr(journey, "export", lambda user, **kwargs: {"local_zip": str(elsewhere), "filename": NAME})

    response = client.post("/api/export", headers=person)

    assert response.status_code == 503
    assert b"not-the-helper-output" not in response.content


def test_a_symlink_planted_in_the_cache_is_refused(client, monkeypatch, tmp_path, person):
    """The attendee's own shell can plant a link under the cache; following it would serve the target."""
    import server.journey as journey

    target = tmp_path / "server-readable.txt"
    target.write_text("dapi-server-side-secret\n")
    link = _cache(person) / NAME
    link.symlink_to(target)
    monkeypatch.setattr(journey, "export", lambda user, **kwargs: {"local_zip": str(link), "filename": NAME})

    response = client.post("/api/export", headers=person)

    assert response.status_code == 503
    assert b"dapi-server-side-secret" not in response.content


@pytest.mark.parametrize("symlink_rel", [".cache", ".cache/workshop", ".cache/workshop/exports"])
def test_a_symlinked_cache_ancestor_directory_is_refused(client, monkeypatch, tmp_path, person, symlink_rel):
    """Replacing ~/.cache, ~/.cache/workshop, or ~/.cache/workshop/exports with a directory symlink
    must not trick parent-path comparison into serving files outside the attendee home."""
    import shutil
    import server.journey as journey
    from server.users import user_manager

    home = Path(user_manager.get(person["X-Forwarded-Email"]).home)
    home.mkdir(parents=True, exist_ok=True)
    outside_root = tmp_path / "outside"
    rel_parts = Path(symlink_rel).parts
    remaining_parts = (".cache", "workshop", "exports")[len(rel_parts):]
    target_exports = outside_root.joinpath(*remaining_parts)
    target_exports.mkdir(parents=True, exist_ok=True)
    (target_exports / NAME).write_text("dapi-ancestor-symlink-secret\n")

    link_path = home.joinpath(*rel_parts)
    if link_path.is_dir() and not link_path.is_symlink():
        shutil.rmtree(link_path)
    link_path.parent.mkdir(parents=True, exist_ok=True)
    link_path.symlink_to(outside_root, target_is_directory=True)

    planted = home / ".cache" / "workshop" / "exports" / NAME
    monkeypatch.setattr(journey, "export", lambda user, **kwargs: {"local_zip": str(planted), "filename": NAME})

    response = client.post("/api/export", headers=person)

    assert response.status_code == 503
    assert b"dapi-ancestor-symlink-secret" not in response.content


def test_a_non_regular_file_in_the_cache_is_refused(client, monkeypatch, person):
    import server.journey as journey

    fifo = _cache(person) / NAME
    os.mkfifo(fifo)
    monkeypatch.setattr(journey, "export", lambda user, **kwargs: {"local_zip": str(fifo), "filename": NAME})

    response = client.post("/api/export", headers=person)

    assert response.status_code == 503


def test_only_a_helper_archive_name_can_be_streamed(client, monkeypatch, tmp_path, person):
    """The route must not become a file server: a receipt pointing elsewhere is refused."""
    import server.journey as journey

    secret = tmp_path / "secret.env"
    secret.write_text("WORKSHOP_PAT=dapi-not-for-download\n")
    receipt = {"local_zip": str(secret), "filename": "secret.env", "path": "", "files": 0}
    monkeypatch.setattr(journey, "export", lambda user, **kwargs: receipt)

    response = client.post("/api/export", headers=person)

    assert response.status_code == 503
    assert b"dapi-not-for-download" not in response.content


def test_a_helper_failure_is_reported_with_its_own_message(client, monkeypatch, person):
    import server.journey as journey

    def fail(user, **kwargs):
        raise RuntimeError("Permission denied or session expired during grants get-effective.")

    monkeypatch.setattr(journey, "export", fail)

    response = client.post("/api/export", headers=person)

    assert response.status_code == 503
    assert response.json()["detail"].startswith("Export did not finish: Permission denied")


def test_download_my_code_reuses_fresh_single_project_cli_export_end_to_end(client, monkeypatch, person):
    """Running `workshop-export --project ...` in the terminal then clicking Download my code
    streams that exact single-project ZIP and filename without a second Volume round-trip."""
    import json
    import subprocess
    import zipfile
    from server.users import user_manager

    user = user_manager.get(person["X-Forwarded-Email"])
    home = Path(user.home)
    for proj in ("garden", "repair-desk"):
        proj_dir = home / "projects" / proj
        proj_dir.mkdir(parents=True, exist_ok=True)
        (proj_dir / "app.py").write_text(f"print({proj!r})\n")

    bin_dir = home / ".local" / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    log_file = home / "databricks-calls.jsonl"
    state_dir = home / "fake-volume"
    state_dir.mkdir(parents=True, exist_ok=True)
    fake_dbx = bin_dir / "databricks"
    fake_dbx.write_text(
        "#!/usr/bin/env python3\n"
        "import json, pathlib, shutil, sys\n"
        f"log = pathlib.Path({str(log_file)!r})\n"
        f"store = pathlib.Path({str(state_dir)!r})\n"
        "args = sys.argv[1:]\n"
        "with log.open('a') as f:\n"
        "    f.write(json.dumps(args) + '\\n')\n"
        "if args[:2] == ['current-user', 'me']:\n"
        "    print(json.dumps({'userName': 'coding-sp'}))\n"
        "elif args[:2] == ['grants', 'get-effective']:\n"
        "    print(json.dumps({'privilege_assignments': [{'principal': 'coding-sp', 'privileges': [{'privilege': 'ALL_PRIVILEGES'}]}]}))\n"
        "elif args[:2] == ['fs', 'cp']:\n"
        "    src, dst = args[2], args[3]\n"
        "    if src.startswith('dbfs:'):\n"
        "        shutil.copyfile(store / pathlib.Path(src).name, dst)\n"
        "    else:\n"
        "        shutil.copyfile(src, store / pathlib.Path(dst).name)\n"
        "else:\n"
        "    print('{}')\n"
    )
    fake_dbx.chmod(0o755)

    env = {
        **user.shell_env(),
        "HOME": str(home),
        "WORKSHOP_CATALOG": "cat",
        "WORKSHOP_SCHEMA": "sch",
        "PATH": f"{bin_dir}:{os.environ.get('PATH', '')}",
    }
    monkeypatch.setattr(user, "shell_env", lambda: env.copy())

    helper = Path(__file__).resolve().parents[1] / "assets" / "bin" / "workshop-export"
    cli_run = subprocess.run(
        [str(helper), "--project", str(home / "projects" / "garden")],
        capture_output=True, text=True, env=env, cwd=str(home), check=True,
    )
    calls_after_cli = len(log_file.read_text().splitlines())
    assert calls_after_cli > 0

    response = client.post("/api/export", headers=person)

    assert response.status_code == 200, response.text
    disposition = response.headers["content-disposition"]
    cli_filename = next(
        word.rstrip(",") for word in cli_run.stdout.split()
        if word.startswith("workshop-code-") and word.rstrip(",").endswith(".zip")
    )
    assert cli_filename in disposition
    assert len(log_file.read_text().splitlines()) == calls_after_cli

    saved_zip = home / "downloaded.zip"
    saved_zip.write_bytes(response.content)
    with zipfile.ZipFile(saved_zip) as zf:
        names = set(zf.namelist())
        assert "workshop-code/garden/app.py" in names
        assert not any("repair-desk" in name for name in names)


def test_export_endpoint_supports_project_filter_and_rejects_traversal(client, monkeypatch, person):
    import server.journey as journey
    from server.users import user_manager

    user = user_manager.get(person["X-Forwarded-Email"])
    (Path(user.home) / "projects" / "garden").mkdir(parents=True, exist_ok=True)
    seen: list[str | None] = []

    def fake_export(u, *, reason="attendee", reuse=False, project=None):
        seen.append(project)
        path = _cache(person) / NAME
        path.write_bytes(b"PK\x05\x06" + b"\x00" * 18)
        return {"local_zip": str(path), "filename": NAME, "path": f"/Volumes/c/s/workshop_exports/code/{NAME}", "files": 1}

    monkeypatch.setattr(journey, "export", fake_export)

    ok = client.post("/api/export?project=garden", headers=person)
    assert ok.status_code == 200
    assert seen == ["garden"]

    bad = client.post("/api/export?project=../outside", headers=person)
    assert bad.status_code == 400


def test_a_failed_export_download_never_blocks_the_closeout_seal(client, monkeypatch):
    """A 503 on POST /api/export (no files yet, transient Volume error, or missing cache ZIP)
    must never leave an unresolvable accepted_writer_failed that blocks WT-02 closeout."""
    import server.event_emitter as emitter_module
    import server.journey as journey
    import server.main as main
    from server.capture_journal import CaptureJournal, MemoryCaptureStorage
    from server.closeout import CloseoutManager
    from server.event_emitter import EventEmitter

    binding = {
        "run_id": "export-closeout-run",
        "unit_id": "export-closeout-unit",
        "workspace_id": "123",
        "owner": "alice@example.com",
    }
    owner_headers = {"X-Forwarded-Email": binding["owner"]}
    monkeypatch.setenv("WORKSHOP_INSIGHT_CAPTURE", "true")
    emitter = EventEmitter(
        run_id=binding["run_id"],
        workspace_id=binding["workspace_id"],
        ingest_url="",
        ingest_token="",
        journal=CaptureJournal(MemoryCaptureStorage(), binding, {}),
    )
    monkeypatch.setattr(emitter_module, "event_emitter", emitter)
    monkeypatch.setattr(main, "event_emitter", emitter)

    # 1. Helper failure -> 503
    def fail_helper(user, **kwargs):
        raise RuntimeError("No project source files were found to export.")

    monkeypatch.setattr(journey, "export", fail_helper)
    r1 = client.post("/api/export", headers=owner_headers)
    assert r1.status_code == 503

    # 2. Missing cache file -> 503
    monkeypatch.setattr(
        journey,
        "export",
        lambda user, **kwargs: {"local_zip": "/nonexistent/" + NAME, "filename": NAME},
    )
    r2 = client.post("/api/export", headers=owner_headers)
    assert r2.status_code == 503

    assert emitter.journal.read().get("closeout", {}).get("writer_outcomes", []) == []

    now = [100.0]
    manager = CloseoutManager(
        emitter,
        clock=lambda: now[0],
        drainer=lambda _timeout: {"complete": True, "issues": [], "outcomes": []},
        finalizer=lambda *_args: {"complete": True, "issues": [], "outputs": {}},
    )
    manager.begin(
        capture_id="export-seal-check",
        stream_id=emitter.stream_id,
        source=binding,
        drain_timeout_seconds=1,
    )
    now[0] += 2
    sealed = manager.advance()
    assert sealed["phase"] == "SEALED"
    assert sealed["complete"] is True
