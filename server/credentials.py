"""Workspace credential for attendee CLIs — minted from the app's identity.

Databricks Apps OBO scopes deliberately exclude the Token API, so the app
cannot mint per-user PATs from forwarded tokens. The bulletproof model is to
mint short-lived rotating tokens from the **app's own service-principal OAuth
identity** (P1-2): that identity is platform-managed and auto-refreshed, so
there is no long-lived secret to expire across a deploy-then-idle-then-event
window. Attendees never touch tokens.

For backwards compatibility the app still accepts a vended ``WORKSHOP_PAT`` as
an *emergency-only* bootstrap, but a static PAT is a time bomb (it expires with
no rotation behind it), so when the app is reduced to serving one directly the
credential is reported as **degraded** and a Control Tower alert is emitted.

A background loop probes credential health on a slow cadence — through idle
windows too — so a misconfigured grant or an expiring credential is caught and
alerted hours/days before the event, not at first use on the day.
"""

from __future__ import annotations

import logging
import os
import threading
import time

import requests

from . import config

logger = logging.getLogger(__name__)

TOKEN_LIFETIME = 900          # 15 minutes — lifetime of each minted CLI token
PROBE_INTERVAL = 300          # 5 minutes — health probe / rotation cadence
HEARTBEAT = 30                # loop wake interval
ALERT_REEMIT_INTERVAL = 1800  # re-emit an unhealthy/degraded alert at most every 30 min
TOKEN_COMMENT = "workshop-terminal-auto"

# Credential health states surfaced through status()["state"].
STATE_UNKNOWN = "unknown"      # not yet probed
STATE_ROTATING = "rotating"    # minting fresh short-lived tokens — fully healthy
STATE_DEGRADED = "degraded"    # serving a static credential, no rotation (time bomb)
STATE_UNHEALTHY = "unhealthy"  # credential rejected/expired or nothing configured


class CredentialError(Exception):
    pass


def vended_pat() -> str:
    return os.environ.get("WORKSHOP_PAT", "").strip()


def app_identity_bearer() -> str | None:
    """Bearer for the app's OWN service-principal OAuth identity (P1-2).

    Databricks Apps run as a service principal and inject its OAuth credentials
    into the runtime; the SDK's ``WorkspaceClient()`` authenticates as that SP.
    We use it to mint the short-lived CLI tokens **without** a long-lived,
    workspace-admin ``WORKSHOP_PAT`` sitting in ``app.yaml`` env where an
    attendee (``CAN_MANAGE`` + ``/Workspace/Shared``) could read it. The app's
    OAuth client secret is platform-managed, not an attendee-readable file/env
    var, and attendees only ever receive the rotating 15-minute tokens minted
    from it. Returns ``None`` if the app identity can't authenticate (e.g. local
    dev), so callers fall back to the vended PAT.
    """
    if config.local_dev():
        # Local dev / tests have no app service-principal identity; skip the SDK
        # auth probe (it would block trying auth methods that can't succeed).
        return None
    try:
        from databricks.sdk import WorkspaceClient

        headers = WorkspaceClient().config.authenticate()  # type: ignore[no-untyped-call]
        auth = (headers or {}).get("Authorization", "")
        if auth.lower().startswith("bearer "):
            return auth[7:].strip() or None
    except Exception as e:  # noqa: BLE001 — any auth failure → fall back
        logger.warning("app-identity auth unavailable: %s", e)
    return None


def _bootstrap_auth() -> str | None:
    """Auth used to MINT short-lived CLI tokens: the app's own OAuth identity
    first (P1-2 — no attendee-readable admin PAT), else a vended PAT for
    backwards compatibility / environments without an app identity."""
    return app_identity_bearer() or (vended_pat() or None)


def _bootstrap_source() -> str | None:
    """Which credential ``_bootstrap_auth`` would use right now, for reporting."""
    if app_identity_bearer():
        return "app_identity"
    if vended_pat():
        return "vended_pat"
    return None


