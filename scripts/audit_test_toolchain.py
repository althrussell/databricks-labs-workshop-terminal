#!/usr/bin/env python3
"""Read-only latest-release audit. Uses laptop package proxies; records public URLs.

Exit 1 means a newer release exists (including a recorded compatibility hold),
2 means metadata could not be verified. Nothing installs or changes deployment.
"""
import concurrent.futures
import json
import re
import subprocess
import sys
import tomllib
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def fetch(url):
    with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "WT-toolchain-audit"}),
                                timeout=30) as response:
        return json.load(response)


def python_release(name, current):
    command = [sys.executable, "-m", "pip", "index", "versions", name,
               "--disable-pip-version-check", "--no-color", "--timeout", "15", "--retries", "0"]
    if re.search(r"(?:a|b|rc)\d", current):
        command.append("--pre")  # OTEL's matching instrumentation release train.
    result = subprocess.run(command, check=True, capture_output=True, text=True, timeout=45)
    match = re.search(r"^Available versions: ([^,\n]+)", result.stdout, re.M)
    if not match:
        raise ValueError("Package release metadata missing")
    latest = match[1].strip()
    return latest, f"https://pypi.org/project/{name}/{latest}/"


def audit(job, registry):
    ecosystem, name, current, source = job
    try:
        if ecosystem == "pypi":
            latest, url = python_release(name, current)
        elif ecosystem == "npm":
            data = fetch(registry + "/" + name)
            latest, url = data["dist-tags"]["latest"], "https://registry.npmjs.org/" + name
        elif ecosystem == "github":
            data = fetch("https://api.github.com/repos/" + source + "/releases/latest")
            latest, url = data["tag_name"].removeprefix("v"), data["html_url"]
        elif ecosystem == "node":
            data = fetch("https://nodejs.org/dist/index.json")
            latest = next(row["version"].removeprefix("v") for row in data if row.get("lts"))
            url = "https://nodejs.org/dist/index.json"
        else:
            raise ValueError("Unknown release ecosystem")
        return {"ecosystem": ecosystem, "name": name, "pinned": current, "latest": latest,
                "source": url, "status": "current" if current.removeprefix("v") == latest else "update_available"}
    except Exception as error:
        # Never export stderr/URLs from credential-bearing proxy configuration.
        return {"ecosystem": ecosystem, "name": name, "pinned": current,
                "status": "unverified", "error_type": type(error).__name__}


def main():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())
    requirements = list(project["project"]["dependencies"])
    for group in project["dependency-groups"].values():
        requirements.extend(group)
    requirements.extend(line.strip() for line in (ROOT / "evals/generated_apps/requirements-browser.txt").read_text().splitlines()
                        if line.strip() and not line.startswith("#"))
    jobs = set()
    for requirement in requirements:
        name, version = requirement.split("==", 1)
        jobs.add(("pypi", name.split("[", 1)[0], version, ""))
    frontend = json.loads((ROOT / "frontend/package.json").read_text())
    for group in ("dependencies", "devDependencies"):
        jobs.update(("npm", name, version, "") for name, version in frontend[group].items())
    artifacts = json.loads((ROOT / "assets/artifacts/manifest.json").read_text())["artifacts"]
    baseline = json.loads((ROOT / "evals/generated_apps/test-baseline.json").read_text())
    jobs.update({
        ("npm", "@openai/codex", artifacts["codex_npm_launcher_package"]["version"], ""),
        ("npm", "@anthropic-ai/claude-code", artifacts["claude_binary"]["version"], ""),
        ("npm", "@databricks/appkit", baseline["libraries"]["appkit"], ""),
        ("npm", "@databricks/appkit-ui", baseline["libraries"]["appkit"], ""),
        ("github", "databricks-cli", artifacts["databricks_cli_archive_linux_x64"]["version"], "databricks/cli"),
        ("github", "databricks-agent-skills", artifacts["databricks_agent_skills"]["version"], "databricks/databricks-agent-skills"),
        ("github", "uv", artifacts["uv_binary"]["version"], "astral-sh/uv"),
        ("node", "node-active-lts", artifacts["node_linux_x64"]["version"], ""),
        ("pypi", "omnigent", artifacts["omnigent_lock"]["version"], ""),
    })
    registry = subprocess.run(["npm", "config", "get", "registry"], check=True,
                              capture_output=True, text=True, timeout=10).stdout.strip().rstrip("/")
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        rows = list(pool.map(lambda job: audit(job, registry), sorted(jobs)))
    print(json.dumps({"checked_at": datetime.now(timezone.utc).isoformat(), "baseline": baseline["id"],
        "scope": "direct WT, evaluator and harness dependencies; transitive versions remain parent-constrained",
        "rows": rows, "workspace_requests": 0, "deployment_mutations": 0,
        "compatibility_holds": {"omnigent": "Requires a qualified matching paired server; see toolchain remediation evidence"}}, indent=2))
    return 2 if any(row["status"] == "unverified" for row in rows) else int(any(row["status"] == "update_available" for row in rows))


if __name__ == "__main__":
    raise SystemExit(main())
