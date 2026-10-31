"""W1: each harness discovers its own native Impeccable payload.

The supported installer writes a Claude payload (plus companion agents) and a
separate Codex payload. WT used to keep only the Codex copy and link it into
Claude's home as well. These tests use deliberately different fixtures and
check cold/warm/fallback/project/worktree delivery, overrides, stale links,
digest mismatch and rollback.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest

from server.bootstrap import impeccable
from server.bootstrap.artifacts import directory_checksum
from tests.plan_approval import approve_for_env

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def attendee(monkeypatch, tmp_path):
    from server import config
    from server.users import User

    monkeypatch.setattr(config, "users_root", lambda: str(tmp_path / "users"))
    monkeypatch.setattr(config, "shared_prefix", lambda: str(tmp_path / "shared"))
    user = User("alice@example.com")
    user.bootstrap_home()
    return user


def _tree(root: Path, *, claude: str = "claude bytes", codex: str = "codex bytes", agents: bool = True) -> Path:
    """A published skills tree with deliberately different provider payloads."""
    for name, text in (("impeccable", codex), ("databricks-apps", "shared skill")):
        (root / name).mkdir(parents=True)
        (root / name / "SKILL.md").write_text(text)
    stage = root.parent / f"{root.name}-stage"
    (stage / "claude").mkdir(parents=True)
    (stage / "claude/SKILL.md").write_text(claude)
    sources = {"claude": stage / "claude"}
    digests = {"codex": directory_checksum(root / "impeccable"), "claude": directory_checksum(stage / "claude")}
    if agents:
        (stage / "agents").mkdir()
        (stage / "agents/impeccable-finish-reviewer.md").write_text("reviewer")
        sources["claude_agents"] = stage / "agents"
        digests["claude_agents"] = directory_checksum(stage / "agents")
    impeccable.publish_payloads(root, sources, digests)
    return root


def _published(monkeypatch, tmp_path, **kwargs) -> Path:
    """Make the fixture tree the verified shared library."""
    from server.bootstrap import install

    tree = _tree(tmp_path / "shared/skills", **kwargs)
    monkeypatch.setattr(install, "skills_ready", lambda: True)
    return tree


def _link(attendee):
    from server import user_content

    user_content._link_skills(attendee)
    return json.loads((Path(attendee.home) / ".config/workshop/skill-routing.json").read_text())


def test_each_home_links_its_own_payload_and_the_audit_says_so(attendee, monkeypatch, tmp_path):
    tree = _published(monkeypatch, tmp_path)
    audit = _link(attendee)
    home = Path(attendee.home)
    assert os.path.realpath(home / ".claude/skills/impeccable") == str((tree / ".providers/claude/impeccable").resolve())
    assert os.path.realpath(home / ".agents/skills/impeccable") == str((tree / "impeccable").resolve())
    # A shared skill is the same copy for both.
    assert os.path.realpath(home / ".claude/skills/databricks-apps") == os.path.realpath(home / ".agents/skills/databricks-apps")
    assert (home / ".claude/agents/impeccable-finish-reviewer.md").read_text() == "reviewer"
    assert audit["harnesses"]["claude"]["status"] == "ok"
    assert audit["harnesses"]["claude"]["effective"]["provider"] == "claude"
    assert audit["harnesses"]["codex"]["status"] == "ok"
    assert audit["harnesses"]["codex"]["effective"]["provider"] == "codex"


def test_a_stale_managed_link_to_the_codex_payload_is_replaced(attendee, monkeypatch, tmp_path):
    tree = _published(monkeypatch, tmp_path)
    claude = Path(attendee.home) / ".claude/skills"
    claude.mkdir(parents=True, exist_ok=True)
    (claude / "impeccable").symlink_to(tree / "impeccable")  # the pre-W1 link
    assert _link(attendee)["harnesses"]["claude"]["status"] == "ok"
    assert os.path.realpath(claude / "impeccable").endswith(".providers/claude/impeccable")


def test_an_attendee_override_is_preserved_and_reported(attendee, monkeypatch, tmp_path):
    _published(monkeypatch, tmp_path)
    own = Path(attendee.home) / ".claude/skills/impeccable"
    own.mkdir(parents=True)
    (own / "SKILL.md").write_text("my own design skill")
    (Path(attendee.home) / ".claude/agents").mkdir(parents=True, exist_ok=True)
    (Path(attendee.home) / ".claude/agents/impeccable-finish-reviewer.md").write_text("mine")
    audit = _link(attendee)
    assert (own / "SKILL.md").read_text() == "my own design skill"
    assert (Path(attendee.home) / ".claude/agents/impeccable-finish-reviewer.md").read_text() == "mine"
    assert audit["harnesses"]["claude"]["status"] == "override"
    assert audit["harnesses"]["claude"]["effective"]["managed"] is False


def test_a_tree_without_a_claude_payload_never_sends_claude_the_codex_one(attendee, monkeypatch, tmp_path):
    from server.bootstrap import install

    tree = tmp_path / "shared/skills"
    (tree / "impeccable").mkdir(parents=True)
    (tree / "impeccable/SKILL.md").write_text("codex bytes, legacy tree with no routing")
    monkeypatch.setattr(install, "skills_ready", lambda: True)
    claude = Path(attendee.home) / ".claude/skills"
    claude.mkdir(parents=True, exist_ok=True)
    (claude / "impeccable").symlink_to(tree / "impeccable")
    audit = _link(attendee)
    assert not (claude / "impeccable").exists()
    assert os.path.realpath(Path(attendee.home) / ".agents/skills/impeccable") == str((tree / "impeccable").resolve())
    assert audit["harnesses"]["claude"]["status"] == "missing"


def test_a_payload_that_differs_from_its_routing_fails_safe(tmp_path):
    tree = _tree(tmp_path / "skills")
    (tree / ".providers/claude/impeccable/SKILL.md").write_text("tampered")
    with pytest.raises(RuntimeError, match="claude payload differs"):
        impeccable.verify_tree(tree)


def test_installer_digest_mismatch_leaves_the_published_tree_alone(tmp_path, monkeypatch):

    target = _tree(tmp_path / "skills")
    before = directory_checksum(target)
    prefix = tmp_path / "prefix"
    (prefix / "bin").mkdir(parents=True)
    bundle_root = tmp_path / "bundle"
    for provider in (".agents", ".claude"):
        skill = bundle_root / provider / "skills/impeccable"
        (skill / "scripts").mkdir(parents=True)
        (skill / "SKILL.md").write_text(f"bytes for {provider}")
        (skill / "scripts/VERSION").write_text(impeccable.ENGINE_VERSION + "\n")
    archive = Path(shutil.make_archive(str(tmp_path / "bundle"), "zip", bundle_root))
    import tarfile

    (tmp_path / "npm/package/cli/bin").mkdir(parents=True)
    (tmp_path / "npm/package/cli/bin/cli.js").write_text("shim")
    launcher = tmp_path / "launcher.tgz"
    with tarfile.open(launcher, "w:gz") as handle:
        handle.add(tmp_path / "npm/package", arcname="package")
    engine = tmp_path / "engine"
    engine.write_text("#!/bin/sh\n")
    entries = {
        "impeccable_npm_launcher": (str(launcher), {"version": impeccable.LAUNCHER_VERSION}),
        "impeccable_engine_linux_x64": (str(engine), {"version": impeccable.ENGINE_VERSION}),
        "impeccable_skill_bundle": (str(archive), {
            "version": impeccable.SKILL_VERSION,
            "content_sha256": directory_checksum(bundle_root / ".agents/skills/impeccable"),
            "claude_content_sha256": "0" * 64}),  # the reviewed Claude digest does not match
    }

    def installer(argv, *, cwd, env, **kwargs):
        for provider in (".claude", ".agents"):
            shutil.copytree(Path(env["IMPECCABLE_BUNDLE_PATH"]) / provider / "skills/impeccable",
                            Path(cwd) / provider / "skills/impeccable")
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(impeccable.subprocess, "run", installer)
    with pytest.raises(RuntimeError, match="claude skill differs"):
        impeccable.install_skill(str(target), str(prefix), entries.__getitem__, {})
    assert directory_checksum(target) == before


def test_rollback_restores_one_coherent_generation(tmp_path):
    from server.bootstrap import install

    target = tmp_path / "shared/skills"
    _tree(target, claude="claude v1", codex="codex v1")
    staged = _tree(tmp_path / "shared/staged", claude="claude v2", codex="codex v2")
    install._publish_skills_tree(str(staged), str(target))
    assert (target / ".providers/claude/impeccable/SKILL.md").read_text() == "claude v2"
    assert install.rollback_skills_tree(str(target))
    assert (target / ".providers/claude/impeccable/SKILL.md").read_text() == "claude v1"
    assert (target / "impeccable/SKILL.md").read_text() == "codex v1"
    impeccable.verify_tree(target)
    # The rolled-back generation stays available to roll forward.
    assert (Path(str(target) + install.LAST_GOOD_SUFFIX) / "impeccable/SKILL.md").read_text() == "codex v2"


def test_rollback_refuses_a_generation_whose_digests_no_longer_verify(tmp_path):
    from server.bootstrap import install

    target = tmp_path / "shared/skills"
    _tree(target, claude="claude v1")
    install._publish_skills_tree(str(_tree(tmp_path / "shared/staged", claude="claude v2")), str(target))
    last_good = Path(str(target) + install.LAST_GOOD_SUFFIX)
    (last_good / ".providers/claude/impeccable/SKILL.md").write_text("tampered")
    with pytest.raises(RuntimeError):
        install.rollback_skills_tree(str(target))
    assert (target / ".providers/claude/impeccable/SKILL.md").read_text() == "claude v2"


def test_packaged_fallback_delivers_both_payloads_cold(attendee, monkeypatch):
    """Before the shared library is verified, the package's own tree is used."""
    from server.bootstrap import install

    monkeypatch.setattr(install, "skills_ready", lambda: False)
    audit = _link(attendee)
    assert audit["harnesses"]["claude"]["status"] == "ok"
    assert audit["harnesses"]["codex"]["status"] == "ok"
    assets = ROOT / "assets/skills"
    assert os.path.realpath(Path(attendee.home) / ".claude/skills/impeccable") == str(
        (assets / impeccable.PROVIDER_PATHS["claude"]).resolve())


