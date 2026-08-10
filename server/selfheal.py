"""Recover from the auth error family before an attendee has to read about it.

Omnigent does not retry. When the attendee's OBO mirror goes stale, the harness
raises once, sanitizes the reason into a code like ``spec_resolver_failed``, and
stops — so the same failure repeats on every subsequent attempt until something
outside Omnigent puts a fresh credential on disk. During the incident that
motivated this module, that something did not exist.

This is that something. Three actions, in the order that makes the next attempt
work:

1. **Re-mirror** — force-write the freshest token this process is holding. Often
   sufficient on its own: the tab has been polling all along and the mirror is
   simply behind.
2. **Wake the host** — the remote host stands itself down into
   ``waiting_for_token`` behind a dead mirror, so it needs telling that the
   mirror is alive again.
3. **Nudge the tab** — the only source of a genuinely newer token is a live
   browser, and it will not send one until it is asked.

Rate-limited per attendee: a crash loop must not turn into a refresh loop, and
none of the three actions gets better for being done twice a second.
"""

from __future__ import annotations

import logging
import re
import threading
import time

logger = logging.getLogger(__name__)

# Recovery is cheap but not free, and a stuck harness re-raises continuously.
COOLDOWN_S = 15.0

# The sanitized codes an attendee sees when the credential is the problem. The
# first two are Omnigent's: they name the call that failed, not the reason,
# which is exactly why they were unactionable during the incident.
AUTH_ERROR_CODES = frozenset(
    {
        "spec_resolver_failed",
        "native_terminal_start_failed",
        "runner_disconnected",
        "obo_stale",
        "credential_unavailable",
    }
)

# Codes we have never seen before still get recovered from when they are shaped
# like a credential failure. A code Omnigent adds next month should not need a
# release here to be survivable.
_AUTH_SHAPE = re.compile(
    r"(?i)\b(auth\w*|token|credential|oauth|unauthorized|forbidden|401|403)\b"
)


def is_auth_error(code: str, message: str = "") -> bool:
    """Whether this failure is one a fresh credential could plausibly fix."""
    normalized = (code or "").strip().lower()
    if normalized in AUTH_ERROR_CODES:
        return True
    # Underscores are word characters, so ``databricks_token_refresh_failed``
    # hides "token" from a plain word-boundary match. Split on them first.
    haystack = re.sub(r"[_\-]+", " ", f"{normalized} {message or ''}")
    return bool(_AUTH_SHAPE.search(haystack))


class SelfHealer:
    """Re-mirror, wake, and nudge — at most once per attendee per cooldown."""

    def __init__(self, *, cooldown: float = COOLDOWN_S, clock=time.monotonic) -> None:
        self._cooldown = cooldown
        self._clock = clock
        self._last: dict[str, float] = {}
        self._lock = threading.Lock()

    def recover(self, email: str, reason: str, *, force: bool = False) -> dict:
        """Try to make the attendee's next Omnigent attempt work.

        Returns what was done and whether the credential is usable afterwards.
        Never raises: this runs from a log sweep and from a request path, and
        neither should die because a recovery step could not be taken.
        """
        email = (email or "").strip().lower()
        if not email:
            return {"attempted": False, "reason": "no attendee"}
        now = self._clock()
        with self._lock:
            last = self._last.get(email, -self._cooldown)
            if not force and now - last < self._cooldown:
                # Still answer the question the caller asked. Recovery a second
                # ago is a reason not to repeat the work, not a reason to tell
                # an attendee their credential is broken when it is not.
                return {
                    "attempted": False,
                    "reason": "cooling down",
                    "credential_fresh": self._is_fresh(email),
                }
            self._last[email] = now

        actions: list[str] = []
        remirrored = self._remirror(email, actions)
        self._wake(email, actions)
        self._nudge(actions)
        fresh = self._is_fresh(email)
        logger.warning(
            "self-heal for %s after %s: %s (credential %s)",
            email,
            reason,
            ", ".join(actions) or "nothing to do",
            "usable" if fresh else "still stale",
        )
        self._emit(email, reason, actions, fresh)
        return {
            "attempted": True,
            "reason": reason,
            "actions": actions,
            "remirrored": remirrored,
            "credential_fresh": fresh,
        }

    # -- steps -------------------------------------------------------------

    def _remirror(self, email: str, actions: list[str]) -> bool:
        try:
            from .obo import obo_manager

            if obo_manager.force_refresh(email):
                actions.append("remirrored")
                return True
        except Exception:  # noqa: BLE001
            logger.debug("self-heal re-mirror failed for %s", email, exc_info=True)
        return False

    def _wake(self, email: str, actions: list[str]) -> None:
        try:
            from . import config

            if not config.omnigent_remote_enabled():
                return
            from .omnigent_remote import remote_host_manager

            remote_host_manager.notify(email)
            actions.append("woke host")
        except Exception:  # noqa: BLE001
            logger.debug("self-heal host wake failed for %s", email, exc_info=True)

    def _nudge(self, actions: list[str]) -> None:
        try:
            from .events import event_hub

            event_hub.publish({"t": "obo_refresh"})
            actions.append("nudged tab")
        except Exception:  # noqa: BLE001
            logger.debug("self-heal nudge failed", exc_info=True)

    def _is_fresh(self, email: str) -> bool:
        try:
            from . import obo

            status = obo.obo_manager.status(email)
            expires_in = status["expires_in"]
            return bool(status["present"]) and (
                expires_in is None or expires_in > obo.FRESH_MARGIN
            )
        except Exception:  # noqa: BLE001
            return False

    def _emit(self, email: str, reason: str, actions: list[str], fresh: bool) -> None:
        from . import telemetry

        telemetry.emit(
            "selfheal.attempted",
            email,
            {"reason": reason[:120], "actions": actions, "credential_fresh": fresh},
        )


self_healer = SelfHealer()


def on_omnigent_error(attendee: str, code: str, message: str = "") -> None:
    """Hook for the log collector: recover from anything credential-shaped.

    The collector sees the real exception seconds after it is written, which is
    long before an attendee finishes reading the sanitized code on their screen.
    Using that window is the difference between a self-healing wobble and a
    support conversation.
    """
    if is_auth_error(code, message):
        self_healer.recover(attendee, f"omnigent error {code}"[:120])


__all__ = [
    "AUTH_ERROR_CODES",
    "SelfHealer",
    "is_auth_error",
    "on_omnigent_error",
    "self_healer",
]
