"""Opt-in, isolated Agent Bricks CLI. The attendee PEX remains unchanged."""

from __future__ import annotations

import os
import shlex
import subprocess

from . import install as bootstrap


LOCK_ARTIFACT = "agentbricks_lock"


def _proof(prefix: str, entries: dict) -> dict:
    return {
        "uv_artifact_sha256": entries["uv_binary"]["sha256"],
        "python_artifact_sha256": entries["python_3_12_runtime"]["sha256"],
        "lock_sha256": entries[LOCK_ARTIFACT]["sha256"],
        "launcher_sha256": bootstrap._file_checksum(os.path.join(prefix, "bin", "agentbricks")),
        "uv_sha256": bootstrap._file_checksum(os.path.join(prefix, "bin", "uv")),
        "venv_sha256": bootstrap._directory_checksum(os.path.join(prefix, "agentbricks-venv")),
    }


def _reusable(prefix: str, entries: dict) -> bool:
    stamp = bootstrap._read_json(os.path.join(prefix, "agentbricks.install.json"))
    proof = _proof(prefix, entries)
    return bool(
        all(proof.values()) and stamp == proof
        and bootstrap._file_checksum(entries[LOCK_ARTIFACT]["source"]) == entries[LOCK_ARTIFACT]["sha256"]
        and entries[LOCK_ARTIFACT]["version"] == bootstrap.AGENTBRICKS_VERSION
        and entries["uv_binary"]["version"] == bootstrap.UV_VERSION
    )


def prewarm_status() -> dict:
    prefix = bootstrap.config.shared_prefix()
    try:
        contract = bootstrap._artifact_contract()
        entries = {name: contract.entry(name) for name in (
            "uv_binary", "python_3_12_runtime", LOCK_ARTIFACT
        )}
        reusable = _reusable(prefix, entries)
    except (OSError, RuntimeError):
        reusable = False
    report = {
        name: {
            "expected": version,
            "actual": bootstrap._read_cli_version(os.path.join(prefix, "bin", name)),
            "actual_checksum": bootstrap._file_checksum(os.path.join(prefix, "bin", name)) or None,
            "source": "persistent",
            "reusable": reusable,
        }
        for name, version in (
            ("agentbricks", bootstrap.AGENTBRICKS_VERSION), ("uv", bootstrap.UV_VERSION)
        )
    }
    for entry in report.values():
        entry["reusable"] = bool(entry["reusable"] and entry["actual"] == entry["expected"])
    return report


def _publish_alias(source: str, target: str) -> None:
    if os.path.lexists(target):
        os.unlink(target)
    os.symlink(source, target)


