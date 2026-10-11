"""Launch preparation and project reuse regressions; no model calls."""
import json
import os
from pathlib import Path
import subprocess
import threading
import time

import pytest

from .conftest import ALICE


@pytest.fixture
def attendee(monkeypatch, tmp_path):
    from server import config
    from server.users import User

    monkeypatch.setattr(config, "users_root", lambda: str(tmp_path / "users"))
    monkeypatch.setattr(config, "shared_prefix", lambda: str(tmp_path / "shared"))
    user = User("alice@example.com")
    user.bootstrap_home()
    return user


def test_required_write_retries_and_repairs_on_reuse(attendee, monkeypatch):
    from server import user_content as content

    write = content._write_instructions
    calls = []

    def fail_once(user):
        calls.append(True)
        if len(calls) == 1:
            raise OSError("disk temporarily unavailable")
        write(user)

    monkeypatch.setattr(content, "_write_instructions", fail_once)
    with pytest.raises(content.PreparationError):
        content.provision(attendee)
    content.provision(attendee)
    policy = Path(attendee.home) / ".codex/AGENTS.md"
    assert "Workshop" in policy.read_text()
    policy.unlink()
    content.provision(attendee)
    assert policy.is_file()


def test_optional_failure_preserves_custom_assets(attendee, monkeypatch):
    from server import user_content as content

    agents = Path(attendee.home) / ".claude/agents"
    agents.mkdir()
    (agents / "my-reviewer.md").write_text("My review preferences")
    (agents / "prd-writer.md").write_text("Old workshop chain")
    (Path(attendee.home) / ".npmrc").write_text("registry=https://registry.npmjs.org/\n")
    (Path(attendee.home) / ".gitconfig").write_text("[alias]\n\tst = status\n")
    content.provision(attendee)
    assert (agents / "my-reviewer.md").read_text() == "My review preferences"
    assert not (agents / "prd-writer.md").exists()
    assert "st = status" in (Path(attendee.home) / ".gitconfig").read_text()

    def unavailable(_user):
        raise OSError("optional convenience failed")

    monkeypatch.setattr(content, "_write_git_setup", unavailable)
    content.provision(attendee)
    assert "registry=" in (Path(attendee.home) / ".npmrc").read_text()


def test_launch_failure_never_spawns_and_retry_recovers(client, launchable_agents, monkeypatch):
    from server import user_content as content
    import server.main as main

    def unavailable(_user):
        raise OSError("missing required policy")

    write = content._write_instructions
    monkeypatch.setattr(content, "_write_instructions", unavailable)
    create = main.session_manager.create
    spawned = []

    def observe(*args, **kwargs):
        spawned.append(args[1])
        return create(*args, **kwargs)

    monkeypatch.setattr(main.session_manager, "create", observe)
    assert client.post("/api/sessions", json={"agent_id": "claude"}, headers=ALICE).status_code == 503
    assert spawned == []
    monkeypatch.setattr(content, "_write_instructions", write)
    assert client.post("/api/sessions", json={"agent_id": "claude"}, headers=ALICE).status_code == 200
    assert spawned == ["claude"]


def test_background_skill_publication_reconciles_fallback_and_retired_links(attendee, monkeypatch, tmp_path):
    from server import user_content as content
    from server.bootstrap import install
    from server.users import user_manager

    user_manager._users[attendee.email] = attendee
    monkeypatch.setattr(install, "skills_ready", lambda: False)
    content._link_skills(attendee)
    target = Path(attendee.home) / ".claude/skills"
    assert str(Path(content._ASSETS)) in str((target / "databricks-apps").resolve())

    source = Path(content.shared_skills_dir())
    (source / "databricks-apps").mkdir(parents=True)
    (source / "databricks-apps/SKILL.md").write_text("current published skill")
    (target / "retired-workshop-skill").symlink_to(source / "retired-workshop-skill")
    custom = tmp_path / "custom"
    custom.mkdir()
    (target / "my-skill").symlink_to(custom)
    monkeypatch.setattr(install, "skills_ready", lambda: True)
    content.refresh_skill_links()
    assert (target / "databricks-apps/SKILL.md").read_text() == "current published skill"
    assert not (target / "retired-workshop-skill").is_symlink()
    assert (target / "my-skill").resolve() == custom


