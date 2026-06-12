"""Workspace credential for attendee CLIs — vended by Control Tower.

Databricks Apps OBO scopes deliberately exclude the Token API, so the app
cannot mint per-user PATs from forwarded tokens. Instead, Control Tower
vends a workspace credential at provision time and injects it as the
WORKSHOP_PAT env var (directly, or via an app secret resource). Attendees
never touch tokens.

The app hardens the vended credential with short-lived rotation when it can:
it chains 15-minute tokens off the bootstrap PAT every 10 minutes (revoking
only its own minted tokens, NEVER the vended bootstrap — restarts must be
able to re-bootstrap from env). If the credential can't call the Token API,
the app simply serves the vended PAT directly.
"""

from __future__ import annotations

import logging
import os
import threading
import time

import requests

from . import config

logger = logging.getLogger(__name__)

TOKEN_LIFETIME = 900          # 15 minutes
ROTATION_INTERVAL = 600       # 10 minutes
TOKEN_COMMENT = "workshop-terminal-auto"


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


class CredentialManager:
    """Instance-level credential: bootstrap from WORKSHOP_PAT, rotate if able."""

    def __init__(self, session_count_fn):
        self._session_count_fn = session_count_fn
        self._token: str | None = None
        self._token_id: str | None = None
        self._minted_at: float = 0.0
        self._rotating = False  # True once we've successfully minted our own
        self._bootstrap_ok = False  # True if app identity or a vended PAT can bootstrap
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self.last_error: str | None = None

    # -- public --

    @property
    def healthy(self) -> bool:
        with self._lock:
            if self._token is None:
                return self._bootstrap_ok or bool(vended_pat())
            if not self._rotating:
                return True  # serving the vended PAT directly
            return (time.time() - self._minted_at) < TOKEN_LIFETIME

    def token(self) -> str:
        """Current CLI token: our freshest minted token, else mint one now.

        The minted token is what attendees' CLIs receive — short-lived and
        rotating. In the P1-2 app-identity mode there is no static token to
        serve, so a first call mints on demand from the app's own OAuth identity.
        A vended PAT (legacy) is served directly only if minting is unavailable.
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

        # Minting unavailable. Serve a vended PAT directly if one exists (legacy).
        bootstrap = vended_pat()
        if bootstrap:
            return bootstrap
        raise CredentialError(
            "No workshop credential available — the app's own identity can't "
            "mint a token and no WORKSHOP_PAT is set. Grant the app's service "
            "principal token-create permission, or inject WORKSHOP_PAT."
        )

    def start(self) -> None:
        """Begin the rotation loop.

        Bootstraps from the app's own OAuth identity (P1-2) when available, else
        a vended PAT. No-op if neither can authenticate.
        """
        self._bootstrap_ok = bool(_bootstrap_auth())
        if not self._bootstrap_ok:
            logger.warning(
                "no app identity and no WORKSHOP_PAT — terminals can't "
                "authenticate until a credential is available"
            )
            return
        if vended_pat() and not app_identity_bearer():
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
            rotating = self._rotating
            last_error = self.last_error
        return {
            "configured": self._bootstrap_ok or rotating or bool(vended_pat()),
            "rotating": rotating,
            "healthy": self.healthy,  # acquires the lock itself — must be outside
            "source": "vended_pat" if (vended_pat() and not self._bootstrap_ok)
            else ("app_identity" if not vended_pat() else "app_identity_or_pat"),
            "last_error": last_error,
        }

    # -- internals --

    def _rotation_loop(self) -> None:
        # First mint happens promptly so CLIs get a short-lived token early.
        while not self._stop.wait(timeout=30 if not self._rotating else ROTATION_INTERVAL):
            try:
                if self._rotating and self._session_count_fn() == 0:
                    continue  # nobody active — don't churn tokens
                self._rotate_once()
            except Exception as e:
                logger.error("credential rotation failed unexpectedly: %s", e)

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
            # Credential can't mint tokens (policy / missing CAN_USE) — serve
            # the vended PAT directly and stop trying so loudly.
            self.last_error = f"token create failed ({resp.status_code}); using vended PAT directly"
            if not self._rotating:
                logger.info("credential rotation unavailable (%s) — vended PAT served as-is",
                            resp.status_code)
            return

        data = resp.json()
        with self._lock:
            old_token_id = self._token_id
            self._token = data["token_value"]
            self._token_id = data["token_info"]["token_id"]
            self._minted_at = time.time()
            self._rotating = True
        self.last_error = None

        self._fanout(data["token_value"])

        # Revoke only our own previously-minted token — never the vended PAT.
        if old_token_id:
            try:
                requests.post(
                    f"{host}/api/2.0/token/delete",
                    headers={"Authorization": f"Bearer {data['token_value']}"},
                    json={"token_id": old_token_id},
                    timeout=30,
                )
            except requests.RequestException as e:
                logger.warning("revoke of rotated token failed: %s", e)
        logger.info("credential rotated (lifetime=%ss)", TOKEN_LIFETIME)

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
