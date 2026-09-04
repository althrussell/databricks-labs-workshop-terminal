"""SP-driven entitlement reconciler — make SP-created resources usable by the labuser.

Resources are created by the app **service principal** (the ``[DEFAULT]``
profile) because that identity is reliable for long, idle, and cross-1h runs
(unlike the tab-bound OBO token). The cost is that the *attendee* (labuser) would
have no access to anything the SP creates. This reconciler closes that gap:

- **Unity Catalog (zero per-object work):** a single ``ALL_PRIVILEGES`` grant on
  ``WORKSHOP_CATALOG`` to the labuser. UC privileges inherit downward, so every
  current and future schema/table/volume/function the SP creates *inside that
  catalog* is instantly usable by the labuser — and therefore visible through
  the ``me``/OBO profile. (The agent is instructed to create UC objects only
  inside ``$WORKSHOP_CATALOG``.)
- **Non-UC:** resource-specific adapters paginate their APIs, verify the app SP
  as creator/owner, enforce the current run boundary, then transfer ownership
  where supported or grant ``CAN_MANAGE``. Pipelines and SQL warehouses use an
  at-start ID baseline because their list APIs lack reliable creation times.

An in-memory transition ledger records discovered, verified, handed-off, and
failed resources for attendee and Control Tower status surfaces. All calls use
the app SP bearer and emit ``entitlements.health`` on failure.

**Coverage audit.** The adapters below cover what a workshop build actually
produces: apps, jobs, pipelines, serving endpoints, Lakebase instances and
projects, warehouses, Lakeview dashboards, and every Unity Catalog object via
the catalog grant. Deliberately *not* covered, because handing off on an
unverified creator is worse than not handing off at all:

- **Genie spaces** — ``/api/2.0/genie/spaces`` returns neither a creator nor a
  creation time, and there is no second read that supplies one. A ``genie``
  permission type exists, so this becomes addable the moment the list does.
- **Vector search endpoints, experiments, SQL queries and alerts, registered
  models** — permission types exist (``vector-search-endpoints``,
  ``experiments``, ``queries``, ``alerts``, ``registered-models``) but the list
  shapes are unverified against a live workspace. Adding a spec with a wrong
  ``items_key`` or ``id_field`` turns the health check permanently red, so each
  one waits for a measured response. UC-resident models are already covered by
  the catalog grant.

**The models an attendee's CLIs talk to are not in scope here, and that is not
an omission.** Those are Unity Catalog model services in ``system.ai``, reached
through Unity AI Gateway, and the grant that governs them is ``EXECUTE`` on the
model service — held by all account users by default, on a schema no workshop
creates and none should try to hand off. The ``serving-endpoints`` adapter below
is about something else entirely: an endpoint an *attendee* deployed during the
workshop, which is theirs to keep and so needs the handoff. That is also why the
list it reads still returns built-in foundation-model endpoints with no id (see
the note in the scan loop) — the legacy surface lingers in that listing after
having stopped serving traffic.
"""

from __future__ import annotations

import copy
import logging
import os
import random
import threading
import time
from collections.abc import Callable
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import requests

from . import config

logger = logging.getLogger(__name__)

HEARTBEAT = 30  # loop wake interval (seconds)
ALERT_REEMIT_INTERVAL = 1800  # re-emit a degraded health event at most every 30 min


