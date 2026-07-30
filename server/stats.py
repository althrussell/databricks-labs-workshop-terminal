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

# Code stats walk git repos — cache per user so periodic harvesting (Control
# Tower polls /api/admin/stats) stays cheap.
_CODE_CACHE_TTL = 300
_code_cache: dict[str, tuple[float, dict]] = {}


def _git(repo: str, *args: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", repo, *args],
            capture_output=True, text=True, timeout=10,
        )
        return result.stdout if result.returncode == 0 else None
    except (subprocess.TimeoutExpired, OSError):
        return None


def _code_stats(user: User, *, fresh: bool = True) -> dict:
    if not fresh:
        cached = _code_cache.get(user.email)
        if cached and time.time() - cached[0] < _CODE_CACHE_TTL:
            return cached[1]
    result = _code_stats_uncached(user)
    _code_cache[user.email] = (time.time(), result)
    return result


def _code_stats_uncached(user: User) -> dict:
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


# P1-14: the stats payload carries a schema_version so Control Tower can
# validate the shape and react to changes instead of silently zeroing fields.
# Bump this whenever the payload's structure changes.
# v3 adds the per-user `signal` rollup (contract C6).
STATS_SCHEMA_VERSION = 3

# Topics that describe where an attendee is in the lab rather than a product
# they touched. A brief that listed "wrap" or "troubleshooting" as a product
# interest would be actively misleading to an account team, so they're excluded
# from `products` while still counting toward `topic_hits`.
_NON_PRODUCT_TOPICS = frozenset({
    "intro", "setup", "build", "wrap", "help", "troubleshooting", "error",
    "errors", "credentials", "workshop", "terminal", "agents",
})


def _signal(user: User, code: dict, resources: dict | None) -> dict:
    """The sales-legible reduction of one attendee's behaviour (contract C6).

    Everything here is derived from counters the app already keeps — no attendee
    text is read. Kept next to the raw fields so the derivation is reviewable
    against them rather than reimplemented in Control Tower, where a change to
    what `topics` means would silently change what a brief claims.
    """
    hits = {topic: count for topic, count in user.topic_hits.items() if count > 0}
    shipped = bool(code.get("commits"))
    agent_sessions = sum(
        n for agent, n in user.sessions_launched.items() if agent != "bash"
    )
    if shipped:
        engagement = "builder"
    elif agent_sessions:
        # Not a lesser category than builder: an attendee who worked with an
        # agent and shipped nothing usually hit a wall, and the wall is the most
        # useful thing a brief can carry. See docs/workshop-insight-contract.md.
        engagement = "explorer"
    else:
        engagement = "observer"
    return {
        "engagement": engagement,
        # Ties break alphabetically so repeated harvests of unchanged state
        # produce an identical payload rather than a flapping primary topic.
        "primary_topic": min(hits, key=lambda t: (-hits[t], t)) if hits else None,
        "topic_hits": dict(sorted(hits.items())),
        "products": sorted(set(user.topics) - _NON_PRODUCT_TOPICS),
        "resource_kinds": sorted(
            kind for kind, count in (resources or {}).items() if count
        ),
        "shipped": shipped,
    }


def _discovery_count(email: str) -> int:
    """Records captured for this attendee, or 0 when discovery is off.

    Surfaced on the harvest so Control Tower can reconcile: the push path is
    fail-soft and bounded, so a CT outage that outlasts the emitter's buffer would
    otherwise leave CT unable to tell "this attendee said nothing" from "we lost
    what they said".
    """
    if not config.discovery_enabled():
        return 0
    from .discovery import discovery_store

    return discovery_store.count_for(email)


