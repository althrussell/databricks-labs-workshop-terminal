"""The headless browser runs in a container with no system packages (W3).

A Databricks Apps container lacks the 21 shared libraries Chrome for Testing's
headless shell links, and has no package manager. Bootstrap installs pinned
Ubuntu packages that provide them, mirror-first like every other artifact, and
points only the browser process at them. When it still cannot run, the reason
is logged at WARNING and exported as a fixed ``event.reason_code``.
"""
from __future__ import annotations

import io
import json
import logging
import os
from pathlib import Path
import subprocess
import tarfile
import zipfile

import pytest

from server import browser_evidence, observability, telemetry
from server.bootstrap import browser_runtime, install
from server.bootstrap.artifacts import ArtifactManifest, ArtifactManifestError
from server.users import User

ROOT = Path(__file__).resolve().parents[1]


def _deb(members: dict[str, bytes | str], *, data_name: str = "data.tar.xz") -> bytes:
    """A Debian package: ``ar`` around a data tarball. ``str`` values are symlinks."""
    data = io.BytesIO()
    compression = data_name.removeprefix("data.tar").lstrip(".")
    with tarfile.open(fileobj=data, mode=f"w:{compression}" if compression else "w") as tar:
        for name, content in members.items():
            info = tarfile.TarInfo(name)
            if isinstance(content, str):
                info.type, info.linkname = tarfile.SYMTYPE, content
                tar.addfile(info)
            else:
                info.size = len(content)
                tar.addfile(info, io.BytesIO(content))
    out = io.BytesIO()
    out.write(b"!<arch>\n")
    for name, body in (("debian-binary", b"2.0\n"), ("control.tar.xz", b""), (data_name, data.getvalue())):
        out.write(f"{name:<16}{0:<12}{0:<6}{0:<6}{100644:<8}{len(body):<10}`\n".encode())
        out.write(body + (b"\n" if len(body) % 2 else b""))
    return out.getvalue()


def _write_deb(tmp_path: Path, name: str, members: dict, **kwargs) -> str:
    path = tmp_path / name
    path.write_bytes(_deb(members, **kwargs))
    return str(path)


def test_only_libraries_and_fonts_are_unpacked_flat_and_links_stay_inside(tmp_path):
    lib = "./usr/lib/x86_64-linux-gnu/"
    deb = _write_deb(tmp_path, "a.deb", {
        lib + "libfoo.so.1.2.3": b"\x7fELF foo",
        lib + "libfoo.so.1": "libfoo.so.1.2.3",
        lib + "nss/libsoft.so": b"\x7fELF soft",
        lib + "libsoft.so": "nss/libsoft.so",
        lib + "libescape.so.1": "/etc/passwd",
        "./lib/x86_64-linux-gnu/libpcre.so.3": b"\x7fELF pcre",
        "./usr/share/doc/libfoo/copyright": b"text",
        "./usr/bin/foo-tool": b"#!/bin/sh",
        "./usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf": b"ttf",
    })
    runtime = tmp_path / "shared" / "browser" / "runtime-x"
    browser_runtime.unpack([deb], str(runtime))

    assert sorted(os.listdir(runtime / "lib")) == ["libfoo.so.1", "libfoo.so.1.2.3", "libpcre.so.3", "libsoft.so"]
    assert os.readlink(runtime / "lib" / "libfoo.so.1") == "libfoo.so.1.2.3"
    assert (runtime / "lib" / "libsoft.so").read_bytes() == b"\x7fELF soft"   # the real file, not a dangling link
    assert os.listdir(runtime / "fonts") == ["LiberationSans-Regular.ttf"]
    conf = (runtime / "fonts.conf").read_text()
    assert f"<dir>{runtime / 'fonts'}</dir>" in conf and "Liberation Sans" in conf
    assert [p for p in runtime.parent.iterdir() if p.name.startswith(".")] == []


@pytest.mark.parametrize("hostile", ["../../etc/cron.d/x", "/etc/ld.so.preload"])
def test_a_package_that_escapes_its_root_writes_nothing(tmp_path, hostile):
    deb = _write_deb(tmp_path, "evil.deb", {
        "./usr/lib/x86_64-linux-gnu/libok.so.1": b"ok",
        hostile: b"pwned",
    })
    runtime = tmp_path / "browser" / "runtime-x"
    with pytest.raises(browser_runtime.ExtractError, match="escapes"):
        browser_runtime.unpack([deb], str(runtime))
    assert not runtime.exists() and not list(tmp_path.glob("**/.browser-runtime-*"))


