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

import copy
import hashlib
import json
import logging
import math
import os
import subprocess
import time
import uuid
from contextlib import nullcontext

import requests

from . import config
from .capture_journal import CaptureHeldError, CaptureStorageError
from .users import User, UserManager, user_snapshot

logger = logging.getLogger(__name__)

_CODE_EXTENSIONS = {
    ".py", ".sql", ".ts", ".tsx", ".js", ".jsx", ".scala", ".r", ".sh",
    ".yaml", ".yml", ".json", ".md", ".html", ".css", ".toml", ".ipynb",
}

# Code stats walk git repos — cache per user so periodic harvesting (Control
# Tower polls /api/admin/stats) stays cheap.
_CODE_CACHE_TTL = 300
_code_cache: dict[str, tuple[float, dict]] = {}


class _CodeCounts(dict):
    def __init__(self, values, *, confirmed=True):
        super().__init__(values)
        self.confirmed = confirmed


def _git(repo: str, *args: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", repo, *args],
            capture_output=True, text=True, timeout=10,
        )
        return result.stdout if result.returncode == 0 else None
    except (subprocess.TimeoutExpired, OSError):
        return None


def _unborn_head(repo: str) -> bool:
    """Prove that HEAD names a branch whose ref has never been created.

    A generic failing log/rev-list can also mean corrupt or unreadable objects.
    Only an intact symbolic HEAD and a specifically absent branch ref justify a
    confirmed zero commits; detached/broken refs and failed probes remain held.
    """
    try:
        symbolic = subprocess.run(
            ["git", "-C", repo, "symbolic-ref", "--quiet", "HEAD"],
            capture_output=True, text=True, timeout=10,
        )
        branch = symbolic.stdout.strip()
        if (symbolic.returncode != 0 or not branch.startswith("refs/heads/")
                or "\n" in branch):
            return False
        reference = subprocess.run(
            ["git", "-C", repo, "show-ref", "--verify", "--quiet", branch],
            capture_output=True, text=True, timeout=10,
        )
        return reference.returncode == 1
    except (subprocess.TimeoutExpired, OSError):
        return False


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
    confirmed = True
    try:
        names = sorted(os.listdir(projects))
    except OSError:
        return _CodeCounts({"projects": 0, "commits": 0, "files": 0, "lines": 0}, confirmed=False)
    for name in names:
        repo = os.path.join(projects, name)
        if not os.path.isdir(os.path.join(repo, ".git")):
            continue
        repos += 1
        count = _git(repo, "rev-list", "--count", "HEAD")
        if count is None and _unborn_head(repo):
            count = "0"
        if count and count.strip().isdigit():
            commits += int(count.strip())
        tracked = _git(repo, "ls-files")
        if count is None or tracked is None:
            confirmed = False
        for rel in (tracked or "").splitlines():
            if os.path.splitext(rel)[1].lower() not in _CODE_EXTENSIONS:
                continue
            path = os.path.join(repo, rel)
            try:
                with open(path, "rb") as f:
                    lines += sum(1 for _ in f)
                files += 1
            except OSError:
                confirmed = False
                continue
    return _CodeCounts({"projects": repos, "commits": commits, "files": files, "lines": lines},
                       confirmed=confirmed)


def _list_checked(url: str, token: str, params: dict, items_key: str) -> tuple[list[dict], bool]:
    try:
        resp = requests.get(
            url, headers={"Authorization": f"Bearer {token}"},
            params=params, timeout=6,
        )
        if resp.status_code != 200:
            return [], False
        payload = resp.json()
        if not isinstance(payload, dict):
            return [], False
        # This bounded census does not enumerate further pages. A successful
        # first page is not a confirmed total; retain the last known count
        # instead of certifying a truncated observation as fresh at closeout.
        if payload.get("next_page_token") or payload.get("has_more"):
            return [], False
        items = payload.get(items_key, []) or []
        if not isinstance(items, list):
            return [], False
        return [item for item in items if isinstance(item, dict)], True
    except (requests.RequestException, ValueError):
        return [], False