def gather_user(
    user: User, *, fresh: bool = True, resources: dict | None = None
) -> dict:
    """Per-user stats (no workspace census — that's instance-level).

    ``resources`` is the instance-level census, passed in only so the derived
    signal can report which resource kinds are non-empty. It is not copied into
    the per-user row, which would imply the census is per-attendee.
    """
    now = time.time()
    minutes = int((now - user.first_seen) / 60) if user.first_seen else 0
    agent_sessions = sum(
        n for agent, n in user.sessions_launched.items() if agent != "bash"
    )
    # Abandonment signal: building time accrued but no recent activity.
    idle_seconds = int(now - user.last_seen) if user.last_seen else None
    code = _code_stats(user, fresh=fresh)
    return {
        "email": user.email,
        "minutes_building": minutes,
        "agent_sessions": agent_sessions,
        "terminal_sessions": sum(user.sessions_launched.values()),
        "topics": sorted(user.topics.keys()),
        "code": code,
        # P1-14 error/abandonment telemetry.
        "errors": user.errors,
        "idle_seconds": idle_seconds,
        # C6 behavioural rollup. Present regardless of WORKSHOP_INSIGHT_CAPTURE:
        # it is derived from counters already in this payload and carries no
        # attendee text, and an operator inspecting the harvest should be able to
        # see exactly what capture would send before enabling it.
        "signal": _signal(user, code, resources),
        # Count only — the records themselves go to CT over the ingest path, not
        # through a polled endpoint an operator's browser can read.
        "discovery_records": _discovery_count(user.email),
    }


def gather(user: User) -> dict:
    """Certificate view: this user's stats + the workspace census."""
    resources = _workspace_resources()
    return {
        "schema_version": STATS_SCHEMA_VERSION,
        **gather_user(user, resources=resources),
        "resources": resources,
    }


def gather_all(users: list[User]) -> dict:
    """Harvest view for Control Tower: every user (cached code stats) plus
    one instance-level workspace census."""
    from .events import event_hub
    from .sessions import session_manager

    resources = _workspace_resources()
    return {
        "schema_version": STATS_SCHEMA_VERSION,
        "users": [gather_user(u, fresh=False, resources=resources) for u in users],
        "resources": resources,
        "websocket_queues": {
            **session_manager.queue_metrics(),
            "events": event_hub.metrics(),
        },
    }


# Signal events bucket by wall-clock window so a long workshop writes a coarse
# time series instead of one row per poll. 600s matches CT's default poll
# interval: a bucket shorter than the poll would never de-duplicate anything.
_SIGNAL_BUCKET_SECONDS = 600


def signal_events(payload: dict, run_id: str) -> list[tuple[str, dict, str]]:
    """Build the ``workshop.signal`` events for a gathered harvest payload.

    Returns ``(attendee, payload, idempotency_key)`` triples. Pure — emission is
    the caller's job — because the derivation is what needs testing against the
    contract, and a function that also posted couldn't be tested without one.
    """
    instance = payload.get("instance") or {}
    resources = payload.get("resources") or {}
    bucket = int(time.time() // _SIGNAL_BUCKET_SECONDS) * _SIGNAL_BUCKET_SECONDS
    events = []
    for row in payload.get("users") or []:
        attendee = row.get("email")
        if not attendee:
            continue
        event_payload = {
            "stats_schema_version": payload.get(
                "schema_version", STATS_SCHEMA_VERSION
            ),
            "minutes_building": row.get("minutes_building", 0),
            "agent_sessions": row.get("agent_sessions", 0),
            "terminal_sessions": row.get("terminal_sessions", 0),
            "topics": row.get("topics") or [],
            "code": row.get("code") or {},
            "resources": resources,
            "errors": row.get("errors", 0),
            "idle_seconds": row.get("idle_seconds"),
            "discovery_records": row.get("discovery_records", 0),
            "signal": row.get("signal") or {},
        }
        phase = instance.get("phase")
        if phase:
            event_payload["phase"] = phase
        events.append(
            (attendee, event_payload, f"signal:{run_id}:{attendee}:{bucket}")
        )
    return events


def emit_signals(payload: dict, emitter=None) -> int:
    """Emit one ``workshop.signal`` per attendee. No-op unless capture is on.

    Called from the harvest path so the signal rides the same cadence Control
    Tower already polls at — a separate timer would produce signals whose
    counters disagree with the snapshot CT stored alongside them.
    """
    if not config.insight_capture_enabled():
        return 0
    if emitter is None:
        from .event_emitter import event_emitter as emitter
    count = 0
    for attendee, event_payload, key in signal_events(payload, emitter.run_id):
        emitter.emit("workshop.signal", attendee, event_payload, idempotency_key=key)
        count += 1
    return count
