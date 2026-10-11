#!/usr/bin/env python3
"""Qualify installed APX, its MCP API and a fresh scaffold/build without cloud writes."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from server.bootstrap import apx, install
from server.bootstrap.artifacts import directory_checksum, load_manifest


def qualify(prefix: Path, temporary: Path) -> dict:
    home = temporary / "attendee"
    template = home / ".config/workshop/project-memory.md"
    template.parent.mkdir(parents=True)
    from server.user_content import _project_memory

    template.write_text(_project_memory())
    (home / ".databrickscfg").write_text("[DEFAULT]\nhost = https://example.invalid\ntoken = offline-fixture\n")
    skills = home / ".claude/skills"
    skills.mkdir(parents=True)
    for name in ("apx", "impeccable"):
        (skills / name).symlink_to(ROOT / "assets/skills" / name)
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
    messages = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2024-11-05", "capabilities": {},
            "clientInfo": {"name": "wt-apx-check", "version": "1"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
    ]
    mcp = subprocess.run([str(prefix / "bin/apx"), "mcp"], cwd=project, env=env,
                         input="".join(json.dumps(item) + "\n" for item in messages),
                         text=True, capture_output=True, timeout=60)
    replies = [json.loads(line) for line in mcp.stdout.splitlines() if line.startswith("{")]
    tools = {item["name"] for reply in replies if reply.get("id") == 2
             for item in reply.get("result", {}).get("tools", [])}
    assert {"check", "routes", "docs", "add_component", "search_registry_components"} <= tools, mcp.stderr[-500:]
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
