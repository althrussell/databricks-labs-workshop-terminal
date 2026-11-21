"""Screenshot upload and sidecar in ``assets/bin/workshop-export`` (contract v2, section 3).

The script is a standalone helper that runs in the attendee's shell, so it is loaded
by path and its Volume calls are replaced with a recorder. Flag-off behaviour is the
regression baseline: one newest PNG per project, no sidecar, nothing renamed.
"""

from __future__ import annotations

import hashlib
import importlib.machinery
import importlib.util
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "assets" / "bin" / "workshop-export"
PNG = b"\x89PNG\r\n\x1a\n" + b"pixels" * 20


@pytest.fixture
def helper():
    loader = importlib.machinery.SourceFileLoader("workshop_export_screenshots", str(SCRIPT))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


@pytest.fixture
def cli(helper, monkeypatch):
    """Record every databricks call; capture the sidecar's bytes while they exist."""
    calls: list[list[str]] = []
    sidecars: list[dict] = []

    def fake_run_cli(args, *, allow_failure=False):
        calls.append(list(args))
        if args[:2] == ["fs", "cp"] and args[2].endswith("manifest.json"):
            # The temporary file is deleted after the call, so read it now.
            sidecars.append(json.loads(Path(args[2]).read_text()))
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(helper, "run_cli", fake_run_cli)
    return SimpleNamespace(calls=calls, sidecars=sidecars)


def _project(root: Path, name: str, shots: dict[str, bytes]) -> Path:
    project = root / name
    evidence = project / ".workshop" / "evidence"
    evidence.mkdir(parents=True)
    for file_name, data in shots.items():
        (evidence / file_name).write_bytes(data)
    return project


SHOTS = {
    "20260730T113012Z-deployed-desktop-initial.png": PNG,
    "20260730T113500Z-deployed-desktop-after-1.png": PNG,
    "20260730T114000Z-local-desktop-after-2.png": PNG,
    "20260730T115000Z-deployed-desktop-after-3.png": PNG,
}


def test_flag_off_uploads_only_the_newest_png_and_writes_no_sidecar(helper, cli, tmp_path, monkeypatch):
    monkeypatch.delenv("WORKSHOP_INSIGHT_V2", raising=False)
    _project(tmp_path, "alpha", SHOTS)

    uploaded = helper.upload_screenshots(tmp_path, "main_demo", "seat", single_project=False)

    assert uploaded == ["/Volumes/main_demo/seat/workshop_exports/screenshots/alpha-20260730T115000Z-deployed-desktop-after-3.png"]
    assert cli.sidecars == []


def test_flag_on_uploads_up_to_three_shots_per_project_under_unchanged_names(helper, cli, tmp_path, monkeypatch):
    monkeypatch.setenv("WORKSHOP_INSIGHT_V2", "1")
    _project(tmp_path, "alpha", SHOTS)

    uploaded = helper.upload_screenshots(tmp_path, "main_demo", "seat", single_project=False)

    names = [u.rsplit("/", 1)[-1] for u in uploaded]
    assert names == [
        "alpha-20260730T115000Z-deployed-desktop-after-3.png",
        "alpha-20260730T114000Z-local-desktop-after-2.png",
        "alpha-20260730T113012Z-deployed-desktop-initial.png",
    ]


def test_a_png_over_two_megabytes_is_not_uploaded(helper, cli, tmp_path, monkeypatch):
    monkeypatch.setenv("WORKSHOP_INSIGHT_V2", "1")
    big = PNG + b"0" * (2 * 1024 * 1024)
    _project(tmp_path, "alpha", {
        "20260730T113012Z-deployed-desktop-initial.png": big,
        "20260730T113500Z-deployed-desktop-after-1.png": PNG,
    })

    uploaded = helper.upload_screenshots(tmp_path, "main_demo", "seat", single_project=False)

    assert [u.rsplit("/", 1)[-1] for u in uploaded] == ["alpha-20260730T113500Z-deployed-desktop-after-1.png"]


def test_the_sidecar_describes_every_uploaded_shot_with_its_digest(helper, cli, tmp_path, monkeypatch):
    monkeypatch.setenv("WORKSHOP_INSIGHT_V2", "1")
    _project(tmp_path, "alpha", {"20260730T113012Z-deployed-desktop-initial.png": PNG})
    evidence = tmp_path / "alpha" / ".workshop"
    (evidence / "design-workflow.json").write_text(json.dumps({"evidence": {"inspect": {"artifacts": [
        {"kind": "screenshot", "path": ".workshop/evidence/20260730T113012Z-deployed-desktop-initial.png",
         "url": "https://alpha.example.invalid/?token=x", "title": "Alpha"},
    ]}}}))

    helper.upload_screenshots(tmp_path, "main_demo", "seat", single_project=False)

    [sidecar] = cli.sidecars
    assert sidecar["schema_version"] == 1
    [shot] = sidecar["shots"]
    assert shot["file"] == "alpha-20260730T113012Z-deployed-desktop-initial.png"
    assert shot["project"] == "alpha"
    assert shot["sha256"] == hashlib.sha256(PNG).hexdigest()
    assert shot["bytes"] == len(PNG)
    assert shot["captured_at"] == "2026-07-30T11:30:12Z"
    assert shot["scope"] == "deployed" and shot["label"] == "initial"
    assert shot["url"] == "https://alpha.example.invalid/", "the query can carry a token"
    assert shot["title"] == "Alpha"


def test_the_sidecar_is_uploaded_with_overwrite_on_full_exports(helper, cli, tmp_path, monkeypatch):
    monkeypatch.setenv("WORKSHOP_INSIGHT_V2", "1")
    _project(tmp_path, "alpha", {"20260730T113012Z-deployed-desktop-initial.png": PNG})

    helper.upload_screenshots(tmp_path, "main_demo", "seat", single_project=False)

    sidecar_calls = [c for c in cli.calls if c[:2] == ["fs", "cp"] and c[3].endswith("screenshots/manifest.json")]
    assert len(sidecar_calls) == 1
    assert sidecar_calls[0][-1] == "--overwrite"


def test_a_single_project_export_writes_no_sidecar(helper, cli, tmp_path, monkeypatch):
    monkeypatch.setenv("WORKSHOP_INSIGHT_V2", "1")
    project = _project(tmp_path, "alpha", {"20260730T113012Z-deployed-desktop-initial.png": PNG})

    uploaded = helper.upload_screenshots(project, "main_demo", "seat", single_project=True)

    assert len(uploaded) == 1
    assert cli.sidecars == []


def test_the_export_keeps_the_same_shot_choice_as_the_manifest(helper, cli, tmp_path, monkeypatch):
    """Both sides must choose the same shots, or the sidecar would describe files that never upload."""
    from server import app_manifest

    monkeypatch.setenv("WORKSHOP_INSIGHT_V2", "1")
    _project(tmp_path, "alpha", SHOTS)
    expected = app_manifest.select_shots(list(SHOTS))

    uploaded = helper.upload_screenshots(tmp_path, "main_demo", "seat", single_project=False)

    assert [u.rsplit("/", 1)[-1] for u in uploaded] == [f"alpha-{name}" for name in expected]
