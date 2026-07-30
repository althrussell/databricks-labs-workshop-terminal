"""Reviewed bootstrap artifact contract and fail-closed checksum verification.

The contract is owned by this repository: ``assets/artifacts/manifest.json``
pins the version and checksum of every artifact the boot path installs, so a
terminal deployed by any means installs the same reviewed software with no
external staging step.

``ARTIFACT_MANIFEST_PATH`` remains supported as a *narrow* override for
air-gapped or mirrored events. It may only redirect ``source`` -- where an
artifact is fetched from. Versions and checksums stay repo-owned, so a stale or
hostile override cannot silently downgrade an attendee's toolchain.
"""

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
    "omnigent_lock",
    "databricks_agent_skills",
})
# Artifacts published only as archives: verified as a file, then extracted at
# boot to reach ``executable_relative_path``.
ARCHIVE_ARTIFACTS = frozenset({"uv_binary", "python_3_12_runtime"})
# Cloned rather than fetched, so provenance is a commit plus a content digest.
_REPOSITORY_ARTIFACTS = frozenset({"databricks_agent_skills"})
DEFAULT_MANIFEST_PATH = os.path.normpath(
    os.path.join(
        os.path.dirname(__file__), "..", "..", "assets", "artifacts", "manifest.json"
    )
)
_SHA256 = re.compile(r"[0-9a-f]{64}")
_COMMIT = re.compile(r"[0-9a-fA-F]{40}")
# Suffixes preserved when staging a downloaded artifact. Several consumers infer
# an artifact's type from its name rather than its content: ``npm install`` reads
# an extensionless path as a *directory* (ENOTDIR on its package.json), and
# ``shutil.unpack_archive`` rejects a name it cannot map to a format. Staging to
# a bare mkstemp name therefore broke Codex and Omnigent while leaving node and
# claude working, because those two are unpacked with an explicit `tar -xzf`.
# Longest-first so ``.tar.gz`` wins over ``.gz``.
_STAGED_SUFFIXES = (
    ".tar.gz",
    ".tar.xz",
    ".tar.bz2",
    ".tar.zst",
    ".tgz",
    ".txz",
    ".tbz2",
    ".zip",
    ".whl",
    ".gz",
    ".xz",
    ".zst",
    ".sh",
)


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
        self.source: str = payload.get("source", "default")

    @classmethod
    def from_path(cls, path: str = "") -> "ArtifactManifest":
        loaded = load_manifest(path)
        return cls(
            loaded["path"],
            {"artifacts": loaded["artifacts"], "source": loaded["source"]},
        )

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
            fd, target = tempfile.mkstemp(
                prefix=f"{name}-",
                suffix=_staged_suffix(parsed.path),
                dir=directory,
            )
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


def _staged_suffix(url_path: str) -> str:
    """Archive suffix to stage ``url_path`` under, or "" when unrecognised.

    Allowlisted rather than echoed from the URL so a manifest override cannot
    choose the staged filename's extension.
    """
    basename = url_path.rsplit("/", 1)[-1].lower()
    for suffix in _STAGED_SUFFIXES:
        if basename.endswith(suffix):
            return suffix
    return ""


def _read_document(path: str) -> dict:
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
    return payload


def _resolved_artifacts(payload: dict, manifest_path: str) -> dict[str, dict]:
    """Copy the artifacts, resolving relative sources against the manifest.

    Repo-relative sources let the committed manifest reference in-repo files
    (the Omnigent lock) without knowing the deployed install prefix.
    """
    base = os.path.dirname(os.path.abspath(manifest_path))
    resolved: dict[str, dict] = {}
    for name, entry in payload["artifacts"].items():
        if not isinstance(entry, dict):
            raise ArtifactManifestError(f"artifact manifest entry is invalid: {name}")
        copied = dict(entry)
        source = copied.get("source")
        if isinstance(source, str) and source and not urlsplit(source).scheme:
            if not os.path.isabs(source):
                copied["source"] = os.path.normpath(os.path.join(base, source))
        resolved[name] = copied
    return resolved


