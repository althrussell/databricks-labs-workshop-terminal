"""Per-user content provisioning and terminal topic detection."""

import json
import os
import subprocess
import time
import uuid

from .conftest import ALICE

# Canonical AppKit mandate sentence — must appear verbatim in every memory
# channel an agent/harness can read (home CLAUDE.md + AGENTS.md, and the
# committed project-level CLAUDE.md + AGENTS.md). The single-source assertion
# fails loud if a skills refresh or instruction edit ever drops it.
APPKIT_MANDATE = "AppKit is the required baseline for every app."

# Skill names the mandate must use, and names it must never use. A mandate that
# points an agent at a skill that no longer exists is worse than no mandate: the
# agent finds nothing and silently improvises a Python framework instead.
CANONICAL_APP_SKILLS = (
    "databricks-apps",
    "databricks-app-design",
    "workshop-design-studio",
)

# Where the Databricks CLI tracks which skills it considers installed.
_AITOOLS_STATE = os.path.join(".databricks", "aitools", "skills", ".state.json")
RETIRED_SKILL_NAMES = (
    "databricks-bundles",
    "databricks-config",
    "databricks-genie",
    "databricks-lakebase-autoscale",
    "databricks-lakebase-provisioned",
    "databricks-spark-declarative-pipelines",
    "spark-python-data-source",
    "7-appkit-ux",
)


def _provisioned_home(client, monkeypatch):
    from server import credentials, user_content
    import server.main as main
    from server.users import user_manager

    monkeypatch.setenv("WORKSHOP_PAT", "dapi-test-token")
    # A provisioned HOME is only fully written when the credential manager can
    # hand over a token: the files that carry one — `.claude/settings.json`
    # among them — are skipped otherwise. Serving the token directly makes
    # that deterministic.
    # Without it the outcome depends on whether an earlier test left a
    # validated credential cached on the module singleton, which is how
    # test_auto_mode_defaults came to pass in a full run and fail on its own.
    monkeypatch.setattr(
        credentials.credential_manager, "token", lambda: "dapi-test-token"
    )
    monkeypatch.setattr(
        main.install,
        "ready",
        lambda: {"claude": True, "codex": True, "omnigent": True},
    )
    monkeypatch.setattr(main.agents, "launch_command", lambda _agent: ["/bin/bash"])
    user_content._provisioned.discard("alice@example.com")
    resp = client.post("/api/sessions", json={"agent_id": "claude"}, headers=ALICE)
    assert resp.status_code == 200
    return user_manager.get("alice@example.com").home


def test_instructions_written_with_coach(client, monkeypatch):
    home = _provisioned_home(client, monkeypatch)
    claude_md = open(os.path.join(home, ".claude", "CLAUDE.md")).read()
    assert "Workshop Edition" in claude_md
    assert "workshop-lab-coach" in claude_md  # coach appended by default

    agents_md = open(os.path.join(home, ".codex", "AGENTS.md")).read()
    assert agents_md.startswith("# Codex Agent Instructions")
    assert "workshop-lab-coach" in agents_md


def test_appkit_mandate_in_home_memory(client, monkeypatch):
    # The mandate must reach Claude (CLAUDE.md) and Codex (AGENTS.md) at the
    # home/global scope.
    home = _provisioned_home(client, monkeypatch)
    claude_md = open(os.path.join(home, ".claude", "CLAUDE.md")).read()
    agents_md = open(os.path.join(home, ".codex", "AGENTS.md")).read()
    assert APPKIT_MANDATE in claude_md
    assert APPKIT_MANDATE in agents_md


def test_mandate_names_canonical_app_skills_and_no_retired_ones(client, monkeypatch):
    home = _provisioned_home(client, monkeypatch)
    channels = {
        "home CLAUDE.md": os.path.join(home, ".claude", "CLAUDE.md"),
        "home AGENTS.md": os.path.join(home, ".codex", "AGENTS.md"),
        "project memory template": os.path.join(
            home, ".config", "workshop", "project-memory.md"
        ),
    }
    for label, path in channels.items():
        text = open(path).read()
        for skill in CANONICAL_APP_SKILLS:
            assert skill in text, f"{label} does not name {skill}"
        for retired in RETIRED_SKILL_NAMES:
            assert retired not in text, f"{label} still names retired {retired}"


_MEMORY_CHANNELS = (
    (".claude", "CLAUDE.md"),
    (".codex", "AGENTS.md"),
    (".config", "workshop", "project-memory.md"),
)


