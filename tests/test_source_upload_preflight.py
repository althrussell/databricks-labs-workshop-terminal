"""Dirty or non-public local inputs must fail before remote-client creation."""

from pathlib import Path
import json
import os
import subprocess

import pytest
import yaml

import dependency_sources
from scripts import deploy_ct_compatible_wt_test, deploy_ct_sim, deploy_dev
from scripts import deploy_generated_app_test, probe_omnigent_app_build
from scripts import qualify_ct_compatible_wt_test, source_upload


ROOT = Path(__file__).resolve().parents[1]


def git(root, *args):
    return subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, text=True)


def commit(root):
    git(root, "add", ".")
    git(root, "-c", "user.name=WT source test", "-c", "user.email=wt-test@example.com",
        "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false",
        "commit", "-qm", "Reviewed fixture inputs")


@pytest.fixture
def release_root(tmp_path):
    root = tmp_path / "release"
    names = set(dependency_sources.REQUIRED_INPUTS)
    names.update(path.relative_to(ROOT).as_posix() for path in (ROOT / "assets/artifacts").glob("*.*"))
    for name in names:
        destination = root / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes((ROOT / name).read_bytes())
    (root / ".gitignore").write_text(".cache/\n**/__pycache__/\n")
    git(root, "init", "-q")
    commit(root)
    dependency_sources.validate_release_inputs(root, require_clean=True)
    return root


def run_upload_cli(runner, root, output, monkeypatch):
    for name in ("ROOT", "REPO_ROOT"):
        if hasattr(runner, name):
            monkeypatch.setattr(runner, name, root)
    if runner is deploy_ct_sim:
        argv = ["--profile", "fixture", "--attendee", "attendee@example.com", "--skills-ref", "v1.2.3",
                "--anthropic-model", "claude-sonnet-5", "--codex-model", "gpt-5-6-terra"]
    elif runner is deploy_generated_app_test:
        argv = ["--spec", str(output.parent / "missing-spec.json"), "--output", str(output), "--execute"]
    elif runner is deploy_ct_compatible_wt_test:
        argv = ["--spec", "missing-spec.json", "--release-manifest", "missing-manifest.json",
                "--artifact", "missing.pex", "--output", str(output), "--execute"]
    elif runner is qualify_ct_compatible_wt_test:
        argv = ["update-observer", "--receipt", "missing-receipt.json", "--output", str(output)]
    else:
        argv = []
    return runner.main(argv)


UPLOAD_RUNNERS = (deploy_dev, deploy_ct_sim, deploy_generated_app_test, probe_omnigent_app_build,
                  deploy_ct_compatible_wt_test, qualify_ct_compatible_wt_test)


@pytest.mark.parametrize("runner", UPLOAD_RUNNERS, ids=lambda module: module.__name__.split(".")[-1])
@pytest.mark.parametrize("invalid", ["dirty", "python-lock", "paired-lock", "npm-lock", "app-env"])
def test_invalid_release_inputs_make_zero_remote_calls(release_root, tmp_path, monkeypatch, runner, invalid):
    if invalid == "dirty":
        (release_root / "unreviewed.py").write_text("# unreviewed source\n")
    elif invalid in {"python-lock", "paired-lock"}:
        path = release_root / ("uv.lock" if invalid == "python-lock" else "deploy/omnigent-app/uv.lock")
        path.write_text(path.read_text().replace("https://pypi.org/simple", "https://unknown-mirror.example/simple", 1))
        commit(release_root)
    elif invalid == "npm-lock":
        path = release_root / "frontend/package-lock.json"
        path.write_text(path.read_text().replace("https://registry.npmjs.org/", "https://npm.unknown.example/", 1))
        commit(release_root)
    else:
        path = release_root / "app.yaml"
        value = yaml.safe_load(path.read_text())
        value["env"].append({"name": "UV_EXTRA_INDEX_URL", "value": "https://private.example/simple"})
        path.write_text(yaml.safe_dump(value))
        commit(release_root)
    calls = []

    def reject_client(*args, **kwargs):
        calls.append("client-created")
        raise AssertionError("Invalid inputs reached remote client creation")

    import databricks.sdk
    monkeypatch.setattr(databricks.sdk, "WorkspaceClient", reject_client)
    if hasattr(runner, "workspace_client"):
        monkeypatch.setattr(runner, "workspace_client", reject_client)
    with pytest.raises(dependency_sources.DependencySourceError):
        run_upload_cli(runner, release_root, tmp_path / "receipt.json", monkeypatch)
    assert calls == []
    assert not (tmp_path / "receipt.json").exists()


def test_snapshot_validates_ignored_upload_inputs_too(release_root):
    # Git correctly reports clean: this poison is ignored, but the dev uploader
    # would otherwise ship it because it is outside its usual exclusions.
    directory = release_root / ".cache"
    directory.mkdir()
    (directory / "requirements.txt").write_text("--index-url https://private.example/simple\n")
    assert git(release_root, "status", "--porcelain").stdout == ""
    with pytest.raises(dependency_sources.DependencySourceError, match="non-public Python index"):
        source_upload.read_upload_sources(release_root, exclude=(".git*",))


