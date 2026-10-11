"""Build separation, context fidelity, launch binding and source recovery."""
import json
import os
from pathlib import Path
import subprocess
import shutil

import pytest

from server import projects, user_content
from server.users import User

ALICE = {"X-Forwarded-Email": "alice@example.com"}
BOB = {"X-Forwarded-Email": "bob@example.com"}


@pytest.fixture
def attendee(tmp_path, monkeypatch):
    user = User("alice@example.com")
    user.home = str(tmp_path / "home")
    projects.root(user)
    template = Path(user.home) / ".config/workshop/project-memory.md"
    template.parent.mkdir(parents=True)
    template.write_text("Workshop policy: preserve other builds and read project context.\n")
    env = {**os.environ, "HOME": user.home, "GIT_CONFIG_GLOBAL": "/dev/null",
           "GIT_AUTHOR_NAME": "Attendee", "GIT_AUTHOR_EMAIL": user.email,
           "GIT_COMMITTER_NAME": "Attendee", "GIT_COMMITTER_EMAIL": user.email}
    monkeypatch.setattr(user, "shell_env", lambda: env.copy())
    return user


def test_a_change_b_return_a_preserves_source_and_latest_goal(attendee):
    projects.select(attendee, "repair-desk", goal="Track repairs", new=True)
    assert projects.launch_directory(attendee) == str(projects.root(attendee))
    assert not projects.project_path(attendee, "repair-desk").exists()
    a = projects.project_path(attendee, "repair-desk")
    a.mkdir()
    (a / "app.py").write_text("attendee's unfinished source")
    projects.select(attendee, "repair-desk", goal="Track bicycle repairs")
    projects.select(attendee, "garden-plan", goal="Plan a pollinator garden", new=True)
    projects.project_path(attendee, "garden-plan").mkdir()
    projects.select(attendee, "repair-desk")
    assert "Track bicycle repairs" in projects.instructions(attendee)
    assert "Plan a pollinator garden" not in projects.instructions(attendee)
    assert (a / "app.py").read_text() == "attendee's unfinished source"
    assert projects.launch_directory(attendee) == str(a)
    # An agent's newer product context outranks the home selection cache.
    context = a / projects.CONTEXT_FILE
    context.write_text(json.dumps({"version": 1, "name": "repair-desk", "goal": "Repair bikes and scooters"}))
    assert projects.inventory(attendee)["projects"][1]["goal"] == "Repair bikes and scooters"
    projects.select(attendee, "garden-plan")
    projects.select(attendee, "repair-desk")
    assert "Repair bikes and scooters" in projects.instructions(attendee)


@pytest.mark.parametrize("name", ["../other", "/tmp/shared", "a/b", ".hidden", "a\\b", "x" * 64])
def test_project_names_do_not_escape_attendee_namespace(attendee, name):
    with pytest.raises(projects.ProjectError):
        projects.select(attendee, name, new=True)


def test_local_links_and_corrupt_selection_do_not_fall_back_to_another_build(attendee, tmp_path):
    (projects.root(attendee) / "shared").symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(projects.ProjectError, match="link or file"):
        projects.select(attendee, "shared")
    projects.selection_path(attendee).write_text("{broken")
    with pytest.raises(projects.ProjectError, match="could not be read"):
        projects.launch_directory(attendee)


def test_selection_survives_a_new_user_object_and_context_does_not_escape(attendee, tmp_path):
    target = projects.project_path(attendee, "repair-desk")
    target.mkdir()
    projects.select(attendee, "repair-desk", goal="Track repairs")
    fresh = User(attendee.email)
    fresh.home = attendee.home
    assert projects.read(fresh)["active"] == "repair-desk"
    assert projects.launch_directory(fresh) == str(target)
    (target / projects.CONTEXT_FILE).unlink()
    (target / ".workshop").rmdir()
    (target / ".workshop").symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(projects.ProjectError, match="symlink"):
        projects.select(fresh, "repair-desk", goal="Do not write outside project")
    assert not (tmp_path / "project.json").exists()


def test_missing_source_is_never_treated_as_a_new_scaffold(attendee):
    target = projects.project_path(attendee, "repair-desk")
    target.mkdir()
    projects.select(attendee, "repair-desk", goal="Track repairs")
    shutil.rmtree(target)
    assert projects.inventory(attendee)["projects"][0]["missing"] is True
    with pytest.raises(projects.ProjectError, match="local source is missing"):
        projects.launch_directory(attendee)
    with pytest.raises(projects.ProjectError, match="recover"):
        projects.select(attendee, "repair-desk")
    assert not target.exists()