class CredentialManager:
    """Instance-level credential: mint+rotate from the app identity when able,
    fall back to a vended PAT (reported degraded), and self-probe for health."""

    def __init__(self, session_count_fn):
        self._session_count_fn = session_count_fn
        self._token: str | None = None
        self._token_id: str | None = None
        self._minted_at: float = 0.0
        self._rotating = False  # True once we can mint our own short-lived tokens
        self._bootstrap_ok = False  # True if app identity or a vended PAT can bootstrap
        self._health = STATE_UNKNOWN
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._last_probe = 0.0
        self._last_alert_at = 0.0
        self.last_error: str | None = None

    # -- public --

    @property
    def healthy(self) -> bool:
        """True when attendees can get a working token right now.

        ``rotating`` means minting is proven to work, so ``token()`` can always
        hand out a fresh short-lived token even if the currently-held one has
        aged out during idle — that is healthy. ``degraded``/``unhealthy`` are
        not. Before the first probe resolves we stay optimistic if anything can
        bootstrap, so a freshly-started app doesn't flap a false alarm.
        """
        with self._lock:
            state = self._health
            bootstrap_ok = self._bootstrap_ok
        if state == STATE_ROTATING:
            return True
        if state in (STATE_DEGRADED, STATE_UNHEALTHY):
            return False
        return bootstrap_ok or bool(vended_pat())

    def token(self) -> str:
        """Current CLI token: our freshest minted token, else mint one now.

        The minted token is what attendees' CLIs receive — short-lived and
        rotating. There is no static token to serve in app-identity mode, so a
        first call (or the first call after an idle gap, where the previous
        minted token has aged out) mints on demand. A vended PAT is served
        directly only if minting is unavailable (emergency fallback).
        """
        with self._lock:
            minted = self._token if self._rotating else None
            fresh = minted and (time.time() - self._minted_at) < TOKEN_LIFETIME
        if minted and fresh:
            return minted

        # No fresh minted token — try to mint now (app identity or PAT bootstrap).
        self._rotate_once()
        with self._lock:
            minted = self._token if self._rotating else None
            fresh = minted and (time.time() - self._minted_at) < TOKEN_LIFETIME
        if minted and fresh:
            return minted

        # Minting unavailable. Serve a vended PAT directly if one exists (legacy,
        # emergency-only — health is reported degraded/unhealthy by the probe).
        bootstrap = vended_pat()
        if bootstrap:
            return bootstrap
        raise CredentialError(
            "No workshop credential available — the app's own identity can't "
            "mint a token and no WORKSHOP_PAT is set. Grant the app's service "
            "principal token-create permission, or inject WORKSHOP_PAT."
        )

    def start(self) -> None:
        """Begin the rotation + health-probe loop.

        Bootstraps from the app's own OAuth identity (P1-2) when available, else
        a vended PAT. The loop runs even when nothing can bootstrap yet, so the
        credential self-heals if a grant/PAT lands later (e.g. between deploy
        and event day).
        """
        self._bootstrap_ok = bool(_bootstrap_auth())
        if not self._bootstrap_ok:
            logger.warning(
                "no app identity and no WORKSHOP_PAT — terminals can't "
                "authenticate until a credential is available; the probe will "
                "keep checking and self-heal once one appears"
            )
            self._set_health(
                STATE_UNHEALTHY,
                "no app identity and no WORKSHOP_PAT configured — grant the app "
                "service principal token CAN_USE, or inject WORKSHOP_PAT",
            )
        elif vended_pat() and not app_identity_bearer():
            logger.info("credential bootstrap: vended WORKSHOP_PAT (no app identity)")
        else:
            logger.info("credential bootstrap: app service-principal OAuth identity (P1-2)")
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
            state = self._health
            rotating = self._rotating
            minted_at = self._minted_at
            last_error = self.last_error
            bootstrap_ok = self._bootstrap_ok
        token_expires_in = None
        if rotating and minted_at:
            token_expires_in = max(0, int(TOKEN_LIFETIME - (time.time() - minted_at)))
        # Cheap source derivation (no SDK probe — status() is polled often):
        has_pat = bool(vended_pat())
        if has_pat and not bootstrap_ok:
            source = "vended_pat"
        elif not has_pat:
            source = "app_identity"
        else:
            source = "app_identity_or_pat"
        return {
            "configured": bootstrap_ok or rotating or has_pat,
            "rotating": rotating,
            "healthy": self.healthy,  # acquires the lock itself — must be outside
            "degraded": state == STATE_DEGRADED,
            "state": state,
            "source": source,
            "token_expires_in": token_expires_in,
            "last_error": last_error,
        }

    # -- internals --

    def _rotation_loop(self) -> None:
        while not self._stop.wait(timeout=HEARTBEAT):
            try:
                self._tick()
            except Exception as e:  # noqa: BLE001 — the loop must never die
                logger.error("credential maintenance failed unexpectedly: %s", e)

    def _tick(self) -> None:
        now = time.time()
        if now - self._last_probe < PROBE_INTERVAL:
            return
        self._last_probe = now
        # Adopt (rotate + fan out) the minted token when there are live
        # consumers; otherwise verify mint capability without churning tokens
        # nobody is using. Either way health is classified and alerted.
        self._self_probe(adopt=self._session_count_fn() > 0)

    def _mint_auth(self) -> str | None:
        """Auth for the next mint: chain off our fresh minted token if we have
        one, else the bootstrap (app identity, then vended PAT)."""
        with self._lock:
            if self._rotating and self._token and (
                time.time() - self._minted_at
            ) < TOKEN_LIFETIME:
                return self._token
        return _bootstrap_auth()

    def _rotate_once(self) -> None:
        """On-demand mint used by token(). Kept cheap — full health
        classification (degraded vs dead) is owned by the periodic probe."""
        auth_token = self._mint_auth()
        if not auth_token:
            self.last_error = (
                "no credential to mint with — app identity can't authenticate "
                "and no WORKSHOP_PAT set"
            )
            return
        host = config.databricks_host()
        try:
            resp = requests.post(
                f"{host}/api/2.0/token/create",
                headers={"Authorization": f"Bearer {auth_token}"},
                json={"lifetime_seconds": TOKEN_LIFETIME, "comment": TOKEN_COMMENT},
                timeout=30,
            )
        except requests.RequestException as e:
            self.last_error = f"token create request failed: {e}"
            return
        if resp.status_code != 200:
            self.last_error = (
                f"token create failed ({resp.status_code}); serving credential "
                "without rotation — grant the app SP token CAN_USE to enable it"
            )
            return
        self._adopt_minted(resp.json())
        self._set_health(STATE_ROTATING, None)
        logger.info("credential rotated (lifetime=%ss)", TOKEN_LIFETIME)

    def _self_probe(self, *, adopt: bool) -> None:
        """Verify the credential end-to-end and surface degradation early.

        Runs on a slow cadence through idle windows so a misconfigured grant or
        an expiring credential is caught and alerted BEFORE attendees hit it.
        """
        auth = self._mint_auth()
        if not auth:
            self._set_health(
                STATE_UNHEALTHY,
                "no credential available to mint or authenticate with — grant "
                "the app SP token CAN_USE, or inject WORKSHOP_PAT",
            )
            return
        host = config.databricks_host()
        try:
            resp = requests.post(
                f"{host}/api/2.0/token/create",
                headers={"Authorization": f"Bearer {auth}"},
                json={"lifetime_seconds": TOKEN_LIFETIME, "comment": TOKEN_COMMENT},
                timeout=30,
            )
        except requests.RequestException as e:
            # A single network blip is not "unhealthy" — note it and let the
            # next probe re-decide rather than alarming on transient failures.
            self.last_error = f"probe mint request failed: {e}"
            return
        if resp.status_code == 200:
            data = resp.json()
            if adopt:
                self._adopt_minted(data)
            else:
                # Verify-only during idle: prove mint capability, then revoke the
                # probe token so idle windows don't accumulate live tokens.
                with self._lock:
                    self._rotating = True
                self._revoke(data["token_info"]["token_id"], data["token_value"])
            self._set_health(STATE_ROTATING, None)
            return
        # Mint failed: tell a valid-but-can't-mint credential (degraded — a
        # static credential is being served) from a rejected/expired one.
        from . import auth as auth_mod

        with self._lock:
            self._rotating = False
        if auth_mod.scim_token_valid(auth):
            self._set_health(
                STATE_DEGRADED,
                f"cannot mint short-lived tokens ({resp.status_code}); serving a "
                "static credential without rotation — grant the app service "
                "principal token CAN_USE to enable rotation before the event",
            )
        else:
            self._set_health(
                STATE_UNHEALTHY,
                f"the served credential was rejected by the workspace "
                f"({resp.status_code}); it may have expired — re-grant the app SP "
                "or re-vend WORKSHOP_PAT before the event",
            )

    def _adopt_minted(self, data: dict) -> None:
        """Make a freshly minted token the served token, fan it out, and revoke
        the previously-minted one (never the vended PAT)."""
        with self._lock:
            old_token_id = self._token_id
            self._token = data["token_value"]
            self._token_id = data["token_info"]["token_id"]
            self._minted_at = time.time()
            self._rotating = True
        self._fanout(data["token_value"])
        if old_token_id:
            self._revoke(old_token_id, data["token_value"])

    def _revoke(self, token_id: str, auth_token: str) -> None:
        host = config.databricks_host()
        try:
            requests.post(
                f"{host}/api/2.0/token/delete",
                headers={"Authorization": f"Bearer {auth_token}"},
                json={"token_id": token_id},
                timeout=30,
            )
        except requests.RequestException as e:
            logger.warning("revoke of minted token failed: %s", e)

    def _set_health(self, state: str, error: str | None) -> None:
        """Record health, log it (recurring for not-healthy so it stays visible
        in app logs), and emit a Control Tower alert on change / periodically."""
        now = time.time()
        with self._lock:
            changed = state != self._health
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
                {"state": state, "error": error, "source": _bootstrap_source()},
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
