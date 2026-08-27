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

from dataclasses import dataclass, field
from datetime import datetime, timezone
import logging
import os
import threading
import time

import requests

from . import config

logger = logging.getLogger(__name__)

HEARTBEAT = 30                # loop wake interval (seconds)
ALERT_REEMIT_INTERVAL = 1800  # re-emit a degraded health event at most every 30 min

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


def _patch(url: str, bearer: str, body: dict) -> tuple[bool, str | None]:
    try:
        resp = requests.patch(
            url, headers={"Authorization": f"Bearer {bearer}"}, json=body, timeout=30
        )
    except requests.RequestException as e:
        return False, str(e)
    if 200 <= resp.status_code < 300:
        return True, None
    return False, f"{resp.status_code} {resp.text[:200]}"


def _get_json(
    url: str, bearer: str, params: dict | None = None
) -> tuple[dict | None, str | None]:
    try:
        resp = requests.get(
            url,
            headers={"Authorization": f"Bearer {bearer}"},
            params=params or {},
            timeout=30,
        )
    except requests.RequestException as e:
        return None, str(e)
    if resp.status_code != 200:
        return None, f"{resp.status_code} {resp.text[:200]}"
    try:
        payload = resp.json()
    except ValueError as e:
        return None, f"invalid JSON: {e}"
    if not isinstance(payload, dict):
        return None, "response was not an object"
    return payload, None


def _grant_catalog_all_privileges(
    host: str, bearer: str, catalog: str, principal: str
) -> tuple[bool, str | None]:
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
) -> tuple[bool, str | None]:
    url = f"{host}/api/2.1/unity-catalog/permissions/catalog/{catalog}"
    body = {"changes": [{"principal": principal, "add": list(privileges)}]}
    return _patch(url, bearer, body)


def _catalog_privileges_for(
    host: str, bearer: str, catalog: str, principal: str
) -> tuple[set[str] | None, str | None]:
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
) -> tuple[bool, str | None]:
    """Optionally transfer catalog ownership to the labuser (off by default)."""
    url = f"{host}/api/2.1/unity-catalog/catalogs/{catalog}"
    return _patch(url, bearer, {"owner": principal})


