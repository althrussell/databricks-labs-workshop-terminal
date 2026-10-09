"""Policy delivery invariants; model behavior is evaluated separately in MLflow."""

import os
from pathlib import Path
import shutil
import subprocess

import pytest

from server import user_content, wizard
from server.users import user_manager
from .test_user_content import _MEMORY_CHANNELS, _provisioned_home

ROOT = Path(__file__).resolve().parents[1]
CONTRACT = ROOT / "assets/instructions/workshop_contract.md"
MARKER = "<!-- workshop-interaction-contract:v1 -->"


@pytest.mark.parametrize("coach", [False, True])
@pytest.mark.parametrize("discovery", [False, True])
@pytest.mark.parametrize("with_wizard", [False, True])
def test_one_identical_contract_reaches_all_channels(
    client, monkeypatch, coach, discovery, with_wizard,
):
    monkeypatch.setenv("LAB_COACH", str(coach).lower())
    monkeypatch.setenv("DISCOVERY_ENABLED", str(discovery).lower())
    home = _provisioned_home(client, monkeypatch)
    user = user_manager.get("alice@example.com")
    if with_wizard:
        wizard.save(user, {"what_building": "An order queue", "industry": "retail"})
        user_content._write_instructions(user)

    canonical = CONTRACT.read_text().strip()
    for parts in _MEMORY_CHANNELS:
        text = Path(home, *parts).read_text()
        assert text.count(MARKER) == 1
        assert canonical in text
        assert "workshop-contract-slot" not in text
        assert "never run a requirements round" not in text
        assert "Even a toy gets one" not in text
        assert "That is the whole gate" not in text
        assert "no clarifying round" not in text
        assert "one line on what the app is for" not in text
        if not discovery:
            assert "workshop-discovery" not in text
        if parts != _MEMORY_CHANNELS[-1]:
            assert ("workshop-lab-coach" in text) == coach


@pytest.mark.parametrize("text", ["no slot", "<!-- workshop-contract-slot -->" * 2])
def test_broken_adapter_cannot_silently_omit_or_duplicate_contract(text):
    with pytest.raises(ValueError, match="exactly one"):
        user_content._compose_workshop_contract(text)


def test_project_only_worker_gets_policy_and_preserved_brief(tmp_path, monkeypatch):
    monkeypatch.setenv("DISCOVERY_ENABLED", "false")
    home = tmp_path / "attendee"
    template = home / ".config/workshop/project-memory.md"
    template.parent.mkdir(parents=True)
    template.write_text(user_content._project_memory())
    project = home / "projects/order-queue"
    project.mkdir(parents=True)
    attendee_notes = "# My project\n\nKeep these attendee notes.\n"
    (project / "README.md").write_text(attendee_notes)
    env = os.environ | {
        "HOME": str(home), "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_AUTHOR_NAME": "Synthetic attendee", "GIT_AUTHOR_EMAIL": "test@example.com",
        "GIT_COMMITTER_NAME": "Synthetic attendee", "GIT_COMMITTER_EMAIL": "test@example.com",
    }
    helper = ROOT / "assets/bin/workshop-init-project"
    subprocess.run(["bash", str(helper), "order-queue"], env=env, check=True, capture_output=True)
    readme = (project / "README.md").read_text().replace(
        "(fill from the attendee's words)", "Show late, unpacked orders first",
    )
    (project / "README.md").write_text(readme)
    subprocess.run(["bash", str(helper), "order-queue"], env=env, check=True, capture_output=True)
    assert (project / "README.md").read_text() == readme
    assert attendee_notes in readme
    assert readme.count("<!-- workshop-brief:v1 -->") == 1

    worker = tmp_path / "worker"
    subprocess.run(["git", "-C", str(project), "worktree", "add", "--detach", str(worker)],
                   env=env, check=True, capture_output=True)
    for name in ("CLAUDE.md", "AGENTS.md"):
        assert CONTRACT.read_text().strip() in (worker / name).read_text()
    assert (worker / "README.md").read_text() == readme


def test_skills_refresh_preserves_fork_policy_and_design_adapter(tmp_path, monkeypatch):
    from scripts import refresh_vendored_skills as refresh

    vendored = tmp_path / "skills"
    vendored.mkdir()
    studio = ROOT / "assets/skills/workshop-design-studio"
    shutil.copytree(studio, vendored / studio.name)
    before = (vendored / studio.name / "SKILL.md").read_bytes()
    upstream = tmp_path / "upstream"
    (upstream / "databricks-apps").mkdir(parents=True)
    (upstream / "databricks-apps/SKILL.md").write_text("Reviewed upstream API guidance")
    monkeypatch.setattr(refresh, "VENDORED_DIR", str(vendored))
    monkeypatch.setattr(refresh, "_clone_reviewed_skills", lambda _: str(upstream))
    composed_before = user_content._base_instructions()
    assert refresh.refresh(write=True) == 0
    assert (vendored / studio.name / "SKILL.md").read_bytes() == before
    assert user_content._base_instructions() == composed_before
    assert CONTRACT.read_text().strip() in composed_before