def test_mandate_ships_on_a_live_url_not_a_browser_suite(client, monkeypatch):
    """The ship gate is typecheck, deploy, open the URL.

    It used to be `databricks apps validate`, which runs Playwright and pulls
    Chromium onto a cold workshop box. For a standalone game with no data that
    is minutes of download and a smoke test to rewrite, all before the attendee
    can see anything — so the gate now stops at the cheap check that actually
    prevents a failed deploy.
    """
    home = _provisioned_home(client, monkeypatch)
    for parts in _MEMORY_CHANNELS:
        text = open(os.path.join(home, *parts)).read()
        assert "tsc --noEmit" in text
        assert "the URL loads" in text or "open the URL" in text


def test_no_memory_channel_puts_playwright_on_the_critical_path(client, monkeypatch):
    """Agents follow the written gate literally — the Space Invaders run put
    "update smoke test selectors" and "run validate + design gate" on its todo
    list because the instructions said to. Every channel has to carry the
    override, or the one that does not silently restores the old behaviour."""
    home = _provisioned_home(client, monkeypatch)
    for parts in _MEMORY_CHANNELS:
        text = open(os.path.join(home, *parts)).read()
        assert "Do not" in text and "databricks apps validate" in text, (
            "the override has to name the command it is overriding"
        )
        assert "workshop-design-gate" not in text
        assert "playwright.visual.spec.ts" not in text


def test_no_design_gate_helper_is_installed(client, monkeypatch):
    """The blocking visual gate is gone, helper included. Leaving the binary on
    PATH would let an agent rediscover it and reintroduce the block."""
    home = _provisioned_home(client, monkeypatch)

    assert not os.path.exists(
        os.path.join(home, ".local", "bin", "workshop-design-gate")
    )


def test_no_tdd_subagents_are_installed(client, monkeypatch):
    """The PRD -> failing-tests -> implement -> review chain turned "build me an
    app" into an interview plus a test suite. The directory is still created and
    emptied, because a HOME can outlive a deploy and a stale definition would
    keep the old behaviour running."""
    home = _provisioned_home(client, monkeypatch)
    agents = os.path.join(home, ".claude", "agents")

    assert os.path.isdir(agents)
    assert [name for name in os.listdir(agents) if name.endswith(".md")] == []


def test_project_helper_installed(client, monkeypatch):
    home = _provisioned_home(client, monkeypatch)

    helper = os.path.join(home, ".local", "bin", "workshop-init-project")
    assert os.path.isfile(helper)
    assert os.access(helper, os.X_OK)

    template = os.path.join(home, ".config", "workshop", "project-memory.md")
    assert APPKIT_MANDATE in open(template).read()


def test_project_helper_commits_appkit_memory(client, monkeypatch, tmp_path):
    """Running the helper must produce a project whose CLAUDE.md and AGENTS.md
    are committed (so they propagate into Omnigent's git worktrees) and carry
    the mandate."""
    home = _provisioned_home(client, monkeypatch)
    helper = os.path.join(home, ".local", "bin", "workshop-init-project")

    # Isolated HOME so we exercise the helper without the provisioned home's
    # post-commit workspace-sync hook. The template is the contract the
    # provisioned home installs.
    fake_home = tmp_path / "attendee"
    (fake_home / ".config" / "workshop").mkdir(parents=True)
    src_template = os.path.join(home, ".config", "workshop", "project-memory.md")
    (fake_home / ".config" / "workshop" / "project-memory.md").write_text(
        open(src_template).read()
    )
    env = {
        "HOME": str(fake_home),
        "PATH": os.environ["PATH"],
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_AUTHOR_NAME": "Attendee",
        "GIT_AUTHOR_EMAIL": "attendee@example.com",
        "GIT_COMMITTER_NAME": "Attendee",
        "GIT_COMMITTER_EMAIL": "attendee@example.com",
    }
    out = subprocess.run(
        ["bash", helper, "my-app"], env=env, capture_output=True, text=True, timeout=30
    )
    assert out.returncode == 0, out.stderr
    project = fake_home / "projects" / "my-app"
    assert APPKIT_MANDATE in (project / "CLAUDE.md").read_text()
    assert APPKIT_MANDATE in (project / "AGENTS.md").read_text()

    # Both files must be committed (tracked) — untracked files do not propagate
    # into the worktrees Omnigent's sub-agents run in.
    tracked = subprocess.run(
        ["git", "-C", str(project), "ls-files"],
        env=env, capture_output=True, text=True, timeout=30,
    ).stdout.split()
    assert "CLAUDE.md" in tracked and "AGENTS.md" in tracked


