#!/usr/bin/env python3
"""Build the immutable Workshop Terminal CPython 3.11 PEX release.

The dependency solve belongs to ``uv.lock``. PEX is only an offline packager:
it resolves the exact project requirements from the uv-created virtualenv and
is forbidden from consulting an index or building an sdist.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import stat
import subprocess
import sys
import tempfile
import tomllib
from typing import Iterable


ROOT = Path(__file__).resolve().parents[1]
RUNTIME_ROOTS = ("server", "static", "content", "assets")
REQUIRED_RUNTIME_FILES = frozenset(
    {
        "server/otel_bootstrap.py",
        "static/index.html",
        "content/agents.json",
        "content/default_pack.json",
        "assets/artifacts/manifest.json",
        "assets/bin/workshop-app-deploy",
        "assets/instructions/CLAUDE.md",
        "assets/skills/SKILLS_SOURCE.md",
    }
)
ARTIFACT_NAME = "workshop-terminal.pex"
RELEASE_MANIFEST_NAME = "release-manifest.json"
CONTENT_MANIFEST_NAME = "runtime-content-manifest.json"
ENTRY_POINT = "server.otel_bootstrap:main"
FORMAT_VERSION = 1
MINIMUM_CONTROL_TOWER_CONTRACT_VERSION = 1
MAX_ARTIFACT_BYTES = 200 * 1024 * 1024


class ReleaseBuildError(RuntimeError):
    """The checkout cannot produce a release that satisfies the contract."""


def _run(*args: str, cwd: Path = ROOT) -> str:
    completed = subprocess.run(
        args,
        cwd=cwd,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    return completed.stdout.strip()


def git_sha(root: Path = ROOT) -> str:
    value = _run("git", "rev-parse", "HEAD", cwd=root).lower()
    if len(value) != 40 or any(char not in "0123456789abcdef" for char in value):
        raise ReleaseBuildError(f"Git reported a non-immutable SHA: {value!r}")
    return value


def checkout_is_clean(root: Path = ROOT) -> bool:
    return not _run("git", "status", "--porcelain", "--untracked-files=all", cwd=root)


def tracked_runtime_paths(root: Path = ROOT) -> list[Path]:
    output = subprocess.run(
        ["git", "ls-files", "-z", "--", *RUNTIME_ROOTS],
        cwd=root,
        check=True,
        stdout=subprocess.PIPE,
    ).stdout
    paths = [Path(value.decode("utf-8")) for value in output.split(b"\0") if value]
    missing = [str(path) for path in paths if not (root / path).is_file()]
    if missing:
        raise ReleaseBuildError(f"Tracked runtime files are missing: {missing}")
    names = {path.as_posix() for path in paths}
    if absent := sorted(REQUIRED_RUNTIME_FILES - names):
        raise ReleaseBuildError(f"Required runtime files are absent: {absent}")
    return sorted(paths, key=lambda path: path.as_posix())


def _update_logical_digest(
    digest: "hashlib._Hash", relative: str, mode: int, data: bytes
) -> None:
    digest.update(relative.encode("utf-8"))
    digest.update(b"\0")
    digest.update(f"{mode:o}".encode("ascii"))
    digest.update(b"\0")
    digest.update(data)
    digest.update(b"\0")


def _normalized_mode(path: Path) -> int:
    """Use only the executable bit Git preserves, independent of checkout umask."""
    if path.is_symlink():
        raise ReleaseBuildError(f"Runtime source cannot be a symlink: {path}")
    return 0o755 if path.stat().st_mode & stat.S_IXUSR else 0o644


def logical_content_manifest(
    paths: Iterable[Path], *, root: Path = ROOT, source_sha: str
) -> dict:
    digest = hashlib.sha256()
    files: list[dict] = []
    for relative in paths:
        source = root / relative
        data = source.read_bytes()
        mode = _normalized_mode(source)
        name = relative.as_posix()
        _update_logical_digest(digest, name, mode, data)
        files.append(
            {
                "path": name,
                "mode": f"{mode:o}",
                "size_bytes": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
            }
        )
    return {
        "format_version": FORMAT_VERSION,
        "wt_git_sha": source_sha,
        "logical_contents_sha256": digest.hexdigest(),
        "file_count": len(files),
        "files": files,
    }


def stage_runtime(
    destination: Path, paths: Iterable[Path], content_manifest: dict, *, root: Path = ROOT
) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    for relative in paths:
        source = root / relative
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        target.chmod(_normalized_mode(source))
    manifest_path = destination / CONTENT_MANIFEST_NAME
    manifest_path.write_text(
        json.dumps(content_manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    manifest_path.chmod(0o644)


def runtime_requirements(root: Path = ROOT) -> list[str]:
    project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    requirements = list(project["project"]["dependencies"])
    if not requirements or any("==" not in item for item in requirements):
        raise ReleaseBuildError("Every direct runtime dependency must be exactly pinned")
    return requirements


def validate_catalog(root: Path = ROOT) -> None:
    catalog = json.loads((root / "content" / "agents.json").read_text(encoding="utf-8"))
    ids = {str(item.get("id", "")).strip().lower() for item in catalog}
    supported = {"claude", "codex", "omnigent"}
    if ids != supported:
        raise ReleaseBuildError(
            f"Release catalog must contain exactly the supported agents: {supported}"
        )


def validate_build_host() -> None:
    machine = platform.machine().lower()
    if sys.implementation.name != "cpython" or sys.version_info[:2] != (3, 11):
        raise ReleaseBuildError("Release PEX must be built with CPython 3.11")
    if sys.platform != "linux" or machine not in {"x86_64", "amd64"}:
        raise ReleaseBuildError("Release PEX must be built on Linux x86_64")


def validate_source_identity(
    requested_sha: str, release_tag: str, *, root: Path = ROOT
) -> str:
    checkout_sha = git_sha(root)
    source_sha = requested_sha.strip().lower() or checkout_sha
    if len(source_sha) != 40 or any(c not in "0123456789abcdef" for c in source_sha):
        raise ReleaseBuildError("--git-sha must be an immutable 40-character SHA")
    if source_sha != checkout_sha:
        raise ReleaseBuildError(
            f"--git-sha {source_sha} does not match checkout HEAD {checkout_sha}"
        )
    if not release_tag:
        raise ReleaseBuildError("--release-tag cannot be empty")
    if release_tag != "unreleased":
        tags = set(_run("git", "tag", "--points-at", "HEAD", cwd=root).splitlines())
        if release_tag not in tags:
            raise ReleaseBuildError(
                f"Release tag {release_tag!r} does not point at checkout HEAD"
            )
    return source_sha


def build_pex(staged: Path, artifact: Path, requirements: list[str]) -> None:
    pex = Path(sys.executable).with_name("pex")
    if not pex.is_file():
        raise ReleaseBuildError(
            "pex is absent; run through `uv run --no-group dev --group release`"
        )
    venv = Path(sys.prefix)
    command = [
        str(pex),
        *requirements,
        "--venv-repository",
        str(venv),
        "--no-pypi",
        "--no-build",
        "--no-compile",
        "--no-use-system-time",
        "--venv",
        "prepend",
        "--python-shebang",
        "/usr/bin/env python3.11",
        "--sources-directory",
        str(staged),
        "--entry-point",
        ENTRY_POINT,
        "--output-file",
        str(artifact),
    ]
    subprocess.run(command, cwd=ROOT, check=True)
    artifact.chmod(0o755)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def release_manifest(
    artifact: Path, content_manifest: dict, *, source_sha: str, release_tag: str
) -> dict:
    size = artifact.stat().st_size
    if size > MAX_ARTIFACT_BYTES:
        raise ReleaseBuildError(
            f"Artifact is {size} bytes; budget is {MAX_ARTIFACT_BYTES} bytes"
        )
    return {
        "format_version": FORMAT_VERSION,
        "artifact_type": "pex",
        "artifact_name": ARTIFACT_NAME,
        "wt_git_sha": source_sha,
        "wt_release_tag": release_tag,
        "python_implementation": "cpython",
        "python_abi": "cp311",
        "platform": "linux_x86_64",
        "entry_point": ENTRY_POINT,
        "size_bytes": size,
        "sha256": _sha256(artifact),
        "logical_contents_sha256": content_manifest["logical_contents_sha256"],
        "logical_contents_file_count": content_manifest["file_count"],
        "minimum_control_tower_contract_version": (
            MINIMUM_CONTROL_TOWER_CONTRACT_VERSION
        ),
    }


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=ROOT / "dist")
    parser.add_argument("--release-tag", default="unreleased")
    parser.add_argument("--git-sha", default="")
    parser.add_argument(
        "--allow-dirty",
        action="store_true",
        help="Permit a local diagnostic build; published builds must never use this.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    validate_build_host()
    if not args.allow_dirty and not checkout_is_clean(ROOT):
        raise ReleaseBuildError("Refusing to package a dirty checkout")
    release_tag = args.release_tag.strip()
    source_sha = validate_source_identity(args.git_sha, release_tag, root=ROOT)

    paths = tracked_runtime_paths(ROOT)
    content = logical_content_manifest(paths, root=ROOT, source_sha=source_sha)
    requirements = runtime_requirements(ROOT)
    validate_catalog(ROOT)

    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=True)
    artifact = output / ARTIFACT_NAME
    manifest_path = output / RELEASE_MANIFEST_NAME
    with tempfile.TemporaryDirectory(prefix="wt-release-") as temporary:
        staged = Path(temporary) / "runtime"
        stage_runtime(staged, paths, content, root=ROOT)
        build_pex(staged, artifact, requirements)

    manifest = release_manifest(
        artifact, content, source_sha=source_sha, release_tag=release_tag
    )
    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(
        f"built {artifact} ({manifest['size_bytes']} bytes, "
        f"sha256={manifest['sha256']})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