def _list(url: str, token: str, params: dict, items_key: str) -> list[dict]:
    return _list_checked(url, token, params, items_key)[0]


def _count(url: str, token: str, params: dict, items_key: str) -> int:
    return len(_list(url, token, params, items_key))


def _is_ours(app: dict) -> bool:
    """Was this app put in the workspace by Control Tower rather than the attendee?

    The census is meant to say what the attendee made, and a workshop workspace
    always already contains this terminal — so counting the raw list credits
    every attendee with an app before they have opened a file. That number goes
    straight into a brief an account team reads, which makes it worse than
    absent.

    Two apps are ours. This one is identified by the service principal it runs
    as, which the Apps runtime hands us as ``DATABRICKS_CLIENT_ID``; matching on
    that rather than on a name means an operator renaming the app template
    cannot quietly reintroduce the miscount. Omnigent is identified by the URL we
    were configured to reach it on, which is the only handle we have for it.
    """
    ours = os.environ.get("DATABRICKS_CLIENT_ID", "").strip()
    if ours and app.get("service_principal_client_id", "").strip() == ours:
        return True
    app_url = (app.get("url") or "").strip().rstrip("/")
    if not app_url:
        return False
    for known in (_omnigent_url(), _own_url()):
        if known and app_url == known.strip().rstrip("/"):
            return True
    return False


def _omnigent_url() -> str:
    """Omnigent's URL, or empty if it is unset or unusable.

    A misconfigured URL must not take the census down with it: the census is
    best-effort by design, and a stats call that raises would cost Control Tower
    the whole harvest for one bad env var.
    """
    try:
        return config.omnigent_app_url()
    except ValueError:
        return ""


def _own_url() -> str:
    """This app's own URL, when the runtime tells us.

    A fallback for the case where the app's service principal is not visible in
    the census rows — without it, a terminal whose own row lacks the SP field
    counts itself.
    """
    return os.environ.get("DATABRICKS_APP_URL", "").strip()


class _Census(dict):
    """Counts plus which requests actually succeeded; failures are not zeroes."""

    def __init__(self, values=(), *, confirmed=()):
        super().__init__(values)
        self.confirmed = set(confirmed)


def _workspace_resources() -> dict:
    """Census of resources in the workshop workspace (best-effort)."""
    from .credentials import CredentialError, credential_manager

    host = config.databricks_host()
    try:
        token = credential_manager.token()
    except CredentialError:
        return _Census()
    if not host:
        return _Census()
    census = _Census()
    for kind, path, params, key in (
        ("jobs", "/api/2.2/jobs/list", {"limit": 100}, "jobs"),
        ("pipelines", "/api/2.0/pipelines", {"max_results": 100}, "statuses"),
        ("apps", "/api/2.0/apps", {}, "apps"),
        ("dashboards", "/api/2.0/lakeview/dashboards", {"page_size": 100}, "dashboards"),
    ):
        items, ok = _list_checked(f"{host}{path}", token, params, key)
        if ok:
            census[kind] = sum(1 for item in items if kind != "apps" or not _is_ours(item))
            census.confirmed.add(kind)
    return census


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


def _discovery_count(email: str) -> int | None:
    """Record count, 0 when disabled, or None when retention cannot be read."""
    if not config.discovery_enabled():
        return 0
    from .discovery import discovery_store

    try:
        return discovery_store.count_for(email)
    except CaptureStorageError:
        # The retained discovery journal shares the storage that may already
        # have failed while reading stats. Unknown is not a confirmed zero.
        return None


def _timestamp(value) -> bool:
    return (type(value) is int and value >= 0
            or type(value) is float and value >= 0 and math.isfinite(value))


def _counts(value) -> bool:
    return isinstance(value, dict) and all(
        isinstance(key, str) and type(count) is int and count >= 0
        for key, count in value.items()
    )