def test_project_helper_always_writes_a_readme(client, monkeypatch, tmp_path):
    """The lead-quality backstop for a workshop that no longer auto-generates
    documents. `artifacts` classifies any README as a `readme`-kind artifact, so
    writing one at init means every session contributes a titled artifact and a
    statement of purpose — including the sessions that never ask for docs.

    Near-zero cost, and it doubles as the attendee's take-home reminder once the
    workspace is torn down.
    """
    home = _provisioned_home(client, monkeypatch)
    helper = os.path.join(home, ".local", "bin", "workshop-init-project")

    fake_home = tmp_path / "attendee"
    (fake_home / ".config" / "workshop").mkdir(parents=True)
    env = {
        "HOME": str(fake_home),
        "PATH": os.environ["PATH"],
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_AUTHOR_NAME": "Attendee",
        "GIT_AUTHOR_EMAIL": "attendee@example.com",
        "GIT_COMMITTER_NAME": "Attendee",
        "GIT_COMMITTER_EMAIL": "attendee@example.com",
    }
    out = subprocess.run(
        ["bash", helper, "fraud-scoring"],
        env=env, capture_output=True, text=True, timeout=30,
    )
    assert out.returncode == 0, out.stderr

    readme = fake_home / "projects" / "fraud-scoring" / "README.md"
    assert readme.is_file(), "every project must start with a README"
    body = readme.read_text()
    assert "fraud-scoring" in body, "the project name is the minimum signal"
    # The two things the agent is told to keep current.
    assert "What this is for" in body
    assert "Live URL" in body

    tracked = subprocess.run(
        ["git", "-C", str(readme.parent), "ls-files"],
        env=env, capture_output=True, text=True, timeout=30,
    ).stdout.split()
    assert "README.md" in tracked


def _attendee_env(home, tmp_path, with_template=True):
    """An isolated HOME with the workshop template, as the helper expects."""
    fake_home = tmp_path / "attendee"
    (fake_home / ".config" / "workshop").mkdir(parents=True, exist_ok=True)
    if with_template:
        src = os.path.join(home, ".config", "workshop", "project-memory.md")
        (fake_home / ".config" / "workshop" / "project-memory.md").write_text(
            open(src).read()
        )
    return fake_home, {
        "HOME": str(fake_home),
        "PATH": os.environ["PATH"],
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_AUTHOR_NAME": "Attendee",
        "GIT_AUTHOR_EMAIL": "attendee@example.com",
        "GIT_COMMITTER_NAME": "Attendee",
        "GIT_COMMITTER_EMAIL": "attendee@example.com",
    }


def test_the_helper_adopts_a_directory_a_scaffold_already_wrote(
    client, monkeypatch, tmp_path
):
    """The failure this exists to prevent, reproduced.

    `databricks apps init` always creates a subdirectory named after the app and
    refuses to write into a directory that already exists. So an agent that made
    the project first could only scaffold by nesting and then flattening with
    `mv`, which replaced the committed CLAUDE.md — the workshop's rules — with
    the scaffold's generic one. Live, that cost an attendee's project memory and
    was only caught by chance.

    Adopting an existing directory removes the reason to ever run that `mv`.
    """
    home = _provisioned_home(client, monkeypatch)
    helper = os.path.join(home, ".local", "bin", "workshop-init-project")
    fake_home, env = _attendee_env(home, tmp_path)

    scaffolded = fake_home / "projects" / "space-invaders"
    (scaffolded / "client").mkdir(parents=True)
    (scaffolded / "client" / "main.tsx").write_text("render()\n")
    (scaffolded / "CLAUDE.md").write_text("# AppKit\nUse AppKit conventions.\n")
    (scaffolded / "README.md").write_text("# space-invaders\nBuilt with AppKit.\n")

    out = subprocess.run(
        ["bash", helper, "space-invaders"],
        env=env, capture_output=True, text=True, timeout=60,
    )
    assert out.returncode == 0, out.stderr

    assert not (scaffolded / "space-invaders").exists(), "must not nest"
    assert (scaffolded / "client" / "main.tsx").is_file(), "scaffold must survive"

    claude = (scaffolded / "CLAUDE.md").read_text()
    assert APPKIT_MANDATE in claude, "the workshop rules must be present"
    assert "Use AppKit conventions." in claude, (
        "the scaffold's own notes are kept, not destroyed — losing them is what "
        "made the live near-miss worth repairing by hand"
    )

    readme = (scaffolded / "README.md").read_text()
    assert "Built with AppKit." in readme, "the scaffold's README survives"
    assert "Live URL" in readme, "and still gains the take-home prompt"


