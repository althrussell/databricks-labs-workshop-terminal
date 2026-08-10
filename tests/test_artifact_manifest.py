import hashlib
import json
import os

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
    "omnigent_lock",
    "databricks_agent_skills",
)


def _override(tmp_path, artifacts):
    """Write an override manifest carrying only the given per-artifact fields."""
    path = tmp_path / "artifact-manifest.json"
    path.write_text(json.dumps({
        "schema_version": 1,
        "reviewed": True,
        "artifacts": artifacts,
    }))
    return path


def test_default_manifest_is_the_complete_reviewed_contract():
    from server.bootstrap.artifacts import load_manifest

    manifest = load_manifest("")

    assert manifest["ok"] is True
    assert manifest["source"] == "default"
    assert manifest["override_path"] is None
    assert set(REQUIRED) <= set(manifest["artifacts"])


def test_boot_needs_no_manifest_environment_variable(monkeypatch):
    """The outage this replaced: an empty path used to fail the whole contract."""
    from server.bootstrap import install
    from server.bootstrap.artifacts import status

    monkeypatch.delenv("ARTIFACT_MANIFEST_PATH", raising=False)

    assert status("")["ok"] is True
    assert install._artifact_contract().source == "default"


def test_standalone_boot_reaches_every_step_without_a_manifest_path(monkeypatch):
    """The outage: the contract gate used to error all seven steps at once."""
    from server.bootstrap import install

    monkeypatch.delenv("ARTIFACT_MANIFEST_PATH", raising=False)
    monkeypatch.setattr(install.config, "omnigent_enabled", lambda: True)
    for step, installer in (
        ("node", "_install_node"),
        ("claude", "_install_claude"),
        ("codex", "_install_codex"),
        ("databricks", "_install_databricks_cli"),
        ("skills", "_install_skills"),
        ("tmux", "_install_tmux"),
        ("omnigent", "_install_omnigent"),
    ):
        monkeypatch.setattr(
            install, installer, lambda step=step: install._set(step, "complete")
        )
    real_thread = install.threading.Thread

    def thread(*args, **kwargs):
        # Only the bootstrap thread runs inline; the installer pool still needs
        # real workers.
        if kwargs.get("name") == "bootstrap":
            return _Immediate(kwargs["target"])
        return real_thread(*args, **kwargs)

    monkeypatch.setattr(install.threading, "Thread", thread)
    with install._state_lock:
        saved = dict(install._state)
        install._state.clear()
    try:
        install.run_in_background()
        steps = install.status()["steps"]
    finally:
        with install._state_lock:
            install._state.clear()
            install._state.update(saved)

    assert {
        name: steps[name]["status"]
        for name in (
            "node",
            "claude",
            "codex",
            "databricks",
            "skills",
            "tmux",
            "omnigent",
        )
    } == {
        "node": "complete",
        "claude": "complete",
        "codex": "complete",
        "databricks": "complete",
        "skills": "complete",
        "tmux": "complete",
        "omnigent": "complete",
    }


class _Immediate:
    """Run the bootstrap body inline so the assertions see a finished boot."""

    def __init__(self, target):
        self._target = target

    def start(self):
        self._target()


def test_every_required_artifact_carries_a_pinned_https_or_repo_source():
    from server.bootstrap.artifacts import load_manifest

    artifacts = load_manifest("")["artifacts"]

    for name in REQUIRED:
        entry = artifacts[name]
        assert entry["version"], name
        source = entry["source"]
        # Either a pinned https URL or a path resolved inside the repo's own
        # assets directory; nothing may be left for an operator to stage.
        assert source.startswith("https://") or "assets/artifacts" in source, name


def test_archive_artifacts_declare_the_executable_inside_them():
    from server.bootstrap.artifacts import ARCHIVE_ARTIFACTS, load_manifest

    artifacts = load_manifest("")["artifacts"]

    for name in ARCHIVE_ARTIFACTS:
        assert artifacts[name]["kind"] == "archive"
        relative = artifacts[name]["executable_relative_path"]
        assert relative and not relative.startswith(("/", "../"))


