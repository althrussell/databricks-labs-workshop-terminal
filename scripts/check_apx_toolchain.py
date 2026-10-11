#!/usr/bin/env python3
"""Qualify installed APX, its MCP API and a fresh scaffold/build without cloud writes."""
import argparse
import json
import os
from pathlib import Path
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from server.bootstrap import apx, install
from server.bootstrap.artifacts import directory_checksum
from server.bootstrap.skill_projection import project_skills


def mcp_tools(prefix: Path, project: Path, env: dict) -> set[str]:
    """Keep stdio open until the server has answered the actual handshake."""
    with tempfile.TemporaryFile(mode="w+") as errors:
        process = subprocess.Popen([str(prefix / "bin/apx"), "mcp"], cwd=project, env=env,
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=errors, text=True, bufsize=1)
        replies = queue.Queue()

        def receive():
            for line in process.stdout:
                try:
                    replies.put(json.loads(line))
                except json.JSONDecodeError:
                    continue
            replies.put(None)

        reader = threading.Thread(target=receive, daemon=True)
        reader.start()

        def request(message: dict):
            process.stdin.write(json.dumps(message) + "\n")
            process.stdin.flush()
            if "id" not in message:
                return
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                reply = replies.get(timeout=max(0.01, deadline - time.monotonic()))
                if reply is None:
                    raise RuntimeError("APX MCP exited before replying")
                if reply.get("id") == message["id"]:
                    if "error" in reply:
                        raise RuntimeError("APX MCP protocol error: " + str(reply["error"]))
                    return reply["result"]
            raise TimeoutError("APX MCP did not reply")

        try:
            request({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
                "protocolVersion": "2024-11-05", "capabilities": {},
                "clientInfo": {"name": "wt-apx-check", "version": "1"}}})
            request({"jsonrpc": "2.0", "method": "notifications/initialized"})
            result = request({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
            return {item["name"] for item in result["tools"]}
        except (RuntimeError, TimeoutError, queue.Empty) as error:
            errors.seek(0)
            raise RuntimeError("APX MCP qualification failed: " + errors.read()[-1500:]) from error
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
            reader.join(timeout=5)
            process.stdin.close()
            process.stdout.close()


def qualify(prefix: Path, temporary: Path) -> dict:
    home = temporary / "attendee"
    template = home / ".config/workshop/project-memory.md"
    template.parent.mkdir(parents=True)
    from server.user_content import _project_memory

    template.write_text(_project_memory())
    (home / ".databrickscfg").write_text("[DEFAULT]\nhost = https://example.invalid\ntoken = offline-fixture\n")
    skills = home / ".claude/skills"
    shutil.copytree(ROOT / "assets/skills", skills,
                    ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
    project_skills(skills)
    env = {**os.environ, "HOME": str(home), "PATH": str(prefix / "bin") + ":" + os.environ["PATH"],
           "DATABRICKS_CONFIG_PROFILE": "DEFAULT", "GIT_CONFIG_GLOBAL": "/dev/null",
           "GIT_AUTHOR_NAME": "APX fixture", "GIT_AUTHOR_EMAIL": "fixture@example.com",
           "GIT_COMMITTER_NAME": "APX fixture", "GIT_COMMITTER_EMAIL": "fixture@example.com",
           "NPM_CONFIG_REGISTRY": "https://registry.npmjs.org/", "UV_INDEX_URL": "https://pypi.org/simple"}
    for name in list(env):
        if name.startswith("DATABRICKS_") and name != "DATABRICKS_CONFIG_PROFILE":
            env.pop(name)
    helper = ROOT / "assets/bin/workshop-init-project"
    result = subprocess.run(["bash", str(helper), "apx-stack-check", "--apx", "--json"],
                            env=env, text=True, capture_output=True, timeout=180)
    if result.returncode:
        raise RuntimeError("APX scaffold failed: " + result.stderr[-1000:])
    status = json.loads(result.stdout)
    assert status["ready"]
    project = Path(status["project_path"])
    assert not (project / ".mcp.json").exists(), "scaffold must not add unpinned browser MCP/hooks"
    for name in ("apx", "impeccable"):
        assert directory_checksum(project / ".agents/skills" / name) == directory_checksum(ROOT / "assets/skills" / name)
    for name in ("AGENTS.md", "CLAUDE.md"):
        assert "Official APX (React + FastAPI) is the default" in (project / name).read_text()
        subprocess.run(["git", "show", "HEAD:" + name], cwd=project, env=env,
                       check=True, stdout=subprocess.DEVNULL)
    # Exercise the actual MCP protocol, with no model or workspace call.
    tools = mcp_tools(prefix, project, env)
    assert {"check", "routes", "docs", "add_component", "search_registry_components"} <= tools, tools
    for command in (["apx", "dev", "check"], ["apx", "build"]):
        result = subprocess.run(command, cwd=project, env=env, capture_output=True, text=True, timeout=600)
        if result.returncode:
            raise RuntimeError("APX check/build failed: " + (result.stdout + result.stderr)[-1500:])
    assert (project / ".build").is_dir()
    return {"versions": {name: install._read_cli_version(str(prefix / "bin" / name))
                         for name in ("apx", "bun", "uv")},
            "mcp_tools": sorted(tools), "scaffold_and_build": "passed", "workspace_requests": 0}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bootstrap", action="store_true")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="wt-apx-check-") as temporary:
        root = Path(temporary)
        prefix = root / "shared" if args.bootstrap else Path(install.config.shared_prefix())
        if args.bootstrap:
            install.config.shared_prefix = lambda: str(prefix)
            apx.install_cli()
            assert all(item["reusable"] for item in apx.prewarm_status().values())
            apx.install_cli()  # Real warm reuse; no package reinstallation.
        print(json.dumps(qualify(prefix, root)))


if __name__ == "__main__":
    main()