def _retained_stats(state: dict) -> dict:
    retained = state.setdefault("stats", {"users": {}, "resources": {},
                                          "resources_observed_at": {}, "signals": {}})
    if (not isinstance(retained, dict)
            or set(retained) != {"users", "resources", "resources_observed_at", "signals"}
            or not _counts(retained.get("resources"))
            or not isinstance(retained.get("resources_observed_at"), dict)
            or set(retained["resources"]) != set(retained["resources_observed_at"])
            or not all(_timestamp(value) for value in retained["resources_observed_at"].values())
            or not isinstance(retained.get("users"), dict)
            or not isinstance(retained.get("signals"), dict)):
        raise CaptureHeldError("retained stats snapshot is invalid")
    for email, saved in retained["users"].items():
        if (not isinstance(email, str) or not email or not isinstance(saved, dict)
                or set(saved) != {"row", "observed_at", "code_observed_at"}
                or not _timestamp(saved["observed_at"])
                or not _timestamp(saved["code_observed_at"])
                or not isinstance(saved["row"], dict)
                or saved["row"].get("email") != email
                or not _counts(saved["row"].get("code"))
                or set(saved["row"]["code"]) != {"projects", "commits", "files", "lines"}):
            raise CaptureHeldError("retained attendee stats are invalid")
    for email, saved in retained["signals"].items():
        if (not isinstance(email, str) or not email or not isinstance(saved, dict)
                or set(saved) != {"revision", "fingerprint", "key", "checkpoint_token"}
                or type(saved["revision"]) is not int or saved["revision"] < 1
                or not isinstance(saved["fingerprint"], str) or len(saved["fingerprint"]) != 64
                or not isinstance(saved["key"], str) or not saved["key"]
                or not isinstance(saved["checkpoint_token"], str) or not saved["checkpoint_token"]):
            raise CaptureHeldError("retained signal revision is invalid")
        insight = state.get("insight", {})
        seq = insight.get("keys", {}).get(saved["key"])
        event = insight.get("events", {}).get(str(seq), {})
        if (event.get("attendee") != email or event.get("type") != "workshop.signal"
                or _fingerprint(event.get("payload")) != saved["fingerprint"]):
            raise CaptureHeldError("retained signal revision has no matching accepted event")
    return retained


def _checked(journal, operation):
    try:
        return operation()
    except CaptureHeldError as error:
        journal.mark_held(error)
        raise


def configure_capture(journal) -> None:
    """Validate retained observations during startup, including headless recovery."""
    _checked(journal, lambda: journal.transaction(lambda state: _retained_stats(state)))


def _saved_stats(user: User) -> dict:
    journal = user._capture_journal
    if journal is None:
        return {"users": {}, "resources": {}, "resources_observed_at": {}, "signals": {}}
    try:
        return _checked(journal, lambda: _retained_stats(journal.read()))
    except CaptureStorageError:
        # The diagnostic harvest must still expose the live counters while the
        # capture/collection APIs hold delivery. Never present unavailable
        # retained observations as confirmed zeroes or as freshly recovered.
        return {"users": {}, "resources": {}, "resources_observed_at": {},
                "signals": {}, "unavailable": True}


def _code_observation(user: User, *, fresh: bool, saved: dict) -> tuple[dict, dict]:
    previous = saved["users"].get(user.email)
    projects = os.path.join(user.home, "projects")
    # The empty HOME created by a browser reconnect after redeploy is not
    # evidence that the attendee's previously observed output disappeared.
    # Previously confirmed zeroes may still be checked against a readable,
    # genuinely empty projects directory; no repository needs restoring then.
    # A nonempty directory can hide an unreadable/unrestored repository, so it
    # must not acquire a fresh zero just because isdir(.git) returned False.
    try:
        names = os.listdir(projects)
        materialized = any(os.path.isdir(os.path.join(projects, name, ".git"))
                           for name in names)
    except OSError:
        names = None
        materialized = False
    if previous and (not os.path.isdir(projects) or (
            user._capture_recovered and not materialized
            and (names != [] or any(previous["row"]["code"].values())))):
        return copy.deepcopy(previous["row"]["code"]), {
            "source": "retained", "observed_at": previous["code_observed_at"],
        }
    code = _code_stats(user, fresh=fresh)
    if previous and not getattr(code, "confirmed", True):
        return copy.deepcopy(previous["row"]["code"]), {
            "source": "retained", "observed_at": previous["code_observed_at"],
        }
    observed_at = (_code_cache.get(user.email, (0, {}))[0]
                   if os.path.isdir(projects) and getattr(code, "confirmed", True) else 0)
    return dict(code), {"source": "local" if observed_at else "unavailable", "observed_at": observed_at}