def test_seeding_an_adopted_project_twice_does_not_duplicate_it(
    client, monkeypatch, tmp_path
):
    """Adoption has to be idempotent or a retry silently doubles the memory file."""
    home = _provisioned_home(client, monkeypatch)
    helper = os.path.join(home, ".local", "bin", "workshop-init-project")
    fake_home, env = _attendee_env(home, tmp_path)

    for _ in range(2):
        out = subprocess.run(
            ["bash", helper, "twice"],
            env=env, capture_output=True, text=True, timeout=60,
        )
        assert out.returncode == 0, out.stderr

    project = fake_home / "projects" / "twice"
    assert (project / "CLAUDE.md").read_text().count(APPKIT_MANDATE) == 1
    assert (project / "README.md").read_text().count("Live URL") == 1


def test_a_failed_appkit_scaffold_still_leaves_a_usable_project(
    client, monkeypatch, tmp_path
):
    """Same stance as the missing-git-identity case: creating the project matters
    more than the step that failed. A network or npm failure during scaffolding
    must not leave the attendee with nothing to build in."""
    home = _provisioned_home(client, monkeypatch)
    helper = os.path.join(home, ".local", "bin", "workshop-init-project")
    fake_home, env = _attendee_env(home, tmp_path)

    # A `databricks` on PATH that fails, standing in for a scaffold that cannot
    # complete. Shadowing it keeps the test offline and deterministic.
    shim = tmp_path / "shim"
    shim.mkdir()
    (shim / "databricks").write_text("#!/bin/sh\necho 'boom' >&2\nexit 1\n")
    (shim / "databricks").chmod(0o755)
    env = {**env, "PATH": f"{shim}:{env['PATH']}"}

    out = subprocess.run(
        ["bash", helper, "unlucky", "--appkit"],
        env=env, capture_output=True, text=True, timeout=60,
    )
    assert out.returncode == 0, out.stderr
    project = fake_home / "projects" / "unlucky"
    assert (project / "CLAUDE.md").is_file()
    assert APPKIT_MANDATE in (project / "CLAUDE.md").read_text()
    assert out.stdout.strip().endswith("projects/unlucky")


def test_the_appkit_flag_scaffolds_into_the_project_root(
    client, monkeypatch, tmp_path
):
    """`--output-dir` is the parent, not the target.

    Getting that backwards is precisely what produced `<name>/<name>` live, so
    pin the invocation rather than trusting the flag name to keep its meaning.
    """
    home = _provisioned_home(client, monkeypatch)
    helper = os.path.join(home, ".local", "bin", "workshop-init-project")
    fake_home, env = _attendee_env(home, tmp_path)

    shim = tmp_path / "shim"
    shim.mkdir()
    recorded = tmp_path / "argv"
    (shim / "databricks").write_text(
        f'#!/bin/sh\nprintf "%s\\n" "$@" > "{recorded}"\nexit 0\n'
    )
    (shim / "databricks").chmod(0o755)
    env = {**env, "PATH": f"{shim}:{env['PATH']}"}

    out = subprocess.run(
        ["bash", helper, "pinned", "--appkit", "--", "--features", "analytics"],
        env=env, capture_output=True, text=True, timeout=60,
    )
    assert out.returncode == 0, out.stderr

    argv = recorded.read_text().split("\n")
    assert argv[:2] == ["apps", "init"]
    assert "--output-dir" in argv
    assert argv[argv.index("--output-dir") + 1] == str(fake_home / "projects"), (
        "the parent, so the scaffold lands at projects/<name> and never nests"
    )
    assert "--features" in argv and "analytics" in argv, "flags pass through"


def test_the_readme_survives_a_missing_git_identity(client, monkeypatch, tmp_path):
    """Creating the project matters more than committing it. If git cannot
    resolve an identity the helper must still leave the attendee with a usable
    project rather than aborting under `set -e`."""
    home = _provisioned_home(client, monkeypatch)
    helper = os.path.join(home, ".local", "bin", "workshop-init-project")

    fake_home = tmp_path / "attendee"
    fake_home.mkdir(parents=True)
    env = {
        "HOME": str(fake_home),
        "PATH": os.environ["PATH"],
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "EMAIL": "",
    }
    out = subprocess.run(
        ["bash", helper, "still-works"],
        env=env, capture_output=True, text=True, timeout=30,
    )
    assert out.returncode == 0, out.stderr
    assert (fake_home / "projects" / "still-works" / "README.md").is_file()
    assert out.stdout.strip().endswith("projects/still-works")


def test_coach_disabled_by_env(client, monkeypatch):
    monkeypatch.setenv("LAB_COACH", "false")
    home = _provisioned_home(client, monkeypatch)
    claude_md = open(os.path.join(home, ".claude", "CLAUDE.md")).read()
    assert "workshop-lab-coach" not in claude_md