def test_snapshot_returns_checked_bytes_and_skips_nested_runtime_caches(release_root):
    cache = release_root / "deploy/omnigent-app/__pycache__"
    cache.mkdir()
    (cache / "requirements.txt").write_text("--index-url https://private.example/simple\n")
    result = source_upload.read_upload_sources(release_root, exclude=(".git*", "__pycache__*"))
    assert result["uv.lock"] == (release_root / "uv.lock").read_bytes()
    assert not any("__pycache__" in name or name.startswith(".git/") for name in result)


@pytest.mark.parametrize("operation", ["qualify", "cleanup"])
def test_no_source_upload_operation_does_not_require_a_clean_checkout(tmp_path, monkeypatch, operation):
    receipt = tmp_path / "receipt.json"
    receipt.write_text(json.dumps({"plan": {"profile": "fixture"}}))

    def no_source_preflight(*args, **kwargs):
        raise AssertionError("Qualification/cleanup must not be blocked by unrelated local edits")

    monkeypatch.setattr(qualify_ct_compatible_wt_test, "read_selected_upload_sources", no_source_preflight)
    monkeypatch.setattr(qualify_ct_compatible_wt_test, "workspace_client", lambda *args, **kwargs: object())
    monkeypatch.setattr(qualify_ct_compatible_wt_test, operation,
                        lambda *args, **kwargs: {"status": "qualified" if operation == "qualify" else "cleanup_verified"})
    assert qualify_ct_compatible_wt_test.main([
        operation, "--receipt", str(receipt), "--output", str(tmp_path / "result.json"),
    ]) == 0


@pytest.mark.parametrize("path,poison", [
    ("requirements.txt", b"--extra-index-url https://private.example/simple\nfastapi==0.1\n"),
    ("app.yaml", b"env: [{name: PIP_INDEX_URL, value: 'https://private.example/simple'}]\n"),
    ("assets/artifacts/manifest.json", b'{"artifacts":{"x":{"source":"https://private.example/wheel", "sha256":"' + b"a" * 64 + b'"}}}'),
])
def test_custom_generated_source_poison_is_held_before_any_client_access(tmp_path, path, poison):
    files = {"app.yaml": (ROOT / "app.yaml").read_bytes(), "requirements.txt": b"fastapi==0.1\n",
             "server/main.py": b"# source\n", "static/index.html": b"<html/>",
             "content/default_pack.json": b"{}", "assets/artifacts/manifest.json": b'{"artifacts":{}}'}
    files[path] = poison
    calls = []

    class NoRemoteClient:
        def __getattr__(self, name):
            calls.append(name)
            raise AssertionError("Poisoned runtime source reached client access")

    with pytest.raises(dependency_sources.DependencySourceError):
        deploy_generated_app_test.deploy({}, files, str(tmp_path / "receipt.json"), client=NoRemoteClient())
    assert calls == []
    assert not (tmp_path / "receipt.json").exists()


def test_generated_cli_holds_edits_during_runtime_snapshot_before_client_creation(release_root, tmp_path, monkeypatch):
    monkeypatch.setattr(deploy_generated_app_test, "ROOT", release_root)
    calls = []

    def edited_runtime_sources(_root):
        (release_root / "late-edit.py").write_text("# changed during snapshot\n")
        return {}

    monkeypatch.setattr(deploy_generated_app_test, "runtime_sources", edited_runtime_sources)
    monkeypatch.setattr(deploy_generated_app_test, "workspace_client", lambda *args, **kwargs: calls.append("client"))
    with pytest.raises(dependency_sources.DependencySourceError, match="dirty checkout"):
        deploy_generated_app_test.main(["--spec", "missing-spec.json", "--output", str(tmp_path / "receipt.json"), "--execute"])
    assert calls == []


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "ancestor-symlink"])
def test_selected_package_helpers_cannot_be_linked_to_outside_release(release_root, tmp_path, kind):
    name = "evals/generated_apps/runtime/wt_bootstrap.py"
    path = release_root / name
    path.parent.mkdir(parents=True)
    outside = tmp_path / "external.py"
    outside.write_bytes(b"# outside release\n")
    if kind == "symlink":
        path.symlink_to(outside)
    elif kind == "hardlink":
        os.link(outside, path)
    else:
        directory = tmp_path / "linked-runtime"
        directory.mkdir()
        (directory / path.name).write_bytes(outside.read_bytes())
        path.parent.rmdir()
        path.parent.symlink_to(directory, target_is_directory=True)
    commit(release_root)
    with pytest.raises(ValueError, match="link"):
        source_upload.read_selected_upload_sources(release_root, {name})


def test_source_walk_rejects_nonexcluded_directory_links(release_root, tmp_path):
    (release_root / "linked-directory").symlink_to(tmp_path, target_is_directory=True)
    commit(release_root)
    with pytest.raises(ValueError, match="directory links"):
        source_upload.read_upload_sources(release_root, exclude=(".git*",))
