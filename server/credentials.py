"""Workspace credential for attendee CLIs — direct app-identity OAuth.

Databricks Apps injects platform-managed app service-principal OAuth
credentials. ``WorkspaceClient.config.authenticate()`` returns their current
short-lived access bearer; this module validates and distributes that bearer
directly. The client secret never enters attendee shells/files and the app never
calls workspace token create/delete APIs.

For backwards compatibility the app still accepts a vended ``WORKSHOP_PAT`` as
an *emergency-only* bootstrap, but a static PAT is a time bomb (it expires with
no rotation behind it), so when the app is reduced to serving one directly the
credential is reported as **degraded** and a Control Tower alert is emitted.

A background loop probes credential health on a slow cadence — through idle
windows too — so a misconfigured grant or an expiring credential is caught and
alerted hours/days before the event, not at first use on the day.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import platform
import re
import threading
import time

import requests

from . import config

logger = logging.getLogger(__name__)

_workspace_client = None
_app_client_id: str | None = None
_identity_lock = threading.Lock()
_secret_protection = {
    "initialized": False,
    "env_scrubbed": False,
    "non_dumpable": False,
    "ok": False,
    "error": None,
}

PROBE_INTERVAL = 300          # 5 minutes — OAuth refresh / validation cadence
OAUTH_VALIDATION_MAX_AGE = 600
OAUTH_EXPIRY_MARGIN = 60
HEARTBEAT = 30                # loop wake interval
ALERT_REEMIT_INTERVAL = 1800  # re-emit an unhealthy/degraded alert at most every 30 min

# Credential health states surfaced through status()["state"].
STATE_UNKNOWN = "unknown"      # not yet probed
STATE_ROTATING = "rotating"    # recently validated auto-refreshing OAuth
STATE_DEGRADED = "degraded"    # serving a static credential, no rotation (time bomb)
STATE_UNHEALTHY = "unhealthy"  # credential rejected/expired or nothing configured


class CredentialError(Exception):
    pass


def vended_pat() -> str:
    return os.environ.get("WORKSHOP_PAT", "").strip()


def _set_non_dumpable() -> bool:
    """Block same-UID ptrace and /proc memory/environ reads on Linux."""
    try:
        import ctypes

        libc = ctypes.CDLL(None, use_errno=True)
        return libc.prctl(4, 0, 0, 0, 0) == 0  # PR_SET_DUMPABLE
    except (AttributeError, OSError):
        return False


def _secret_env_names() -> list[str]:
    return [
        name
        for name in os.environ
        if name.startswith("DATABRICKS_CLIENT_SECRET")
        or name.startswith("DATABRICKS_OAUTH_CLIENT_SECRET")
    ]


def initialize_app_identity(
    *,
    workspace_client_cls=None,
    harden_fn=_set_non_dumpable,
    system: str | None = None,
):
    """Create exactly one explicit OAuth M2M client, then scrub/harden secrets."""
    global _workspace_client, _app_client_id, _secret_protection
    with _identity_lock:
        if _workspace_client is not None:
            return _workspace_client
        host = os.environ.get("DATABRICKS_HOST", "").strip()
        client_id = os.environ.get("DATABRICKS_CLIENT_ID", "").strip()
        client_secret = os.environ.get("DATABRICKS_CLIENT_SECRET", "").strip()
        if not all((host, client_id, client_secret)):
            _secret_protection = {
                "initialized": False,
                "env_scrubbed": False,
                "non_dumpable": False,
                "ok": False,
                "error": "explicit OAuth M2M environment is incomplete",
            }
            raise RuntimeError("DATABRICKS_HOST/CLIENT_ID/CLIENT_SECRET are required")
        if workspace_client_cls is None:
            from databricks.sdk import WorkspaceClient

            workspace_client_cls = WorkspaceClient
        client = workspace_client_cls(
            host=host,
            client_id=client_id,
            client_secret=client_secret,
            auth_type="oauth-m2m",
        )
        _workspace_client = client
        _app_client_id = client_id
        for name in _secret_env_names():
            os.environ.pop(name, None)
            os.unsetenv(name)
        env_scrubbed = not _secret_env_names()
        linux = (system or platform.system()) == "Linux"
        non_dumpable = bool(harden_fn()) if linux else True
        ok = env_scrubbed and non_dumpable
        _secret_protection = {
            "initialized": True,
            "env_scrubbed": env_scrubbed,
            "non_dumpable": non_dumpable,
            "ok": ok,
            "error": None if ok else "server process could not be made non-dumpable",
        }
        if linux and not non_dumpable and not config.local_dev():
            _workspace_client = None
            raise RuntimeError("production Linux server process must be non-dumpable")
        return client


def workspace_client():
    """The one OAuth M2M client ``initialize_app_identity`` built, or ``None``.

    Callers inside the app must use this rather than constructing an ambient
    ``WorkspaceClient``: initialization scrubs the client secret from the
    environment, so a client built afterwards has nothing left to authenticate
    with. ``None`` before initialization and in local dev.
    """
    return _workspace_client


def secret_protection_status() -> dict:
    return dict(_secret_protection)


def _reset_app_identity_for_tests() -> None:
    global _workspace_client, _app_client_id, _secret_protection
    with _identity_lock:
        _workspace_client = None
        _app_client_id = None
        _secret_protection = {
            "initialized": False,
            "env_scrubbed": False,
            "non_dumpable": False,
            "ok": False,
            "error": None,
        }


def app_identity_bearer() -> str | None:
    """Bearer for the app's OWN service-principal OAuth identity (P1-2).

    Databricks Apps run as a service principal and inject its OAuth credentials
    into the runtime; our explicit singleton ``WorkspaceClient`` authenticates
    with ``auth_type="oauth-m2m"`` as that SP.
    We distribute the current short-lived OAuth bearer **without** a long-lived,
    workspace-admin ``WORKSHOP_PAT`` sitting in ``app.yaml`` env where an
    attendee (``CAN_MANAGE`` + ``/Workspace/Shared``) could read it. The app's
    OAuth client secret is platform-managed, not an attendee-readable file/env
    var. Returns ``None`` if the app identity can't authenticate (e.g. local
    dev), so callers fall back to the vended PAT.
    """
    if config.local_dev():
        # Local dev / tests have no app service-principal identity; skip the SDK
        # auth probe (it would block trying auth methods that can't succeed).
        return None
    try:
        if _workspace_client is None:
            return None
        headers = _workspace_client.config.authenticate()
        auth = (headers or {}).get("Authorization", "")
        if auth.lower().startswith("bearer "):
            return auth[7:].strip() or None
    except Exception as e:  # noqa: BLE001 — any auth failure → fall back
        logger.warning("app-identity auth unavailable: %s", e)
    return None


def _bootstrap_auth() -> str | None:
    """Current app OAuth bearer, or the explicit emergency PAT fallback."""
    return app_identity_bearer() or (vended_pat() or None)


def _identity_request(
    url: str, endpoint: str, token: str
) -> tuple[dict, dict | None]:
    """Call one identity endpoint and retain only safe, identity-shaped evidence."""
    entry = {
        "endpoint": endpoint,
        "status": None,
        "observed_identity": {},
    }
    try:
        response = requests.get(
            url,
            headers={"Authorization": f"Bearer {token}"},
            timeout=10,
        )
    except requests.RequestException:
        entry["status"] = "request_error"
        return entry, None

    entry["status"] = response.status_code
    if response.status_code != 200:
        return entry, None
    try:
        payload = response.json()
    except (AttributeError, ValueError):
        return entry, None
    if not isinstance(payload, dict):
        return entry, None

    for field in ("applicationId", "application_id", "userName", "id"):
        value = payload.get(field)
        if isinstance(value, (str, int)) and str(value):
            entry["observed_identity"][field] = str(value)
    return entry, payload


def _identity_values(payload: dict | None, fields: tuple[str, ...]) -> list[str]:
    if not payload:
        return []
    return [
        str(payload[field])
        for field in fields
        if isinstance(payload.get(field), (str, int)) and str(payload[field])
    ]


def _primary_identity_value(
    payload: dict | None, fields: tuple[str, ...]
) -> str | None:
    values = _identity_values(payload, fields)
    return values[0] if values else None


class CredentialManager:
    """Serve the app SP's auto-refreshing OAuth bearer directly to attendee CLIs.

    The Databricks Apps runtime owns the client secret and refresh grant. This
    manager only receives short-lived access bearers from ``WorkspaceClient``,
    validates them against a read-only workspace endpoint, and copies changed
    bearers into attendee-owned token files. ``WORKSHOP_PAT`` is an explicit
    degraded fallback and is never used to mint another credential.
    """

    def __init__(self, session_count_fn, *, now_fn=time.time):
        self._session_count_fn = session_count_fn
        self._now = now_fn
        self._token: str | None = None
        self._expires_at: float | None = None
        self._rotating = False
        self._source: str | None = None
        self._bootstrap_ok = False
        self._health = STATE_UNKNOWN
        self._lock = threading.Lock()
        self._refresh_lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._last_probe = 0.0
        self._last_successful_at = 0.0
        self._last_alert_at = 0.0
        self.last_error: str | None = None
        self._validation_diagnostic: dict | None = None

    # -- public --

    @property
    def healthy(self) -> bool:
        with self._lock:
            return self._effective_state_locked() == STATE_ROTATING

    def token(self) -> str:
        """Return fresh direct OAuth, refreshing/validating after idle on demand."""
        with self._lock:
            token = self._token
            fresh = self._oauth_fresh_locked()
        if token and fresh:
            return token
        self._rotate_once()
        with self._lock:
            token = self._token
            fresh = self._oauth_fresh_locked()
        if token and fresh:
            return token
        emergency = vended_pat()
        if emergency and self._validate_emergency_pat(emergency):
            return emergency
        # Distinguish "nothing configured" from "configured but rejected" — the
        # combined message sent operators hunting for a missing PAT when one was
        # present and failing validation.
        detail = (
            "the emergency WORKSHOP_PAT was also rejected"
            if emergency
            else "no emergency WORKSHOP_PAT is set"
        )
        raise CredentialError(
            "No workshop credential available — Databricks Apps app-identity "
            f"OAuth could not authenticate and {detail}."
            + (f" Last error: {self.last_error}" if self.last_error else "")
        )

    def start(self) -> None:
        """Begin periodic OAuth acquisition and validation through idle windows."""
        # Validate synchronously so post-deploy health checks never observe a
        # false ``unknown`` window before the maintenance thread's first wake.
        self._self_probe(adopt=True)
        with self._lock:
            self._bootstrap_ok = self._source is not None
        if not self._bootstrap_ok:
            logger.warning(
                "no app-identity OAuth and no emergency WORKSHOP_PAT available"
            )
            self._set_health(
                STATE_UNHEALTHY,
                "app-identity OAuth unavailable and no emergency WORKSHOP_PAT configured",
            )
        elif self.status()["source"] == "emergency_workshop_pat":
            logger.warning("credential bootstrap: emergency WORKSHOP_PAT")
        else:
            logger.info("credential bootstrap: direct app service-principal OAuth")
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._rotation_loop, daemon=True, name="credential-rotation"
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def status(self) -> dict:
        with self._lock:
            state = self._effective_state_locked()
            rotating = self._rotating
            last_error = self.last_error
            bootstrap_ok = self._bootstrap_ok
            last_successful_at = self._last_successful_at
            source = self._source
            expires_at = self._expires_at
            validation_diagnostic = self._validation_diagnostic
        has_pat = bool(vended_pat())
        if source is None:
            source = "emergency_workshop_pat" if has_pat else "unknown"
        token_expires_in = (
            max(0, int(expires_at - self._now()))
            if expires_at is not None
            else None
        )
        return {
            "configured": bootstrap_ok or rotating or has_pat,
            "rotating": state == STATE_ROTATING and rotating,
            "healthy": state == STATE_ROTATING,
            "degraded": state == STATE_DEGRADED,
            "state": state,
            "source": source,
            "token_expires_in": token_expires_in,
            "last_successful_at": last_successful_at or None,
            "last_error": last_error,
            "validation_diagnostic": validation_diagnostic,
        }

    # -- internals --

    def _rotation_loop(self) -> None:
        while not self._stop.wait(timeout=HEARTBEAT):
            try:
                self._tick()
            except Exception as e:  # noqa: BLE001 — the loop must never die
                logger.error("credential maintenance failed unexpectedly: %s", e)

    def _tick(self) -> None:
        now = self._now()
        if now - self._last_probe < PROBE_INTERVAL:
            return
        self._last_probe = now
        self._self_probe(adopt=True)

    def _rotate_once(self) -> None:
        with self._refresh_lock:
            with self._lock:
                if self._oauth_fresh_locked():
                    return
            self._probe_unlocked(adopt=True)

    def _self_probe(self, *, adopt: bool) -> None:
        """Acquire app OAuth, validate it read-only, and adopt changed bearers."""
        with self._refresh_lock:
            self._probe_unlocked(adopt=adopt)

    def _probe_unlocked(self, *, adopt: bool) -> None:
        oauth = app_identity_bearer()
        if oauth:
            if not self._validate(oauth):
                self._set_health(
                    STATE_UNHEALTHY,
                    self.last_error
                    or "app-identity OAuth bearer was rejected by the workspace",
                    clear=True,
                )
                return
            expires_at = _jwt_expiry(oauth)
            if expires_at is not None and expires_at <= self._now() + OAUTH_EXPIRY_MARGIN:
                self._set_health(
                    STATE_UNHEALTHY,
                    "app-identity OAuth bearer is too close to expiry",
                    clear=True,
                )
                return
            self._adopt_oauth(oauth, expires_at, fanout=adopt)
            return

        emergency = vended_pat()
        if emergency:
            self._validate_emergency_pat(emergency)
        else:
            self._set_health(
                STATE_UNHEALTHY,
                "app-identity OAuth unavailable and no emergency WORKSHOP_PAT configured",
                clear=True,
            )

    def _validate(self, token: str, *, require_app_identity: bool = True) -> bool:
        host = config.databricks_host()
        expected = str(_app_client_id or os.environ.get("DATABRICKS_CLIENT_ID") or "")
        expected_sp_id = os.environ.get("WORKSHOP_APP_SP_ID", "").strip()
        expected_sp_id_valid = bool(re.fullmatch(r"[0-9]+", expected_sp_id))
        diagnostic = {
            "result": "pending",
            "expected_application_id": expected or None,
            "observed_application_id": None,
            "expected_service_principal_id": expected_sp_id or None,
            "observed_service_principal_id": None,
            "endpoints": [],
        }

        current_entry, current_payload = _identity_request(
            f"{host}/api/2.0/current-user/me",
            "current-user/me",
            token,
        )
        diagnostic["endpoints"].append(current_entry)
        if not require_app_identity and current_entry["status"] == 200:
            diagnostic["result"] = "authenticated"
            self._validation_diagnostic = diagnostic
            self.last_error = None
            return True

        # Preconditions apply to app-identity binding only. A user token
        # (require_app_identity=False) is judged by whether the workspace
        # accepts it, and its fallback lives below at the SCIM probe — checking
        # app-SP preconditions here made that fallback unreachable whenever
        # current-user/me answered anything but 200.
        if require_app_identity and not expected:
            diagnostic["result"] = "expected_identity_missing"
            self._record_validation_failure(diagnostic)
            return False

        # A *malformed* WORKSHOP_APP_SP_ID is a broken deploy: fail loudly rather
        # than silently downgrade to a weaker binding.
        if require_app_identity and expected_sp_id and not expected_sp_id_valid:
            diagnostic["result"] = "expected_service_principal_id_invalid"
            self._record_validation_failure(diagnostic)
            return False

        # An *absent* one only strengthens the binding when present, so it cannot
        # gate it. Control Tower can only resolve the numeric id after app
        # create, so a terminal that refused to authenticate without it locked
        # its own agents out of the AI gateway whenever that injection was
        # missed. Bind on the platform-injected client id, and additionally
        # require the numeric id whenever we actually have one.
        unverified_sp_id = not expected_sp_id_valid
        diagnostic["service_principal_id_unverified"] = unverified_sp_id

        def _sp_id_matches(observed: str | None) -> bool:
            return unverified_sp_id or observed == expected_sp_id

        def _matched_result() -> str:
            return "matched_without_sp_id" if unverified_sp_id else "matched"

        current_id = _primary_identity_value(
            current_payload, ("applicationId", "application_id")
        )
        if current_id:
            current_sp_id = _primary_identity_value(current_payload, ("id",))
            diagnostic["observed_application_id"] = current_id
            diagnostic["observed_service_principal_id"] = current_sp_id
            if current_id == expected and _sp_id_matches(current_sp_id):
                diagnostic["result"] = _matched_result()
                self._validation_diagnostic = diagnostic
                self.last_error = None
                return True
            diagnostic["result"] = "identity_mismatch"
            self._record_validation_failure(diagnostic)
            return False

        scim_entry, scim_payload = _identity_request(
            f"{host}/api/2.0/preview/scim/v2/Me",
            "scim/v2/Me",
            token,
        )
        diagnostic["endpoints"].append(scim_entry)
        if not require_app_identity and scim_entry["status"] == 200:
            diagnostic["result"] = "authenticated"
            self._validation_diagnostic = diagnostic
            self.last_error = None
            return True

        scim_application_id = _identity_values(
            scim_payload, ("applicationId", "application_id")
        )
        if scim_application_id:
            scim_sp_id = _primary_identity_value(scim_payload, ("id",))
            diagnostic["observed_application_id"] = scim_application_id[0]
            diagnostic["observed_service_principal_id"] = scim_sp_id
            if (
                scim_application_id == [expected]
                and _sp_id_matches(scim_sp_id)
            ):
                diagnostic["result"] = _matched_result()
                self._validation_diagnostic = diagnostic
                self.last_error = None
                return True
            diagnostic["result"] = "identity_mismatch"
        elif scim_entry["status"] == 200:
            scim_user_name = _primary_identity_value(scim_payload, ("userName",))
            scim_sp_id = _primary_identity_value(scim_payload, ("id",))
            diagnostic["observed_application_id"] = scim_user_name
            diagnostic["observed_service_principal_id"] = scim_sp_id
            if scim_user_name == expected and _sp_id_matches(scim_sp_id):
                diagnostic["result"] = _matched_result()
                self._validation_diagnostic = diagnostic
                self.last_error = None
                return True
            else:
                diagnostic["result"] = "identity_mismatch"
        else:
            diagnostic["result"] = "endpoints_unavailable"
        self._record_validation_failure(diagnostic)
        return False

    def _record_validation_failure(self, diagnostic: dict) -> None:
        self._validation_diagnostic = diagnostic
        endpoint_statuses = ", ".join(
            f"{entry['endpoint']}={entry['status']}"
            for entry in diagnostic["endpoints"]
        )
        self.last_error = (
            f"OAuth identity validation failed: {diagnostic['result']}"
            f" ({endpoint_statuses})"
        )

    def _validate_emergency_pat(self, token: str) -> bool:
        if self._validate(token, require_app_identity=False):
            self._set_health(
                STATE_DEGRADED,
                "serving explicit emergency WORKSHOP_PAT without automatic refresh",
                clear=True,
                source="emergency_workshop_pat",
            )
            return True
        else:
            self._set_health(
                STATE_UNHEALTHY,
                "emergency WORKSHOP_PAT was rejected by the workspace",
                clear=True,
            )
            return False

    def _adopt_oauth(
        self, token: str, expires_at: float | None, *, fanout: bool
    ) -> None:
        with self._lock:
            changed = token != self._token
            self._token = token
            self._expires_at = expires_at
            self._last_successful_at = self._now()
            self._rotating = True
            self._source = "app_identity_oauth"
            self._bootstrap_ok = True
        self._set_health(STATE_ROTATING, None)
        if changed and fanout:
            self._fanout(token)

    def _oauth_fresh_locked(self) -> bool:
        if (
            not self._token
            or not self._rotating
            or self._health != STATE_ROTATING
            or self._source != "app_identity_oauth"
            or self._now() - self._last_successful_at > OAUTH_VALIDATION_MAX_AGE
        ):
            return False
        return (
            self._expires_at is None
            or self._expires_at > self._now() + OAUTH_EXPIRY_MARGIN
        )

    def _effective_state_locked(self) -> str:
        if self._health == STATE_ROTATING and not self._oauth_fresh_locked():
            return STATE_UNHEALTHY
        return self._health

    def _set_health(
        self,
        state: str,
        error: str | None,
        *,
        clear: bool = False,
        source: str | None = None,
    ) -> None:
        """Record health, log it (recurring for not-healthy so it stays visible
        in app logs), and emit a Control Tower alert on change / periodically."""
        now = self._now()
        with self._lock:
            changed = state != self._health
            if clear:
                self._token = None
                self._expires_at = None
                self._rotating = False
                self._source = None
                self._last_successful_at = 0.0
                self._bootstrap_ok = False
            if source is not None:
                self._source = source
            self._health = state
            self.last_error = error
        if state == STATE_ROTATING:
            if changed:
                logger.info("credential healthy — rotating short-lived tokens")
        elif state == STATE_DEGRADED:
            logger.warning("credential degraded: %s", error)
        elif state == STATE_UNHEALTHY:
            logger.error("credential UNHEALTHY: %s", error)
        if changed or (state != STATE_ROTATING and now - self._last_alert_at > ALERT_REEMIT_INTERVAL):
            self._last_alert_at = now
            self._emit_health_alert(state, error)

    def _emit_health_alert(self, state: str, error: str | None) -> None:
        """Push a credential-health event to Control Tower (no-op unless CT
        ingest is configured), so a degraded/expiring credential is visible to
        operators ahead of the event instead of only in app logs."""
        try:
            from .event_emitter import event_emitter

            event_emitter.emit(
                "credential.health",
                "system",
                {"state": state, "error": error, "source": self.status()["source"]},
            )
        except Exception:  # noqa: BLE001 — alerting must never break the loop
            pass

    def _fanout(self, token: str) -> None:
        """Push the fresh token into every known attendee's CLI configs."""
        from . import cli_config
        from .users import user_manager

        for user in user_manager.all():
            try:
                cli_config.update_tokens(user, token)
            except OSError as e:
                logger.warning("token fanout to %s failed: %s", user.email, e)


def _jwt_expiry(token: str) -> float | None:
    """Read ``exp`` as freshness metadata; signature validation stays server-side."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        value = json.loads(base64.urlsafe_b64decode(payload).decode("utf-8"))
        expiry = value.get("exp")
        return float(expiry) if isinstance(expiry, (int, float)) else None
    except (ValueError, IndexError, TypeError, json.JSONDecodeError):
        return None


def _count_sessions() -> int:
    from .sessions import session_manager
    return session_manager.count_all()


credential_manager = CredentialManager(_count_sessions)


def ensure_user_credentials(user) -> None:
    """Write/refresh this user's CLI configs with the current token."""
    from . import cli_config

    token = credential_manager.token()  # raises CredentialError when unconfigured
    if user.cli_ready:
        cli_config.update_tokens(user, token)
    else:
        cli_config.configure_all(user, token)