def test_skills_installed(client, monkeypatch):
    home = _provisioned_home(client, monkeypatch)

    # Every harness reads its own directory: Claude ~/.claude/skills, Codex (and
    # Omnigent's Codex worker, which shares CODEX_HOME) ~/.codex/skills. This is
    # the layout `databricks aitools install` produces.
    for relative in (".claude/skills", ".codex/skills"):
        skill = os.path.join(home, relative, "databricks-docs")
        assert os.path.islink(skill), relative
        assert os.path.isfile(os.path.join(skill, "SKILL.md")), relative


def test_databricks_cli_sees_the_skills_we_installed(client, monkeypatch, tmp_path):
    """The CLI reports skills as installed only from its own state file.

    It never inspects the harness directories, so placing skills ourselves left
    ``databricks aitools`` telling attendees "no skills installed. Run
    'databricks aitools install'" while Claude and Codex were loading them fine.
    """
    from server.bootstrap import install

    shared = install.config.shared_prefix()
    upstream = ["databricks-docs", "databricks-dabs"]
    _write_skills_stamp(shared, upstream)

    home = _provisioned_home(client, monkeypatch)
    state = json.load(open(os.path.join(home, _AITOOLS_STATE)))

    assert state["schema_version"] == 1
    assert state["release"] == install.SKILLS_REF
    assert set(state["skills"]) == set(upstream)
    # Versionless upstream skills are recorded as 0.0.1, matching the CLI.
    assert state["skills"]["databricks-dabs"] == "0.0.1"


def test_vendored_workflow_skills_are_not_declared_to_the_cli(
    client, monkeypatch, tmp_path
):
    """Only upstream skills belong in the CLI's state.

    The shared tree also holds our vendored workflow skills, and no name
    pattern separates them (``databricks-app-apx`` is vendored, not upstream),
    so the set comes from install-time provenance.
    """
    from server.bootstrap import install

    _write_skills_stamp(install.config.shared_prefix(), ["databricks-docs"])

    home = _provisioned_home(client, monkeypatch)
    state = json.load(open(os.path.join(home, _AITOOLS_STATE)))

    assert set(state["skills"]) == {"databricks-docs"}
    assert "brainstorming" not in state["skills"]


def test_no_aitools_state_without_upstream_provenance(client, monkeypatch):
    """Vendored fallback: claiming an upstream release we did not install would
    make the CLI report skills as current when they are the offline copy."""
    from server.bootstrap import install
    from server.users import user_manager

    # The shared prefix and the attendee home both outlive a single test, so
    # clear what a sibling left behind before asserting on a fresh provision.
    for stale in (
        install._skills_stamp_path(),
        os.path.join(
            user_manager.get("alice@example.com").home,
            _AITOOLS_STATE,
        ),
    ):
        if os.path.exists(stale):
            os.unlink(stale)

    home = _provisioned_home(client, monkeypatch)
    assert not os.path.exists(os.path.join(home, _AITOOLS_STATE))


def _write_skills_stamp(shared: str, names: list[str]) -> None:
    from server.bootstrap import install

    os.makedirs(shared, exist_ok=True)
    with open(install._skills_stamp_path(), "w") as f:
        json.dump({"upstream_skills": names}, f)


def test_a_harness_owned_skills_directory_still_receives_the_skills(
    client, monkeypatch, tmp_path
):
    """A real ~/.codex/skills (harness- or attendee-created) used to shadow the
    whole-directory symlink, leaving Codex with no Databricks skills at all."""
    from server import user_content

    home = tmp_path / "attendee"
    own = home / ".codex" / "skills" / "my-own-skill"
    own.mkdir(parents=True)
    (own / "SKILL.md").write_text("mine")

    user_content._link_skills(type("U", (), {"home": str(home), "email": "a@b"})())

    assert (own / "SKILL.md").read_text() == "mine"  # attendee's copy survives
    assert os.path.islink(home / ".codex" / "skills" / "databricks-docs")
    assert os.path.islink(home / ".claude" / "skills" / "databricks-docs")


def test_git_identity_and_sync_hook(client, monkeypatch):
    home = _provisioned_home(client, monkeypatch)
    gitconfig = open(os.path.join(home, ".gitconfig")).read()
    assert "email = alice@example.com" in gitconfig

    hook = open(os.path.join(home, ".githooks", "post-commit")).read()
    assert "/Workspace/Users/alice@example.com/projects" in hook
    assert "databricks sync" in hook


def _clear_sync_status():
    """Drop any record left by an earlier case.

    The attendee home is shared across tests in a session, so a stale record
    reads as this test's result -- which would let a case assert a state the
    hook never produced.
    """
    from server import user_content
    from server.users import user_manager

    path = user_content.workspace_sync_status_path(
        user_manager.get("alice@example.com")
    )
    if os.path.exists(path):
        os.unlink(path)


