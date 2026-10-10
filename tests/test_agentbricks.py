"""Installation integrity, opt-in isolation and attendee project continuity."""
import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from server import config
from server.bootstrap import agentbricks, install

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def cli_install(monkeypatch, tmp_path):
    prefix = tmp_path / "shared"
    prefix.mkdir()
    lock = ROOT / "assets/artifacts/agentbricks-0.4.0.lock"
    entries = {
        "uv_binary": {"version": install.UV_VERSION, "sha256": "a" * 64},
        "python_3_12_runtime": {"version": "3.12.15", "sha256": "b" * 64},
        agentbricks.LOCK_ARTIFACT: {
            "version": install.AGENTBRICKS_VERSION, "source": str(lock),
            "sha256": install._file_checksum(lock),
        },
    }
    uv = tmp_path / "uv"
    uv.write_text("reviewed-uv")
    monkeypatch.setattr(config, "shared_prefix", lambda: str(prefix))
    monkeypatch.setattr(install, "_state", {})
    monkeypatch.setattr(install, "_artifact_contract", lambda: SimpleNamespace(entry=entries.__getitem__))
    monkeypatch.setattr(install, "_verified_artifact", lambda name: (entries[name]["source"], entries[name]))
    monkeypatch.setattr(install, "_extracted_artifact_executable", lambda name: (str(uv), entries[name]))
    monkeypatch.setattr(install, "_install_source", lambda *args: "local")
    commands = []

    def fake_run(command, **kwargs):
        commands.append((command, kwargs))
        if command[1] == "venv":
            venv = prefix / "agentbricks-venv"
            (venv / "bin").mkdir(parents=True, exist_ok=True)
            (venv / "bin/agentbricks").write_text("installed CLI")
            (venv / "library.py").write_text("reviewed library")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(agentbricks.subprocess, "run", fake_run)
    monkeypatch.setattr(install, "_read_cli_version", lambda path: (
        install.UV_VERSION if Path(path).name == "uv" else
        install.AGENTBRICKS_VERSION if Path(path).exists() else None
    ))
    return prefix, entries, commands


def test_beta_cli_is_opt_in_and_configuration_is_dynamic(monkeypatch):
    monkeypatch.delenv("AGENTBRICKS_ENABLED", raising=False)
    assert config.agentbricks_enabled() is False
    monkeypatch.setenv("AGENTBRICKS_ENABLED", "true")
    assert config.agentbricks_enabled() is True
    assert install._release_specs()["agentbricks"][0] is True
    # Coding harness readiness is independent of the beta utility.
    assert install._ready_from({name: {"status": "complete"} for name in
                               ("node", "databricks", "claude")})["claude"] is True


def test_cli_installs_isolated_hashed_wheels_from_public_pypi(cli_install, monkeypatch):
    prefix, _, commands = cli_install
    monkeypatch.setenv("PIP_INDEX_URL", "https://laptop.invalid/simple")
    monkeypatch.setenv("DATABRICKS_TOKEN", "synthetic-do-not-forward")
    install._install_agentbricks()
    assert install.status()["steps"]["agentbricks"]["status"] == "complete"
    command, kwargs = next(row for row in commands if row[0][1:3] == ["pip", "sync"])
    assert "--require-hashes" in command
    assert command[command.index("--only-binary") + 1] == ":all:"
    assert str(prefix / "agentbricks-venv/bin/python") in command
    assert kwargs["env"]["UV_PYTHON_DOWNLOADS"] == "never"
    assert kwargs["env"]["UV_INDEX_URL"] == "https://pypi.org/simple"
    assert not any(name.startswith("DATABRICKS_") for name in kwargs["env"])
    assert (prefix / "bin/uv").is_symlink()
    assert all(row["reusable"] for row in agentbricks.prewarm_status().values())


def test_reuse_checks_transitive_library_bytes_and_reinstalls_tampering(cli_install):
    prefix, _, commands = cli_install
    install._install_agentbricks()
    count = len(commands)
    install._install_agentbricks()
    assert len(commands) == count
    assert install.status()["steps"]["agentbricks"]["source"] == "prewarmed"
    (prefix / "agentbricks-venv/library.py").write_text("changed dependency")
    assert agentbricks.prewarm_status()["agentbricks"]["reusable"] is False
    install._install_agentbricks()
    assert len(commands) > count
    assert agentbricks.prewarm_status()["agentbricks"]["reusable"] is True


