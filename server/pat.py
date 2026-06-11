"""Per-user PAT lifecycle: zero-touch mint via the forwarded OBO token,
then background rotation.

The forwarded user-authorization token is used ONLY to call the Token API —
it never reaches a CLI or terminal. The CLIs run on a real short-lived PAT
(full workspace coverage). Rotation chains off the current PAT every
ROTATION_INTERVAL; if the chain breaks while the user is idle (PAT expired,
rotation paused), the next request's forwarded token re-bootstraps it.
"""

from __future__ import annotations

import logging
import threading
import time

import requests

from . import config
from .users import User

logger = logging.getLogger(__name__)

TOKEN_LIFETIME = 900          # 15 minutes
ROTATION_INTERVAL = 600       # 10 minutes
TOKEN_COMMENT = "workshop-terminal-auto"


class PatError(Exception):
    pass


class UserPatManager:
    """Mints and rotates one user's PAT, fanning it out to their CLI configs."""

    def __init__(self, user: User, session_count_fn):
        self.user = user
        self._session_count_fn = session_count_fn
        self._token: str | None = None
        self._token_id: str | None = None
        self._minted_at: float = 0.0
        self._last_obo_token: str | None = None  # recovery path only, memory only
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self.last_error: str | None = None

    # -- public --

    @property
    def token(self) -> str | None:
        with self._lock:
            return self._token

    @property
    def healthy(self) -> bool:
        with self._lock:
            return bool(self._token) and (time.time() - self._minted_at) < TOKEN_LIFETIME

    def ensure(self, obo_token: str | None) -> None:
        """Called on every authenticated request that needs CLI credentials.

        Stores the freshest forwarded token for recovery and (re)bootstraps
        the PAT chain when there is no live PAT.
        """
        if obo_token:
            with self._lock:
                self._last_obo_token = obo_token
        if self.healthy:
            return
        bootstrap = obo_token or self._last_obo_token
        if not bootstrap:
            raise PatError(
                "User authorization is not configured on this app "
                "(no forwarded access token) — terminals can't authenticate."
            )
        self._mint(bootstrap, revoke_previous=True)
        self._start_rotation()

    def stop(self) -> None:
        self._stop.set()

    # -- internals --

    def _mint(self, auth_token: str, revoke_previous: bool) -> None:
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
            raise PatError(self.last_error) from e
        if resp.status_code != 200:
            self.last_error = f"token create failed ({resp.status_code}): {resp.text[:200]}"
            logger.error("PAT mint for %s: %s", self.user.email, self.last_error)
            raise PatError(self.last_error)

        data = resp.json()
        new_token = data["token_value"]
        new_token_id = data["token_info"]["token_id"]
        with self._lock:
            old_token_id = self._token_id
            self._token = new_token
            self._token_id = new_token_id
            self._minted_at = time.time()
        self.last_error = None

        from . import cli_config
        cli_config.update_tokens(self.user, new_token)

        if revoke_previous and old_token_id:
            self._revoke(new_token, old_token_id)
        logger.info("PAT minted for %s (id=%s, lifetime=%ss)", self.user.email, new_token_id, TOKEN_LIFETIME)

    def _revoke(self, auth_token: str, token_id: str) -> None:
        # Best-effort — a missed revoke expires naturally within TOKEN_LIFETIME.
        try:
            requests.post(
                f"{config.databricks_host()}/api/2.0/token/delete",
                headers={"Authorization": f"Bearer {auth_token}"},
                json={"token_id": token_id},
                timeout=30,
            )
        except requests.RequestException as e:
            logger.warning("PAT revoke for %s failed: %s", self.user.email, e)

    def _start_rotation(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._rotation_loop, daemon=True,
            name=f"pat-rotation-{self.user.slug}",
        )
        self._thread.start()

    def _rotation_loop(self) -> None:
        while not self._stop.wait(timeout=ROTATION_INTERVAL):
            try:
                if self._session_count_fn(self.user.email) == 0:
                    logger.info("PAT rotation for %s: no active sessions — skipping", self.user.email)
                    continue
                current = self.token
                if not current:
                    continue
                self._mint(current, revoke_previous=True)
            except PatError:
                # Chain broken (expired while paused, policy change). ensure()
                # re-bootstraps from the next request's forwarded token.
                logger.warning("PAT rotation chain broken for %s — awaiting re-bootstrap", self.user.email)
            except Exception as e:
                logger.error("PAT rotation for %s failed unexpectedly: %s", self.user.email, e)


def ensure_user_pat(user: User, obo_token: str | None) -> UserPatManager:
    from .sessions import session_manager
    with user.lock:
        if user.pat_manager is None:
            user.pat_manager = UserPatManager(user, session_manager.count_for)
    user.pat_manager.ensure(obo_token)
    return user.pat_manager