def test_current_ux_links_retire_legacy_managed_discovery(attendee, monkeypatch):
    from server import user_content as content
    from server.bootstrap import install

    source = Path(content.shared_skills_dir())
    for relative in (".claude/skills", ".codex/skills"):
        target = Path(attendee.home) / relative
        target.mkdir(parents=True, exist_ok=True)
        for name in ("workshop-design-studio", "databricks-app-design", "promote"):
            (target / name).symlink_to(source / name)
    legacy = Path(attendee.home) / ".codex/skills"
    custom = legacy / "my-skill"
    custom.mkdir()
    (custom / "SKILL.md").write_text("attendee-owned")
    monkeypatch.setattr(install, "skills_ready", lambda: False)
    content._link_skills(attendee)
    for relative in content.HARNESS_SKILL_DIRS.values():
        target = Path(attendee.home) / relative
        assert (target / "impeccable/SKILL.md").is_file()
        assert not any((target / name).is_symlink() for name in ("workshop-design-studio", "databricks-app-design", "promote"))
        assert (target / "workshop-export/SKILL.md").is_file()
    assert sorted(path.name for path in legacy.iterdir()) == ["my-skill"]
    assert (custom / "SKILL.md").read_text() == "attendee-owned"


def test_launcher_follows_reinstalled_binary(attendee, tmp_path):
    from server import config

    shared = Path(config.shared_prefix()) / "bin"
    shared.mkdir(parents=True)
    old, new = tmp_path / "old-codex", tmp_path / "new-codex"
    old.write_text("old")
    new.write_text("new")
    (shared / "codex").symlink_to(old)
    attendee.refresh_launchers()
    link = Path(attendee.home) / ".local/bin/codex"
    assert link.read_text() == "old"
    (shared / "codex").unlink()
    (shared / "codex").symlink_to(new)
    assert link.read_text() == "new"


def test_redeploy_refreshes_links_into_previous_package(attendee, monkeypatch, tmp_path):
    from server import user_content as content
    from server.bootstrap import install

    monkeypatch.setattr(install, "skills_ready", lambda: False)
    for version in ("old-package", "new-package"):
        assets = tmp_path / version
        skill = assets / "skills/workshop-example"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text(version)
        monkeypatch.setattr(content, "_ASSETS", str(assets))
        content._link_skills(attendee)
    for relative in content.HARNESS_SKILL_DIRS.values():
        assert (Path(attendee.home) / relative / "workshop-example/SKILL.md").read_text() == "new-package"


@pytest.mark.parametrize("wizard", ["true", "false"])
def test_direct_launch_without_wizard_brief(client, launchable_agents, monkeypatch, wizard):
    monkeypatch.setenv("WORKSHOP_ONBOARDING_WIZARD", wizard)
    response = client.post("/api/sessions", json={"agent_id": "codex"}, headers=ALICE)
    assert response.status_code == 200