def test_changed_lock_or_version_never_reuses_existing_tool(cli_install):
    _, entries, _ = cli_install
    install._install_agentbricks()
    entries[agentbricks.LOCK_ARTIFACT]["sha256"] = "f" * 64
    assert agentbricks.prewarm_status()["agentbricks"]["reusable"] is False
    entries[agentbricks.LOCK_ARTIFACT]["version"] = "0.3.0"
    install._install_agentbricks()
    steps = install.status()["steps"]
    assert steps["agentbricks"]["status"] == "error"
    assert "configured CLI version" in steps["agentbricks"]["error"]


def test_prerequisite_failure_settles_both_setup_steps(cli_install, monkeypatch):
    def fail(_):
        raise RuntimeError("reviewed artifact unavailable")

    monkeypatch.setattr(install, "_extracted_artifact_executable", fail)
    install._install_agentbricks()
    steps = install.status()["steps"]
    assert steps["agentbricks"]["status"] == steps["uv"]["status"] == "error"
    assert "reviewed artifact unavailable" in steps["agentbricks"]["error"]


def test_agentbricks_lock_matches_its_manifest_and_is_fully_hashed():
    from server.bootstrap.artifacts import _fully_pinned_hashed_lock, load_manifest

    entry = load_manifest("")["artifacts"][agentbricks.LOCK_ARTIFACT]
    assert _fully_pinned_hashed_lock(entry["source"])
    assert install._file_checksum(entry["source"]) == entry["sha256"] == entry["lock_sha256"]


def test_helper_preserves_both_scaffold_memories_and_inherited_profile(tmp_path):
    home = tmp_path / "attendee"
    memory = home / ".config/workshop/project-memory.md"
    memory.parent.mkdir(parents=True)
    memory.write_text("<!-- workshop-project-memory -->\nWorkshop instructions\n")
    skill = home / ".claude/skills/workshop-agent-bricks-cli"
    skill.parent.mkdir(parents=True)
    skill.symlink_to(ROOT / "assets/skills/workshop-agent-bricks-cli", target_is_directory=True)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "uv").write_text("#!/bin/sh\nexit 0\n")
    (bin_dir / "uv").chmod(0o755)
    cli = bin_dir / "agentbricks"
    cli.write_text("#!/bin/bash\nset -e\n"
                   'printf "%s\\n" "$@" > "$HOME/cli-args"\n'
                   'DEST="${7}"\nmkdir -p "$DEST"\n'
                   'printf "schema_version = 1\\n" > "$DEST/agent.toml"\n'
                   'printf "Upstream agent notes\\n" > "$DEST/AGENTS.md"\n'
                   'printf ".env\\n" > "$DEST/.gitignore"\n'
                   'printf "DATABRICKS_CONFIG_PROFILE=%s\\n" "${2}" > "$DEST/.env"\n')
    cli.chmod(0o755)
    env = os.environ | {
        "HOME": str(home), "PATH": str(bin_dir) + os.pathsep + os.environ["PATH"],
        "DATABRICKS_CONFIG_PROFILE": "me", "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_SYSTEM": "/dev/null", "GIT_AUTHOR_NAME": "Test", "GIT_COMMITTER_NAME": "Test",
        "GIT_AUTHOR_EMAIL": "test@example.invalid", "GIT_COMMITTER_EMAIL": "test@example.invalid",
    }
    command = ["bash", str(ROOT / "assets/bin/workshop-init-project"), "my-agent", "--agentbricks"]
    first = subprocess.run(command, env=env, capture_output=True, text=True, check=True)
    project = Path(first.stdout.strip())
    assert (home / "cli-args").read_text().splitlines()[:5] == ["--profile", "me", "init", "--profile", "me"]
    for name in ("AGENTS.md", "CLAUDE.md"):
        assert "Workshop instructions" in (project / name).read_text()
    assert "Upstream agent notes" in (project / "AGENTS.md").read_text()
    tracked = subprocess.check_output(["git", "ls-files"], cwd=project, env=env, text=True).splitlines()
    assert ".env" not in tracked
    assert {"AGENTS.md", "CLAUDE.md"} <= set(tracked)
    assert ".agents/skills/workshop-agent-bricks-cli/SKILL.md" in tracked
    assert ".claude/skills/workshop-agent-bricks-cli" in tracked
    assert (project / ".claude/skills/workshop-agent-bricks-cli/SKILL.md").is_file()
    before = (project / "AGENTS.md").read_bytes()
    subprocess.run(command, env=env, capture_output=True, text=True, check=True)
    assert (project / "AGENTS.md").read_bytes() == before


def test_conflicting_scaffolds_fail_before_creating_a_project(tmp_path):
    result = subprocess.run(
        ["bash", str(ROOT / "assets/bin/workshop-init-project"), "test", "--appkit", "--agentbricks"],
        env=os.environ | {"HOME": str(tmp_path)}, capture_output=True, text=True,
    )
    assert result.returncode == 2
    assert not (tmp_path / "projects").exists()
