"""Package qualification proves new lifecycle helpers and attendee-safe views."""
import copy
import json
from pathlib import Path
import zipfile

import pytest

from scripts import build_release, smoke_release

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def packaged(tmp_path):
    names = ["server/workshop_lifecycle.py", "server/bootstrap/package_sources.py", "content/agents.json"]
    for name in names:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes((ROOT / name).read_bytes())
    return tmp_path, names


def make_artifact(root, names):
    paths = [Path(name) for name in names]
    content = build_release.logical_content_manifest(paths, root=root, source_sha="a" * 40)
    artifact = root / "workshop-terminal.pex"
    with zipfile.ZipFile(artifact, "w") as archive:
        archive.writestr("runtime-content-manifest.json", json.dumps(content))
        archive.writestr("PEX-INFO", json.dumps({"distributions": {
            name + "-1.0-py3-none-any.whl": "hash"
            for name in ("fastapi", "uvicorn", "grpcio", "cryptography", "pillow")}}))
        for path in paths:
            archive.write(root / path, path.as_posix())
    manifest = build_release.release_manifest(artifact, content, source_sha="a" * 40, release_tag="unreleased")
    release_path = root / "release-manifest.json"
    release_path.write_text(json.dumps(manifest))
    return artifact, release_path


def test_package_integrity_includes_new_runtime_helpers(packaged):
    root, names = packaged
    artifact, manifest = make_artifact(root, names)
    smoke_release.verify_artifact(artifact, manifest, root)


@pytest.mark.parametrize("missing", ["server/workshop_lifecycle.py", "server/bootstrap/package_sources.py"])
def test_self_consistent_package_without_new_helper_is_rejected(packaged, missing):
    root, names = packaged
    artifact, manifest = make_artifact(root, [name for name in names if name != missing])
    with pytest.raises(AssertionError, match="helper is absent"):
        smoke_release.verify_artifact(artifact, manifest, root)


@pytest.mark.parametrize("state", ["open", "closed"])
def test_packaged_attendee_readback_checks_barrier_and_default_docs(monkeypatch, state):
    status = {"state": state, "accepting_input": state == "open"}
    config = {"workshop": status, "shell": {"links": [{"label": "AI Docs", "url": "https://naveenb83.github.io/ai-dev-day/"}]}}
    monkeypatch.setattr(smoke_release, "_request", lambda method, url:
                        (200, status if url.endswith("/api/workshop-status") else config))
    smoke_release._attendee_presentation("http://local.invalid", expected_state=state)


@pytest.mark.parametrize("invalid", ["stale-lifecycle", "stale-config", "stale-docs", "collection-notice"])
def test_packaged_attendee_proof_rejects_stale_or_disclosing_views(monkeypatch, invalid):
    status = {"state": "closed", "accepting_input": False}
    config = {"workshop": copy.deepcopy(status), "shell": {"links": [
        {"label": "AI Docs", "url": "https://naveenb83.github.io/ai-dev-day/"}]}}
    if invalid == "stale-lifecycle":
        status = {"state": "open", "accepting_input": True}
    elif invalid == "stale-config":
        config["workshop"] = {"state": "open", "accepting_input": True}
    elif invalid == "stale-docs":
        config["shell"]["links"][0]["url"] = "https://naveenb83.github.io/ai-dev-day-brisbane/"
    else:
        config["shell"]["links"].append({"label": "Account team follow-up", "url": "https://example.invalid"})
    monkeypatch.setattr(smoke_release, "_request", lambda method, url:
                        (200, status if url.endswith("/api/workshop-status") else config))
    with pytest.raises(AssertionError):
        smoke_release._attendee_presentation("http://local.invalid", expected_state="closed")


@pytest.mark.parametrize("field,value,reason", [
    ("wt_git_sha", "b" * 40, "source identities"),
    ("logical_contents_file_count", 999, "content counts"),
])
def test_external_manifest_cannot_disagree_with_embedded_provenance(packaged, field, value, reason):
    root, names = packaged
    artifact, manifest = make_artifact(root, names)
    release = json.loads(manifest.read_text())
    release[field] = value
    manifest.write_text(json.dumps(release))
    with pytest.raises(AssertionError, match=reason):
        smoke_release.verify_artifact(artifact, manifest, root)