def test_override_may_redirect_a_source_to_a_mirror(tmp_path):
    from server.bootstrap.artifacts import load_manifest

    mirror = "https://mirror.internal.example/node-v22.14.0-linux-x64.tar.xz"
    path = _override(tmp_path, {"node_linux_x64": {"source": mirror}})

    manifest = load_manifest(str(path))

    assert manifest["source"] == "override"
    assert manifest["override_path"] == str(path)
    assert manifest["artifacts"]["node_linux_x64"]["source"] == mirror
    # Everything the override did not mention still comes from the repo.
    assert manifest["artifacts"]["node_linux_arm64"]["sha256"] == load_manifest("")[
        "artifacts"
    ]["node_linux_arm64"]["sha256"]


@pytest.mark.parametrize(
    "field, value",
    [
        ("version", "22.13.0"),
        ("sha256", "d" * 64),
    ],
)
def test_override_cannot_downgrade_a_version_or_checksum(tmp_path, field, value):
    from server.bootstrap.artifacts import ArtifactManifestError, load_manifest

    path = _override(tmp_path, {"node_linux_x64": {field: value}})

    with pytest.raises(ArtifactManifestError, match="only change source"):
        load_manifest(str(path))


def test_override_cannot_introduce_an_artifact_the_repo_does_not_review(tmp_path):
    from server.bootstrap.artifacts import ArtifactManifestError, load_manifest

    path = _override(tmp_path, {"rogue_tool": {"source": "https://example/x"}})

    with pytest.raises(ArtifactManifestError, match="unknown artifact"):
        load_manifest(str(path))


def test_override_source_must_stay_https(tmp_path):
    from server.bootstrap.artifacts import ArtifactManifestError, load_manifest

    path = _override(
        tmp_path, {"node_linux_x64": {"source": "http://mirror.example/node.tar.xz"}}
    )

    with pytest.raises(ArtifactManifestError, match="https"):
        load_manifest(str(path))


def test_unreadable_or_unreviewed_override_fails_closed(tmp_path):
    from server.bootstrap.artifacts import ArtifactManifestError, load_manifest

    unreviewed = tmp_path / "unreviewed.json"
    unreviewed.write_text(json.dumps({
        "schema_version": 1,
        "reviewed": False,
        "artifacts": {},
    }))

    with pytest.raises(ArtifactManifestError):
        load_manifest(str(unreviewed))
    with pytest.raises(ArtifactManifestError):
        load_manifest(str(tmp_path / "absent.json"))


def test_committed_omnigent_lock_is_fully_pinned_and_hashed():
    from server.bootstrap.artifacts import _fully_pinned_hashed_lock, load_manifest

    lock = load_manifest("")["artifacts"]["omnigent_lock"]

    assert lock["lock_sha256"] == lock["sha256"]
    assert _fully_pinned_hashed_lock(lock["source"]) is True
    assert (
        hashlib.sha256(open(lock["source"], "rb").read()).hexdigest()
        == lock["sha256"]
    )


def test_omnigent_lock_rejects_an_unpinned_or_unhashed_requirement(tmp_path):
    from server.bootstrap.artifacts import _fully_pinned_hashed_lock

    unpinned = tmp_path / "unpinned.lock"
    unpinned.write_text("omnigent==0.8.2\ntransitive-package\n")
    unhashed = tmp_path / "unhashed.lock"
    unhashed.write_text("omnigent>=0.8.2 --hash=sha256:" + "d" * 64 + "\n")

    assert _fully_pinned_hashed_lock(str(unpinned)) is False
    assert _fully_pinned_hashed_lock(str(unhashed)) is False


def test_skills_provenance_is_a_commit_and_content_digest():
    from server.bootstrap import install
    from server.bootstrap.artifacts import load_manifest

    kit = load_manifest("")["artifacts"]["databricks_agent_skills"]

    assert kit["version"] == install.SKILLS_REF
    assert kit["source"] == install.SKILLS_REPO
    assert len(kit["commit"]) == 40
    assert len(kit["content_sha256"]) == 64