def test_a_zstd_or_corrupt_package_fails_with_a_clear_reason(tmp_path):
    zst = tmp_path / "zst.deb"
    zst.write_bytes(_deb({}, data_name="data.tar").replace(b"data.tar        ", b"data.tar.zst    "))
    with pytest.raises(browser_runtime.ExtractError, match="unsupported package compression data.tar.zst"):
        browser_runtime.unpack([str(zst)], str(tmp_path / "rt"))
    junk = tmp_path / "junk.deb"
    junk.write_bytes(b"PK\x03\x04 not a deb")
    with pytest.raises(browser_runtime.ExtractError, match="not a Debian package"):
        browser_runtime.unpack([str(junk)], str(tmp_path / "rt"))


def test_every_runtime_package_is_a_pinned_mirrorable_manifest_artifact():
    import dependency_sources

    manifest = json.loads((ROOT / "assets" / "artifacts" / "manifest.json").read_text())["artifacts"]
    contract = ArtifactManifest.from_path("")
    assert len(browser_runtime.RUNTIME_DEBS) == len(set(browser_runtime.artifact_names())) == 29
    for package, version, filename, sha256 in browser_runtime.RUNTIME_DEBS:
        entry = manifest[browser_runtime.artifact_name(package)]
        assert entry == {"version": version, "source": browser_runtime.source_url(filename), "sha256": sha256}
        assert contract.entry(browser_runtime.artifact_name(package))["sha256"] == sha256
        assert dependency_sources.public_url(entry["source"], kind="artifact")
    # Only Ubuntu's package archive on Launchpad, never the rest of the site.
    assert not dependency_sources.public_url("https://launchpad.net/~someone/+archive/ppa/+files/x.deb",
                                             kind="artifact")
    assert not dependency_sources.public_url("https://launchpad.net/ubuntu/+archive/primary/+files/x.sh",
                                             kind="artifact")


# ---------------------------------------------------------------------------
# Bootstrap: mirror-first fetch, scoped environment, reason codes
# ---------------------------------------------------------------------------

_CHROME = "chrome-headless-shell-linux64/chrome-headless-shell"


@pytest.fixture
def browser_install(monkeypatch, tmp_path):
    """Install from local fakes. ``script`` is the fake browser's shell body."""
    shared = tmp_path / "shared"
    shared.mkdir()
    monkeypatch.setattr(install.config, "shared_prefix", lambda: str(shared))
    monkeypatch.setattr(browser_evidence.config, "shared_prefix", lambda: str(shared))
    monkeypatch.delenv("WORKSHOP_BROWSER", raising=False)
    deb = _write_deb(tmp_path, "runtime.deb", {"./usr/lib/x86_64-linux-gnu/libnss3.so": b"\x7fELF"})
    states: list[tuple] = []
    monkeypatch.setattr(install, "_set", lambda step, status, *a, **k: states.append((step, status, a, k)))
    sources: dict[str, str] = {}

    def run(script: str, *, source: str = "volume", fetch_error: Exception | None = None):
        archive = tmp_path / "browser.zip"
        with zipfile.ZipFile(archive, "w") as bundle:
            info = zipfile.ZipInfo(_CHROME)
            info.external_attr = 0o755 << 16
            bundle.writestr(info, "#!/bin/sh\n" + script)

        def fetch(name):
            if fetch_error is not None and name.startswith("browser_runtime_"):
                raise fetch_error
            sources[name] = source
            install._artifact_sources[name] = source
            return (str(archive) if name == install.BROWSER_ARTIFACT else deb), {"sha256": "a" * 64}

        monkeypatch.setattr(install, "_verified_artifact", fetch)
        install._install_browser()
        stamp = json.loads((shared / "browser.install.json").read_text())
        return states[-1], stamp

    yield run
    for name in sources:
        install._artifact_sources.pop(name, None)


def test_the_browser_runs_with_its_own_libraries_and_no_one_else_gets_them(browser_install, monkeypatch, tmp_path):
    monkeypatch.delenv("LD_LIBRARY_PATH", raising=False)
    monkeypatch.delenv("FONTCONFIG_FILE", raising=False)
    seen = tmp_path / "seen-env"
    (state, stamp) = browser_install(f'echo "$LD_LIBRARY_PATH|$FONTCONFIG_FILE" > {seen}\nexit 0\n')

    step, status, _, kwargs = state
    assert (step, status) == ("browser", "complete") and kwargs["source"] == "volume"
    runtime = Path(stamp["runtime"])
    assert stamp["binary"].endswith(_CHROME) and stamp["error"] == "" and runtime.name.startswith("runtime-")
    assert seen.read_text().strip() == f"{runtime / 'lib'}|{runtime / 'fonts.conf'}"
    assert (runtime / "lib" / "libnss3.so").is_file()

    # The server's own environment is untouched, so nothing it starts inherits them...
    assert "LD_LIBRARY_PATH" not in os.environ and "FONTCONFIG_FILE" not in os.environ
    # ...the inspect process gets them, without the app's credentials...
    monkeypatch.setenv("DATABRICKS_CLIENT_SECRET", "app-secret")
    env = browser_evidence.browser_env(stamp["binary"])
    assert env["LD_LIBRARY_PATH"] == str(runtime / "lib") and "DATABRICKS_CLIENT_SECRET" not in env
    assert "LD_LIBRARY_PATH" not in browser_evidence.browser_env("/usr/bin/some-other-chrome")
    # ...and an attendee shell never does, even if the server had them.
    monkeypatch.setenv("LD_LIBRARY_PATH", str(runtime / "lib"))
    monkeypatch.setenv("FONTCONFIG_FILE", str(runtime / "fonts.conf"))
    shell = User("alice@example.com").shell_env()
    assert "LD_LIBRARY_PATH" not in shell and "FONTCONFIG_FILE" not in shell


