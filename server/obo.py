"""On-behalf-of-user (OBO) token manager.

Captures the attendee's forwarded ``x-forwarded-access-token`` (a 1-hour U2M
OAuth token, refreshed per request by the Databricks Apps proxy) and persists
it to a second databricks CLI profile (default ``[me]``) so the agent can run
``databricks --profile me ...`` as the *attendee* — seeing exactly the Unity
Catalog objects, row filters, and column masks the attendee is governed by,
instead of the service principal's grants.

Design constraints (grounded in Databricks Apps auth guidance — "read the OBO
token per request; use the app SP for long-running work"):

- The token must not be cached long. The ``[me]`` file is (re)written whenever
  the captured token *changes*. Because the frontend polls every ~5s through
  ``get_current_user``, the captured value is essentially always current while
  a tab is open, so ``[me]`` is never more than seconds stale.
- The app holds no refresh token, so it cannot mint a fresh OBO token itself —
  freshness is necessarily *pulled* from the live browser tab. A fully closed
  tab is therefore unrecoverable by design (the SP/``[DEFAULT]`` profile powers
  all background/long-running work instead).
- The token is **never logged** and never enters the attendee shell env.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
import os
import stat
import tempfile
import threading
import time
from pathlib import Path

from . import config

logger = logging.getLogger(__name__)

# When the on-file token is within this many seconds of its JWT ``exp`` we treat
# the profile as not-fresh in status(), so operators can catch a stale `me`
# before an attendee hits a 401.
FRESH_MARGIN = 60


def _decode_jwt_claims(token: str) -> dict:
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return {}
        payload = parts[1]
        payload += "=" * (-len(payload) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload))
        return claims if isinstance(claims, dict) else {}
    except (ValueError, binascii.Error, json.JSONDecodeError, TypeError):
        return {}


def decode_jwt_times(token: str) -> tuple[float | None, float | None]:
    """Best-effort parse of JWT ``iat`` and ``exp`` timestamps.

    No signature verification and no new dependency — we only read the
    self-reported timestamps for capture ordering and expiry.
    """
    try:
        claims = _decode_jwt_claims(token)
        iat = claims.get("iat")
        exp = claims.get("exp")
        return (
            float(iat) if iat is not None else None,
            float(exp) if exp is not None else None,
        )
    except (ValueError, TypeError):
        return None, None


def decode_jwt_exp(token: str) -> float | None:
    return decode_jwt_times(token)[1]


def _token_order(iat: float | None, exp: float | None) -> tuple[float, float]:
    return (
        iat if iat is not None else float("-inf"),
        exp if exp is not None else float("-inf"),
    )


def decode_jwt_scopes(token: str) -> set[str]:
    """Read scopes from a trusted proxy-forwarded OAuth JWT."""
    claims = _decode_jwt_claims(token)
    raw = claims.get("scope", claims.get("scp", []))
    if isinstance(raw, str):
        return {scope for scope in raw.replace(",", " ").split() if scope}
    if isinstance(raw, list):
        return {
            scope.strip()
            for scope in raw
            if isinstance(scope, str) and scope.strip()
        }
    return set()


class _Record:
    __slots__ = (
        "token",
        "iat",
        "exp",
        "last_capture_exp",
        "scopes",
        "captured_at",
        "written_token",
        "written_at",
    )

    def __init__(
        self,
        token: str,
        iat: float | None,
        exp: float | None,
        captured_at: float,
    ):
        self.token = token
        self.iat = iat
        self.exp = exp
        self.last_capture_exp = exp
        self.scopes = decode_jwt_scopes(token)
        self.captured_at = captured_at
        self.written_token: str | None = None
        self.written_at: float = 0.0


class OboManager:
    """Process-local store of the freshest OBO token per attendee email."""

    def __init__(self) -> None:
        self._by_email: dict[str, _Record] = {}
        self._lock = threading.Lock()

    def capture(self, email: str, token: str | None) -> None:
        """Record the freshest forwarded token for ``email`` and, if the user's
        home exists, (re)write the ``[me]`` profile.

        Guarded: a no-op when OBO is disabled or no token is present, and never
        raises into the request path.
        """
        email = (email or "").strip().lower()
        remote_url = config.omnigent_app_url()
        if (
            not (config.obo_enabled() or remote_url)
            or not token
            or "@" not in email
        ):
            return
        try:
            if remote_url:
                # Remote auth can arrive on the first authenticated request,
                # before a terminal/session path has created the attendee HOME.
                from .users import user_manager

                user_manager.get(email)
            self._capture(email, token)
        except Exception as e:  # noqa: BLE001 — capture must never break a request
            logger.warning("OBO capture for %s failed: %s", email, e)

    def _capture(self, email: str, token: str) -> None:
        now = time.time()
        token_iat, token_exp = decode_jwt_times(token)
        with self._lock:
            rec = self._by_email.get(email)
            # Concurrent proxy requests can complete out of order. Never let a
            # late expired snapshot replace a bearer that is still fresh.
            if (
                rec is not None
                and token_exp is not None
                and token_exp <= now
                and rec.exp is not None
                and rec.exp > now
            ):
                # Mark the latest observed auth snapshot stale for health
                # reporting, while preserving the still-usable on-disk token.
                rec.last_capture_exp = token_exp
                rec.captured_at = now
                return
            if (
                rec is not None
                and rec.token != token
                and _token_order(token_iat, token_exp)
                <= _token_order(rec.iat, rec.exp)
            ):
                return
            if rec is None:
                rec = _Record(token, token_iat, token_exp, now)
                self._by_email[email] = rec
            else:
                rec.token = token
                rec.iat = token_iat
                rec.exp = token_exp
                rec.last_capture_exp = token_exp
                rec.scopes = decode_jwt_scopes(token)
                rec.captured_at = now
            # Throttle: only touch the file when the token string actually
            # changed. Rewriting an identical token can't extend its life, so a
            # near-expiry rewrite of the same value buys nothing — only a fresher
            # capture from the live tab (or force_refresh) helps.
            needs_write = rec.written_token != rec.token
        if needs_write:
            self._write(email, rec)

    def force_refresh(self, email: str) -> bool:
        """Force-write the latest captured token for ``email`` (the reactive
        self-heal path used by ``/api/obo/refresh`` and the ``databricks-me``
        wrapper). Returns True if a token was written, False if none is held."""
        if not (config.obo_enabled() or config.omnigent_remote_enabled()):
            return False
        with self._lock:
            rec = self._by_email.get(email)
        if rec is None or not rec.token:
            return False
        try:
            self._write(email, rec)
            return True
        except Exception as e:  # noqa: BLE001
            logger.warning("OBO force-refresh for %s failed: %s", email, e)
            return False

    def user_ready(self, user) -> None:
        """Flush a token captured before ``user`` had a bootstrapped home.

        User creation calls this only after releasing the user-registry lock and
        creating the home, so the callback cannot recurse into ``get()`` or
        deadlock with token capture.
        """
        if not (config.obo_enabled() or config.omnigent_remote_enabled()):
            return
        with self._lock:
            rec = self._by_email.get(user.email)
            needs_write = rec is not None and rec.written_token != rec.token
        if not needs_write or rec is None:
            return
        try:
            self._write_user(user, rec)
        except Exception as e:  # noqa: BLE001 — user creation must still succeed
            logger.warning("Deferred OBO write for %s failed: %s", user.email, e)

    def _write(self, email: str, rec: _Record) -> None:
        from .users import user_manager

        user = user_manager.peek(email)
        if user is None:
            return  # user_ready() flushes this as soon as the home is bootstrapped
        self._write_user(user, rec)

    def _write_user(self, user, rec: _Record) -> None:
        from . import cli_config

        # Serialize the token selection and disk write with every other
        # per-user config update. Capture never holds _lock while waiting for
        # user.lock, so briefly reading the record in the opposite direction
        # here cannot form a lock cycle. Selecting *after* user.lock is acquired
        # means an older queued writer always writes the newest held token.
        mirrored = False
        mirrored_exp: float | None = None
        with user.lock:
            with self._lock:
                current = self._by_email.get(user.email)
                if current is None:
                    return
                token = current.token
                # Keep record commit atomic with the disk write. status() and
                # later captures wait until both agree. Use the lock-held helper
                # so this path never recursively acquires user.lock.
                if config.obo_enabled():
                    cli_config.update_me_profile_locked(user, token)
                remote_url = config.omnigent_app_url()
                if remote_url and current.exp is not None:
                    mirrored_exp = current.exp
                    _write_omnigent_token_locked(
                        user.home,
                        remote_url,
                        token,
                        user.email,
                        current.exp,
                    )
                    mirrored = True
                current.written_token = token
                current.written_at = time.time()
        if mirrored and mirrored_exp is not None and mirrored_exp > time.time():
            _notify_remote_host(user.email)

    def status(self, email: str | None = None) -> dict:
        """Health/presence view for one attendee (no token material)."""
        enabled = config.obo_enabled()
        with self._lock:
            rec = self._by_email.get(email) if email else max(
                self._by_email.values(),
                key=lambda candidate: candidate.captured_at,
                default=None,
            )
            present = rec is not None and bool(rec.written_token)
            exp = rec.last_capture_exp if rec else None
            last_refresh = rec.written_at if rec else 0.0
            observed_scopes = set(rec.scopes) if rec else set()
            validated_at = rec.captured_at if rec and rec.scopes else 0.0
        configured_scopes = {
            scope.strip()
            for scope in config.obo_scopes().split(",")
            if scope.strip()
        }
        if not observed_scopes:
            validation_state = "pending"
        elif configured_scopes <= observed_scopes:
            validation_state = "verified"
        else:
            validation_state = "insufficient"
        expires_in: int | None = None
        fresh = present
        if exp is not None:
            expires_in = max(0, int(exp - time.time()))
            fresh = present and expires_in > FRESH_MARGIN
        return {
            "enabled": enabled,
            "profile": config.obo_profile_name(),
            "scopes": config.obo_scopes(),
            "configured_scopes": sorted(configured_scopes),
            "observed_scopes": sorted(observed_scopes),
            "verified_scopes": sorted(observed_scopes),
            "validation_state": validation_state,
            "scope_source": "jwt_claim" if observed_scopes else None,
            "validated_at": validated_at or None,
            "present": present,
            "fresh": bool(enabled and fresh),
            "expires_in": expires_in,
            "last_refresh": last_refresh or None,
        }


obo_manager = OboManager()


def _write_omnigent_token_locked(
    home: str,
    server_url: str,
    token: str,
    user_id: str,
    expires_at: float,
) -> None:
    """Merge one Omnigent server record using an atomic durable replace.

    The caller holds the attendee's existing ``User.lock``, serializing this
    read/merge/write with every other auth snapshot for that HOME.
    """
    directory = Path(home) / ".omnigent"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "auth_tokens.json"
    data: dict = {}
    try:
        loaded = json.loads(path.read_text())
        if isinstance(loaded, dict):
            data = loaded
    except (OSError, json.JSONDecodeError):
        pass
    data[server_url.rstrip("/")] = {
        "token": token,
        "user_id": user_id.lower(),
        "expires_at": expires_at,
    }

    fd, temporary = tempfile.mkstemp(
        prefix=".auth_tokens.", suffix=".tmp", dir=directory
    )
    try:
        os.fchmod(fd, stat.S_IRUSR | stat.S_IWUSR)
        with os.fdopen(fd, "w") as handle:
            json.dump(data, handle, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
        dir_fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _notify_remote_host(email: str) -> None:
    """Wake the per-user host after a fresh token reached disk."""
    from .omnigent_remote import remote_host_manager

    remote_host_manager.notify(email)
