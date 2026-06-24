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
- **Non-UC (apps, jobs, pipelines, database instances, serving endpoints):**
  these do not inherit, so the loop enumerates them and grants the labuser
  ``CAN_MANAGE`` via each resource's permissions API.

All calls use the app SP bearer (``credentials.app_identity_bearer``), are
idempotent (additive PATCH — re-running is a no-op), never block a session, and
emit an ``entitlements.health`` event to Control Tower on failure (same envelope
as ``credential.health``). Modeled on ``server/credentials.py``.
"""

from __future__ import annotations

import logging
import threading
import time

import requests

from . import config

logger = logging.getLogger(__name__)

HEARTBEAT = 30                # loop wake interval (seconds)
ALERT_REEMIT_INTERVAL = 1800  # re-emit a degraded health event at most every 30 min

# Non-UC resource types to sweep: (label, list_url, response_key, id_field,
# permissions object_type). The generic Permissions API
# (PATCH /api/2.0/permissions/{type}/{id}) additively grants CAN_MANAGE.
_NON_UC = (
    ("jobs", "/api/2.1/jobs/list", "jobs", "job_id", "jobs"),
    ("pipelines", "/api/2.0/pipelines", "statuses", "pipeline_id", "pipelines"),
    ("serving-endpoints", "/api/2.0/serving-endpoints", "endpoints", "id", "serving-endpoints"),
    ("apps", "/api/2.0/apps", "apps", "name", "apps"),
    ("database-instances", "/api/2.0/database/instances", "database_instances", "name", "database-instances"),
)


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


def _grant_catalog_all_privileges(
    host: str, bearer: str, catalog: str, principal: str
) -> tuple[bool, str | None]:
    """Grant the labuser ALL_PRIVILEGES on the catalog (inherited downward)."""
    url = f"{host}/api/2.1/unity-catalog/permissions/catalog/{catalog}"
    body = {"changes": [{"principal": principal, "add": ["ALL_PRIVILEGES"]}]}
    return _patch(url, bearer, body)


def _set_catalog_owner(
    host: str, bearer: str, catalog: str, principal: str
) -> tuple[bool, str | None]:
    """Optionally transfer catalog ownership to the labuser (off by default)."""
    url = f"{host}/api/2.1/unity-catalog/catalogs/{catalog}"
    return _patch(url, bearer, {"owner": principal})


def _enumerate(host: str, bearer: str, list_url: str, resp_key: str, id_field: str) -> list[str]:
    resp = requests.get(
        f"{host}{list_url}", headers={"Authorization": f"Bearer {bearer}"}, timeout=30
    )
    if resp.status_code != 200:
        raise RuntimeError(f"{resp.status_code} {resp.text[:120]}")
    items = resp.json().get(resp_key, []) or []
    return [str(it[id_field]) for it in items if isinstance(it, dict) and it.get(id_field) is not None]


def _grant_can_manage(
    host: str, bearer: str, perm_type: str, resource_id: str, principal: str
) -> tuple[bool, str | None]:
    url = f"{host}/api/2.0/permissions/{perm_type}/{resource_id}"
    body = {"access_control_list": [{"user_name": principal, "permission_level": "CAN_MANAGE"}]}
    return _patch(url, bearer, body)


def _sweep_non_uc(host: str, bearer: str, principal: str) -> tuple[dict, list[str]]:
    counts: dict[str, int] = {}
    errors: list[str] = []
    for label, list_url, resp_key, id_field, perm_type in _NON_UC:
        try:
            ids = _enumerate(host, bearer, list_url, resp_key, id_field)
        except Exception as e:  # noqa: BLE001 — one resource type failing must not stop the rest
            errors.append(f"{label} list: {e}")
            continue
        granted = 0
        for rid in ids:
            ok, err = _grant_can_manage(host, bearer, perm_type, rid, principal)
            if ok:
                granted += 1
            elif err:
                errors.append(f"{label} {rid}: {err}")
        counts[label] = granted
    return counts, errors


class EntitlementManager:
    """Background reconciliation loop + on-demand trigger."""

    def __init__(self) -> None:
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._last_run_at = 0.0
        self._last_reconcile = 0.0
        self._ok: bool | None = None
        self._last_error: str | None = None
        self._last_alert_at = 0.0

    def start(self) -> None:
        if not config.entitlements_enabled():
            logger.info("entitlement reconciler disabled (ENABLE_ENTITLEMENTS off)")
            return
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
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

    def _loop(self) -> None:
        while not self._stop.wait(timeout=HEARTBEAT):
            if time.time() - self._last_run_at < config.entitlement_reconcile_interval():
                continue
            try:
                self.reconcile()
            except Exception as e:  # noqa: BLE001 — the loop must never die
                logger.error("entitlement reconcile failed unexpectedly: %s", e)

    def reconcile(self, email: str | None = None) -> dict:
        """Idempotently make SP-created resources usable by the labuser(s).

        With ``email`` set, reconciles just that attendee (the on-demand
        ``workshop-grant-me`` path); otherwise every known attendee. Never
        raises — failures are recorded and surfaced via health + status.
        """
        if not config.entitlements_enabled():
            return {"enabled": False}
        self._last_run_at = time.time()

        from .users import user_manager

        emails = [email] if email else [u.email for u in user_manager.all()]
        result: dict = {"enabled": True, "emails": emails, "catalog": None, "non_uc": {}, "errors": []}

        bearer = _sp_bearer()
        host = config.databricks_host()
        if not bearer or not host:
            msg = "no app service-principal bearer/host — cannot reconcile entitlements"
            self._record(False, msg)
            result["errors"] = [msg]
            return result

        errors: list[str] = []
        catalog = config.workshop_catalog()
        if catalog:
            for em in emails:
                if "@" not in em:
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
            if "@" not in em:
                continue
            counts, errs = _sweep_non_uc(host, bearer, em)
            result["non_uc"][em] = counts
            errors.extend(errs)

        self._record(not errors, "; ".join(errors[:5]) if errors else None)
        result["errors"] = errors
        return result

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
        return {
            "enabled": config.entitlements_enabled(),
            "catalog": config.workshop_catalog() or None,
            "schema": config.workshop_schema() or None,
            "ok": ok,
            "last_reconcile": last_reconcile or None,
            "last_error": last_error,
            "interval": config.entitlement_reconcile_interval(),
        }


entitlement_manager = EntitlementManager()