def test_inspect_launches_the_browser_with_the_scoped_environment(browser_install, monkeypatch, tmp_path):
    (_, stamp) = browser_install("exit 0\n")
    launched = {}
    real_popen = subprocess.Popen

    class Stop(Exception):
        pass

    def popen(args, **kwargs):
        if args[0] != stamp["binary"]:
            return real_popen(args, **kwargs)
        launched.update(args=args, env=kwargs.get("env"))
        raise Stop

    monkeypatch.setattr(browser_evidence.subprocess, "Popen", popen)
    request = browser_evidence.InspectRequest(url="http://localhost:8123/", scope="local", viewports=["desktop"],
                                              actions=[], project=tmp_path)
    monkeypatch.setenv("DATABRICKS_APP_PORT", "8000")
    with pytest.raises(Stop):
        browser_evidence.inspect(request)
    assert launched["args"][0] == stamp["binary"]
    assert launched["env"]["LD_LIBRARY_PATH"] == str(Path(stamp["runtime"]) / "lib")
    assert launched["env"]["FONTCONFIG_FILE"] == str(Path(stamp["runtime"]) / "fonts.conf")


@pytest.mark.parametrize("script, fetch_error, code", [
    ("echo 'error while loading shared libraries: libnss3.so: cannot open shared object file' >&2\nexit 127\n",
     None, "missing_library"),
    ("echo 'Segmentation fault' >&2\nexit 139\n", None, "launch_failed"),
    ("exit 0\n", ArtifactManifestError("artifact checksum mismatch: browser_runtime_libnss3"), "checksum_mismatch"),
    ("exit 0\n", ArtifactManifestError("artifact is not available from the toolchain mirror: x (miss)"),
     "mirror_unavailable"),
    ("exit 0\n", OSError("Temporary failure in name resolution"), "download_failed"),
])
def test_a_browser_that_cannot_run_says_why_with_a_reason_code(browser_install, caplog, script, fetch_error, code):
    with caplog.at_level(logging.WARNING, logger=install.logger.name):
        (state, stamp) = browser_install(script, fetch_error=fetch_error)
    step, status, args, kwargs = state
    assert (step, status) == ("browser", "error") and kwargs["reason_code"] == code
    assert stamp["binary"] == "" and stamp["runtime"] == "" and stamp["reason_code"] == code and stamp["error"]
    [warning] = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert f"reason_code={code}" in warning.getMessage()
    assert browser_evidence.status() == {"available": False, "reason": stamp["error"], "reason_code": code}


def test_a_hung_browser_is_a_timeout(browser_install, monkeypatch):
    def hang(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], 60)

    monkeypatch.setattr(install.subprocess, "run", hang)
    (state, stamp) = browser_install("exit 0\n")
    assert state[3]["reason_code"] == "timeout" and stamp["reason_code"] == "timeout"


def test_the_source_is_the_one_that_served_the_bytes(browser_install):
    (state, _) = browser_install("exit 0\n", source="network")
    assert state[3]["source"] == "network"
    install._artifact_sources[install.BROWSER_ARTIFACT] = "volume"
    assert install._artifact_source(install.BROWSER_ARTIFACT, "browser_runtime_libnss3") == "mixed"


def test_the_reason_code_reaches_telemetry_as_event_reason_code(monkeypatch):
    emitted = []
    monkeypatch.setattr(telemetry, "emit", lambda kind, who, payload=None: emitted.append((kind, payload)))
    telemetry.install_step_failed("browser", "error", "libnss3.so missing", code="missing_library")
    telemetry.bootstrap_phase("browser", "error", 12, "volume", code="missing_library")
    telemetry.install_step_failed("claude", "error", "npm exploded")
    telemetry.install_step_failed("browser", "error", "x", code="libnss3.so: cannot open shared object")

    failed, phase, plain, free_text = (payload for _, payload in emitted)
    assert observability.safe_event_attributes(failed)["event.reason_code"] == "missing_library"
    assert observability.safe_event_attributes(phase)["event.reason_code"] == "missing_library"
    assert "code" not in plain
    assert free_text["code"] == "unknown"
