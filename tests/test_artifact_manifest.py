import hashlib
import json

import pytest


REQUIRED = (
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
)


def _manifest(tmp_path):
    artifacts = {}
    for name in REQUIRED:
        if name in {"omnigent_wheelhouse", "python_3_12_runtime"}:
            path = tmp_path / name
            path.mkdir()
            if name == "omnigent_wheelhouse":
                relative = "omnigent-0.5.1-py3-none-any.whl"
                (path / relative).write_bytes(b"wheel")
                content = f"{relative}\0".encode() + b"wheel\0"
            else:
                relative = "bin/python3.12"
                (path / "bin").mkdir()
                (path / relative).write_bytes(b"python")
                content = f"{relative}\0".encode() + b"python\0"
            artifacts[name] = {
                "version": "test-version",
                "source": str(path),
                "kind": "directory",
                "content_sha256": hashlib.sha256(content).hexdigest(),
            }
            if name == "python_3_12_runtime":
                artifacts[name]["executable_relative_path"] = relative
            continue
        if name == "omnigent_lock":
            payload = b"omnigent==0.5.1 --hash=sha256:" + b"d" * 64 + b"\n"
        else:
            payload = f"reviewed-{name}".encode()
        path = tmp_path / name
        path.write_bytes(payload)
        artifacts[name] = {
            "version": "test-version",
            "source": str(path),
            "sha256": hashlib.sha256(payload).hexdigest(),
        }
    artifacts["node_linux_x64"]["sha256"] = (
        "69b09dba5c8dcb05c4e4273a4340db1005abeafe3927efda2bc5b249e80437ec"
    )
    artifacts["tmux_linux_x64"]["sha256"] = (
        "a23e56e9913d610c31f2893a1c9c669a73cb8bb2b8ded1180f6572bb55e52ca5"
    )
    artifacts["ai_dev_kit"].update({
        "commit": "a" * 40,
        "content_sha256": "b" * 64,
    })
    artifacts["codex_native_package_linux_x64"].update({
        "executable_sha256": "c" * 64,
    })
    artifacts["omnigent_lock"].update({
        "lock_sha256": artifacts["omnigent_lock"]["sha256"],
    })
    path = tmp_path / "artifact-manifest.json"
    path.write_text(json.dumps({
        "schema_version": 1,
        "reviewed": True,
        "artifacts": artifacts,
    }))
    return path


def test_reviewed_artifact_manifest_requires_complete_contract(tmp_path):
    from server.bootstrap.artifacts import load_manifest

    manifest = load_manifest(str(_manifest(tmp_path)))

    assert manifest["ok"] is True
    assert set(REQUIRED) <= set(manifest["artifacts"])


def test_artifact_manifest_rejects_missing_or_unreviewed_entries(tmp_path):
    from server.bootstrap.artifacts import ArtifactManifestError, load_manifest

    path = _manifest(tmp_path)
    payload = json.loads(path.read_text())
    payload["reviewed"] = False
    payload["artifacts"].pop("claude_installer")
    path.write_text(json.dumps(payload))

    with pytest.raises(ArtifactManifestError):
        load_manifest(str(path))


def test_omnigent_manifest_rejects_unpinned_or_unhashed_lock(tmp_path):
    from server.bootstrap.artifacts import ArtifactManifestError, load_manifest

    path = _manifest(tmp_path)
    payload = json.loads(path.read_text())
    lock = tmp_path / "omnigent.lock"
    lock.write_text("omnigent==0.5.1\ntransitive-package\n")
    payload["artifacts"]["omnigent_lock"].update({
        "source": str(lock),
        "sha256": hashlib.sha256(lock.read_bytes()).hexdigest(),
        "lock_sha256": hashlib.sha256(lock.read_bytes()).hexdigest(),
    })
    path.write_text(json.dumps(payload))

    with pytest.raises(ArtifactManifestError, match="fully pinned"):
        load_manifest(str(path))


def test_staged_artifact_is_verified_before_use_and_rejects_tampering(tmp_path):
    from server.bootstrap.artifacts import ArtifactManifest, ArtifactManifestError

    path = _manifest(tmp_path)
    payload = json.loads(path.read_text())
    artifact = tmp_path / "synthetic-claude-installer"
    artifact.write_bytes(b"reviewed installer")
    payload["artifacts"]["claude_installer"].update({
        "source": str(artifact),
        "sha256": hashlib.sha256(artifact.read_bytes()).hexdigest(),
    })
    path.write_text(json.dumps(payload))
    contract = ArtifactManifest.from_path(str(path))

    assert contract.verified_local_path("claude_installer") == str(artifact)
    artifact.write_bytes(b"tampered")
    with pytest.raises(ArtifactManifestError, match="checksum"):
        contract.verified_local_path("claude_installer")


def test_bootstrap_never_pipes_unverified_network_content_to_an_interpreter():
    from server.bootstrap import install

    source = open(install.__file__, encoding="utf-8").read()
    node_source = open(
        install.os.path.join(install.os.path.dirname(install.__file__), "install_node.sh"),
        encoding="utf-8",
    ).read()

    assert '["curl"' not in source
    assert "curl -fsSL" not in node_source
    assert "| sh" not in source
    assert "| bash" not in source


def test_one_directory_checksum_handles_python_symlink_trees_identically(
    tmp_path
):
    from server.bootstrap import artifacts, install

    runtime = tmp_path / "python-runtime"
    (runtime / "bin").mkdir(parents=True)
    (runtime / "lib/python3.12/site-packages/pkg").mkdir(parents=True)
    (runtime / "bin/python3.12").write_bytes(b"python-binary")
    (runtime / "bin/python").symlink_to("python3.12")
    (runtime / "lib/python3.12/site-packages/pkg/__init__.py").write_bytes(
        b"package"
    )
    (runtime / "lib/current").symlink_to("python3.12", target_is_directory=True)

    canonical = artifacts.directory_checksum(runtime)

    assert canonical == install._directory_checksum(runtime)
    (runtime / "bin/python").unlink()
    (runtime / "bin/python").symlink_to("different-python")
    assert artifacts.directory_checksum(runtime) != canonical


def test_manifest_and_prewarm_share_directory_symlink_semantics(tmp_path):
    from server.bootstrap import artifacts

    tree = tmp_path / "tree"
    tree.mkdir()
    (tree / "target").write_bytes(b"value")
    (tree / "link").symlink_to("target")
    expected = artifacts.directory_checksum(tree)

    assert artifacts._directory_checksum(tree) == expected