def _apply_source_overrides(
    artifacts: dict[str, dict], overrides: dict[str, dict]
) -> None:
    """Overlay an override manifest, which may redirect ``source`` and nothing else."""
    for name, override in overrides.items():
        if name not in artifacts:
            raise ArtifactManifestError(
                f"artifact manifest override adds unknown artifact: {name}"
            )
        entry = artifacts[name]
        for field, value in override.items():
            if field == "source":
                continue
            if entry.get(field) != value:
                raise ArtifactManifestError(
                    f"artifact manifest override may only change source: {name}.{field}"
                )
        source = override.get("source")
        if source is not None:
            if not isinstance(source, str) or not source:
                raise ArtifactManifestError(
                    f"artifact manifest override source is invalid: {name}"
                )
            entry["source"] = source


def _validate_entry(name: str, entry: dict) -> None:
    if (
        not isinstance(entry.get("version"), str)
        or not entry["version"]
        or not isinstance(entry.get("source"), str)
        or not entry["source"]
    ):
        raise ArtifactManifestError(f"artifact manifest entry is invalid: {name}")
    # The skills repository is a git clone rather than a fetched file; its
    # integrity is the commit plus content digest checked in load_manifest.
    if name not in _REPOSITORY_ARTIFACTS and not _SHA256.fullmatch(
        str(entry.get("sha256") or "")
    ):
        raise ArtifactManifestError(f"artifact manifest entry is invalid: {name}")
    scheme = urlsplit(entry["source"]).scheme
    if scheme and scheme != "https":
        raise ArtifactManifestError(
            f"artifact network source must use https: {name}"
        )
    if name not in ARCHIVE_ARTIFACTS:
        return
    if entry.get("kind") != "archive":
        raise ArtifactManifestError(f"artifact must be an archive: {name}")
    relative = str(entry.get("executable_relative_path") or "")
    if (
        not relative
        or os.path.isabs(relative)
        or relative.startswith("../")
        or "/../" in relative
    ):
        raise ArtifactManifestError(
            f"artifact executable path is invalid: {name}"
        )


def load_manifest(path: str = "") -> dict:
    """Resolve the repo-owned contract, optionally source-overridden by ``path``."""
    default = _read_document(DEFAULT_MANIFEST_PATH)
    artifacts = _resolved_artifacts(default, DEFAULT_MANIFEST_PATH)
    override_path = None
    if path:
        override_path = os.path.abspath(path)
        _apply_source_overrides(
            artifacts,
            _resolved_artifacts(_read_document(path), path),
        )
    missing = sorted(REQUIRED_ARTIFACTS - set(artifacts))
    if missing:
        raise ArtifactManifestError("artifact manifest is incomplete")
    for name in REQUIRED_ARTIFACTS:
        _validate_entry(name, artifacts[name])
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
    skills = artifacts["databricks_agent_skills"]
    if (
        not _COMMIT.fullmatch(str(skills.get("commit") or ""))
        or not _SHA256.fullmatch(str(skills.get("content_sha256") or ""))
    ):
        raise ArtifactManifestError("skills commit/content provenance is invalid")
    return {
        "ok": True,
        "path": override_path or DEFAULT_MANIFEST_PATH,
        "source": "override" if override_path else "default",
        "default_path": DEFAULT_MANIFEST_PATH,
        "override_path": override_path,
        "artifacts": artifacts,
    }


def status(path: str = "") -> dict:
    try:
        loaded = load_manifest(path)
    except ArtifactManifestError as error:
        return {
            "ok": False,
            "error": str(error),
            "path": os.path.abspath(path) if path else DEFAULT_MANIFEST_PATH,
            "source": "override" if path else "default",
            "override_path": os.path.abspath(path) if path else None,
        }
    return {
        "ok": True,
        "error": None,
        "path": loaded["path"],
        "source": loaded["source"],
        "override_path": loaded["override_path"],
        "artifact_count": len(loaded["artifacts"]),
    }


__all__ = [
    "ARCHIVE_ARTIFACTS",
    "ArtifactManifest",
    "ArtifactManifestError",
    "DEFAULT_MANIFEST_PATH",
    "NODE_LINUX_X64_SHA256",
    "REQUIRED_ARTIFACTS",
    "TMUX_LINUX_X64_SHA256",
    "directory_checksum",
    "load_manifest",
    "status",
]