def _commit_with_hook(home, tmp_path, *, cli_exit, cli_stderr="boom"):
    """Commit in a real repo under ~/projects and let the real hook fire.

    Runs the shipped hook rather than asserting on its text. The bug this covers
    was never in what the script said -- it was that the script's outcome went
    nowhere -- so a test that greps the template would have passed throughout.
    """
    shim = tmp_path / "shim"
    shim.mkdir(exist_ok=True)
    (shim / "databricks").write_text(
        f"#!/bin/sh\necho '{cli_stderr}' >&2\nexit {cli_exit}\n"
    )
    (shim / "databricks").chmod(0o755)
    _clear_sync_status()

    # realpath because the hook guards on "$HOME/projects"/* and git reports the
    # physical path: on macOS the temp home is /var/... while git resolves
    # /private/var/..., so an unresolved HOME makes the hook exit before syncing
    # and the test would pass for the wrong reason.
    real_home = os.path.realpath(home)
    repo = os.path.join(real_home, "projects", f"demo-{uuid.uuid4().hex[:8]}")
    os.makedirs(repo, exist_ok=True)
    env = {
        "HOME": real_home,
        "PATH": f"{shim}:{os.environ['PATH']}",
        "GIT_CONFIG_GLOBAL": os.path.join(home, ".gitconfig"),
        "GIT_AUTHOR_NAME": "Attendee",
        "GIT_AUTHOR_EMAIL": "alice@example.com",
        "GIT_COMMITTER_NAME": "Attendee",
        "GIT_COMMITTER_EMAIL": "alice@example.com",
    }
    with open(os.path.join(repo, "app.py"), "w") as handle:
        handle.write("print('hi')\n")
    for argv in (
        ["git", "init", "-q"],
        ["git", "add", "-A"],
        ["git", "commit", "-q", "-m", "work"],
    ):
        done = subprocess.run(
            argv, cwd=repo, env=env, capture_output=True, text=True, timeout=60
        )
        assert done.returncode == 0, done.stderr

    from server import user_content
    from server.users import user_manager

    user = user_manager.get("alice@example.com")
    # The sync is backgrounded so the attendee's commit never blocks on the
    # network, which means the record lands after the hook has already returned.
    deadline = time.time() + 30
    while time.time() < deadline:
        status = user_content.workspace_sync_status(user)
        if status["state"] != "never":
            return status
        time.sleep(0.1)
    return user_content.workspace_sync_status(user)


def test_a_failed_workspace_sync_is_recorded_instead_of_only_logged(
    client, monkeypatch, tmp_path
):
    """The hook ran as the app service principal, which has no write access to
    the attendee's Workspace home, so it failed on every commit of a live event
    and said so only in ~/.sync.log -- inside a container nobody can open."""
    home = _provisioned_home(client, monkeypatch)

    status = _commit_with_hook(
        home, tmp_path, cli_exit=1, cli_stderr="PERMISSION_DENIED: cannot write"
    )

    assert status["state"] == "failed"
    assert status["exit"] == 1
    assert "PERMISSION_DENIED" in status["detail"]


def test_a_successful_workspace_sync_records_no_failure_detail(
    client, monkeypatch, tmp_path
):
    home = _provisioned_home(client, monkeypatch)

    status = _commit_with_hook(home, tmp_path, cli_exit=0, cli_stderr="")

    assert status["state"] == "ok"
    assert status["exit"] == 0
    assert status["detail"] == ""


def test_a_failed_sync_is_reported_without_taking_the_workshop_down(
    client, monkeypatch, tmp_path
):
    """Red, but still serving. What a failed sync costs is the attendee's route
    to their work from outside the terminal; refusing to serve would cost them
    the workshop to protect a copy that DATA_ROOT already holds."""
    home = _provisioned_home(client, monkeypatch)
    _commit_with_hook(home, tmp_path, cli_exit=1)
    # The instance-level report speaks for the bound attendee, which is the
    # identity Control Tower injects when it provisions the app.
    monkeypatch.setenv("WORKSHOP_ATTENDEE_EMAIL", "alice@example.com")

    from server import readiness

    report = readiness.evaluate_runtime()
    check = report["checks"]["workspace_sync"]

    assert check["soft"] is True
    assert check["state"] == "red"
    assert check["ok"] is False
    assert "exists only inside the terminal" in check["detail"]