def test_a_project_and_its_worktree_carry_each_providers_bytes(attendee):
    from server import skill_routing, user_content

    user_content.provision(attendee)
    env = {**os.environ, "HOME": attendee.home, "GIT_CONFIG_GLOBAL": "/dev/null",
           "GIT_AUTHOR_NAME": "Attendee", "GIT_AUTHOR_EMAIL": "alice@example.com",
           "GIT_COMMITTER_NAME": "Attendee", "GIT_COMMITTER_EMAIL": "alice@example.com"}
    helper = str(Path(user_content._ASSETS) / "bin/workshop-init-project")
    approve_for_env(env, "routed")
    subprocess.run(["bash", helper, "routed"], env=env, capture_output=True, check=True, timeout=60)
    project = Path(attendee.home) / "projects/routed"
    audit = skill_routing.audit(attendee.home, project, tree=str(ROOT / "assets/skills"))
    assert audit["harnesses"]["codex"]["effective"]["scope"] == "project"
    assert audit["harnesses"]["codex"]["status"] == "ok"
    claude_project = next(c for c in audit["harnesses"]["claude"]["candidates"] if c["scope"] == "project")
    assert claude_project["provider"] == "claude" and claude_project["managed"] is True
    manifest = json.loads((project / ".agents/workshop-skills.json").read_text())
    assert "impeccable" in manifest["claude"] and manifest["agents"]
    committed = subprocess.run(["git", "-C", str(project), "ls-files"], env=env, capture_output=True,
                               text=True, check=True).stdout.splitlines()
    assert ".claude/skills/impeccable/SKILL.md" in committed
    assert ".claude/agents/impeccable-finish-reviewer.md" in committed
    # Rerunning is idempotent and keeps attendee-owned agents.
    (project / ".claude/agents/my-agent.md").write_text("mine")
    approve_for_env(env, "routed")
    subprocess.run(["bash", helper, "routed"], env=env, capture_output=True, check=True, timeout=60)
    assert (project / ".claude/agents/my-agent.md").read_text() == "mine"
    assert skill_routing.audit(attendee.home, project, tree=str(ROOT / "assets/skills"))["harnesses"]["codex"]["status"] == "ok"