def test_staged_artifact_is_verified_before_use_and_rejects_tampering(tmp_path):
    from server.bootstrap.artifacts import ArtifactManifest, ArtifactManifestError

    artifact = tmp_path / "synthetic-claude-installer"
    artifact.write_bytes(b"reviewed installer")
    path = _override(tmp_path, {"claude_installer": {"source": str(artifact)}})
    contract = ArtifactManifest.from_path(str(path))
    # The override redirected the source but not the checksum, so the reviewed
    # digest still governs: the substituted file is refused.
    with pytest.raises(ArtifactManifestError, match="checksum"):
        contract.verified_local_path("claude_installer")

    vendored = ArtifactManifest.from_path("")
    installer = vendored.verified_local_path("claude_installer")

    assert installer.endswith("claude-code-bootstrap.sh")


def test_every_staged_artifact_keeps_the_extension_its_consumer_needs():
    """The Codex/Omnigent outage: staging dropped the source's extension.

    npm reads an extensionless local path as a *directory* and fails with
    ENOTDIR on its package.json, and ``shutil.unpack_archive`` refuses a name it
    cannot map to a format -- so Codex and both Omnigent archives never
    installed, while node and claude survived on an explicit ``tar -xzf``.
    """
    from urllib.parse import urlsplit

    from server.bootstrap.artifacts import _staged_suffix, load_manifest

    sources = load_manifest("")["artifacts"]
    required = {
        "codex_npm_launcher_package": ".tgz",
        "codex_native_package_linux_x64": ".tgz",
        "uv_binary": ".tar.gz",
        "python_3_12_runtime": ".tar.gz",
        "databricks_cli_archive_linux_x64": ".zip",
        "node_linux_x64": ".tar.xz",
        "node_linux_arm64": ".tar.xz",
        "tmux_linux_x64": ".gz",
    }
    for name, suffix in required.items():
        path = urlsplit(str(sources[name]["source"])).path
        assert _staged_suffix(path) == suffix, name

    # An extensionless source stays extensionless rather than inventing one.
    assert _staged_suffix(urlsplit(str(sources["claude_binary"]["source"])).path) == ""


def test_staged_download_lands_on_a_name_npm_accepts(tmp_path, monkeypatch):
    """End of the chain: the file npm is handed must look like a tarball."""
    import io

    from server.bootstrap import artifacts

    payload = b"not really a tarball"
    monkeypatch.setattr(artifacts, "urlopen", lambda *a, **k: io.BytesIO(payload))
    # Naming is what is under test; the reviewed digest is asserted by
    # test_staged_artifact_is_verified_before_use_and_rejects_tampering.
    monkeypatch.setattr(
        artifacts,
        "_checksum",
        lambda path: artifacts.ArtifactManifest.from_path("").entry(
            "codex_npm_launcher_package"
        )["sha256"],
    )
    contract = artifacts.ArtifactManifest.from_path("")

    staged = contract.verified_local_path(
        "codex_npm_launcher_package", staging_dir=str(tmp_path)
    )

    assert staged.endswith(".tgz")
    assert os.path.isfile(staged)


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


def test_running_the_code_in_a_tree_does_not_invalidate_its_checksum(tmp_path):
    from server.bootstrap import artifacts

    venv = tmp_path / "omnigent-venv" / "lib" / "python3.12" / "site-packages" / "omni"
    venv.mkdir(parents=True)
    (venv / "agent.py").write_text("def run(): pass\n")
    tree = tmp_path / "omnigent-venv"
    installed = artifacts.directory_checksum(tree)

    # Importing a module writes bytecode beside it. Measured live: the first
    # Omnigent host start wrote 593 .pyc files into the venv, which invalidated
    # the supply_chain proof permanently and left /readyz at 503 for the rest of
    # the event — a hard admission gate failing because the product was used.
    cache = venv / "__pycache__"
    cache.mkdir()
    (cache / "agent.cpython-312.pyc").write_bytes(b"\x00compiled")

    assert artifacts.directory_checksum(tree) == installed
    # Source is still covered, which is what the proof is actually for.
    (venv / "agent.py").write_text("def run(): steal()\n")
    assert artifacts.directory_checksum(tree) != installed


def test_manifest_and_prewarm_share_directory_symlink_semantics(tmp_path):
    from server.bootstrap import artifacts

    tree = tmp_path / "tree"
    tree.mkdir()
    (tree / "target").write_bytes(b"value")
    (tree / "link").symlink_to("target")
    expected = artifacts.directory_checksum(tree)

    assert artifacts._directory_checksum(tree) == expected