class EntitlementApiError(Exception):
    """Structured Databricks API failure retained through reconciliation."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        error_code: str | None = None,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.error_code = (error_code or "").strip().upper() or None
        self.retry_after = retry_after

    @property
    def rate_limit_reason(self) -> str | None:
        if self.error_code == "RESOURCE_EXHAUSTED":
            return "resource_exhausted"
        if self.status_code == 429:
            return "http_429"
        return None


@dataclass
class _RequestStats:
    requests: int = 0
    rate_limits: int = 0
    http_429s: int = 0
    cache_hits: int = 0
    cache_misses: int = 0
    retry_after: float = 0.0
    rate_limit_reason: str | None = None
    blocked_error: EntitlementApiError | None = None


@dataclass
class _EnumerationCacheEntry:
    items: list[dict]
    refreshed_at: float
    expires_at: float


_request_stats: ContextVar[_RequestStats | None] = ContextVar(
    "entitlement_request_stats", default=None
)


def _retry_after_seconds(response, now: float | None = None) -> float | None:
    raw = str(getattr(response, "headers", {}).get("Retry-After", "") or "").strip()
    if not raw:
        return None
    try:
        return max(0.0, float(raw))
    except ValueError:
        try:
            instant = parsedate_to_datetime(raw).timestamp()
        except (TypeError, ValueError, OverflowError):
            return None
        return max(0.0, instant - (time.time() if now is None else now))


def _response_error_code(response) -> str | None:
    try:
        payload = response.json()
    except (TypeError, ValueError):
        payload = None
    if not isinstance(payload, dict):
        return None
    direct = payload.get("error_code") or payload.get("code")
    nested = payload.get("error")
    if not direct and isinstance(nested, dict):
        direct = nested.get("error_code") or nested.get("code") or nested.get("status")
    return str(direct).strip().upper() if direct else None


def _api_error(response) -> EntitlementApiError:
    status = int(getattr(response, "status_code", 0) or 0) or None
    code = _response_error_code(response)
    text = str(getattr(response, "text", "") or "")[:200]
    if not code and "RESOURCE_EXHAUSTED" in text.upper():
        code = "RESOURCE_EXHAUSTED"
    message = " ".join(
        part for part in (str(status or "request failed"), code, text) if part
    )
    return EntitlementApiError(
        message,
        status_code=status,
        error_code=code,
        retry_after=_retry_after_seconds(response),
    )


def _before_request() -> EntitlementApiError | None:
    stats = _request_stats.get()
    if stats is None:
        return None
    if stats.blocked_error is not None:
        return stats.blocked_error
    stats.requests += 1
    return None


def _observe_error(error: EntitlementApiError) -> None:
    reason = error.rate_limit_reason
    stats = _request_stats.get()
    if stats is None or reason is None:
        return
    stats.rate_limits += 1
    if error.status_code == 429:
        stats.http_429s += 1
    stats.rate_limit_reason = reason
    stats.retry_after = max(stats.retry_after, error.retry_after or 0.0)
    # Once the platform has asked us to stop, do not walk the remaining APIs in
    # the same reconcile and amplify one 429 into a control-plane storm.
    stats.blocked_error = error


@dataclass(frozen=True)
class ResourceSpec:
    label: str
    list_path: str
    items_key: str
    id_field: str
    permission_type: str
    creator_field: str | None
    created_field: str | None
    list_params: dict = field(default_factory=dict)
    supports_owner: bool = False
    requires_baseline: bool = False
    created_millis: bool = False
    unsupported_reason: str | None = None
    detail_path: str | None = None
    creator_is_path: bool = False
    state_field: str | None = None
    skip_states: frozenset[str] = frozenset()


# Every adapter names the API's permission identifier, not merely its display
# name. Pipelines and warehouses do not expose a reliable creation timestamp,
# so they are eligible only when absent from the at-start baseline.
_RESOURCE_SPECS = (
    ResourceSpec(
        "jobs", "/api/2.2/jobs/list", "jobs", "job_id", "jobs",
        "creator_user_name", "created_time", {"limit": 100},
        supports_owner=True, created_millis=True,
    ),
    ResourceSpec(
        "pipelines", "/api/2.0/pipelines", "statuses", "pipeline_id", "pipelines",
        "creator_user_name", None, {"max_results": 100},
        supports_owner=True, requires_baseline=True,
    ),
    ResourceSpec(
        "serving-endpoints", "/api/2.0/serving-endpoints", "endpoints", "id",
        "serving-endpoints", "creator", "creation_timestamp", created_millis=True,
    ),
    ResourceSpec(
        "apps", "/api/2.0/apps", "apps", "name", "apps", "creator", "create_time",
        {"page_size": 100},
    ),
    ResourceSpec(
        "database-instances", "/api/2.0/database/instances", "database_instances",
        "name", "database-instances", "creator", "creation_time", {"page_size": 100},
    ),
    ResourceSpec(
        "database-projects", "/api/2.0/postgres/projects", "projects",
        "project_id", "database-projects", "status.owner", "create_time",
        {"page_size": 100},
    ),
    ResourceSpec(
        "warehouses", "/api/2.0/sql/warehouses", "warehouses", "id", "warehouses",
        "creator_name", None, {"page_size": 100},
        supports_owner=True, requires_baseline=True,
    ),
    # Lakeview exposes no creator field anywhere, but a dashboard created
    # without an explicit parent_path lands in the caller's workspace home, and
    # a service principal's home is /Users/<application-id> — an identity the
    # SCIM lookup below already knows. The list response omits the path, so the
    # creator check costs one detail read per dashboard. A dashboard the agent
    # put in the attendee's own folder fails this check and is correctly left
    # alone: they already inherit CAN_MANAGE from their home directory.
    ResourceSpec(
        "dashboards", "/api/2.0/lakeview/dashboards", "dashboards", "dashboard_id",
        "dashboards", "parent_path", "create_time", {"page_size": 100},
        detail_path="/api/2.0/lakeview/dashboards/{id}",
        creator_is_path=True,
        state_field="lifecycle_state",
        skip_states=frozenset({"TRASHED"}),
    ),
)


_APPS_SPEC = next(spec for spec in _RESOURCE_SPECS if spec.label == "apps")


def _sp_bearer() -> str | None:
    """The app service-principal bearer used for every reconcile call (P1-2:
    auto-refreshed OAuth identity, falls back to a vended PAT)."""
    from .credentials import app_identity_bearer, vended_pat

    return app_identity_bearer() or (vended_pat() or None)


def _patch(
    url: str, bearer: str, body: dict
) -> tuple[bool, EntitlementApiError | None]:
    if deferred := _before_request():
        return False, deferred
    try:
        resp = requests.patch(
            url, headers={"Authorization": f"Bearer {bearer}"}, json=body, timeout=30
        )
    except requests.RequestException as e:
        return False, EntitlementApiError(str(e))
    if 200 <= resp.status_code < 300:
        return True, None
    error = _api_error(resp)
    _observe_error(error)
    return False, error


def _get_json(
    url: str, bearer: str, params: dict | None = None
) -> tuple[dict | None, EntitlementApiError | None]:
    if deferred := _before_request():
        return None, deferred
    try:
        resp = requests.get(
            url,
            headers={"Authorization": f"Bearer {bearer}"},
            params=params or {},
            timeout=30,
        )
    except requests.RequestException as e:
        return None, EntitlementApiError(str(e))
    if resp.status_code != 200:
        error = _api_error(resp)
        _observe_error(error)
        return None, error
    try:
        payload = resp.json()
    except ValueError as e:
        return None, EntitlementApiError(f"invalid JSON: {e}")
    if not isinstance(payload, dict):
        return None, EntitlementApiError("response was not an object")
    return payload, None


def _grant_catalog_all_privileges(
    host: str, bearer: str, catalog: str, principal: str
) -> tuple[bool, EntitlementApiError | None]:
    """Grant the labuser ALL_PRIVILEGES on the catalog (inherited downward)."""
    url = f"{host}/api/2.1/unity-catalog/permissions/catalog/{catalog}"
    body = {"changes": [{"principal": principal, "add": ["ALL_PRIVILEGES"]}]}
    return _patch(url, bearer, body)


# Read plus write inside the attendee's own catalog, and deliberately not
# MANAGE: the app should be able to build and query, not re-grant. Catalog
# scope so it inherits to schemas and tables the app has not created yet.
APP_SP_CATALOG_PRIVILEGES = (
    "USE_CATALOG",
    "USE_SCHEMA",
    "SELECT",
    "MODIFY",
    "CREATE_SCHEMA",
    "CREATE_TABLE",
)


def _grant_catalog_privileges(
    host: str, bearer: str, catalog: str, principal: str, privileges: tuple[str, ...]
) -> tuple[bool, EntitlementApiError | None]:
    url = f"{host}/api/2.1/unity-catalog/permissions/catalog/{catalog}"
    body = {"changes": [{"principal": principal, "add": list(privileges)}]}
    return _patch(url, bearer, body)


def _catalog_privileges_for(
    host: str, bearer: str, catalog: str, principal: str
) -> tuple[set[str] | None, EntitlementApiError | None]:
    payload, error = _get_json(
        f"{host}/api/2.1/unity-catalog/permissions/catalog/{catalog}", bearer
    )
    if error:
        return None, error
    wanted = principal.strip().lower()
    for assignment in (payload or {}).get("privilege_assignments", []) or []:
        if not isinstance(assignment, dict):
            continue
        if str(assignment.get("principal") or "").strip().lower() != wanted:
            continue
        return {str(v) for v in assignment.get("privileges", []) or []}, None
    return set(), None


def _set_catalog_owner(
    host: str, bearer: str, catalog: str, principal: str
) -> tuple[bool, EntitlementApiError | None]:
    """Optionally transfer catalog ownership to the labuser (off by default)."""
    url = f"{host}/api/2.1/unity-catalog/catalogs/{catalog}"
    return _patch(url, bearer, {"owner": principal})


def _verify_catalog_access(
    host: str, bearer: str, catalog: str, principal: str, *, require_owner: bool = False
) -> tuple[bool, EntitlementApiError | str | None]:
    """Prove the attendee can use ``catalog``.

    ``require_owner`` mirrors ``ENTITLEMENT_TRANSFER_OWNERSHIP``: ownership only
    matters when the deployment asked for it. Demanding it unconditionally
    contradicted the default configuration and reported every catalog as broken
    while the attendee's ALL_PRIVILEGES grant was in place and working.
    """
    metadata, error = _get_json(
        f"{host}/api/2.1/unity-catalog/catalogs/{catalog}", bearer
    )
    if error:
        return False, f"catalog metadata read: {error}"
    permissions, error = _get_json(
        f"{host}/api/2.1/unity-catalog/permissions/catalog/{catalog}", bearer
    )
    if error:
        return False, f"catalog permissions read: {error}"
    owner = str((metadata or {}).get("owner") or "").strip().lower()
    privileges: set[str] = set()
    for assignment in (permissions or {}).get("privilege_assignments", []) or []:
        if (
            isinstance(assignment, dict)
            and str(assignment.get("principal") or "").strip().lower()
            == principal.lower()
        ):
            privileges.update(
                str(value) for value in assignment.get("privileges", [])
            )
    if require_owner and owner != principal.lower():
        return False, f"catalog owner is {owner or 'unset'}, expected attendee"
    if "ALL_PRIVILEGES" not in privileges:
        return False, "attendee ALL_PRIVILEGES grant not visible after patch"
    return True, None


def _enumerate(host: str, bearer: str, spec: ResourceSpec) -> list[dict]:
    items: list[dict] = []
    page_token: str | None = None
    seen_tokens: set[str] = set()
    while True:
        params = dict(spec.list_params)
        if page_token:
            params["page_token"] = page_token
        payload, error = _get_json(f"{host}{spec.list_path}", bearer, params)
        if error:
            raise error
        assert payload is not None
        page_items = payload.get(spec.items_key, []) or []
        items.extend(item for item in page_items if isinstance(item, dict))
        next_token = payload.get("next_page_token")
        if not next_token:
            return items
        page_token = str(next_token)
        if page_token in seen_tokens:
            raise RuntimeError("pagination returned a repeated next_page_token")
        seen_tokens.add(page_token)


def _created_by_sp(spec: ResourceSpec, item: dict, sp_identities: set[str]) -> bool:
    """Whether ``item`` is attributable to this app's service principal.

    Specs without a creator field cannot answer, so they say yes and let the
    later per-resource verification decide.
    """
    if not spec.creator_field:
        return True
    creator = str(_field_value(item, spec.creator_field) or "").strip().lower()
    if spec.creator_is_path:
        # A workspace path names its owner in the last segment — /Users/<email>
        # for a person, /Users/<application-id> for a service principal.
        creator = creator.rstrip("/").rsplit("/", 1)[-1]
    return bool(creator) and creator in sp_identities


def _permission_url(host: str, spec: ResourceSpec, resource_id: str) -> str:
    return f"{host}/api/2.0/permissions/{spec.permission_type}/{resource_id}"


def _sp_identities(host: str, bearer: str) -> set[str]:
    identities = {
        value.strip().lower()
        for value in (os.environ.get("DATABRICKS_CLIENT_ID", ""),)
        if value.strip()
    }
    payload, _ = _get_json(
        f"{host}/api/2.0/preview/scim/v2/Me",
        bearer,
        {"attributes": "id,userName,applicationId"},
    )
    if payload:
        for key in ("id", "userName", "applicationId"):
            value = payload.get(key)
            if value:
                identities.add(str(value).strip().lower())
    return identities


def _timestamp_seconds(value, *, milliseconds: bool = False) -> float | None:
    if isinstance(value, (int, float)):
        return float(value) / 1000.0 if milliseconds else float(value)
    if isinstance(value, str) and value.strip():
        raw = value.strip()
        try:
            numeric = float(raw)
        except ValueError:
            try:
                return datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(
                    timezone.utc
                ).timestamp()
            except ValueError:
                return None
        return numeric / 1000.0 if milliseconds else numeric
    return None


def _field_value(item: dict, path: str) -> object | None:
    value: object = item
    for part in path.split("."):
        if not isinstance(value, dict):
            return None
        value = value.get(part)
    return value


class EntitlementManager:
    """Background reconciliation loop + on-demand trigger."""

    def __init__(
        self,
        *,
        run_started_at: float | None = None,
        clock: Callable[[], float] | None = None,
        monotonic: Callable[[], float] | None = None,
        jitter: Callable[[], float] | None = None,
    ) -> None:
        self._clock = clock or time.time
        self._monotonic = monotonic or time.monotonic
        self._jitter = jitter or random.random
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._wake_event = threading.Event()
        self._lock = threading.Lock()
        self._reconcile_lock = threading.Lock()
        self._wake_generation = 0
        self._last_wake_reason = "not_started"
        self._run_started_at = (
            run_started_at if run_started_at is not None else self._clock()
        )
        self._baseline: dict[str, set[str]] = {}
        self._baseline_ready: set[str] = set()
        self._baseline_capture_attempted = False
        self._baseline_retry_pending = False
        self._ledger: dict[tuple[str, str, str], dict] = {}
        self._adapter_errors: dict[tuple[str, str], tuple[str, ...]] = {}
        self._enumeration_cache: dict[str, _EnumerationCacheEntry] = {}
        self._cache_invalidations: dict[str, int] = {}
        self._cache_retry_at: dict[str, float] = {}
        self._last_run_at = 0.0
        self._last_reconcile = 0.0
        self._last_attempt_at = 0.0
        self._ok: bool | None = None
        self._health_state = "pending"
        self._last_error: str | None = None
        self._last_alert_at = 0.0
        self._last_verified_at = 0.0
        self._verified_email: str | None = None
        self._verified_catalog: str | None = None
        self._verification_source: str | None = None
        self._next_attempt_at = 0.0
        self._next_attempt_reason = "not_started"
        self._backoff_attempt = 0
        self._backoff_seconds = 0.0
        self._deferred_reason: str | None = None
        self._idle = False
        self._last_request_count = 0
        self._last_rate_limit_count = 0
        self._last_http_429_count = 0
        self._cache_hits = 0
        self._cache_misses = 0
        self._convergence_started_at: float | None = self._clock()
        self._last_convergence_ms: float | None = None
        # client_id -> catalog it has been proven readable on. Keeps the app-SP
        # grant a one-shot per app rather than a per-sweep re-grant.
        self._app_sp_grants: dict[str, str] = {}

    def start(self) -> None:
        if not config.entitlements_enabled():
            logger.info("entitlement reconciler disabled (ENABLE_ENTITLEMENTS off)")
            return
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._wake_event.clear()
        # Capture pre-existing IDs before any attendee session can create
        # resources. Failure is fail-closed for adapters without create_time.
        self.capture_baseline()
        now = self._clock()
        # App fleets tend to start in one burst.  Keep baseline capture eager
        # (it is the security boundary for timestamp-less resources), but spread
        # the first full scan over the normal cadence.
        with self._lock:
            if not self._deferred_reason:
                self._next_attempt_at = now + self._startup_delay()
                self._next_attempt_reason = "startup_jitter"
        self._thread = threading.Thread(
            target=self._loop, daemon=True, name="entitlement-reconcile"
        )
        self._thread.start()
        logger.info(
            "entitlement reconciler started (catalog=%s, interval=%ss)",
            config.workshop_catalog() or "-",
            config.entitlement_reconcile_interval(),
        )

    def stop(self) -> None:
        self._stop.set()
        self._wake_event.set()

    @staticmethod
    def _resource_label(value: str | None) -> str | None:
        raw = (value or "").strip().lower().replace("_", "-")
        if not raw:
            return None
        aliases = {
            "job": "jobs",
            "pipeline": "pipelines",
            "serving-endpoint": "serving-endpoints",
            "endpoint": "serving-endpoints",
            "app": "apps",
            "database-instance": "database-instances",
            "lakebase": "database-instances",
            "database-project": "database-projects",
            "warehouse": "warehouses",
            "dashboard": "dashboards",
        }
        label = aliases.get(raw, raw)
        if label not in {spec.label for spec in _RESOURCE_SPECS}:
            raise ValueError(f"unknown entitlement resource type: {value}")
        return label

    def wake(self, reason: str, *, resource_type: str | None = None) -> None:
        """Leave idle cadence for a session or known resource event.

        A known resource invalidates only its adapter. Session start invalidates
        every adapter so its wake cannot be satisfied entirely from a cache
        populated before the attendee arrived. Backoff remains a hard floor: a
        launch can make reconciliation urgent, but it must not ignore a platform
        request to stop sending calls.
        """
        label = self._resource_label(resource_type)
        now = self._clock()
        with self._lock:
            self._wake_generation += 1
            self._last_wake_reason = reason
            invalidated = [label] if label else []
            if reason == "session_start" and not invalidated:
                invalidated = [spec.label for spec in _RESOURCE_SPECS]
            for invalidated_label in invalidated:
                self._enumeration_cache.pop(invalidated_label, None)
                self._cache_retry_at.pop(invalidated_label, None)
                self._cache_invalidations[invalidated_label] = (
                    self._wake_generation
                )
            self._idle = False
            if self._convergence_started_at is None:
                self._convergence_started_at = now
            if not self._deferred_reason or now >= self._next_attempt_at:
                self._next_attempt_at = now
                self._next_attempt_reason = reason
        self._wake_event.set()

    def notify_resource_created(self, resource_type: str) -> None:
        self.wake("resource_created", resource_type=resource_type)

    def _jittered_delay(self, minimum: float, maximum: float) -> float:
        fraction = max(0.0, min(1.0, float(self._jitter())))
        return minimum + fraction * max(0.0, maximum - minimum)

    def _startup_delay(self) -> float:
        active = max(HEARTBEAT * 2, config.entitlement_reconcile_interval())
        return self._jittered_delay(min(active, HEARTBEAT * 2), active)

    def _cache_lifetime(self) -> float:
        maximum = float(config.entitlement_cache_ttl())
        minimum = min(
            maximum, max(float(HEARTBEAT), config.entitlement_reconcile_interval())
        )
        return self._jittered_delay(minimum, maximum)

    def capture_baseline(self) -> dict:
        """Snapshot APIs that cannot prove creation time.

        Only resources absent from a successfully captured baseline can be
        handed off later. A failed snapshot never widens access.
        """
        host = config.databricks_host()
        bearer = _sp_bearer()
        result = {"captured": [], "errors": []}
        started_at = self._monotonic()
        with self._lock:
            self._baseline_capture_attempted = True
            self._baseline_retry_pending = True
        if not host or not bearer:
            result["errors"].append("no app service-principal bearer/host")
            return result
        stats = _RequestStats()
        token = _request_stats.set(stats)
        try:
            for spec in _RESOURCE_SPECS:
                if not spec.requires_baseline:
                    continue
                try:
                    items = self._enumerate_resources(host, bearer, spec, force=True)
                    ids = {
                        str(item[spec.id_field])
                        for item in items
                        if item.get(spec.id_field) is not None
                    }
                except Exception as e:  # noqa: BLE001 — fail closed per adapter
                    result["errors"].append(f"{spec.label}: {e}")
                    if isinstance(e, EntitlementApiError) and e.rate_limit_reason:
                        break
                    continue
                with self._lock:
                    self._baseline[spec.label] = ids
                    self._baseline_ready.add(spec.label)
                result["captured"].append(spec.label)
        finally:
            _request_stats.reset(token)
        backoff_seconds = 0.0
        if stats.rate_limits:
            with self._lock:
                previous_ledger = copy.deepcopy(self._ledger)
            backoff_seconds = self._defer_backoff(stats, previous_ledger)
            result["deferred"] = True
            result["next_attempt_at"] = self.status()["next_attempt_at"]
        result["requests"] = stats.requests
        with self._lock:
            self._baseline_retry_pending = any(
                spec.requires_baseline and spec.label not in self._baseline_ready
                for spec in _RESOURCE_SPECS
            )
        from . import telemetry

        telemetry.entitlement_reconcile(
            source="baseline",
            outcome=(
                "degraded_backoff"
                if stats.rate_limits
                else "degraded" if result["errors"] else "ok"
            ),
            reason=(
                stats.rate_limit_reason
                or ("baseline_failed" if result["errors"] else "baseline_complete")
            ),
            duration_ms=(self._monotonic() - started_at) * 1000,
            rate_limited=bool(stats.rate_limits),
            request_count=stats.requests,
            rate_limit_count=stats.rate_limits,
            http_429_count=stats.http_429s,
            backoff_seconds=backoff_seconds,
            cache_hits=stats.cache_hits,
            cache_misses=stats.cache_misses,
            convergence_ms=0,
        )
        return result

    def _capture_missing_baselines(self, host: str, bearer: str) -> list[str]:
        """Retry fail-closed startup baselines before normal reconciliation."""
        with self._lock:
            if not self._baseline_capture_attempted or not self._baseline_retry_pending:
                return []
        errors: list[str] = []
        for spec in _RESOURCE_SPECS:
            with self._lock:
                ready = spec.label in self._baseline_ready
            if not spec.requires_baseline or ready:
                continue
            try:
                items = self._enumerate_resources(host, bearer, spec, force=True)
            except Exception as exc:  # noqa: BLE001 — fail closed per adapter
                errors.append(f"{spec.label} baseline: {exc}")
                if isinstance(exc, EntitlementApiError) and exc.rate_limit_reason:
                    break
                continue
            identifiers = {
                str(item[spec.id_field])
                for item in items
                if item.get(spec.id_field) is not None
            }
            with self._lock:
                self._baseline[spec.label] = identifiers
                self._baseline_ready.add(spec.label)
        with self._lock:
            self._baseline_retry_pending = any(
                spec.requires_baseline and spec.label not in self._baseline_ready
                for spec in _RESOURCE_SPECS
            )
        return errors

    def _loop(self) -> None:
        while not self._stop.is_set():
            now = self._clock()
            with self._lock:
                deadline = self._next_attempt_at
            timeout = (
                min(HEARTBEAT, max(0.0, deadline - now)) if deadline else HEARTBEAT
            )
            self._wake_event.wait(timeout=timeout)
            self._wake_event.clear()
            if self._stop.is_set():
                return
            with self._lock:
                due = self._next_attempt_at <= self._clock()
            if not due:
                continue
            try:
                self.reconcile()
            except Exception as e:  # noqa: BLE001 — the loop must never die
                logger.error("entitlement reconcile failed unexpectedly: %s", e)
                from . import telemetry

                telemetry.emit(
                    "background.task_failed",
                    "system",
                    {
                        "code": "unhandled_exception",
                        "source": "entitlements",
                        "exception_type": type(e).__name__,
                    },
                )

    def _enumerate_resources(
        self,
        host: str,
        bearer: str,
        spec: ResourceSpec,
        *,
        force: bool = False,
    ) -> list[dict]:
        now = self._clock()
        stats = _request_stats.get()
        with self._lock:
            cached = self._enumeration_cache.get(spec.label)
            invalidation = self._cache_invalidations.get(spec.label)
            retry_at = self._cache_retry_at.get(spec.label, 0.0)
            if not force and retry_at > now:
                raise EntitlementApiError(
                    f"resource enumeration retry deferred until {retry_at:.3f}"
                )
            if (
                not force
                and invalidation is None
                and cached
                and cached.expires_at > now
            ):
                if stats:
                    stats.cache_hits += 1
                return copy.deepcopy(cached.items)
        if stats:
            stats.cache_misses += 1
        try:
            items = _enumerate(host, bearer, spec)
        except Exception as exc:
            rate_limited = (
                isinstance(exc, EntitlementApiError)
                and exc.rate_limit_reason is not None
            )
            with self._lock:
                # Once a live refresh fails, its older list is no longer proof
                # that enumeration is healthy. Never let a forced failure fall
                # back to that stale entry on the next background pass.
                self._enumeration_cache.pop(spec.label, None)
            if rate_limited:
                # The retry must exercise the endpoint that throttled us. If an
                # explicit refresh left an older entry behind, accepting that
                # entry would report recovery without proving the list API did.
                with self._lock:
                    self._cache_retry_at.pop(spec.label, None)
            else:
                with self._lock:
                    self._cache_retry_at[spec.label] = now + max(
                        HEARTBEAT, config.entitlement_reconcile_interval()
                    )
            raise
        expires_at = now + self._cache_lifetime()
        with self._lock:
            current_invalidation = self._cache_invalidations.get(spec.label)
            # A resource notification can land while the list request is in
            # flight. Its response may predate that resource, so do not let it
            # refill the cache or consume the newer invalidation.
            if current_invalidation in (None, invalidation):
                self._enumeration_cache[spec.label] = _EnumerationCacheEntry(
                    items=copy.deepcopy(items),
                    refreshed_at=now,
                    expires_at=expires_at,
                )
                if current_invalidation is not None:
                    self._cache_invalidations.pop(spec.label, None)
            self._cache_retry_at.pop(spec.label, None)
        return items

    def _next_cache_expiry(self, now: float) -> float | None:
        deadlines: list[float] = []
        with self._lock:
            for label, entry in self._enumeration_cache.items():
                if entry.expires_at > now:
                    deadlines.append(entry.expires_at)
                else:
                    deadlines.append(self._cache_retry_at.get(label, now))
            deadlines.extend(
                retry_at
                for label, retry_at in self._cache_retry_at.items()
                if label not in self._enumeration_cache
            )
        return min(deadlines) if deadlines else None

    def reconcile(
        self,
        email: str | None = None,
        resource_type: str | None = None,
        *,
        source: str | None = None,
    ) -> dict:
        """Idempotently make SP-created resources usable by the labuser(s).

        With ``email`` set, reconciles just that attendee (the on-demand
        ``workshop-grant-me`` path); otherwise every known attendee. Never
        raises — failures are recorded and surfaced via health + status.
        """
        if not config.entitlements_enabled():
            return {"enabled": False}

        label = self._resource_label(resource_type)
        source = source or ("on_demand" if email else "background")
        deferred, backoff_remaining = self._backoff_response()
        if deferred is not None:
            from . import telemetry

            telemetry.entitlement_reconcile(
                source=source,
                outcome="degraded_backoff",
                reason="backoff_active",
                duration_ms=0,
                rate_limited=False,
                request_count=0,
                rate_limit_count=0,
                backoff_seconds=backoff_remaining,
                cache_hits=0,
                cache_misses=0,
                convergence_ms=0,
            )
            return deferred

        with self._reconcile_lock:
            # The first check can race with a caller already reconciling. Check
            # again after serialization so a queued caller cannot immediately
            # undo a 429 backoff chosen by the request ahead of it.
            deferred, backoff_remaining = self._backoff_response()
            if deferred is not None:
                from . import telemetry

                telemetry.entitlement_reconcile(
                    source=source,
                    outcome="degraded_backoff",
                    reason="backoff_active",
                    duration_ms=0,
                    rate_limited=False,
                    backoff_seconds=backoff_remaining,
                )
                return deferred
            try:
                return self._reconcile_once(email, label=label, source=source)
            except Exception as exc:  # noqa: BLE001 — public reconcile is fail-soft
                # ``finish`` normally restores the ContextVar. Keep an
                # unexpected adapter exception from poisoning this worker's
                # next request before the outer loop reports the failure.
                stats = _request_stats.get() or _RequestStats()
                _request_stats.set(None)
                message = f"unexpected {type(exc).__name__}: {exc}"
                self._record(False, message)
                from . import telemetry

                telemetry.entitlement_reconcile(
                    source=source,
                    outcome="degraded",
                    reason="unhandled_exception",
                    duration_ms=0,
                    rate_limited=False,
                    request_count=stats.requests,
                    rate_limit_count=stats.rate_limits,
                    cache_hits=stats.cache_hits,
                    cache_misses=stats.cache_misses,
                )
                telemetry.emit(
                    "background.task_failed",
                    "system",
                    {
                        "code": "unhandled_exception",
                        "source": "entitlements",
                        "exception_type": type(exc).__name__,
                    },
                )
                return {
                    "enabled": True,
                    "errors": [message],
                    "handoff": self._handoff_snapshot(),
                }

    def _backoff_response(self) -> tuple[dict | None, float]:
        now = self._clock()
        with self._lock:
            if not self._deferred_reason or now >= self._next_attempt_at:
                return None, 0.0
            return (
                {
                    "enabled": True,
                    "deferred": True,
                    "state": "degraded_backoff",
                    "reason": self._deferred_reason,
                    "next_attempt_at": self._next_attempt_at,
                    "errors": [],
                    "handoff": self._handoff_snapshot_locked(),
                },
                self._next_attempt_at - now,
            )

    def _reconcile_once(
        self, email: str | None, *, label: str | None, source: str
    ) -> dict:
        started_at = self._monotonic()
        now = self._clock()
        stats = _RequestStats()
        stats_token = _request_stats.set(stats)
        with self._lock:
            self._last_run_at = now
            self._last_attempt_at = now
            attempt_wake_generation = self._wake_generation
            prior_ok = self._ok
            before_signature = self._ledger_signature_locked()
            before_ledger = copy.deepcopy(self._ledger)
            before_adapter_errors = copy.deepcopy(self._adapter_errors)
            before_app_sp_grants = dict(self._app_sp_grants)
            before_verification = (
                self._last_verified_at,
                self._verified_email,
                self._verified_catalog,
                self._verification_source,
            )
            if source != "background" and self._convergence_started_at is None:
                self._convergence_started_at = now

        from .users import user_manager

        def finish(result: dict, reason: str) -> dict:
            try:
                errors = [str(error) for error in result.get("errors", [])]
                with self._lock:
                    after_signature = self._ledger_signature_locked()
                no_change = (
                    not errors
                    and prior_ok is True
                    and before_signature == after_signature
                )
                if stats.rate_limits:
                    backoff_seconds = self._defer_backoff(
                        stats,
                        before_ledger,
                        before_verification,
                        before_app_sp_grants,
                        before_adapter_errors,
                    )
                    outcome = "degraded_backoff"
                    reason_code = stats.rate_limit_reason or "rate_limited"
                    convergence_ms = 0.0
                    result.update(
                        {
                            "deferred": True,
                            "state": "degraded_backoff",
                            "reason": reason_code,
                            "next_attempt_at": self.status()["next_attempt_at"],
                        }
                    )
                else:
                    convergence_ms = self._record(
                        not errors,
                        "; ".join(errors[:5]) if errors else None,
                        verified_no_change=no_change,
                    )
                    self._schedule_pending_wake(attempt_wake_generation)
                    backoff_seconds = 0.0
                    outcome = "ok" if not errors else "degraded"
                    reason_code = reason
                with self._lock:
                    self._last_request_count = stats.requests
                    self._last_rate_limit_count = stats.rate_limits
                    self._last_http_429_count = stats.http_429s
                    self._cache_hits += stats.cache_hits
                    self._cache_misses += stats.cache_misses
                result["requests"] = {
                    "count": stats.requests,
                    "rate_limited": stats.rate_limits,
                    "http_429": stats.http_429s,
                    "cache_hits": stats.cache_hits,
                    "cache_misses": stats.cache_misses,
                }
                from . import telemetry

                telemetry.entitlement_reconcile(
                    source=source,
                    outcome=outcome,
                    reason=reason_code,
                    duration_ms=(self._monotonic() - started_at) * 1000,
                    rate_limited=bool(stats.rate_limits),
                    request_count=stats.requests,
                    rate_limit_count=stats.rate_limits,
                    http_429_count=stats.http_429s,
                    backoff_seconds=backoff_seconds,
                    cache_hits=stats.cache_hits,
                    cache_misses=stats.cache_misses,
                    convergence_ms=convergence_ms,
                )
                return result
            finally:
                _request_stats.reset(stats_token)

        if email:
            emails = [email]
        else:
            # The instance-wide binding, not just whoever has already knocked.
            # Control Tower gates admission on /readyz, and this reconciler's
            # proof is one of the checks — so keying it on the roster alone made
            # every fresh instance red until the attendee arrived, which is the
            # one moment the gate exists to happen before. Measured on a live
            # deployment: "no attendee available for entitlement verification"
            # on an instance whose attendee was configured all along.
            from . import attendee as attendee_binding

            bound = attendee_binding.resolved_email()
            emails = [u.email for u in user_manager.all()]
            if bound and bound not in emails:
                emails.insert(0, bound)
        result: dict = {
            "enabled": True,
            "emails": emails,
            "catalog": None,
            "non_uc": {},
            "errors": [],
        }
        if not emails:
            msg = "no attendee available for entitlement verification"
            result["errors"] = [msg]
            return finish(result, "no_attendee")

        bearer = _sp_bearer()
        host = config.databricks_host()
        if not bearer or not host:
            msg = "no app service-principal bearer/host — cannot reconcile entitlements"
            result["errors"] = [msg]
            return finish(result, "credential_unavailable")

        errors = self._capture_missing_baselines(host, bearer)
        catalog = config.workshop_catalog()
        if not catalog:
            # Not an error: an event can run entitlements on without handing out
            # a per-attendee catalog, and plenty of builds never touch Unity
            # Catalog at all (a game, a landing page). Control Tower only injects
            # WORKSHOP_CATALOG when the app is configured for entitlements, while
            # the app defaults ENABLE_ENTITLEMENTS on — so this combination is
            # the normal case, not a misconfiguration.
            #
            # Reporting it turned the entitlements check red and made
            # `workshop-grant-me` print a catalog complaint at an attendee whose
            # app grant had just succeeded, over something they could not act on.
            # Same reasoning as the missing-id case in _handoff_resources below.
            logger.debug("no WORKSHOP_CATALOG set — skipping the catalog grant")
        else:
            for em in emails:
                if not em or "@" not in em:
                    errors.append("invalid attendee identity for catalog verification")
                    continue
                ok, err = _grant_catalog_all_privileges(host, bearer, catalog, em)
                if not ok and err:
                    errors.append(f"catalog grant {em}: {err}")
            if config.entitlement_transfer_ownership() and emails:
                ok, err = _set_catalog_owner(host, bearer, catalog, emails[0])
                if not ok and err:
                    errors.append(f"catalog owner: {err}")
            result["catalog"] = catalog
            for em in emails:
                if not em or "@" not in em:
                    continue
                verified, error = _verify_catalog_access(
                    host,
                    bearer,
                    catalog,
                    em,
                    require_owner=config.entitlement_transfer_ownership(),
                )
                if not verified:
                    errors.append(
                        f"catalog verification {em}: {error or 'failed'}"
                    )
                else:
                    with self._lock:
                        self._last_verified_at = self._clock()
                        self._verified_email = em
                        self._verified_catalog = catalog
                        self._verification_source = source

        identities = _sp_identities(host, bearer)
        if not identities:
            errors.append("cannot verify app service-principal identity")
        else:
            for em in emails:
                if "@" not in em:
                    continue
                counts, errs = self._handoff_resources(
                    host,
                    bearer,
                    em,
                    identities,
                    catalog,
                    force_all=source != "background" and label is None,
                    force_label=label,
                )
                result["non_uc"][em] = counts
                errors.extend(errs)

        # A targeted pass proves only the adapter it visited. Preserve failures
        # from every untouched adapter so a narrow success cannot turn global
        # readiness green or move the reconciler into idle cadence.
        for unresolved in self._adapter_error_messages():
            if unresolved not in errors:
                errors.append(unresolved)
        result["errors"] = errors
        result["handoff"] = self._handoff_snapshot()
        return finish(result, "reconcile_failed" if errors else "complete")

    def _handoff_resources(
        self,
        host: str,
        bearer: str,
        principal: str,
        sp_identities: set[str],
        catalog: str | None = None,
        *,
        force_all: bool = False,
        force_label: str | None = None,
    ) -> tuple[dict[str, int], list[str]]:
        counts: dict[str, int] = {}
        errors: list[str] = []
        for spec in _RESOURCE_SPECS:
            if force_label and spec.label != force_label:
                continue
            adapter_error_start = len(errors)
            try:
                items = self._enumerate_resources(
                    host,
                    bearer,
                    spec,
                    force=force_all or force_label == spec.label,
                )
            except Exception as e:  # noqa: BLE001 — isolate API failures
                message = f"{spec.label} list: {e}"
                errors.append(message)
                if not isinstance(e, EntitlementApiError) or not e.rate_limit_reason:
                    self._transition(
                        principal, spec.label, "<discovery>", "failed", error=message
                    )
                self._replace_adapter_errors(
                    principal, spec.label, errors[adapter_error_start:]
                )
                if isinstance(e, EntitlementApiError) and e.rate_limit_reason:
                    break
                continue
            self._clear_stale_adapter_failures(principal, spec, items)
            granted = 0
            for item in items:
                if spec.skip_states and str(
                    item.get(spec.state_field or "") or ""
                ).strip().upper() in spec.skip_states:
                    # Deleted resources keep appearing in list responses for a
                    # while. Granting on one is harmless but it lands in the
                    # attendee's handoff ledger as something they can open.
                    continue
                raw_id = item.get(spec.id_field)
                if raw_id is None:
                    # Built-in resources are listed alongside the app's own and
                    # need no handoff — foundation-model serving endpoints carry
                    # no id at all. Reporting their absent field turned the
                    # entitlements check red in every workspace over something no
                    # attendee could act on, so only an id we actually needed is
                    # worth an error.
                    if _created_by_sp(spec, item, sp_identities):
                        errors.append(f"{spec.label}: response missing {spec.id_field}")
                    continue
                resource_id = str(raw_id)

                if spec.label == "apps" and catalog:
                    # Before the handoff logic, and outside its already-done
                    # short circuit: an app whose CAN_MANAGE handoff succeeded
                    # on an earlier sweep skips the rest of this loop, and its
                    # catalog grant would never be retried if it had failed.
                    errors.extend(
                        self._grant_app_sp_catalog_access(
                            host, bearer, catalog, principal, item, sp_identities
                        )
                    )

                previous_level = self._handed_off_level(
                    principal, spec.label, resource_id
                )
                if previous_level:
                    durable, verification_error, effective_level = (
                        self._verify_resource_permission(
                            host, bearer, spec, resource_id, principal
                        )
                    )
                    if verification_error:
                        message = f"permission verification: {verification_error}"
                        errors.append(f"{spec.label} {resource_id}: {message}")
                        self._transition(
                            principal, spec.label, resource_id, "failed", error=message
                        )
                        continue
                    if durable:
                        granted += 1
                        self._transition(
                            principal,
                            spec.label,
                            resource_id,
                            "handed_off",
                            permission_level=effective_level,
                        )
                        continue
                self._transition(principal, spec.label, resource_id, "discovered")

                if spec.unsupported_reason:
                    self._transition(
                        principal,
                        spec.label,
                        resource_id,
                        "unsupported",
                        error=spec.unsupported_reason,
                    )
                    continue

                verified, verification_error = self._verify_resource(
                    host, bearer, spec, resource_id, item, sp_identities
                )
                if verification_error:
                    errors.append(f"{spec.label} {resource_id}: {verification_error}")
                    self._transition(
                        principal,
                        spec.label,
                        resource_id,
                        "failed",
                        error=verification_error,
                    )
                    continue
                if not verified:
                    continue
                self._transition(
                    principal, spec.label, resource_id, "verified_creator"
                )

                ok, err, permission_level = self._grant_resource(
                    host, bearer, spec, resource_id, principal
                )
                if ok:
                    granted += 1
                    self._transition(
                        principal,
                        spec.label,
                        resource_id,
                        "handed_off",
                        permission_level=permission_level,
                    )
                else:
                    message = err or "permission handoff failed"
                    errors.append(f"{spec.label} {resource_id}: {message}")
                    self._transition(
                        principal,
                        spec.label,
                        resource_id,
                        "failed",
                        error=message,
                    )
            counts[spec.label] = granted
            self._replace_adapter_errors(
                principal, spec.label, errors[adapter_error_start:]
            )
        return counts, errors

    def _grant_app_sp_catalog_access(
        self,
        host: str,
        bearer: str,
        catalog: str,
        principal: str,
        app: dict,
        sp_identities: set[str],
    ) -> list[str]:
        """Give an attendee-built app's own service principal access to the catalog.

        When an attendee's agent deploys an app, Databricks mints a service
        principal for it. That principal is new, so it is in none of the groups
        the catalog was granted to and it holds nothing; the app then cannot
        read the data it was built against. At servco this surfaced as "does not
        have USE CATALOG privilege" and "does not have SELECT privilege" and was
        cleared by hand mid-event.

        This is a **backstop**. Control Tower grants the whole workspace read on
        the attendee's catalog at provision time, which covers these principals
        without anyone discovering them, and covers them before this reconciler
        has run. This exists for the instance whose provisioning predates that,
        or whose catalog is not the provisioned one.

        Once per app, not once per sweep. A repeated identical grant is a write
        against the control plane for no change, and at a hundred instances
        sweeping every few minutes that is exactly the shape of load that
        produced the 429s in the servco handoff ledgers. A failed grant is not
        memoized, so it retries.
        """
        if not _created_by_sp(_APPS_SPEC, app, sp_identities):
            # An app this SP did not create is not the attendee's build. Most
            # likely it is the Workshop Terminal itself, whose grants belong to
            # Control Tower.
            return []
        client_id = str(
            app.get("service_principal_client_id")
            or app.get("service_principal_id")
            or ""
        ).strip()
        app_name = str(app.get("name") or client_id or "<unnamed>")
        if not client_id:
            # Apps report their principal only once it exists. A just-created
            # app has none yet and will on the next sweep, so this is a normal
            # intermediate state rather than something to report.
            return []
        with self._lock:
            if self._app_sp_grants.get(client_id) == catalog:
                return []

        ok, error = _grant_catalog_privileges(
            host, bearer, catalog, client_id, APP_SP_CATALOG_PRIVILEGES
        )
        if not ok:
            message = f"app service principal catalog grant: {error or 'failed'}"
            self._transition(
                principal, "app-service-principals", app_name, "failed", error=message
            )
            return [f"app {app_name} ({client_id}): {message}"]

        held, read_error = _catalog_privileges_for(host, bearer, catalog, client_id)
        if read_error:
            message = f"app service principal grant readback: {read_error}"
            self._transition(
                principal, "app-service-principals", app_name, "failed", error=message
            )
            return [f"app {app_name} ({client_id}): {message}"]
        missing = [
            p
            for p in APP_SP_CATALOG_PRIVILEGES
            if p not in (held or set()) and "ALL_PRIVILEGES" not in (held or set())
        ]
        if missing:
            # A PATCH that returned 200 is not proof. Report what is actually
            # absent, because the attendee-visible symptom names a privilege.
            message = (
                f"app service principal missing {', '.join(missing)} on {catalog} "
                f"after grant"
            )
            self._transition(
                principal, "app-service-principals", app_name, "failed", error=message
            )
            return [f"app {app_name} ({client_id}): {message}"]

        with self._lock:
            self._app_sp_grants[client_id] = catalog
        self._transition(
            principal,
            "app-service-principals",
            app_name,
            "handed_off",
            permission_level=",".join(APP_SP_CATALOG_PRIVILEGES),
        )
        return []

    def _handed_off_level(
        self, principal: str, resource_type: str, resource_id: str
    ) -> str | None:
        with self._lock:
            entry = self._ledger.get((principal, resource_type, resource_id))
            if entry and entry["state"] == "handed_off":
                return str(entry.get("permission_level") or "") or None
            return None

    def _verify_resource(
        self,
        host: str,
        bearer: str,
        spec: ResourceSpec,
        resource_id: str,
        item: dict,
        sp_identities: set[str],
    ) -> tuple[bool, str | None]:
        if spec.unsupported_reason:
            return False, spec.unsupported_reason
        if (
            spec.detail_path
            and spec.creator_field
            and _field_value(item, spec.creator_field) is None
        ):
            # Summary listings drop the fields that identify a creator. Read the
            # resource itself rather than handing off on a guess.
            detail, error = _get_json(
                f"{host}{spec.detail_path.format(id=resource_id)}", bearer
            )
            if error:
                return False, f"detail read: {error}"
            item = {**item, **(detail or {})}
        if not _created_by_sp(spec, item, sp_identities):
            return False, None

        if spec.requires_baseline:
            with self._lock:
                ready = spec.label in self._baseline_ready
                existed = resource_id in self._baseline.get(spec.label, set())
            if not ready:
                return False, "at-start baseline unavailable; refusing handoff"
            return (not existed), None

        created_at = _timestamp_seconds(
            item.get(spec.created_field or ""),
            milliseconds=spec.created_millis,
        )
        if created_at is None:
            return False, "creation time unavailable; refusing handoff"
        return created_at >= self._run_started_at, None

    def _grant_resource(
        self,
        host: str,
        bearer: str,
        spec: ResourceSpec,
        resource_id: str,
        principal: str,
    ) -> tuple[bool, str | None, str]:
        url = _permission_url(host, spec, resource_id)
        if spec.supports_owner:
            owner_body = {
                "access_control_list": [{
                    "user_name": principal,
                    "permission_level": "IS_OWNER",
                }]
            }
            ok, _ = _patch(url, bearer, owner_body)
            if ok:
                verified, error, effective = self._verify_resource_permission(
                    host, bearer, spec, resource_id, principal
                )
                if not verified:
                    return (
                        False,
                        f"permission verification: {error or 'required effective access absent'}",
                        "IS_OWNER",
                    )
                return True, None, effective or "IS_OWNER"
        body = {
            "access_control_list": [{
                "user_name": principal,
                "permission_level": "CAN_MANAGE",
            }]
        }
        ok, error = _patch(url, bearer, body)
        if not ok:
            return False, error, "CAN_MANAGE"
        verified, verification_error, effective = self._verify_resource_permission(
            host, bearer, spec, resource_id, principal
        )
        if not verified:
            return (
                False,
                f"permission verification: {verification_error or 'required effective access absent'}",
                "CAN_MANAGE",
            )
        return True, None, effective or "CAN_MANAGE"

    def _verify_resource_permission(
        self,
        host: str,
        bearer: str,
        spec: ResourceSpec,
        resource_id: str,
        principal: str,
    ) -> tuple[bool, str | None, str | None]:
        payload, error = _get_json(
            _permission_url(host, spec, resource_id), bearer
        )
        if error:
            return False, error, None
        for entry in (payload or {}).get("access_control_list", []) or []:
            if not isinstance(entry, dict):
                continue
            identity = str(
                entry.get("user_name")
                or entry.get("service_principal_name")
                or entry.get("group_name")
                or ""
            ).strip().lower()
            if identity != principal.lower():
                continue
            levels = {
                str(permission.get("permission_level") or "")
                for permission in entry.get("all_permissions", []) or []
                if isinstance(permission, dict)
            }
            if "IS_OWNER" in levels:
                return True, None, "IS_OWNER"
            if "CAN_MANAGE" in levels:
                return True, None, "CAN_MANAGE"
        return False, None, None

    def _transition(
        self,
        principal: str,
        resource_type: str,
        resource_id: str,
        state: str,
        *,
        error: str | None = None,
        permission_level: str | None = None,
    ) -> None:
        key = (principal, resource_type, resource_id)
        with self._lock:
            entry = self._ledger.setdefault(key, {
                "email": principal,
                "resource_type": resource_type,
                "resource_id": resource_id,
                "state": state,
                "states": [],
                "error": None,
                "permission_level": None,
                "updated_at": 0.0,
            })
            if not entry["states"] or entry["states"][-1] != state:
                entry["states"].append(state)
            entry["state"] = state
            entry["error"] = error
            if permission_level:
                entry["permission_level"] = permission_level
            entry["updated_at"] = self._clock()

    def _replace_adapter_errors(
        self, principal: str, resource_type: str, errors: list[str]
    ) -> None:
        """Replace the current failure proof for one principal/adapter pass."""
        key = (principal, resource_type)
        with self._lock:
            if errors:
                self._adapter_errors[key] = tuple(errors)
            else:
                self._adapter_errors.pop(key, None)

    def _adapter_error_messages(self) -> list[str]:
        """Return unresolved failures from adapters not visited by this pass."""
        with self._lock:
            return [
                message
                for messages in self._adapter_errors.values()
                for message in messages
            ]

    def _clear_stale_adapter_failures(
        self, principal: str, spec: ResourceSpec, items: list[dict]
    ) -> None:
        """Remove failed ledger entries disproven by a successful fresh list.

        A discovery failure is resolved by any successful list. A failed
        resource that no longer appears has been deleted and is no longer an
        operational entitlement failure. Entries still present are updated by
        the normal verification loop below.
        """
        current_ids = {
            str(item[spec.id_field])
            for item in items
            if item.get(spec.id_field) is not None
        }
        with self._lock:
            stale = [
                key
                for key, entry in self._ledger.items()
                if key[0] == principal
                and key[1] == spec.label
                and entry.get("state") == "failed"
                and (key[2] == "<discovery>" or key[2] not in current_ids)
            ]
            for key in stale:
                self._ledger.pop(key, None)

    def _handoff_snapshot(self) -> dict:
        with self._lock:
            return self._handoff_snapshot_locked()

    def _handoff_snapshot_locked(self) -> dict:
        details = [copy.deepcopy(entry) for entry in self._ledger.values()]
        summary = {
            state: sum(1 for entry in details if entry["state"] == state)
            for state in (
                "discovered",
                "verified_creator",
                "handed_off",
                "unsupported",
                "failed",
            )
        }
        return {
            "summary": summary,
            "details": sorted(
                details,
                key=lambda entry: (
                    entry["email"],
                    entry["resource_type"],
                    entry["resource_id"],
                ),
            ),
        }

    def _ledger_signature_locked(self) -> tuple:
        return tuple(
            sorted(
                (
                    key,
                    entry.get("state"),
                    entry.get("error"),
                    entry.get("permission_level"),
                )
                for key, entry in self._ledger.items()
            )
        )

    def _defer_backoff(
        self,
        stats: _RequestStats,
        previous_ledger: dict,
        previous_verification: tuple[float, str | None, str | None, str | None]
        | None = None,
        previous_app_sp_grants: dict[str, str] | None = None,
        previous_adapter_errors: dict[tuple[str, str], tuple[str, ...]]
        | None = None,
    ) -> float:
        now = self._clock()
        with self._lock:
            self._ledger = previous_ledger
            if previous_verification is not None:
                (
                    self._last_verified_at,
                    self._verified_email,
                    self._verified_catalog,
                    self._verification_source,
                ) = previous_verification
            if previous_app_sp_grants is not None:
                self._app_sp_grants = previous_app_sp_grants
            if previous_adapter_errors is not None:
                self._adapter_errors = previous_adapter_errors
            self._backoff_attempt += 1
            window = min(
                float(config.entitlement_backoff_cap()),
                float(config.entitlement_backoff_base())
                * (2 ** min(self._backoff_attempt - 1, 16)),
            )
            jittered = self._jittered_delay(1.0, max(1.0, window))
            delay = min(
                float(config.entitlement_backoff_cap()),
                max(jittered, stats.retry_after),
            )
            self._backoff_seconds = delay
            self._deferred_reason = stats.rate_limit_reason or "rate_limited"
            self._health_state = "degraded_backoff"
            self._next_attempt_at = now + delay
            self._next_attempt_reason = self._deferred_reason
            self._idle = False
            if self._convergence_started_at is None:
                self._convergence_started_at = now
        logger.warning(
            "entitlement reconcile deferred for %.1fs (%s)",
            delay,
            stats.rate_limit_reason or "rate_limited",
        )
        self._wake_event.set()
        return delay

    def _record(
        self, ok: bool, error: str | None, *, verified_no_change: bool = False
    ) -> float:
        now = self._clock()
        cache_expiry = self._next_cache_expiry(now)
        convergence_ms = 0.0
        with self._lock:
            changed = ok != self._ok
            self._ok = ok
            self._last_error = error
            self._last_reconcile = now
            self._deferred_reason = None
            self._backoff_attempt = 0
            self._backoff_seconds = 0.0
            if ok:
                self._health_state = "healthy"
                self._idle = verified_no_change
                if self._convergence_started_at is not None:
                    convergence_ms = max(
                        0.0, (now - self._convergence_started_at) * 1000
                    )
                    self._last_convergence_ms = convergence_ms
                    self._convergence_started_at = None
                cadence = (
                    config.entitlement_idle_interval()
                    if self._idle
                    else config.entitlement_reconcile_interval()
                )
                self._next_attempt_reason = (
                    "cache_expiry" if cache_expiry and cache_expiry < now + cadence
                    else "idle_cadence" if self._idle
                    else "active_cadence"
                )
                self._next_attempt_at = min(
                    now + cadence, cache_expiry or float("inf")
                )
            else:
                self._health_state = "unhealthy"
                self._idle = False
                self._next_attempt_at = now + max(
                    HEARTBEAT, config.entitlement_reconcile_interval()
                )
                self._next_attempt_reason = "failed_verification"
                if self._convergence_started_at is None:
                    self._convergence_started_at = now
        if not ok:
            logger.warning("entitlement reconcile degraded: %s", error)
        elif changed:
            logger.info("entitlements healthy — labuser access reconciled")
        if changed or (not ok and now - self._last_alert_at > ALERT_REEMIT_INTERVAL):
            self._last_alert_at = now
            self._emit_health(ok, error)
        self._wake_event.set()
        return convergence_ms

    def _schedule_pending_wake(self, attempt_wake_generation: int) -> None:
        """Preserve notifications that arrived while an attempt was running.

        ``_record`` chooses the normal active/idle cadence. A concurrent wake
        must win over that choice, otherwise its event has already been set but
        the loop observes a future deadline and quietly discards the request.
        Targeted invalidations that failed for a non-rate-limit reason retain
        their per-adapter retry deadline instead of spinning immediately.
        """
        now = self._clock()
        with self._lock:
            generation_changed = self._wake_generation != attempt_wake_generation
            pending_deadline = min(
                (
                    self._cache_retry_at.get(label, now)
                    for label in self._cache_invalidations
                ),
                default=None,
            )
            if not generation_changed and pending_deadline is None:
                return
            deadline = (
                max(now, pending_deadline)
                if pending_deadline is not None
                else now
            )
            if not self._next_attempt_at or deadline < self._next_attempt_at:
                self._next_attempt_at = deadline
                self._next_attempt_reason = (
                    self._last_wake_reason
                    if generation_changed
                    else "resource_refresh"
                )
            self._idle = False
        self._wake_event.set()

    def _emit_health(self, ok: bool, error: str | None) -> None:
        try:
            from .event_emitter import event_emitter

            event_emitter.emit(
                "entitlements.health",
                "system",
                {"ok": ok, "error": error, "catalog": config.workshop_catalog() or None},
            )
        except Exception:  # noqa: BLE001 — alerting must never break the loop
            pass

    def status(self) -> dict:
        with self._lock:
            ok = self._ok
            last_reconcile = self._last_reconcile
            last_error = self._last_error
            last_verified_at = self._last_verified_at
            verified_email = self._verified_email
            verified_catalog = self._verified_catalog
            verification_source = self._verification_source
            health_state = self._health_state
            last_attempt_at = self._last_attempt_at
            next_attempt_at = self._next_attempt_at
            next_attempt_reason = self._next_attempt_reason
            deferred_reason = self._deferred_reason
            backoff_seconds = self._backoff_seconds
            backoff_attempt = self._backoff_attempt
            idle = self._idle
            last_request_count = self._last_request_count
            last_rate_limit_count = self._last_rate_limit_count
            last_http_429_count = self._last_http_429_count
            cache_hits = self._cache_hits
            cache_misses = self._cache_misses
            convergence_ms = self._last_convergence_ms
            baseline_ready = sorted(self._baseline_ready)
            baseline_retry_pending = self._baseline_retry_pending
            handoff = self._handoff_snapshot_locked()
            cache = {
                spec.label: {
                    "cached": spec.label in self._enumeration_cache,
                    "refreshed_at": (
                        self._enumeration_cache[spec.label].refreshed_at
                        if spec.label in self._enumeration_cache
                        else None
                    ),
                    "expires_at": (
                        self._enumeration_cache[spec.label].expires_at
                        if spec.label in self._enumeration_cache
                        else None
                    ),
                    "retry_at": self._cache_retry_at.get(spec.label),
                }
                for spec in _RESOURCE_SPECS
            }
        return {
            "enabled": config.entitlements_enabled(),
            "catalog": config.workshop_catalog() or None,
            "schema": config.workshop_schema() or None,
            "ok": ok,
            "state": health_state,
            "last_reconcile": last_reconcile or None,
            "last_attempt_at": last_attempt_at or None,
            "last_error": last_error,
            "thread_alive": bool(self._thread and self._thread.is_alive()),
            "last_verified_at": last_verified_at or None,
            "verified_email": verified_email,
            "verified_catalog": verified_catalog,
            "verification_source": verification_source,
            "interval": config.entitlement_reconcile_interval(),
            "idle_interval": config.entitlement_idle_interval(),
            "cache_ttl_cap": config.entitlement_cache_ttl(),
            "backoff_cap": config.entitlement_backoff_cap(),
            "fleet_request_budget_per_minute": (
                config.entitlement_fleet_request_budget_per_minute()
            ),
            "idle": idle,
            "next_attempt_at": next_attempt_at or None,
            "next_attempt_reason": next_attempt_reason,
            "deferred_reason": deferred_reason,
            "backoff_seconds": backoff_seconds,
            "backoff_attempt": backoff_attempt,
            "last_request_count": last_request_count,
            "last_rate_limit_count": last_rate_limit_count,
            "last_http_429_count": last_http_429_count,
            "cache_hits": cache_hits,
            "cache_misses": cache_misses,
            "cache": cache,
            "last_convergence_ms": convergence_ms,
            "run_started_at": self._run_started_at,
            "baseline_ready": baseline_ready,
            "baseline_retry_pending": baseline_retry_pending,
            "handoff": handoff,
        }


entitlement_manager = EntitlementManager()
