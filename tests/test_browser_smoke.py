import hashlib
import io
import tarfile
import urllib.request
from types import SimpleNamespace

from scripts import smoke_browser


def _python_archive() -> bytes:
    payload = io.BytesIO()
    with tarfile.open(fileobj=payload, mode="w:gz") as bundle:
        for name, contents, mode in (
            ("python/bin/python3.11", b"verified standalone python", 0o755),
            ("python/share/manual", b"excluded case-only share content", 0o644),
        ):
            member = tarfile.TarInfo(name)
            member.size = len(contents)
            member.mode = mode
            bundle.addfile(member, io.BytesIO(contents))
    return payload.getvalue()


def _fake_browser_artifacts(monkeypatch, tmp_path) -> bytes:
    from server.bootstrap import browser_runtime, install
    from server.bootstrap.artifacts import ArtifactManifest

    contents = b"browser artifact bytes"
    artifact = tmp_path / "browser-artifact"
    artifact.write_bytes(contents)

    class Contract:
        def fetch(self, name, *, staging_dir):
            return SimpleNamespace(path=str(artifact), source="local")

    monkeypatch.setattr(
        ArtifactManifest, "from_path", classmethod(lambda cls, path="": Contract())
    )
    monkeypatch.setattr(install, "BROWSER_ARTIFACT", "browser")
    monkeypatch.setattr(browser_runtime, "artifact_names", lambda: [])
    return contents


def _pin_python(monkeypatch, payload: bytes) -> tuple[str, str]:
    url = "https://example.invalid/python.tar.gz"
    expected = hashlib.sha256(payload).hexdigest()
    monkeypatch.setattr(smoke_browser, "PYTHON_311", (url, expected))
    return url, expected


def _archive_path(work, expected: str):
    return work / f"python-3.11-{expected}.tar.gz"


def test_python_archive_cache_hit_avoids_network_and_reextracts(
    tmp_path, monkeypatch, capsys
):
    browser_bytes = _fake_browser_artifacts(monkeypatch, tmp_path)
    payload = _python_archive()
    _url, expected = _pin_python(monkeypatch, payload)
    work = tmp_path / "stage"
    (work / "python" / "bin").mkdir(parents=True)
    archive = _archive_path(work, expected)
    archive.write_bytes(payload)
    (work / "python" / "bin" / "python3.11").write_bytes(b"untrusted extraction")
    (work / "python" / "stale").write_text("remove me")

    def no_network(*args, **kwargs):
        raise AssertionError("a verified archive cache hit must not use the network")

    monkeypatch.setattr(urllib.request, "urlopen", no_network)

    smoke_browser.stage(work)

    assert archive.read_bytes() == payload
    assert (work / "python" / "bin" / "python3.11").read_bytes() == (
        b"verified standalone python"
    )
    assert not (work / "python" / "stale").exists()
    assert not (work / "python" / "share").exists()
    output = capsys.readouterr().out
    assert "standalone Python cache hit" in output
    assert (
        f"browser artifact fetch complete: name=browser bytes={len(browser_bytes)} source=local"
        in output
    )


def test_corrupt_python_archive_is_downloaded_verified_and_replaced(
    tmp_path, monkeypatch, capsys
):
    _fake_browser_artifacts(monkeypatch, tmp_path)
    payload = _python_archive()
    url, expected = _pin_python(monkeypatch, payload)
    work = tmp_path / "stage"
    work.mkdir()
    archive = _archive_path(work, expected)
    archive.write_bytes(b"corrupt cached archive")
    calls = []

    def download(source, timeout):
        calls.append((source, timeout))
        return io.BytesIO(payload)

    monkeypatch.setattr(urllib.request, "urlopen", download)

    smoke_browser.stage(work)

    assert calls == [(url, 300)]
    assert archive.read_bytes() == payload
    assert hashlib.sha256(archive.read_bytes()).hexdigest() == expected
    assert (work / "python" / "bin" / "python3.11").read_bytes() == (
        b"verified standalone python"
    )
    output = capsys.readouterr().out
    assert "standalone Python download started" in output
    assert "cache=checksum-mismatch" in output
    assert "standalone Python download complete" in output
    assert "source=network" in output
