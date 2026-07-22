"""Reviewed bootstrap artifact contract and fail-closed checksum verification."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from urllib.parse import urlsplit
from urllib.request import urlopen


NODE_LINUX_X64_SHA256 = (
    "69b09dba5c8dcb05c4e4273a4340db1005abeafe3927efda2bc5b249e80437ec"
)
TMUX_LINUX_X64_SHA256 = (
    "a23e56e9913d610c31f2893a1c9c669a73cb8bb2b8ded1180f6572bb55e52ca5"
)
REQUIRED_ARTIFACTS = frozenset({
    "node_linux_x64",
    "node_linux_arm64",
    "tmux_linux_x64",
    "claude_installer",
    "claude_binary",
    "codex_npm_launcher_package",
    "codex_native_package_linux_x64",
    "databricks_cli_installer",
    "databricks_cli_archive_linux_x64",
    "uv_binary",
    "python_3_12_runtime",
    "omnigent_wheelhouse",
    "omnigent_lock",
    "ai_dev_kit",
})
_SHA256 = re.compile(r"[0-9a-f]{64}")
_COMMIT = re.compile(r"[0-9a-fA-F]{40}")


class ArtifactManifestError(RuntimeError):
    pass


def _checksum(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def directory_checksum(
    path: os.PathLike[str] | str,
    names: set[str] | None = None,
) -> str:
    root = os.fspath(path)
    digest = hashlib.sha256()
    if not os.path.isdir(root):
        return ""
    for current, dirs, files in os.walk(root, followlinks=False):
        dirs.sort()
        files.sort()
        relative_dir = os.path.relpath(current, root)
        top = "" if relative_dir == "." else relative_dir.split(os.sep, 1)[0]
        if names is not None and top and top not in names:
            dirs[:] = []
            continue
        if relative_dir == "." and names is not None:
            dirs[:] = [name for name in dirs if name in names]
            files = [name for name in files if name in names]
        symlink_dirs = [
            name for name in dirs if os.path.islink(os.path.join(current, name))
        ]
        dirs[:] = [
            name for name in dirs if not os.path.islink(os.path.join(current, name))
        ]
        for name in symlink_dirs:
            full_path = os.path.join(current, name)
            relative = os.path.relpath(full_path, root).replace(os.sep, "/")
            _update_path_digest(digest, full_path, relative)
        for name in files:
            full_path = os.path.join(current, name)
            relative = os.path.relpath(full_path, root).replace(os.sep, "/")
            _update_path_digest(digest, full_path, relative)
    return digest.hexdigest()


def _update_path_digest(digest, full_path: str, relative: str) -> None:
    digest.update(relative.encode("utf-8"))
    digest.update(b"\0")
    if os.path.islink(full_path):
        digest.update(b"link:")
        digest.update(os.readlink(full_path).encode("utf-8"))
    else:
        with open(full_path, "rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    digest.update(b"\0")


_directory_checksum = directory_checksum


def _fully_pinned_hashed_lock(path: str) -> bool:
    try:
        lines = open(path, encoding="utf-8").read().splitlines()
    except OSError:
        return False
    blocks: list[str] = []
    current = ""
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        current = f"{current} {line}".strip()
        if current.endswith("\\"):
            current = current[:-1].strip()
            continue
        blocks.append(current)
        current = ""
    if current:
        blocks.append(current)
    return bool(blocks) and all(
        "==" in block
        and "--hash=sha256:" in block
        and not block.startswith(("-", "git+", "http:", "https:"))
        for block in blocks
    )


class ArtifactManifest:
    def __init__(self, path: str, payload: dict):
        self.path = path
        self.payload = payload
        self.artifacts: dict[str, dict] = payload["artifacts"]

    @classmethod
    def from_path(cls, path: str) -> "ArtifactManifest":
        status = load_manifest(path)
        return cls(path, {"artifacts": status["artifacts"]})

    def entry(self, name: str) -> dict:
        try:
            return self.artifacts[name]
        except KeyError as error:
            raise ArtifactManifestError(f"artifact manifest missing {name}") from error

    def verified_local_path(self, name: str, *, staging_dir: str | None = None) -> str:
        entry = self.entry(name)
        source = str(entry["source"])
        parsed = urlsplit(source)
        if parsed.scheme in {"http", "https"}:
            if parsed.scheme != "https":
                raise ArtifactManifestError("artifact network source must use https")
            directory = staging_dir or tempfile.mkdtemp(prefix="workshop-artifact-")
            os.makedirs(directory, exist_ok=True)
            fd, target = tempfile.mkstemp(prefix=f"{name}-", dir=directory)
            try:
                with os.fdopen(fd, "wb") as output, urlopen(source, timeout=60) as response:
                    for chunk in iter(lambda: response.read(1024 * 1024), b""):
                        output.write(chunk)
            except Exception:
                if os.path.exists(target):
                    os.unlink(target)
                raise
        else:
            target = source
        if entry.get("kind") == "directory":
            if not os.path.isdir(target):
                raise ArtifactManifestError(
                    f"artifact directory is unavailable: {name}"
                )
            if directory_checksum(target) != entry["content_sha256"]:
                raise ArtifactManifestError(f"artifact checksum mismatch: {name}")
            return target
        if not os.path.isfile(target):
            raise ArtifactManifestError(f"artifact source is unavailable: {name}")
        actual = _checksum(target)
        if actual != entry["sha256"]:
            raise ArtifactManifestError(f"artifact checksum mismatch: {name}")
        return target


def load_manifest(path: str) -> dict:
    if not path:
        raise ArtifactManifestError("ARTIFACT_MANIFEST_PATH is required")
    try:
        with open(path, encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, ValueError) as error:
        raise ArtifactManifestError("artifact manifest is unreadable") from error
    if (
        not isinstance(payload, dict)
        or payload.get("schema_version") != 1
        or payload.get("reviewed") is not True
        or not isinstance(payload.get("artifacts"), dict)
    ):
        raise ArtifactManifestError("artifact manifest is not reviewed schema version 1")
    artifacts = payload["artifacts"]
    missing = sorted(REQUIRED_ARTIFACTS - set(artifacts))
    if missing:
        raise ArtifactManifestError("artifact manifest is incomplete")
    for name in REQUIRED_ARTIFACTS:
        entry = artifacts[name]
        if name in {"omnigent_wheelhouse", "python_3_12_runtime"}:
            if (
                not isinstance(entry, dict)
                or not isinstance(entry.get("version"), str)
                or not entry["version"]
                or not isinstance(entry.get("source"), str)
                or not entry["source"]
                or entry.get("kind") != "directory"
                or not _SHA256.fullmatch(str(entry.get("content_sha256") or ""))
            ):
                raise ArtifactManifestError(
                    f"artifact manifest entry is invalid: {name}"
                )
            if name == "python_3_12_runtime":
                relative = str(entry.get("executable_relative_path") or "")
                executable = os.path.normpath(
                    os.path.join(str(entry["source"]), relative)
                )
                if (
                    not relative
                    or relative.startswith("../")
                    or not os.path.isfile(executable)
                ):
                    raise ArtifactManifestError(
                        "Python runtime executable is missing"
                    )
            continue
        if (
            not isinstance(entry, dict)
            or not isinstance(entry.get("version"), str)
            or not entry["version"]
            or not isinstance(entry.get("source"), str)
            or not entry["source"]
            or not isinstance(entry.get("sha256"), str)
            or not _SHA256.fullmatch(entry["sha256"])
        ):
            raise ArtifactManifestError(f"artifact manifest entry is invalid: {name}")
    local_only = (
        "codex_npm_launcher_package",
        "codex_native_package_linux_x64",
        "uv_binary",
        "python_3_12_runtime",
        "omnigent_wheelhouse",
        "omnigent_lock",
    )
    if any(urlsplit(str(artifacts[name]["source"])).scheme for name in local_only):
        raise ArtifactManifestError(
            "Codex and Omnigent event artifacts must be staged locally"
        )
    if not _SHA256.fullmatch(
        str(
            artifacts["codex_native_package_linux_x64"].get(
                "executable_sha256"
            )
            or ""
        )
    ):
        raise ArtifactManifestError("Codex native executable checksum is required")
    lock = artifacts["omnigent_lock"]
    if (
        lock.get("lock_sha256") != lock.get("sha256")
        or not _fully_pinned_hashed_lock(str(lock["source"]))
    ):
        raise ArtifactManifestError(
            "Omnigent lock must be fully pinned with SHA-256 hashes"
        )
    if artifacts["node_linux_x64"]["sha256"] != NODE_LINUX_X64_SHA256:
        raise ArtifactManifestError("official Node linux-x64 checksum mismatch")
    if artifacts["tmux_linux_x64"]["sha256"] != TMUX_LINUX_X64_SHA256:
        raise ArtifactManifestError("reviewed tmux checksum mismatch")
    kit = artifacts["ai_dev_kit"]
    if (
        not _COMMIT.fullmatch(str(kit.get("commit") or ""))
        or not _SHA256.fullmatch(str(kit.get("content_sha256") or ""))
    ):
        raise ArtifactManifestError("ai-dev-kit commit/content provenance is invalid")
    return {
        "ok": True,
        "path": os.path.abspath(path),
        "artifacts": artifacts,
    }


def status(path: str) -> dict:
    try:
        loaded = load_manifest(path)
    except ArtifactManifestError as error:
        return {"ok": False, "error": str(error), "path": path or None}
    return {
        "ok": True,
        "error": None,
        "path": loaded["path"],
        "artifact_count": len(loaded["artifacts"]),
    }


__all__ = [
    "ArtifactManifest",
    "ArtifactManifestError",
    "NODE_LINUX_X64_SHA256",
    "REQUIRED_ARTIFACTS",
    "TMUX_LINUX_X64_SHA256",
    "directory_checksum",
    "load_manifest",
    "status",
]
