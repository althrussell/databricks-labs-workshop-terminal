#!/usr/bin/env python3
"""Real released-CLI scaffold/doctor check. No workspace calls or deployments.

On Linux, --bootstrap exercises WT's reviewed installer, reuse and tamper proof.
On a laptop, --bin-dir selects a disposable proxy-installed CLI environment.
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bootstrap", action="store_true")
    parser.add_argument("--bin-dir", type=Path)
    args = parser.parse_args()
    from server.bootstrap import install
    from server.user_content import _project_memory

    with tempfile.TemporaryDirectory(prefix="wt-agentbricks-contract-") as scratch:
        root = Path(scratch)
        home = root / "attendee"
        home.mkdir()
        env = {name: value for name, value in os.environ.items() if not name.startswith("DATABRICKS_")}
        env.update({
            "HOME": str(home), "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_SYSTEM": "/dev/null", "GIT_AUTHOR_NAME": "WT CLI check",
            "GIT_AUTHOR_EMAIL": "synthetic@example.invalid",
            "GIT_COMMITTER_NAME": "WT CLI check", "GIT_COMMITTER_EMAIL": "synthetic@example.invalid",
            "DATABRICKS_CONFIG_FILE": str(root / "no-credentials"),
            "DATABRICKS_CONFIG_PROFILE": "DEFAULT", "PYTHONDONTWRITEBYTECODE": "1",
        })
        bootstrap_proof = None
        if args.bootstrap:
            if (platform.system(), platform.machine()) != ("Linux", "x86_64"):
                raise SystemExit("Production bootstrap check requires Linux x86_64")
            # No production prefix or credentials; only temporary filesystem writes.
            os.environ["DATA_ROOT"] = str(root / "data")
            os.environ["AGENTBRICKS_ENABLED"] = "true"
            install._install_agentbricks()
            status = install.status()
            assert status["steps"]["agentbricks"]["status"] == "complete", status["steps"]
            from server.bootstrap.agentbricks import prewarm_status

            bootstrap_proof = prewarm_status()
            assert all(row["reusable"] for row in bootstrap_proof.values()), bootstrap_proof
            install._install_agentbricks()
            assert install.status()["steps"]["agentbricks"]["source"] == "prewarmed"
            bin_dir = Path(install.config.shared_prefix()) / "bin"
        elif args.bin_dir:
            bin_dir = args.bin_dir.resolve()
        else:
            raise SystemExit("Select --bootstrap or --bin-dir")
        env["PATH"] = str(bin_dir) + os.pathsep + env.get("PATH", "/usr/bin:/bin")
        # init and doctor are documented offline. Refuse any network connection
        # from their Python process so accidental eager API calls fail this check.
        guard = root / "network-guard"
        guard.mkdir()
        (guard / "sitecustomize.py").write_text(
            "import socket\n"
            "def blocked(*args, **kwargs):\n    raise RuntimeError('Offline CLI check attempted network access')\n"
            "socket.socket.connect = blocked\nsocket.socket.connect_ex = blocked\n"
        )
        env["PYTHONPATH"] = str(guard)
        config = home / ".config/workshop"
        config.mkdir(parents=True)
        (config / "project-memory.md").write_text(_project_memory())
        home_skill = home / ".claude/skills/workshop-agent-bricks-cli"
        home_skill.parent.mkdir(parents=True)
        home_skill.symlink_to(ROOT / "assets/skills/workshop-agent-bricks-cli", target_is_directory=True)

        def run(command, cwd=None):
            result = subprocess.run(command, env=env, cwd=cwd, capture_output=True, text=True, timeout=120)
            if result.returncode:
                raise RuntimeError(f"CLI check failed ({command[0]}): {result.stderr or result.stdout}")
            return result.stdout

        assert install._parse_version(run([str(bin_dir / "agentbricks"), "--version"])) == install.AGENTBRICKS_VERSION
        assert install._parse_version(run([str(bin_dir / "uv"), "--version"])) == install.UV_VERSION
        rows = []
        for framework in ("langgraph", "openai"):
            command = ["bash", str(ROOT / "assets/bin/workshop-init-project"),
                       f"check-{framework}", "--agentbricks", "--", "--framework", framework]
            project = Path(run(command).strip())
            manifest = tomllib.loads((project / "agent.toml").read_text())
            assert manifest["agent"] == {"framework": framework, "server": "agentbricks"}
            assert not (project / project.name).exists(), "Scaffold was nested"
            doctor = run([str(bin_dir / "agentbricks"), "--output", "json", "doctor", "."], cwd=project)
            report = json.loads(doctor)
            assert report["onboarded"] is True
            tracked = run(["git", "ls-files"], cwd=project).splitlines()
            assert {"AGENTS.md", "CLAUDE.md", "agent.toml", "README.md"} <= set(tracked)
            assert ".agents/skills/workshop-agent-bricks-cli/SKILL.md" in tracked
            assert ".claude/skills/workshop-agent-bricks-cli" in tracked
            assert (project / ".claude/skills/workshop-agent-bricks-cli/SKILL.md").is_file()
            assert ".env" not in tracked
            for name in ("AGENTS.md", "CLAUDE.md"):
                assert "<!-- workshop-project-memory -->" in (project / name).read_text()
            assert "<!-- workshop-brief:v1 -->" in (project / "README.md").read_text()
            assert "DATABRICKS_CONFIG_PROFILE=DEFAULT" in (project / ".env").read_text()
            before = run(["git", "rev-parse", "HEAD"], cwd=project)
            run(command)
            assert run(["git", "rev-parse", "HEAD"], cwd=project) == before
            # Migration preparation must preserve existing application bytes.
            agent_file = project / "agent/agent.py"
            original = agent_file.read_bytes()
            run([str(bin_dir / "agentbricks"), "init", "--framework", framework,
                 "--existing", "--disable-chat-app", "."], cwd=project)
            assert agent_file.read_bytes() == original
            assert (project / "agent-bricks-migrate").is_dir()
            rows.append({"framework": framework, "scaffold": "pass", "doctor": report,
                         "memory_committed": True, "no_nested_project": True,
                         "rerun_preserved_commit": True, "migration_preserved_source": True})
        if args.bootstrap:
            from server.bootstrap.agentbricks import prewarm_status

            python_source = next((Path(install.config.shared_prefix()) / "agentbricks-venv").rglob("databricks_agentbricks/cli/app.py"))
            python_source.write_text(python_source.read_text() + "\n# tamper qualification\n")
            assert not prewarm_status()["agentbricks"]["reusable"]
        print(json.dumps({"cli": install.AGENTBRICKS_VERSION, "uv": install.UV_VERSION,
                          "platform": platform.system(), "bootstrap_proof": bootstrap_proof,
                          "checks": rows, "offline_network_guard": True,
                          "workspace_requests": 0, "deployment_mutations": 0}, indent=2))


if __name__ == "__main__":
    main()
