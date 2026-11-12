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


@pytest.fixture
def export_as(monkeypatch, tmp_path):
    """Stand in for the helper: return a receipt pointing at a file the test made."""
    import server.journey as journey

    def use(data: bytes | None, name: str = NAME) -> Path:
        path = tmp_path / name
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