def test_a_recorded_failure_still_reports_after_the_registry_is_lost(
    client, monkeypatch, tmp_path
):
    """The registry is process-local; the record is on DATA_ROOT and outlives
    the container. Reading the failure through the registry would report the
    reassuring "nothing committed yet" over the top of it, for the whole window
    after a restart -- when an operator is most likely to be looking."""
    home = _provisioned_home(client, monkeypatch)
    _commit_with_hook(home, tmp_path, cli_exit=1)
    monkeypatch.setenv("WORKSHOP_ATTENDEE_EMAIL", "alice@example.com")

    from server import readiness
    from server.users import user_manager

    user_manager._users.clear()  # the restart: registry empty, disk intact

    check = readiness.evaluate_runtime()["checks"]["workspace_sync"]

    assert check["state"] == "red"


def test_a_provisioned_home_is_the_home_readiness_computes(client, monkeypatch):
    """Pins the invariant the two tests above depend on, and that broke them.

    ``User.home`` is fixed at construction from ``config.users_root()``, while
    ``readiness`` recomputes the path from the same function at call time. Those
    agree in production, where the root never moves — and disagreed across tests,
    because the registry is a process-wide singleton and modules repoint the root
    at their own ``tmp_path``. A stale ``alice`` cached by an earlier module sent
    the hook's record to a directory readiness never looked in, so both sync
    tests reported ``amber`` when run after ``test_omnigent_remote`` and ``red``
    when run alone.

    Asserted here rather than left to the ``conftest`` fixture alone: deleting
    that fixture should fail a test that explains why it exists, not quietly
    restore an order-dependent suite that nobody trusts during event week.
    """
    from server import config
    from server.users import email_slug, user_manager

    home = _provisioned_home(client, monkeypatch)

    assert home == os.path.join(config.users_root(), email_slug("alice@example.com"))
    assert user_manager.peek("alice@example.com").home == home


def test_no_commits_yet_is_not_reported_as_a_sync_failure(client, monkeypatch):
    """Otherwise operators learn to ignore the field on exactly the instances
    where it later matters."""
    from server import user_content
    from server.users import user_manager

    _provisioned_home(client, monkeypatch)
    _clear_sync_status()
    status = user_content.workspace_sync_status(
        user_manager.get("alice@example.com")
    )

    assert status["state"] == "never"
    assert status["exit"] is None


def test_a_half_written_sync_record_does_not_break_the_readiness_endpoint(
    client, monkeypatch
):
    """The record is written by a shell hook on the far side of a network call,
    so truncation is a normal outcome, not an exceptional one."""
    from server import user_content
    from server.users import user_manager

    _provisioned_home(client, monkeypatch)
    user = user_manager.get("alice@example.com")
    path = user_content.workspace_sync_status_path(user)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as handle:
        handle.write("at=nonsense\nexit=\ndet")

    assert user_content.workspace_sync_status(user)["state"] == "never"


def test_presence_carries_the_sync_state_per_attendee(
    client, monkeypatch, as_admin
):
    _provisioned_home(client, monkeypatch)
    _clear_sync_status()

    body = client.get(
        "/api/admin/presence", headers={"X-Forwarded-Email": "op@example.com"}
    ).json()

    assert body["users"][0]["workspace_sync"]["state"] == "never"


def test_claude_json_mcp_off_by_default(client, monkeypatch):
    # P1-21: public MCP egress is opt-in; default is no external MCP servers.
    monkeypatch.delenv("ENABLE_PUBLIC_MCP", raising=False)
    home = _provisioned_home(client, monkeypatch)
    data = json.load(open(os.path.join(home, ".claude.json")))
    assert data["hasCompletedOnboarding"] is True
    assert data["mcpServers"] == {}


def test_claude_json_mcp_opt_in(client, monkeypatch):
    monkeypatch.setenv("ENABLE_PUBLIC_MCP", "true")
    home = _provisioned_home(client, monkeypatch)
    data = json.load(open(os.path.join(home, ".claude.json")))
    assert "deepwiki" in data["mcpServers"] and "exa" in data["mcpServers"]


def test_launch_never_injects_prompts(monkeypatch):
    # Coaching context belongs in memory files, never in a fabricated user
    # message — the launch command must be the bare CLI.
    from server import agents

    agent = agents.get_agent("claude")
    assert "greeting" not in agent
    assert agents.launch_command(agent) == ["/bin/bash", "-c", "exec claude"]


def test_auto_mode_defaults(client, monkeypatch):
    from server import cli_config
    from server.users import user_manager

    home = _provisioned_home(client, monkeypatch)
    settings = json.load(open(os.path.join(home, ".claude", "settings.json")))
    assert settings["permissions"]["defaultMode"] == "bypassPermissions"
    assert settings["skipDangerousModePermissionPrompt"] is True

    codex_toml = open(os.path.join(home, ".codex", "config.toml")).read()
    assert 'approval_policy = "never"' in codex_toml
    assert 'sandbox_mode = "danger-full-access"' in codex_toml

    # Opt out restores safe prompts.
    monkeypatch.setenv("WORKSHOP_AUTO_MODE", "false")
    user = user_manager.get("alice@example.com")
    cli_config.configure_claude(user, "tok")
    cli_config.configure_codex(user, "tok")
    settings = json.load(open(os.path.join(home, ".claude", "settings.json")))
    assert settings.get("permissions", {}).get("defaultMode") != "bypassPermissions"
    codex_toml = open(os.path.join(home, ".codex", "config.toml")).read()
    assert "approval_policy" not in codex_toml