def test_remote_ui_first_retries_preparation_before_host_spawn(attendee, monkeypatch):
    from server import user_content as content
    from server.omnigent_remote import RemoteHostManager
    from server.users import UserManager
    from .test_omnigent_remote import _FakeProcess, REMOTE_URL, _wait_for

    monkeypatch.setenv("OMNIGENT_APP_URL", REMOTE_URL)
    monkeypatch.setenv("WORKSHOP_ATTENDEE_EMAIL", attendee.email)
    (Path(attendee.home) / ".omnigent/auth_tokens.json").write_text(json.dumps({
        REMOTE_URL: {"token": "test", "user_id": attendee.email, "expires_at": time.time() + 3600},
    }))
    users = UserManager()
    users._users[attendee.email] = attendee
    write = content._write_instructions
    attempts = []

    def fail_once(user):
        attempts.append(True)
        if len(attempts) == 1:
            raise OSError("temporary policy failure")
        write(user)

    monkeypatch.setattr(content, "_write_instructions", fail_once)
    spawned = []

    def spawn(*args, **kwargs):
        assert (Path(attendee.home) / ".codex/AGENTS.md").is_file()
        assert (Path(attendee.home) / ".local/bin/workshop-init-project").is_file()
        assert (Path(attendee.home) / ".claude/skills/databricks-apps/SKILL.md").is_file()
        spawned.append(True)
        return _FakeProcess(exits=True)

    hosts = RemoteHostManager(user_manager=users, popen_factory=spawn,
                              binary_resolver=lambda: "/opt/bin/omnigent", backoff_cap=0.01)
    try:
        hosts.start()
        hosts.notify(attendee.email)
        _wait_for(lambda: bool(spawned), timeout=3)
        assert len(attempts) >= 2
    finally:
        hosts.stop()


def test_selected_dependency_readiness_and_retry(monkeypatch):
    from server.bootstrap import install

    state = {name: {"status": "complete"} for name in ("node", "databricks", "claude")}
    state["agentbricks"] = {"status": "error"}
    assert install._ready_from(state)["claude"] is True
    state["databricks"] = {"status": "error", "error": "download failed"}
    assert install._ready_from(state)["claude"] is False
    monkeypatch.setattr(install, "_state", state)
    completed = threading.Event()

    def repair():
        install._set("databricks", "complete")
        completed.set()

    monkeypatch.setattr(install, "_install_databricks_cli", repair)
    assert install.retry_failed(["claude"]) is True
    assert completed.wait(3)
    assert install.ready()["claude"] is True
    assert state["agentbricks"]["status"] == "error"


def test_setup_retry_keeps_the_active_harness(client, launchable_agents, monkeypatch):
    import server.main as main

    response = client.post("/api/sessions", json={"agent_id": "claude"}, headers=ALICE)
    assert response.status_code == 200
    session = main.session_manager.active()
    attempted = []
    monkeypatch.setattr(main.install, "retry_failed", lambda requires: attempted.append(requires) or True)
    response = client.post("/api/agents/codex/retry-setup", headers=ALICE)
    assert response.status_code == 200 and response.json()["retrying"] is True
    assert attempted == [["codex"]]
    assert main.session_manager.active() is session
    assert not session.exited
    assert client.post("/api/agents/not-offered/retry-setup", headers=ALICE).status_code == 404


def test_helper_refreshes_memory_without_committing_attendee_changes(attendee, tmp_path):
    from server import user_content as content

    content.provision(attendee)
    env = {**os.environ, "HOME": attendee.home, "GIT_CONFIG_GLOBAL": "/dev/null",
           "GIT_AUTHOR_NAME": "Attendee", "GIT_AUTHOR_EMAIL": "alice@example.com",
           "GIT_COMMITTER_NAME": "Attendee", "GIT_COMMITTER_EMAIL": "alice@example.com"}
    helper = str(Path(content._ASSETS) / "bin/workshop-init-project")

    def run(*args):
        return subprocess.run(["bash", helper, "my-app", *args], env=env, capture_output=True, text=True, timeout=30)

    assert run().returncode == 0
    project = Path(attendee.home) / "projects/my-app"
    for name in ("AGENTS.md", "CLAUDE.md"):
        with (project / name).open("a") as handle:
            handle.write("\n## My decision\nUse weekly reporting.\n")
    (project / "attendee.txt").write_text("unrelated work in progress")
    subprocess.run(["git", "-C", str(project), "add", "attendee.txt"], env=env, check=True)
    template = Path(attendee.home) / ".config/workshop/project-memory.md"
    template.write_text("<!-- workshop-project-memory -->\nCurrent preparation policy\n")
    result = run("--json")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["ready"] is True
    for name in ("AGENTS.md", "CLAUDE.md"):
        text = (project / name).read_text()
        assert "Current preparation policy" in text
        assert "Always build apps with AppKit" not in text
        assert "Use weekly reporting." in text
    committed = subprocess.run(["git", "-C", str(project), "ls-tree", "--name-only", "HEAD"], env=env, capture_output=True, text=True, check=True).stdout
    assert "attendee.txt" not in committed
    assert "preparation.json" not in committed


