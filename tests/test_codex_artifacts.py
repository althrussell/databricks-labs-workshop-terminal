import io
import json
import tarfile

import pytest


VERSION = "0.148.0"
NATIVE_VERSION = f"{VERSION}-linux-x64"
NATIVE_MEMBER = (
    "package/vendor/x86_64-unknown-linux-musl/bin/codex"
)


def make_codex_tarballs(tmp_path):
    launcher = tmp_path / "codex-npm-0.148.0.tgz"
    native = tmp_path / "codex-npm-linux-x64-0.148.0.tgz"
    launcher_metadata = {
        "name": "@openai/codex",
        "version": VERSION,
        "bin": {"codex": "bin/codex.js"},
        "type": "module",
        "optionalDependencies": {
            "@openai/codex-linux-x64": (
                f"npm:@openai/codex@{VERSION}-linux-x64"
            ),
            "@openai/codex-linux-arm64": (
                f"npm:@openai/codex@{VERSION}-linux-arm64"
            ),
            "@openai/codex-darwin-x64": (
                f"npm:@openai/codex@{VERSION}-darwin-x64"
            ),
            "@openai/codex-darwin-arm64": (
                f"npm:@openai/codex@{VERSION}-darwin-arm64"
            ),
            "@openai/codex-win32-x64": (
                f"npm:@openai/codex@{VERSION}-win32-x64"
            ),
            "@openai/codex-win32-arm64": (
                f"npm:@openai/codex@{VERSION}-win32-arm64"
            ),
        },
    }
    native_metadata = {
        "name": "@openai/codex",
        "version": NATIVE_VERSION,
        "os": ["linux"],
        "cpu": ["x64"],
        "files": ["vendor"],
    }
    _write_tar(launcher, {
        "package/package.json": json.dumps(launcher_metadata).encode(),
        "package/bin/codex.js": b"#!/usr/bin/env node\n",
    })
    _write_tar(native, {
        "package/package.json": json.dumps(native_metadata).encode(),
        NATIVE_MEMBER: b"reviewed-native",
        (
            "package/vendor/x86_64-unknown-linux-musl/"
            "codex-resources/bwrap"
        ): b"reviewed-sidecar",
    })
    return launcher, native


def _write_tar(path, files):
    with tarfile.open(path, "w:gz") as archive:
        for name, payload in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            info.mode = 0o755 if name.endswith((".js", "/codex")) else 0o644
            archive.addfile(info, io.BytesIO(payload))


def test_validates_exact_01446_alias_and_native_layout(tmp_path):
    from server.bootstrap.codex_artifacts import validate_codex_tarballs

    launcher, native = make_codex_tarballs(tmp_path)

    proof = validate_codex_tarballs(str(launcher), str(native), VERSION)

    assert proof["alias"] == "@openai/codex-linux-x64"
    assert proof["alias_target"] == f"npm:@openai/codex@{VERSION}-linux-x64"
    assert proof["native_member"] == NATIVE_MEMBER


def test_extracts_same_named_native_package_under_launcher_alias(tmp_path):
    from server.bootstrap.codex_artifacts import install_native_alias

    launcher, native = make_codex_tarballs(tmp_path)
    target = tmp_path / "node_modules/@openai/codex-linux-x64"

    installed = install_native_alias(
        str(launcher), str(native), str(target), VERSION
    )

    assert installed == str(
        target / "vendor/x86_64-unknown-linux-musl/bin/codex"
    )
    metadata = json.loads((target / "package.json").read_text())
    assert metadata["name"] == "@openai/codex"
    assert metadata["version"] == NATIVE_VERSION


def test_rejects_alias_or_layout_drift(tmp_path):
    from server.bootstrap.codex_artifacts import (
        CodexArtifactError,
        validate_codex_tarballs,
    )

    launcher, native = make_codex_tarballs(tmp_path)
    bad = tmp_path / "bad-native.tgz"
    _write_tar(bad, {
        "package/package.json": json.dumps({
            "name": "@openai/codex-linux-x64",
            "version": NATIVE_VERSION,
        }).encode(),
        "package/vendor/wrong/codex": b"wrong",
    })

    with pytest.raises(CodexArtifactError):
        validate_codex_tarballs(str(launcher), str(bad), VERSION)


def test_production_tarball_validation_script_checks_all_three_hashes(
    monkeypatch, tmp_path, capsys
):
    from scripts import validate_codex_packages
    from server.bootstrap.codex_artifacts import validate_codex_tarballs

    launcher, native = make_codex_tarballs(tmp_path)
    proof = validate_codex_tarballs(str(launcher), str(native), VERSION)
    monkeypatch.setattr("sys.argv", [
        "validate_codex_packages.py",
        "--launcher", str(launcher),
        "--native", str(native),
        "--version", VERSION,
        "--launcher-sha256", proof["launcher_package_sha256"],
        "--native-package-sha256", proof["native_package_sha256"],
        "--native-executable-sha256", proof["native_executable_sha256"],
    ])

    assert validate_codex_packages.main() == 0
    assert json.loads(capsys.readouterr().out)["status"] == "verified"

    monkeypatch.setattr(
        "sys.argv",
        [*__import__("sys").argv[:-1], "0" * 64],
    )
    assert validate_codex_packages.main() == 1
