"""Default stack installation, real project memory and framework preservation."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import zipfile

import pytest

from server.bootstrap import apx, install
from server.bootstrap.artifacts import directory_checksum, load_manifest

ROOT = Path(__file__).resolve().parents[1]


def test_official_skill_is_reviewed_and_legacy_route_is_retired():
    entry = load_manifest("")["artifacts"]["apx_wheel_linux_x64"]
    assert entry["skill_commit"] == apx.SOURCE_COMMIT
    assert directory_checksum(ROOT / "assets/skills/apx") == entry["skill_content_sha256"]
    assert not (ROOT / "assets/skills/databricks-app-apx").exists()


def test_wheel_install_is_offline_and_warm_restart_detects_tampering(tmp_path, monkeypatch):
    prefix = tmp_path / "shared"
    monkeypatch.setattr(install.config, "shared_prefix", lambda: str(prefix))
    wheel, bun = tmp_path / "digest.whl", tmp_path / "bun.zip"
    wheel.write_bytes(b"checksum verified wheel")
    with zipfile.ZipFile(bun, "w") as archive:
        archive.writestr("bun-linux-x64/bun", b"reviewed bun")
    entries = {
        "apx_wheel_linux_x64": {"version": apx.VERSION, "sha256": "a" * 64,
                               "executable_sha256": hashlib.sha256(b"reviewed apx").hexdigest()},
        "bun_linux_x64": {"version": apx.BUN_VERSION, "sha256": "b" * 64,
                          "executable_sha256": hashlib.sha256(b"reviewed bun").hexdigest()},
        "uv_binary": {"version": install.UV_VERSION, "sha256": "c" * 64},
        "python_3_12_runtime": {"sha256": "d" * 64},
    }
    monkeypatch.setattr(install, "_artifact_contract", lambda: type("Contract", (), {"entry": lambda self, name: entries[name]})())
    uv, python = tmp_path / "uv", tmp_path / "python"
    uv.write_bytes(b"reviewed uv")
    python.write_bytes(b"reviewed python")
    monkeypatch.setattr(install, "_extracted_artifact_executable", lambda name: (str(uv if name == "uv_binary" else python), entries[name]))
    monkeypatch.setattr(install, "_verified_artifact", lambda name: (str(wheel if name == "apx_wheel_linux_x64" else bun), entries[name]))
    versions = {"apx": apx.VERSION, "bun": apx.BUN_VERSION, "uv": install.UV_VERSION}
    monkeypatch.setattr(install, "_read_cli_version", lambda path: versions[Path(path).name] if Path(path).is_file() else None)
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        if "install" in argv:
            assert argv[-3:-1] == ["--no-index", "--no-deps"]
            assert Path(argv[-1]).name == f"apx-{apx.VERSION}-py3-none-manylinux_2_28_x86_64.whl"
            assert kwargs["env"]["UV_PYTHON_DOWNLOADS"] == "never"
            native = prefix / "apx-venv/bin/apx"
            native.parent.mkdir(parents=True)
            native.write_bytes(b"reviewed apx")
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(apx.subprocess, "run", run)
    apx.install_cli()
    assert len(calls) == 2
    assert all(item["reusable"] for item in apx.prewarm_status().values())
    apx.install_cli()
    assert len(calls) == 2, "warm deployment must reuse the installed toolchain"
    (prefix / "bin/bun").write_bytes(b"corrupted bun")
    assert not any(item["reusable"] for item in apx.prewarm_status().values())


@pytest.mark.parametrize("failure", [False, True])
def test_helper_scaffolds_without_nesting_or_overwriting_notes(tmp_path, failure):
    home, binaries = tmp_path / "home", tmp_path / "bin"
    binaries.mkdir()
    template = home / ".config/workshop/project-memory.md"
    template.parent.mkdir(parents=True)
    template.write_text("APX default; use impeccable\n")
    apx_cli = binaries / "apx"
    apx_cli.write_text("""#!/usr/bin/env python3
import pathlib,sys
assert sys.argv[1] == 'init'
assert sys.argv[sys.argv.index('--profile')+1] == 'DEFAULT'
assert sys.argv[sys.argv.index('--addons')+1] == 'ui'
root=pathlib.Path(sys.argv[2]);root.mkdir(parents=True)
(root/'pyproject.toml').write_text('[tool.apx.metadata]\\n')
(root/'CLAUDE.md').write_text('Scaffold notes to preserve\\n')
print('scaffold progress on stdout')
sys.exit(""" + ("1" if failure else "0") + ")\n")
    apx_cli.chmod(0o755)
    for name in ("uv", "bun"):
        (binaries / name).symlink_to(apx_cli)
    env = os.environ | {"HOME": str(home), "PATH": str(binaries) + ":" + os.environ["PATH"],
                        "DATABRICKS_CONFIG_PROFILE": "DEFAULT", "GIT_CONFIG_GLOBAL": "/dev/null",
                        "GIT_AUTHOR_NAME": "Fixture", "GIT_AUTHOR_EMAIL": "test@example.com",
                        "GIT_COMMITTER_NAME": "Fixture", "GIT_COMMITTER_EMAIL": "test@example.com"}
    helper = str(ROOT / "assets/bin/workshop-init-project")
    result = subprocess.run(["bash", helper, "repair-desk", "--apx", "--json"], env=env, text=True, capture_output=True)
    assert result.returncode == int(failure)
    status = json.loads(result.stdout)
    assert status["ready"] is not failure
    assert status["scaffold"] == ("failed" if failure else "complete")
    project = home / "projects/repair-desk"
    assert not (project / "repair-desk").exists()
    assert "Scaffold notes to preserve" in (project / "CLAUDE.md").read_text()
    assert "scaffold progress" in result.stderr
    if not failure:
        rerun = subprocess.run(["bash", helper, "repair-desk", "--apx"], env=env, text=True, capture_output=True)
        assert rerun.returncode == 0
        assert rerun.stdout.strip() == str(project)
    plain = home / "projects/existing-appkit"
    plain.mkdir()
    (plain / "package.json").write_text('{"name":"existing"}')
    rejected = subprocess.run(["bash", helper, "existing-appkit", "--apx"], env=env, capture_output=True)
    assert rejected.returncode == 1
    assert (plain / "package.json").read_text() == '{"name":"existing"}'