def _resource_observation(resources: dict, users: list[User]) -> tuple[dict, dict]:
    saved = next((_saved_stats(user) for user in users if user._capture_journal is not None), None)
    previous = saved or {"resources": {}, "resources_observed_at": {}}
    confirmed = getattr(resources, "confirmed", set(resources))
    counts = dict(previous["resources"])
    counts.update({key: value for key, value in resources.items() if key in confirmed})
    now = time.time()
    observation = {kind: {"source": "live" if kind in confirmed else "retained",
                           "observed_at": now if kind in confirmed else previous["resources_observed_at"][kind]}
                   for kind in counts}
    return counts, observation


def _save_observation(state: dict, row: dict, resources: dict, resource_observation: dict, now: float) -> None:
    retained = _retained_stats(state)
    previous = retained["users"].get(row["email"])
    row = copy.deepcopy(row)
    code_observed_at = row.get("capture_observation", {}).get("code", {}).get("observed_at", 0)
    if previous and previous["code_observed_at"] > code_observed_at:
        row["code"] = copy.deepcopy(previous["row"]["code"])
        code_observed_at = previous["code_observed_at"]
        row.setdefault("capture_observation", {})["code"] = {
            "source": "retained", "observed_at": code_observed_at,
        }
    retained["users"][row["email"]] = {
        "row": row, "observed_at": now, "code_observed_at": code_observed_at,
    }
    for kind, count in resources.items():
        observation = resource_observation.get(kind, {})
        if (observation.get("source") == "live"
                and observation["observed_at"] >= retained["resources_observed_at"].get(kind, 0)):
            retained["resources"][kind] = count
            retained["resources_observed_at"][kind] = observation["observed_at"]


def gather_user(
    user: User, *, fresh: bool = True, resources: dict | None = None
) -> dict:
    """Per-user stats (no workspace census — that's instance-level).

    ``resources`` is the instance-level census, passed in only so the derived
    signal can report which resource kinds are non-empty. It is not copied into
    the per-user row, which would imply the census is per-attendee.
    """
    checkpoint_unavailable = False
    if user._capture_journal is not None:
        from .users import user_manager

        try:
            # A replacement/overlapping container can have committed counters
            # since this process last saw the owner. Reconcile before deriving
            # the snapshot and its event, rather than publishing stale totals
            # and refreshing the local owner only afterward.
            user_manager.checkpoint(user)
        except CaptureStorageError:
            # A write-only outage may still allow all subsequent reads. Keep
            # this failed checkpoint visible rather than mistaking readable
            # older observations for confirmation of the live counter delta.
            checkpoint_unavailable = True
    now = time.time()
    minutes = int((now - user.first_seen) / 60) if user.first_seen else 0
    agent_sessions = sum(
        n for agent, n in user.sessions_launched.items() if agent != "bash"
    )
    # Abandonment signal: building time accrued but no recent activity.
    idle_seconds = int(now - user.last_seen) if user.last_seen else None
    saved = _saved_stats(user)
    code, code_observation = _code_observation(user, fresh=fresh, saved=saved)
    discovery_records = _discovery_count(user.email)
    observations_unavailable = checkpoint_unavailable or saved.get("unavailable") or discovery_records is None
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
        "discovery_records": discovery_records,
        "capture_observation": {"code": code_observation, "recovered": user._capture_recovered,
                                "journal": "unavailable" if observations_unavailable else "ready"},
    }


