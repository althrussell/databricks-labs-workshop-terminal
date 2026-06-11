"""Per-attendee build stats for the wrap-phase brag certificate.

Three sources, all best-effort with hard timeouts:
- the app's own counters (agent sessions, time in workshop, topic trail)
- the attendee's ~/projects repos (commits, files, lines of code)
- a workspace resource census via the vended credential (jobs, pipelines,
  apps, dashboards). In the standard Control Tower topology the workspace is
  per-attendee, so the census is theirs; in shared instances it reflects the
  whole cohort's workspace and is labelled accordingly on the certificate.
"""

from __future__ import annotations

import logging
import os
import subprocess
import time

import requests

from . import config
from .users import User

logger = logging.getLogger(__name__)

_CODE_EXTENSIONS = {
    ".py", ".sql", ".ts", ".tsx", ".js", ".jsx", ".scala", ".r", ".sh",
    ".yaml", ".yml", ".json", ".md", ".html", ".css", ".toml", ".ipynb",
}


def _git(repo: str, *args: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", repo, *args],
            capture_output=True, text=True, timeout=10,
        )
        return result.stdout if result.returncode == 0 else None
    except (subprocess.TimeoutExpired, OSError):
        return None


def _code_stats(user: User) -> dict:
    projects = os.path.join(user.home, "projects")
    repos = commits = files = lines = 0
    if not os.path.isdir(projects):
        return {"projects": 0, "commits": 0, "files": 0, "lines": 0}
    for name in sorted(os.listdir(projects)):
        repo = os.path.join(projects, name)
        if not os.path.isdir(os.path.join(repo, ".git")):
            continue
        repos += 1
        count = _git(repo, "rev-list", "--count", "HEAD")
        if count and count.strip().isdigit():
            commits += int(count.strip())
        tracked = _git(repo, "ls-files")
        for rel in (tracked or "").splitlines():
            if os.path.splitext(rel)[1].lower() not in _CODE_EXTENSIONS:
                continue
            path = os.path.join(repo, rel)
            try:
                with open(path, "rb") as f:
                    lines += sum(1 for _ in f)
                files += 1
            except OSError:
                continue
    return {"projects": repos, "commits": commits, "files": files, "lines": lines}


def _count(url: str, token: str, params: dict, items_key: str) -> int:
    try:
        resp = requests.get(
            url, headers={"Authorization": f"Bearer {token}"},
            params=params, timeout=6,
        )
        if resp.status_code != 200:
            return 0
        return len(resp.json().get(items_key, []) or [])
    except requests.RequestException:
        return 0


def _workspace_resources() -> dict:
    """Census of resources in the workshop workspace (best-effort)."""
    from .credentials import CredentialError, credential_manager

    host = config.databricks_host()
    try:
        token = credential_manager.token()
    except CredentialError:
        return {}
    if not host:
        return {}
    return {
        "jobs": _count(f"{host}/api/2.2/jobs/list", token, {"limit": 100}, "jobs"),
        "pipelines": _count(f"{host}/api/2.0/pipelines", token, {"max_results": 100}, "statuses"),
        "apps": _count(f"{host}/api/2.0/apps", token, {}, "apps"),
        "dashboards": _count(f"{host}/api/2.0/lakeview/dashboards", token, {"page_size": 100}, "dashboards"),
    }


def gather(user: User) -> dict:
    now = time.time()
    minutes = int((now - user.first_seen) / 60) if user.first_seen else 0
    agent_sessions = sum(
        n for agent, n in user.sessions_launched.items() if agent != "bash"
    )
    return {
        "minutes_building": minutes,
        "agent_sessions": agent_sessions,
        "terminal_sessions": sum(user.sessions_launched.values()),
        "topics": sorted(user.topics.keys()),
        "code": _code_stats(user),
        "resources": _workspace_resources(),
    }