def test_helper_reports_scaffold_and_commit_failure_in_json(attendee, monkeypatch, tmp_path):
    from server import user_content as content

    content.provision(attendee)
    shim = tmp_path / "shim"
    shim.mkdir()
    (shim / "databricks").write_text("#!/bin/sh\necho 'scaffold unavailable' >&2\nexit 1\n")
    (shim / "databricks").chmod(0o755)
    env = {"HOME": attendee.home, "PATH": f"{shim}:{os.environ['PATH']}",
           "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null",
           "GIT_AUTHOR_NAME": "", "GIT_AUTHOR_EMAIL": "", "GIT_COMMITTER_NAME": "", "GIT_COMMITTER_EMAIL": ""}
    result = subprocess.run(["bash", str(Path(content._ASSETS) / "bin/workshop-init-project"),
                             "incomplete", "--appkit", "--json"], env=env, capture_output=True, text=True, timeout=30)
    assert result.returncode == 1
    status = json.loads(result.stdout)
    assert status["ready"] is False
    assert status["scaffold"] == "failed"
    assert status["commit"] == "failed"
    assert "NOT committed" in result.stderr
    assert Path(status["project_path"]).is_dir()


@pytest.mark.parametrize("attendee_edit", [False, True])
def test_appkit_skill_lint_exclusion_preserves_rules_and_attendee_changes(attendee, attendee_edit):
    from server import user_content as content

    content.provision(attendee)
    env = {**os.environ, "HOME": attendee.home, "GIT_CONFIG_GLOBAL": "/dev/null",
           "GIT_AUTHOR_NAME": "Attendee", "GIT_AUTHOR_EMAIL": "alice@example.com",
           "GIT_COMMITTER_NAME": "Attendee", "GIT_COMMITTER_EMAIL": "alice@example.com"}
    helper = str(Path(content._ASSETS) / "bin/workshop-init-project")

    def run():
        result = subprocess.run(["bash", helper, "lint-app"], env=env,
                                capture_output=True, text=True, timeout=30)
        assert result.returncode == 0, result.stderr

    def git(*args):
        return subprocess.run(["git", "-C", str(project), *args], env=env,
                              capture_output=True, text=True, check=True).stdout

    run()
    project = Path(attendee.home) / "projects/lint-app"
    config = project / "eslint.config.js"
    original = "export default tseslint.config(\n  { rules: { 'no-unused-vars': 'error' } },\n);\n"
    config.write_text(original)
    git("add", "eslint.config.js")
    git("commit", "-m", "Application lint rules")
    (project / "attendee.txt").write_text("Work in progress")
    git("add", "attendee.txt")
    if attendee_edit:
        config.write_text(original + "// My application-specific note\n")

    run()
    updated = config.read_text()
    assert "**/.agents/skills/**" in updated
    assert "**/.claude/skills/**" in updated
    assert "{ rules: { 'no-unused-vars': 'error' } }" in updated
    assert "attendee.txt" not in git("ls-tree", "--name-only", "HEAD")
    assert "attendee.txt" in git("diff", "--cached", "--name-only")
    if attendee_edit:
        assert "My application-specific note" in updated
        assert git("show", "HEAD:eslint.config.js") == original
    else:
        assert git("show", "HEAD:eslint.config.js") == updated
    revision = git("rev-parse", "HEAD")
    run()
    assert config.read_text() == updated
    assert git("rev-parse", "HEAD") == revision


