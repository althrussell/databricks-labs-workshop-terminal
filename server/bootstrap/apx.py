"""Official APX wheel and shared Bun/uv, compatible with the Apps runtime.

The standalone APX Linux release needs a newer glibc than Ubuntu 22.04.
Install its official manylinux wheel into an isolated environment instead.
No package-name lookup, source build or unpinned dependency download is needed.
"""
from __future__ import annotations

import os
import hashlib
from pathlib import Path
import shlex
import shutil
import subprocess
import tempfile
import zipfile


VERSION = "0.3.8"
SOURCE_COMMIT = "50073e561a689ae5292f82e69ae0505417d75d3e"
BUN_VERSION = "1.3.8"  # The version bundled by this APX release.
ARTIFACTS = ("apx_wheel_linux_x64", "bun_linux_x64", "uv_binary", "python_3_12_runtime")


def _proof(prefix: Path, entries: dict) -> dict:
    from . import install as bootstrap
    return {
        "artifacts": {name: entry["sha256"] for name, entry in entries.items()},
        "launcher": bootstrap._file_checksum(str(prefix / "bin/apx")),
        "venv": bootstrap._directory_checksum(str(prefix / "apx-venv")),
        "bun": bootstrap._file_checksum(str(prefix / "bin/bun")),
        "uv": bootstrap._file_checksum(str(prefix / "bin/uv")),
    }


def prewarm_status() -> dict:
    from . import install as bootstrap
    prefix = Path(bootstrap.config.shared_prefix())
    try:
        entries = {name: bootstrap._artifact_contract().entry(name) for name in ARTIFACTS}
        reusable = bootstrap._read_json(str(prefix / "apx.install.json")) == _proof(prefix, entries)
    except (OSError, RuntimeError):
        reusable = False
    return {
        name: {"expected": version, "actual": actual,
               "actual_checksum": bootstrap._file_checksum(str(prefix / "bin" / name)),
               "source": "persistent", "reusable": reusable and actual == version}
        for name, version in (("apx", VERSION), ("bun", BUN_VERSION), ("uv", bootstrap.UV_VERSION))
        for actual in (bootstrap._read_cli_version(str(prefix / "bin" / name)),)
    }


def install_cli() -> None:
    from . import install as bootstrap
    prefix = Path(bootstrap.config.shared_prefix())
    (prefix / "bin").mkdir(parents=True, exist_ok=True)
    for name, version in (("apx", VERSION), ("bun", BUN_VERSION), ("uv", bootstrap.UV_VERSION)):
        bootstrap._set(name, "running", expected_version=version)
    entries = {name: bootstrap._artifact_contract().entry(name) for name in ARTIFACTS}
    for name, version in (("apx_wheel_linux_x64", VERSION), ("bun_linux_x64", BUN_VERSION),
                          ("uv_binary", bootstrap.UV_VERSION)):
        if entries[name]["version"] != version:
            raise RuntimeError(f"{name}: configured and reviewed versions differ")
    status = prewarm_status()
    if not all(item["reusable"] for item in status.values()):
        uv, _ = bootstrap._extracted_artifact_executable("uv_binary")
        python, _ = bootstrap._extracted_artifact_executable("python_3_12_runtime")
        wheel, _ = bootstrap._verified_artifact("apx_wheel_linux_x64")
        bun_archive, entry = bootstrap._verified_artifact("bun_linux_x64")
        with zipfile.ZipFile(bun_archive) as archive:
            payload = archive.read("bun-linux-x64/bun")
        if hashlib.sha256(payload).hexdigest() != entry["executable_sha256"]:
            raise RuntimeError("Bun executable differs from reviewed artifact")
        temporary = prefix / "bin/.bun-new"
        temporary.write_bytes(payload)
        temporary.chmod(0o755)
        os.replace(temporary, prefix / "bin/bun")
        uv_alias = prefix / "bin/uv"
        if os.path.lexists(uv_alias):
            uv_alias.unlink()
        uv_alias.symlink_to(uv)
        env = bootstrap._install_env()
        env["UV_PYTHON_DOWNLOADS"] = "never"
        venv = prefix / "apx-venv"
        # uv validates wheel filenames; the artifact cache uses digest names.
        with tempfile.TemporaryDirectory(prefix=".apx-install-", dir=prefix) as temporary:
            named_wheel = Path(temporary) / f"apx-{VERSION}-py3-none-manylinux_2_28_x86_64.whl"
            shutil.copyfile(wheel, named_wheel)
            for command in (
                [uv, "venv", "--clear", "--python", python, str(venv)],
                [uv, "pip", "install", "--python", str(venv / "bin/python"),
                 "--no-index", "--no-deps", str(named_wheel)],
            ):
                result = subprocess.run(command, env=env, capture_output=True, text=True, timeout=180)
                if result.returncode:
                    raise RuntimeError("APX wheel install failed: " + result.stderr[-500:])
        native = venv / "bin/apx"
        if bootstrap._file_checksum(str(native)) != entries["apx_wheel_linux_x64"]["executable_sha256"]:
            raise RuntimeError("APX executable differs from reviewed wheel")
        launcher = prefix / "bin/apx"
        temporary = prefix / "bin/.apx-new"
        temporary.write_text(
            "#!/bin/sh\nexport APX_BUN_PATH=" + shlex.quote(str(prefix / "bin/bun"))
            + "\nexport APX_UV_PATH=" + shlex.quote(str(prefix / "bin/uv"))
            + "\nexec " + shlex.quote(str(native)) + ' "$@"\n'
        )
        temporary.chmod(0o755)
        os.replace(temporary, launcher)
        source = "reviewed-wheel"
    else:
        source = "prewarmed"
    for name, version, artifact in (("apx", VERSION, "apx_wheel_linux_x64"),
                                   ("bun", BUN_VERSION, "bun_linux_x64"),
                                   ("uv", bootstrap.UV_VERSION, "uv_binary")):
        actual = bootstrap._read_cli_version(str(prefix / "bin" / name))
        if actual != version:
            raise RuntimeError(f"{name} version check failed: expected {version}, got {actual}")
        bootstrap._set(name, "complete", expected_version=version, actual_version=actual,
                       source=source, expected_checksum=entries[artifact]["sha256"],
                       actual_checksum=bootstrap._file_checksum(str(prefix / "bin" / name)))
    bootstrap._write_json_atomic(str(prefix / "apx.install.json"), _proof(prefix, entries))
