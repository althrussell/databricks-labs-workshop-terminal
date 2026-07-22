"""Validate and install the pinned Codex npm alias package layout."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tarfile
import tempfile


ALIAS = "@openai/codex-linux-x64"
TARGET = "x86_64-unknown-linux-musl"
NATIVE_MEMBER = f"package/vendor/{TARGET}/bin/codex"


class CodexArtifactError(RuntimeError):
    pass


def _metadata(archive: tarfile.TarFile) -> dict:
    try:
        member = archive.getmember("package/package.json")
        handle = archive.extractfile(member)
        value = json.load(handle) if handle is not None else None
    except (KeyError, ValueError, tarfile.TarError) as error:
        raise CodexArtifactError("Codex package metadata is invalid") from error
    if not isinstance(value, dict):
        raise CodexArtifactError("Codex package metadata is invalid")
    return value


def validate_codex_tarballs(
    launcher_path: str, native_path: str, version: str
) -> dict:
    alias_target = f"npm:@openai/codex@{version}-linux-x64"
    try:
        with tarfile.open(launcher_path, "r:gz") as launcher:
            launcher_metadata = _metadata(launcher)
            launcher_names = {member.name for member in launcher.getmembers()}
        with tarfile.open(native_path, "r:gz") as native:
            native_metadata = _metadata(native)
            native_members = native.getmembers()
            native_names = {member.name for member in native_members}
    except (OSError, tarfile.TarError) as error:
        raise CodexArtifactError("Codex package archive is invalid") from error
    optional = launcher_metadata.get("optionalDependencies")
    optional = optional if isinstance(optional, dict) else {}
    if (
        launcher_metadata.get("name") != "@openai/codex"
        or launcher_metadata.get("version") != version
        or launcher_metadata.get("bin") != {"codex": "bin/codex.js"}
        or optional.get(ALIAS) != alias_target
        or "package/bin/codex.js" not in launcher_names
    ):
        raise CodexArtifactError("Codex launcher alias metadata drifted")
    if (
        native_metadata.get("name") != "@openai/codex"
        or native_metadata.get("version") != f"{version}-linux-x64"
        or native_metadata.get("os") != ["linux"]
        or native_metadata.get("cpu") != ["x64"]
        or NATIVE_MEMBER not in native_names
    ):
        raise CodexArtifactError("Codex native package metadata/layout drifted")
    for member in native_members:
        normalized = os.path.normpath(member.name)
        if (
            normalized.startswith("../")
            or os.path.isabs(normalized)
            or member.issym()
            or member.islnk()
        ):
            raise CodexArtifactError("unsafe Codex native package member")
    with tarfile.open(native_path, "r:gz") as native:
        native_handle = native.extractfile(NATIVE_MEMBER)
        if native_handle is None:
            raise CodexArtifactError("Codex native executable is unreadable")
        native_executable_sha256 = _stream_checksum(native_handle)
    return {
        "alias": ALIAS,
        "alias_target": alias_target,
        "native_member": NATIVE_MEMBER,
        "launcher_package_sha256": _file_checksum(launcher_path),
        "native_package_sha256": _file_checksum(native_path),
        "native_executable_sha256": native_executable_sha256,
    }


def _stream_checksum(handle) -> str:
    digest = hashlib.sha256()
    with handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _file_checksum(path: str) -> str:
    with open(path, "rb") as handle:
        return _stream_checksum(handle)


def install_native_alias(
    launcher_path: str,
    native_path: str,
    target_directory: str,
    version: str,
) -> str:
    proof = validate_codex_tarballs(launcher_path, native_path, version)
    parent = os.path.dirname(target_directory)
    os.makedirs(parent, exist_ok=True)
    staging = tempfile.mkdtemp(prefix=".codex-native-", dir=parent)
    try:
        with tarfile.open(native_path, "r:gz") as archive:
            for member in archive.getmembers():
                if not member.isfile() or not member.name.startswith("package/"):
                    continue
                relative = member.name.removeprefix("package/")
                destination = os.path.join(staging, relative)
                os.makedirs(os.path.dirname(destination), exist_ok=True)
                source = archive.extractfile(member)
                if source is None:
                    raise CodexArtifactError("Codex package member is unreadable")
                with source, open(destination, "wb") as output:
                    shutil.copyfileobj(source, output)
                os.chmod(destination, member.mode & 0o777)
        if os.path.exists(target_directory):
            shutil.rmtree(target_directory)
        os.replace(staging, target_directory)
        staging = ""
    finally:
        if staging:
            shutil.rmtree(staging, ignore_errors=True)
    return os.path.join(
        target_directory,
        proof["native_member"].removeprefix("package/"),
    )


__all__ = [
    "ALIAS",
    "CodexArtifactError",
    "NATIVE_MEMBER",
    "install_native_alias",
    "validate_codex_tarballs",
]