def install_cli() -> None:
    """Hash-checked wheel-only install using the reviewed Python and uv builds."""
    prefix = bootstrap.config.shared_prefix()
    bin_dir = os.path.join(prefix, "bin")
    os.makedirs(bin_dir, exist_ok=True)
    contract = bootstrap._artifact_contract()
    entries = {name: contract.entry(name) for name in (
        "uv_binary", "python_3_12_runtime", LOCK_ARTIFACT
    )}
    if entries[LOCK_ARTIFACT]["version"] != bootstrap.AGENTBRICKS_VERSION:
        raise RuntimeError("Agent Bricks lock differs from configured CLI version")
    if entries["uv_binary"]["version"] != bootstrap.UV_VERSION:
        raise RuntimeError("Agent Bricks uv differs from reviewed version")
    bootstrap._set("uv", "running", expected_version=bootstrap.UV_VERSION)
    try:
        uv_path, _ = bootstrap._extracted_artifact_executable("uv_binary")
        python_path, _ = bootstrap._extracted_artifact_executable("python_3_12_runtime")
        lock_path, _ = bootstrap._verified_artifact(LOCK_ARTIFACT)
        with open(lock_path, encoding="utf-8") as handle:
            requirements = handle.read()
        if f"databricks-agentbricks=={bootstrap.AGENTBRICKS_VERSION} \\" not in requirements:
            raise RuntimeError("Agent Bricks lock does not pin the configured package")
        uv_target = os.path.join(bin_dir, "uv")
        _publish_alias(uv_path, uv_target)
        if bootstrap._read_cli_version(uv_target) != bootstrap.UV_VERSION:
            raise RuntimeError("Agent Bricks uv version check failed")
        bootstrap._set(
            "uv", "complete", expected_version=bootstrap.UV_VERSION,
            actual_version=bootstrap.UV_VERSION,
            source=bootstrap._install_source("staged", "uv_binary"),
            expected_checksum=entries["uv_binary"]["sha256"],
            actual_checksum=bootstrap._file_checksum(uv_target),
        )
        target = os.path.join(bin_dir, "agentbricks")
        actual = bootstrap._read_cli_version(target)
        if actual == bootstrap.AGENTBRICKS_VERSION and _reusable(prefix, entries):
            bootstrap._set(
                "agentbricks", "complete", expected_version=bootstrap.AGENTBRICKS_VERSION,
                actual_version=actual, source="prewarmed",
                expected_checksum=entries[LOCK_ARTIFACT]["sha256"],
                actual_checksum=bootstrap._file_checksum(target),
            )
            return
        env = bootstrap._install_env()
        for name in list(env):
            if name.startswith("DATABRICKS_"):
                env.pop(name)
        # Apps install from public PyPI. Operator laptop validation injects its
        # own proxy in a disposable environment; no proxy URL ships in a lock.
        for name in ("UV_INDEX_URL", "UV_DEFAULT_INDEX", "UV_INDEX_PYPI_URL", "PIP_INDEX_URL"):
            env[name] = "https://pypi.org/simple"
        for name in ("UV_EXTRA_INDEX_URL", "UV_INDEX", "PIP_EXTRA_INDEX_URL", "UV_FIND_LINKS", "PIP_FIND_LINKS"):
            env.pop(name, None)
        env["UV_PYTHON_DOWNLOADS"] = "never"
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        venv = os.path.join(prefix, "agentbricks-venv")
        commands = (
            [uv_path, "venv", "--clear", "--python", python_path, venv],
            [uv_path, "pip", "sync", "--python", os.path.join(venv, "bin", "python"),
             "--require-hashes", "--only-binary", ":all:", lock_path],
        )
        for command in commands:
            result = subprocess.run(command, env=env, capture_output=True, text=True, timeout=900)
            if result.returncode:
                raise RuntimeError("Agent Bricks isolated install failed: " + (result.stderr or result.stdout)[-500:])
        # Avoid mutable bytecode in a shared venv whose whole tree is verified.
        script = os.path.join(venv, "bin", "agentbricks")
        if not os.path.isfile(script):
            raise RuntimeError("Agent Bricks install produced no executable")
        if os.path.lexists(target):
            os.unlink(target)
        with open(target, "w", encoding="utf-8") as handle:
            handle.write("#!/bin/sh\nPYTHONDONTWRITEBYTECODE=1 exec " + shlex.quote(script) + ' "$@"\n')
        os.chmod(target, 0o755)
        actual = bootstrap._read_cli_version(target)
        if actual != bootstrap.AGENTBRICKS_VERSION:
            raise RuntimeError("Agent Bricks CLI version check failed")
        bootstrap._write_json_atomic(os.path.join(prefix, "agentbricks.install.json"), _proof(prefix, entries))
        bootstrap._set(
            "agentbricks", "complete", expected_version=bootstrap.AGENTBRICKS_VERSION,
            actual_version=actual, source="pypi-hashed-lock",
            expected_checksum=entries[LOCK_ARTIFACT]["sha256"],
            actual_checksum=bootstrap._file_checksum(target),
        )
    except Exception:
        # A prerequisite failure must not leave the uv setup spinner running.
        if bootstrap.status()["steps"].get("uv", {}).get("status") != "complete":
            bootstrap._set("uv", "error", "Agent Bricks prerequisite installation failed")
        raise