def test_topic_detection_flags_user(client, monkeypatch):
    from server.content import content_service
    from server.main import _observe_output
    from server.sessions import session_manager
    from server.users import user_manager

    _provisioned_home(client, monkeypatch)
    session = session_manager.list_for("alice@example.com")[0]

    assert "lakebase" in content_service.scan_topics(
        "$ databricks lakebase create-database-instance workshop-db"
    )
    _observe_output(session, "$ psql -h inst.database.cloud.databricks.com -c '\\dt'")
    user = user_manager.get("alice@example.com")
    assert "lakebase" in user.topics

    nuggets = client.get("/api/nuggets", headers=ALICE).json()["nuggets"]
    matched = [n for n in nuggets if n["matched_topic"] == "lakebase"]
    assert matched and matched[0]["id"] == "topic-lakebase"


def test_talking_about_a_product_is_not_using_it(client, monkeypatch):
    """A topic is not cosmetic — it becomes `products` on the insight payload.

    An attendee built a Space Invaders clone: no data, no persistence, nothing
    beyond an app. The brief that reached an account team said they had touched
    Genie and Lakebase, because the agent *said the words* while explaining it
    was skipping both, and because the skills it loaded quote those commands as
    examples. Every claim below was made about someone who did none of it.
    """
    from server.content import content_service

    monkeypatch.setenv("WORKSHOP_PAT", "dapi-test-token")

    declined = (
        "This app has zero data and zero persistence, so I skipped the Data "
        "Access Decision Gate and the Lakebase question entirely. No Genie "
        "space is needed either, and there is no Unity Catalog object to make."
    )
    assert content_service.scan_topics(declined) == set(), (
        "prose that declines a product must not report it as used"
    )

    offered = "You could add a Genie space later, or a Lakebase database."
    assert content_service.scan_topics(offered) == set()

    listing = "databricks-genie  databricks-lakebase  databricks-apps"
    assert content_service.scan_topics(listing) == set(), (
        "a directory listing of skill names is not activity"
    )


def test_quoted_commands_in_documentation_are_not_activity(client, monkeypatch):
    """The `databricks-apps` skill is loaded on every single app build, and it
    quotes both `--features lakebase` and `databricks genie list-spaces` as
    reference. Printing a skill is how an agent reads; it is not how an attendee
    builds. Backticks are the tell, and they never appear in real command output.
    """
    from server.content import content_service

    monkeypatch.setenv("WORKSHOP_PAT", "dapi-test-token")

    doc = (
        "## Project Structure (after `databricks apps init --features lakebase`)\n"
        "3. If creating, run `databricks genie create-space` to make the space.\n"
        "```bash\ndatabricks jobs create --json @job.json\n```\n"
    )
    assert content_service.scan_topics(doc) == set()

    # The same commands, actually run, must still register.
    assert content_service.scan_topics(
        "$ databricks apps init --features lakebase"
    ) == {"lakebase"}
    assert content_service.scan_topics("$ databricks genie create-space") == {"genie"}


def test_shipping_an_app_is_still_detected(client, monkeypatch):
    """The guard against over-correcting. Precision that loses the one product
    every attendee actually uses would make the brief useless in the other
    direction."""
    from server.content import content_service

    monkeypatch.setenv("WORKSHOP_PAT", "dapi-test-token")
    live = "Deployment complete — live at https://space-invaders.aws.databricksapps.com"
    assert "apps" in content_service.scan_topics(live)
    assert "build-complete" in content_service.scan_topics(live)
    assert "apps" in content_service.scan_topics("$ databricks bundle deploy -t dev")


def test_topic_detection_opt_out(client, monkeypatch):
    from server.main import _observe_output
    from server.sessions import session_manager
    from server.users import user_manager

    monkeypatch.setenv("TOPIC_DETECTION", "false")
    _provisioned_home(client, monkeypatch)
    session = session_manager.list_for("alice@example.com")[0]
    user = user_manager.get("alice@example.com")
    user.topics.clear()
    # Text that would definitely register with detection on, so the assertion
    # tests the opt-out rather than the sample happening not to match.
    _observe_output(session, "$ databricks lakebase create-database-instance db1")
    assert user.topics == {}