@pytest.mark.parametrize("rel, expected", [
    ("src/app.py", True), ("server/routes.ts", True), ("README.md", True),
    (".agents/skills/impeccable/scripts/live-browser.js", False), (".claude/skills/x/SKILL.md", False),
    (".claude/agents/impeccable-documenter.md", False), (".workshop/design-workflow.json", False),
    (".impeccable/design.json", False), ("ui/node_modules/react/index.js", False), ("ui/dist/app.js", False),
    ("package-lock.json", False), ("ui/bun.lock", False), ("uv.lock", False), ("logo.png", False),
])
def test_managed_and_generated_paths_are_not_attendee_code(rel, expected):
    from server.stats import counted

    assert counted(rel) is expected


def test_attendee_sessions_turn_impeccable_telemetry_off(attendee):
    """Impeccable 4.6+ reports anonymous usage unless DO_NOT_TRACK or
    IMPECCABLE_NO_TELEMETRY is set; the engine honours both."""
    env = attendee.shell_env()
    assert env["DO_NOT_TRACK"] == "1" and env["IMPECCABLE_NO_TELEMETRY"] == "1"


def test_the_shared_impeccable_wrapper_turns_telemetry_off(tmp_path, monkeypatch):

    source = (ROOT / "server/bootstrap/impeccable.py").read_text()
    assert "export DO_NOT_TRACK=1 IMPECCABLE_NO_TELEMETRY=1" in source