def test_recovery_uses_existing_synced_source_and_preserves_context(attendee, monkeypatch):
    parent = f"/Workspace/Users/{attendee.email}/projects"
    calls = []

    def cli(user, arguments, **kwargs):
        calls.append(arguments)
        if arguments[1] == "list":
            payload = [{"path": parent.removeprefix("/Workspace") + "/repair-desk", "object_type": "DIRECTORY"},
                       {"path": parent + "/../other", "object_type": "DIRECTORY"},
                       {"path": "/Workspace/Users/bob@example.com/projects/private", "object_type": "DIRECTORY"}]
            return subprocess.CompletedProcess(arguments, 0, json.dumps(payload), "")
        assert arguments[:3] == ["workspace", "export-dir", parent + "/repair-desk"]
        staged = Path(arguments[3])
        staged.mkdir()
        (staged / "app.py").write_text("exact synced source")
        (staged / "PRODUCT.md").write_text("Repair bicycles, preserve existing design")
        (staged / ".workshop").mkdir()
        (staged / projects.CONTEXT_FILE).write_text(json.dumps(
            {"version": 1, "name": "repair-desk", "goal": "Track bicycle repairs"}))
        (staged / "node_modules").mkdir()
        (staged / "node_modules/cache.js").write_text("installed dependency")
        return subprocess.CompletedProcess(arguments, 0, "", "")

    monkeypatch.setattr(projects, "_cli", cli)
    saved = projects.saved_inventory(attendee)
    assert [entry["name"] for entry in saved["projects"]] == ["repair-desk"]
    restored = projects.recover(attendee, "repair-desk")
    assert restored["active"] == "repair-desk"
    target = projects.project_path(attendee, "repair-desk")
    assert (target / "app.py").read_text() == "exact synced source"
    assert (target / "PRODUCT.md").read_text() == "Repair bicycles, preserve existing design"
    assert not (target / "node_modules").exists()
    assert restored["projects"][0]["goal"] == "Track bicycle repairs"
    tracked = subprocess.run(["git", "show", "HEAD:.workshop/project.json"], cwd=target,
                             env=attendee.shell_env(), text=True, capture_output=True, check=True)
    assert json.loads(tracked.stdout)["goal"] == "Track bicycle repairs"
    # The second recovery must not even fetch or touch local edits.
    (target / "app.py").write_text("new local work")
    before = len(calls)
    with pytest.raises(projects.ProjectError, match="will not overwrite"):
        projects.recover(attendee, "repair-desk")
    assert len(calls) == before
    assert (target / "app.py").read_text() == "new local work"


def test_failed_download_creates_no_replacement_project(attendee, monkeypatch):
    monkeypatch.setattr(projects, "saved_inventory", lambda user: {
        "active": "", "projects": [{"name": "repair-desk", "saved": True}]})

    def fail(*args, **kwargs):
        raise projects.ProjectError("Workspace project access failed")

    monkeypatch.setattr(projects, "_cli", fail)
    with pytest.raises(projects.ProjectError, match="access failed"):
        projects.recover(attendee, "repair-desk")
    assert not projects.project_path(attendee, "repair-desk").exists()
    assert list(projects.root(attendee).iterdir()) == []


def test_saved_source_apis_prepare_credentials_before_any_workspace_access(client, monkeypatch):
    import server.main as main
    from server.credentials import CredentialError

    calls = []
    def unavailable(user):
        calls.append(user.email)
        raise CredentialError("No current credential")
    monkeypatch.setattr(main, "ensure_user_credentials", unavailable)
    monkeypatch.setattr(projects, "_cli", lambda *args, **kwargs: pytest.fail("No Workspace access before credentials"))
    for path in ("/api/projects/saved", "/api/projects/repair-desk/recover"):
        response = client.post(path, headers=ALICE)
        assert response.status_code == 503 and "credentials" in response.json()["detail"]
    assert calls == ["alice@example.com", "alice@example.com"]


def test_api_owner_separation_switch_binding_and_harness_cwd(client, launchable_agents, tmp_path, monkeypatch):
    from server import config

    monkeypatch.setattr(config, "users_root", lambda: str(tmp_path / "users"))
    assert client.post("/api/projects/select", json={"name": "repair-desk", "goal": "Track repairs", "new": True}, headers=ALICE).status_code == 200
    assert client.get("/api/projects", headers=BOB).json()["projects"] == []
    from server.users import user_manager

    user = user_manager.get("alice@example.com")
    projects.project_path(user, "repair-desk").mkdir()
    assert client.post("/api/projects/select", json={"name": "garden-plan", "goal": "Plan a garden", "new": True}, headers=ALICE).status_code == 200
    projects.project_path(user, "garden-plan").mkdir()
    # A stale tab must not launch an agent into whichever build another tab chose.
    stale = client.post("/api/sessions", json={"agent_id": "claude", "project_name": "repair-desk"}, headers=ALICE)
    assert stale.status_code == 409 and "active build changed" in stale.json()["detail"]
    live = client.post("/api/sessions", json={"agent_id": "codex", "project_name": "garden-plan"}, headers=ALICE)
    assert live.status_code == 200, live.text
    assert live.json()["session"]["project_name"] == "garden-plan"
    blocked = client.post("/api/projects/select", json={"name": "repair-desk"}, headers=ALICE)
    assert blocked.status_code == 409
    assert projects.read(user)["active"] == "garden-plan"
    assert client.delete("/api/sessions/" + live.json()["session"]["id"], headers=ALICE).status_code == 200
    assert client.post("/api/projects/select", json={"name": "repair-desk"}, headers=ALICE).status_code == 200
    fresh = client.post("/api/sessions", json={"agent_id": "claude", "project_name": "repair-desk"}, headers=ALICE)
    assert fresh.status_code == 200
    assert fresh.json()["session"]["project_name"] == "repair-desk"
    assert "Track repairs" in (Path(user.home) / ".claude/CLAUDE.md").read_text()
    assert "Plan a garden" not in (Path(user.home) / ".claude/CLAUDE.md").read_text()


def test_current_build_overrides_old_wizard_task_but_keeps_help_preference(attendee, monkeypatch):
    projects.select(attendee, "garden-plan", goal="Plan flowers", new=True)
    monkeypatch.setattr(user_content, "_wizard_overlay", lambda user: "Build a repair desk immediately")
    monkeypatch.setattr(user_content, "_persona_overlay", lambda user: "Use concise explanations")
    user_content._write_instructions(attendee)
    for name in (".claude/CLAUDE.md", ".codex/AGENTS.md"):
        text = (Path(attendee.home) / name).read_text()
        assert "Plan flowers" in text and "Use concise explanations" in text
        assert "Build a repair desk immediately" not in text