def test_appkit_lint_exclusion_preserves_other_configs_and_symlinks(attendee, tmp_path):
    from server import user_content as content

    content.provision(attendee)
    env = {**os.environ, "HOME": attendee.home, "GIT_CONFIG_GLOBAL": "/dev/null",
           "GIT_AUTHOR_NAME": "Attendee", "GIT_AUTHOR_EMAIL": "alice@example.com",
           "GIT_COMMITTER_NAME": "Attendee", "GIT_COMMITTER_EMAIL": "alice@example.com"}
    helper = str(Path(content._ASSETS) / "bin/workshop-init-project")
    command = ["bash", helper, "custom-lint"]
    result = subprocess.run(command, env=env, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    project = Path(attendee.home) / "projects/custom-lint"
    external = tmp_path / "attendee-eslint.js"
    external.write_text("export default tseslint.config({ rules: {} });\n")
    (project / "eslint.config.js").symlink_to(external)
    custom = project / "eslint.config.mjs"
    custom.write_text("export default [{ rules: { 'no-unused-vars': 'error' } }];\n")
    result = subprocess.run(command, env=env, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert external.read_text() == "export default tseslint.config({ rules: {} });\n"
    assert custom.read_text() == "export default [{ rules: { 'no-unused-vars': 'error' } }];\n"


def test_fresh_scaffold_source_is_committed_for_workers(attendee, tmp_path):
    from server import user_content as content

    content.provision(attendee)
    shim = tmp_path / "shim"
    shim.mkdir()
    (shim / "databricks").write_text(
        '#!/bin/sh\nmkdir -p "$HOME/projects/fresh"\nprintf "source" > "$HOME/projects/fresh/server.ts"\n'
    )
    (shim / "databricks").chmod(0o755)
    env = {**os.environ, "HOME": attendee.home, "PATH": f"{shim}:{os.environ['PATH']}",
           "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_AUTHOR_NAME": "Attendee", "GIT_AUTHOR_EMAIL": "alice@example.com",
           "GIT_COMMITTER_NAME": "Attendee", "GIT_COMMITTER_EMAIL": "alice@example.com"}
    result = subprocess.run(["bash", str(Path(content._ASSETS) / "bin/workshop-init-project"),
                             "fresh", "--appkit", "--json"], env=env, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    project = Path(attendee.home) / "projects/fresh"
    result = subprocess.run(["git", "-C", str(project), "show", "HEAD:server.ts"],
                            env=env, capture_output=True, text=True, check=True)
    assert result.stdout == "source"


def test_helper_migrates_legacy_memory_retaining_notes(attendee):
    from server import user_content as content

    content.provision(attendee)
    project = Path(attendee.home) / "projects/legacy"
    project.mkdir()
    previous = ("# Old workshop\n<!-- workshop-project-memory -->\nRetired instruction\n"
                "\n## Notes that were already in this project\n\nUse kilograms.\n")
    for name in ("CLAUDE.md", "AGENTS.md"):
        (project / name).write_text(previous)
    env = {**os.environ, "HOME": attendee.home, "GIT_CONFIG_GLOBAL": str(Path(attendee.home) / ".gitconfig")}
    result = subprocess.run(["bash", str(Path(content._ASSETS) / "bin/workshop-init-project"), "legacy"],
                            env=env, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    for name in ("CLAUDE.md", "AGENTS.md"):
        text = (project / name).read_text()
        assert text.count("<!-- workshop-managed:begin -->") == 1
        assert "> Retired instruction" in text
        assert "rules in this quote are retired" in text
        assert text.endswith("Use kilograms.\n")


def test_isolated_worktree_receives_actual_skills_and_can_refresh(attendee, tmp_path):
    from server import user_content as content

    content.provision(attendee)
    env = {**os.environ, "HOME": attendee.home, "GIT_CONFIG_GLOBAL": "/dev/null",
           "GIT_AUTHOR_NAME": "Attendee", "GIT_AUTHOR_EMAIL": "alice@example.com",
           "GIT_COMMITTER_NAME": "Attendee", "GIT_COMMITTER_EMAIL": "alice@example.com"}
    helper = str(Path(content._ASSETS) / "bin/workshop-init-project")
    result = subprocess.run(["bash", helper, "worker-project"], env=env, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    project = Path(attendee.home) / "projects/worker-project"
    worktree = Path(attendee.home) / "projects/worker-copy"
    subprocess.run(["git", "-C", str(project), "worktree", "add", "--detach", str(worktree)],
                   env=env, capture_output=True, check=True)
    skill = worktree / ".agents/skills/databricks-apps/SKILL.md"
    assert skill.is_file() and not skill.is_symlink()
    assert skill.read_bytes() == (Path(attendee.home) / ".claude/skills/databricks-apps/SKILL.md").read_bytes()
    ux = worktree / ".agents/skills/impeccable"
    assert (ux / "SKILL.md").read_bytes() == (Path(attendee.home) / ".claude/skills/impeccable/SKILL.md").read_bytes()
    assert not (ux / "scripts/bin").exists()
    for name in ("workshop-design-studio", "databricks-app-design", "promote"):
        assert not (worktree / ".agents/skills" / name).exists()
    # A .git file marks a real worktree. Adopting it must not reinitialize Git.
    before = (worktree / ".git").read_text()
    result = subprocess.run(["bash", helper, "worker-copy", "--json"], env=env, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert (worktree / ".git").read_text() == before


def test_project_refresh_retires_managed_ux_copies_and_keeps_notes(attendee):
    from server import user_content as content

    content.provision(attendee)
    env = {**os.environ, "HOME": attendee.home, "GIT_CONFIG_GLOBAL": "/dev/null",
           "GIT_AUTHOR_NAME": "Attendee", "GIT_AUTHOR_EMAIL": "alice@example.com",
           "GIT_COMMITTER_NAME": "Attendee", "GIT_COMMITTER_EMAIL": "alice@example.com"}
    helper = str(Path(content._ASSETS) / "bin/workshop-init-project")
    subprocess.run(["bash", helper, "ux-refresh"], env=env, capture_output=True, check=True, timeout=30)
    project = Path(attendee.home) / "projects/ux-refresh"
    manifest = project / ".agents/workshop-skills.json"
    prior = json.loads(manifest.read_text())
    for name in ("workshop-design-studio", "databricks-app-design", "promote"):
        old = project / ".agents/skills" / name
        old.mkdir()
        (old / "SKILL.md").write_text("old managed UX")
        (project / ".claude/skills" / name).symlink_to("../../.agents/skills/" + name)
        prior["skills"][name] = "previous-release"
    manifest.write_text(json.dumps(prior))
    with (project / "AGENTS.md").open("a") as handle:
        handle.write("\nAttendee note: use kilograms.\n")
    subprocess.run(["bash", helper, "ux-refresh"], env=env, capture_output=True, check=True, timeout=30)
    for name in ("workshop-design-studio", "databricks-app-design", "promote"):
        assert not (project / ".agents/skills" / name).exists()
        assert not (project / ".claude/skills" / name).is_symlink()
    assert "Attendee note: use kilograms." in (project / "AGENTS.md").read_text()
    assert (project / ".agents/skills/impeccable/SKILL.md").is_file()


@pytest.mark.parametrize("name", ["../other", "a/b", ".", "space name", "-bad"])
def test_helper_rejects_invalid_names(tmp_path, name):
    from server import user_content as content

    result = subprocess.run(["bash", str(Path(content._ASSETS) / "bin/workshop-init-project"), name],
                            env={**os.environ, "HOME": str(tmp_path)}, capture_output=True, text=True)
    assert result.returncode == 2
    assert result.stdout == ""
    assert not (tmp_path / "projects").exists()