def _verify_catalog_access(
    host: str, bearer: str, catalog: str, principal: str, *, require_owner: bool = False
) -> tuple[bool, str | None]:
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
            raise RuntimeError(error)
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

    def __init__(self, *, run_started_at: float | None = None) -> None:
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._run_started_at = run_started_at if run_started_at is not None else time.time()
        self._baseline: dict[str, set[str]] = {}
        self._baseline_ready: set[str] = set()
        self._ledger: dict[tuple[str, str, str], dict] = {}
        self._last_run_at = 0.0
        self._last_reconcile = 0.0
        self._ok: bool | None = None
        self._last_error: str | None = None
        self._last_alert_at = 0.0
        self._last_verified_at = 0.0
        self._verified_email: str | None = None
        self._verified_catalog: str | None = None
        self._verification_source: str | None = None
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
        # Capture pre-existing IDs before any attendee session can create
        # resources. Failure is fail-closed for adapters without create_time.
        self.capture_baseline()
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

    def capture_baseline(self) -> dict:
        """Snapshot APIs that cannot prove creation time.

        Only resources absent from a successfully captured baseline can be
        handed off later. A failed snapshot never widens access.
        """
        host = config.databricks_host()
        bearer = _sp_bearer()
        result = {"captured": [], "errors": []}
        if not host or not bearer:
            result["errors"].append("no app service-principal bearer/host")
            return result
        for spec in _RESOURCE_SPECS:
            if not spec.requires_baseline:
                continue
            try:
                items = _enumerate(host, bearer, spec)
                ids = {
                    str(item[spec.id_field])
                    for item in items
                    if item.get(spec.id_field) is not None
                }
            except Exception as e:  # noqa: BLE001 — fail closed per adapter
                result["errors"].append(f"{spec.label}: {e}")
                continue
            with self._lock:
                self._baseline[spec.label] = ids
                self._baseline_ready.add(spec.label)
            result["captured"].append(spec.label)
        return result

    def _loop(self) -> None:
        while not self._stop.wait(timeout=HEARTBEAT):
            if time.time() - self._last_run_at < config.entitlement_reconcile_interval():
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

    def reconcile(self, email: str | None = None) -> dict:
        """Idempotently make SP-created resources usable by the labuser(s).

        With ``email`` set, reconciles just that attendee (the on-demand
        ``workshop-grant-me`` path); otherwise every known attendee. Never
        raises — failures are recorded and surfaced via health + status.
        """
        if not config.entitlements_enabled():
            return {"enabled": False}
        started_at = time.monotonic()
        self._last_run_at = time.time()

        from .users import user_manager

        source = "on_demand" if email else "background"

        def finish(result: dict, reason: str) -> dict:
            errors = [str(error) for error in result.get("errors", [])]
            rate_limited = any("429" in error for error in errors)
            from . import telemetry

            telemetry.entitlement_reconcile(
                source=source,
                outcome="ok" if not errors else "degraded",
                reason="rate_limited" if rate_limited else reason,
                duration_ms=(time.monotonic() - started_at) * 1000,
                rate_limited=rate_limited,
            )
            return result

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
            self._record(False, msg)
            result["errors"] = [msg]
            return finish(result, "no_attendee")

        bearer = _sp_bearer()
        host = config.databricks_host()
        if not bearer or not host:
            msg = "no app service-principal bearer/host — cannot reconcile entitlements"
            self._record(False, msg)
            result["errors"] = [msg]
            return finish(result, "credential_unavailable")

        errors: list[str] = []
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
                        self._last_verified_at = time.time()
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
                    host, bearer, em, identities, catalog
                )
                result["non_uc"][em] = counts
                errors.extend(errs)

        self._record(not errors, "; ".join(errors[:5]) if errors else None)
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
    ) -> tuple[dict[str, int], list[str]]:
        counts: dict[str, int] = {}
        errors: list[str] = []
        for spec in _RESOURCE_SPECS:
            try:
                items = _enumerate(host, bearer, spec)
            except Exception as e:  # noqa: BLE001 — isolate API failures
                message = f"{spec.label} list: {e}"
                errors.append(message)
                self._transition(
                    principal, spec.label, "<discovery>", "failed", error=message
                )
                continue
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
            entry["updated_at"] = time.time()

    def _handoff_snapshot(self) -> dict:
        with self._lock:
            details = [dict(entry) for entry in self._ledger.values()]
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

    def _record(self, ok: bool, error: str | None) -> None:
        now = time.time()
        with self._lock:
            changed = ok != self._ok
            self._ok = ok
            self._last_error = error
            self._last_reconcile = now
        if not ok:
            logger.warning("entitlement reconcile degraded: %s", error)
        elif changed:
            logger.info("entitlements healthy — labuser access reconciled")
        if changed or (not ok and now - self._last_alert_at > ALERT_REEMIT_INTERVAL):
            self._last_alert_at = now
            self._emit_health(ok, error)

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
        return {
            "enabled": config.entitlements_enabled(),
            "catalog": config.workshop_catalog() or None,
            "schema": config.workshop_schema() or None,
            "ok": ok,
            "last_reconcile": last_reconcile or None,
            "last_error": last_error,
            "thread_alive": bool(self._thread and self._thread.is_alive()),
            "last_verified_at": last_verified_at or None,
            "verified_email": verified_email,
            "verified_catalog": verified_catalog,
            "verification_source": verification_source,
            "interval": config.entitlement_reconcile_interval(),
            "run_started_at": self._run_started_at,
            "baseline_ready": sorted(self._baseline_ready),
            "handoff": self._handoff_snapshot(),
        }


entitlement_manager = EntitlementManager()
