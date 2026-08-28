import hashlib
import json
from pathlib import Path

import pytest

from scripts import build_release


ROOT = Path(__file__).resolve().parents[1]


def _direct_requirements(path: Path) -> set[str]:
    return {
        line.strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith(("#", "-r "))
    }


def test_pyproject_is_the_uv_locked_runtime_dependency_contract():
    assert (ROOT / "uv.lock").is_file()
    assert set(build_release.runtime_requirements(ROOT)) == _direct_requirements(
        ROOT / "requirements.in"
    )


def test_release_inventory_is_tracked_deterministic_and_complete():
    first = build_release.tracked_runtime_paths(ROOT)
    second = build_release.tracked_runtime_paths(ROOT)

    assert first == second == sorted(first, key=lambda path: path.as_posix())
    names = {path.as_posix() for path in first}
    assert build_release.REQUIRED_RUNTIME_FILES <= names
    assert {path.parts[0] for path in first} == set(build_release.RUNTIME_ROOTS)
    assert not any("__pycache__" in path.parts or path.suffix == ".pyc" for path in first)


def test_logical_manifest_changes_on_bytes_paths_or_modes(tmp_path):
    (tmp_path / "server").mkdir()
    one = tmp_path / "server" / "one.py"
    two = tmp_path / "server" / "two.py"
    one.write_text("one\n")
    two.write_text("two\n")
    paths = [Path("server/one.py"), Path("server/two.py")]

    initial = build_release.logical_content_manifest(
        paths, root=tmp_path, source_sha="a" * 40
    )
    repeated = build_release.logical_content_manifest(
        paths, root=tmp_path, source_sha="a" * 40
    )
    assert initial == repeated

    two.write_text("changed\n")
    changed = build_release.logical_content_manifest(
        paths, root=tmp_path, source_sha="a" * 40
    )
    assert changed["logical_contents_sha256"] != initial["logical_contents_sha256"]

    two.write_text("two\n")
    two.chmod(0o755)
    executable = build_release.logical_content_manifest(
        paths, root=tmp_path, source_sha="a" * 40
    )
    assert executable["logical_contents_sha256"] != initial["logical_contents_sha256"]


def test_release_manifest_is_flat_immutable_and_ct_versioned(tmp_path):
    artifact = tmp_path / build_release.ARTIFACT_NAME
    artifact.write_bytes(b"pex bytes")
    content = {
        "logical_contents_sha256": "b" * 64,
        "file_count": 417,
    }

    manifest = build_release.release_manifest(
        artifact,
        content,
        source_sha="a" * 40,
        release_tag="v1.2.3",
    )

    fixture = json.loads(
        (ROOT / "tests" / "fixtures" / "wt-release-manifest-v1.json").read_text()
    )
    assert fixture["sha256"] == hashlib.sha256(b"pex bytes").hexdigest()
    assert manifest == fixture


def test_release_catalog_retains_omnigent_and_excludes_pi_and_raw_terminal():
    build_release.validate_catalog(ROOT)
    catalog = json.loads((ROOT / "content" / "agents.json").read_text())
    assert {item["id"] for item in catalog} == {"omnigent", "claude", "codex"}


def test_release_builder_refuses_the_wrong_host(monkeypatch):
    monkeypatch.setattr(build_release.sys, "platform", "darwin")
    with pytest.raises(
        build_release.ReleaseBuildError, match="CPython 3.11|Linux x86_64"
    ):
        build_release.validate_build_host()


def test_release_identity_must_match_checkout_and_tag():
    head = build_release.git_sha(ROOT)
    assert build_release.validate_source_identity(head, "unreleased", root=ROOT) == head

    with pytest.raises(build_release.ReleaseBuildError, match="does not match checkout"):
        build_release.validate_source_identity("0" * 40, "unreleased", root=ROOT)
    with pytest.raises(build_release.ReleaseBuildError, match="does not point"):
        build_release.validate_source_identity(head, "v-not-a-real-tag", root=ROOT)


def test_packaged_launcher_is_early_otel_and_one_worker():
    assert build_release.ENTRY_POINT == "server.otel_bootstrap:main"
    source = (ROOT / "server" / "otel_bootstrap.py").read_text(encoding="utf-8")
    main_body = source.split("def main()", 1)[1]
    assert main_body.index("prepare_environment") < main_body.index("uvicorn_command")
    command = __import__("server.otel_bootstrap", fromlist=["uvicorn_command"])
    argv = command.uvicorn_command({"DATABRICKS_APP_PORT": "8123"})
    assert argv[argv.index("--workers") + 1] == "1"


def test_packaged_smoke_covers_offline_entrypoint_and_supported_lifecycles():
    container = (ROOT / "scripts" / "smoke_release_container.sh").read_text()
    smoke = (ROOT / "scripts" / "smoke_release.py").read_text()

    assert container.count("--network none") == 2
    assert "benchmark_release.py" in container
    assert "PEX_INTERPRETER=1" in container
    for endpoint in ("/healthz", "/readyz", "/api/agents", "/api/sessions"):
        assert endpoint in smoke or endpoint in (
            ROOT / "scripts" / "benchmark_release.py"
        ).read_text()
    for agent in ("claude", "codex", "omnigent"):
        assert f'"{agent}"' in smoke