def gather(user: User) -> dict:
    """Certificate view: this user's stats + the workspace census."""
    resources, observation = _resource_observation(_workspace_resources(), [user])
    row = gather_user(user, resources=resources)
    if user._capture_journal is not None:
        now = time.time()
        _checked(user._capture_journal, lambda: user._capture_journal.transaction(
            lambda state: _save_observation(state, row, resources, observation, now)))
    return {
        "schema_version": STATS_SCHEMA_VERSION,
        **row,
        "resources": resources,
        "resource_observation": observation,
    }


def gather_all(users: list[User], *, fresh: bool = False) -> dict:
    """Harvest view for Control Tower: every user (cached code stats) plus
    one instance-level workspace census."""
    from .events import event_hub
    from .sessions import session_manager

    resources, observation = _resource_observation(_workspace_resources(), users)
    return {
        "schema_version": STATS_SCHEMA_VERSION,
        "users": [gather_user(u, fresh=fresh, resources=resources) for u in users],
        "resources": resources,
        "resource_observation": observation,
        "websocket_queues": {
            **session_manager.queue_metrics(),
            "events": event_hub.metrics(),
        },
    }


# Signal events bucket by wall-clock window so a long workshop writes a coarse
# time series instead of one row per poll. 600s matches CT's default poll
# interval: a bucket shorter than the poll would never de-duplicate anything.
_SIGNAL_BUCKET_SECONDS = 600


def _fingerprint(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     allow_nan=False).encode()).hexdigest()


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
            (attendee, event_payload, f"signal:{run_id}:{attendee}:{bucket}:{_fingerprint(event_payload)}")
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
    from .users import user_manager
    emitter._ensure_ready()
    count = 0
    rows = {row["email"]: row for row in payload.get("users", []) if row.get("email")}
    if any(row.get("capture_observation", {}).get("journal") == "unavailable" for row in rows.values()):
        raise CaptureStorageError("capture observations were unavailable; retry the harvest")
    for attendee, event_payload, _key in signal_events(payload, emitter.run_id):
        user = user_manager.peek(attendee)
        retained_user = user if user is not None and user._capture_journal is emitter.journal else None
        with retained_user._capture_lock if retained_user is not None else nullcontext():
            current = user_snapshot(retained_user) if retained_user is not None else None
            baseline = copy.deepcopy(retained_user._capture_baseline) if retained_user is not None else None
            token, now = str(uuid.uuid4()), time.time()
            fingerprint = _fingerprint(event_payload)
            def prepare(state):
                retained = _retained_stats(state)
                previous = retained["signals"].get(attendee)
                if previous and previous["fingerprint"] == fingerprint:
                    return event_payload, previous["key"]
                revision = previous["revision"] + 1 if previous else 1
                key = f"signal:{emitter.run_id}:{attendee}:r{revision}"
                if retained_user is not None:
                    UserManager.checkpoint_state(state, retained_user, current, baseline)
                _save_observation(state, rows[attendee], payload.get("resources") or {},
                                  payload.get("resource_observation") or {}, now)
                retained["signals"][attendee] = {"revision": revision, "fingerprint": fingerprint,
                                                 "key": key, "checkpoint_token": token}
                return event_payload, key
            _checked(emitter.journal, lambda: emitter.emit_transaction("workshop.signal", attendee, prepare))
            if retained_user is not None:
                committed = _checked(emitter.journal, lambda: emitter.journal.read())
                if _retained_stats(committed)["signals"][attendee]["checkpoint_token"] == token:
                    UserManager.accept_checkpoint(retained_user, current, committed["users"][attendee])
                else:
                    # A content-identical retry reuses its original envelope;
                    # output observed since that poll still gets checkpointed.
                    user_manager.checkpoint(retained_user)
        count += 1
    return count
