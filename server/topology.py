"""Topology guard (gap P1-11a).

Workshop Terminal's security model assumes **one disposable workspace per
attendee**: the credential manager is instance-level (one vended token for the
whole app), per-user HOMEs are not uid-isolated, and git-sync writes as a single
identity. Running multiple distinct attendees on one instance therefore collapses
credential isolation and attribution.

Static session-cap checks remain defense in depth, but they do not establish
identity: several principals can use sessions sequentially or below the cap.
When remote Omnigent is enabled, a process-local, lock-protected binding accepts
the first authenticated attendee and rejects every different attendee before
HOME creation or credential writes.
"""

from __future__ import annotations

import fcntl
import os
import threading
from pathlib import Path

from . import config


def config_permits_multi_attendee(global_cap: int, per_user_cap: int) -> bool:
    """True if the session caps allow a second distinct attendee to run sessions.

    With ``MAX_SESSIONS_GLOBAL > MAX_SESSIONS_PER_USER`` a single attendee cannot
    consume the global pool, so a second attendee could also get sessions.
    """
    return global_cap > per_user_cap


def validate_remote_omnigent() -> None:
    """Fail closed when remote OBO files would share one Unix uid."""
    if not config.omnigent_app_url():
        return
    if config.allow_shared_topology():
        raise ValueError(
            "Remote Omnigent requires one attendee per instance because OBO "
            "tokens are stored under a shared Unix uid. Unset "
            "ALLOW_SHARED_TOPOLOGY and deploy one Workshop Terminal instance "
            "per attendee workspace."
        )
    if config_permits_multi_attendee(
        config.max_sessions_global(), config.max_sessions_per_user()
    ):
        raise ValueError(
            "Remote Omnigent requires one attendee per instance. Configure "
            "MAX_SESSIONS_GLOBAL <= MAX_SESSIONS_PER_USER and deploy one "
            "Workshop Terminal instance per attendee workspace."
        )


class AttendeeBindingConflict(RuntimeError):
    """A remote instance is already owned by a different attendee."""


class AttendeeBinding:
    """Concurrency-safe first-attendee binding for remote Omnigent."""

    def __init__(self) -> None:
        self._email: str | None = None
        self._lock = threading.Lock()

    @staticmethod
    def _paths() -> tuple[Path, Path]:
        root = Path(config.users_root()).parent
        return (
            root / ".omnigent-attendee-binding",
            root / ".omnigent-attendee-binding.lock",
        )

    def _persisted_email(self, claim: str | None = None) -> str | None:
        marker, lock_path = self._paths()
        marker.parent.mkdir(parents=True, exist_ok=True)
        with open(lock_path, "a+", encoding="utf-8") as lock_file:
            os.chmod(lock_path, 0o600)
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                if marker.exists():
                    persisted = marker.read_text(encoding="utf-8").strip().lower()
                    if not persisted:
                        raise AttendeeBindingConflict(
                            "Remote Omnigent attendee binding is invalid; "
                            "operator intervention is required."
                        )
                    return persisted
                if claim is None:
                    return None
                fd = os.open(
                    marker,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                    0o600,
                )
                try:
                    os.write(fd, f"{claim}\n".encode())
                    os.fsync(fd)
                finally:
                    os.close(fd)
                os.chmod(marker, 0o600)
                directory_fd = os.open(marker.parent, os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
                return claim
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    def bind(self, email: str) -> str | None:
        """Bind to ``email`` in remote mode, or no-op when remote is disabled."""
        if not config.omnigent_remote_enabled():
            return None
        normalized = (email or "").strip().lower()
        configured = config.workshop_attendee_email()
        with self._lock:
            self._email = configured
            if configured != normalized:
                raise AttendeeBindingConflict(
                    "Remote Omnigent instance is configured for another attendee; "
                    "deploy one Workshop Terminal instance per attendee workspace."
                )
            return self._email

    def require_bound(self, email: str) -> str | None:
        """Allow a helper only after an authenticated request established ownership."""
        if not config.omnigent_remote_enabled():
            return None
        normalized = (email or "").strip().lower()
        configured = config.workshop_attendee_email()
        with self._lock:
            self._email = configured
            if configured != normalized:
                raise AttendeeBindingConflict(
                    "Remote Omnigent instance is configured for another attendee; "
                    "deploy one Workshop Terminal instance per attendee workspace."
                )
            return self._email

    def status(self) -> dict[str, object]:
        enabled = config.omnigent_remote_enabled()
        with self._lock:
            if enabled and self._email is None:
                self._email = config.workshop_attendee_email()
            bound = self._email is not None
        return {
            "enforced": enabled,
            "status": "bound" if enabled and bound else (
                "unbound" if enabled else "disabled"
            ),
        }


attendee_binding = AttendeeBinding()


def startup_warning() -> str | None:
    """Warning to log at startup, or None when the topology is unambiguous."""
    if config.allow_shared_topology():
        return None
    if config_permits_multi_attendee(
        config.max_sessions_global(), config.max_sessions_per_user()
    ):
        return (
            "Session caps permit more than one attendee on this instance "
            f"(MAX_SESSIONS_GLOBAL={config.max_sessions_global()} > "
            f"MAX_SESSIONS_PER_USER={config.max_sessions_per_user()}), but the "
            "security model is one disposable workspace per attendee: the vended "
            "credential and git identity are shared instance-wide and HOMEs are "
            "not uid-isolated. Deploy one instance per attendee workspace, or set "
            "ALLOW_SHARED_TOPOLOGY=true to acknowledge shared use for a trusted group."
        )
    return None


def second_attendee_warning(distinct_attendees: int) -> str | None:
    """Warning when a second distinct attendee registers without opt-in."""
    if config.allow_shared_topology():
        return None
    if distinct_attendees > 1:
        return (
            f"{distinct_attendees} distinct attendees are now using this single "
            "instance. They share one vended credential and git identity, and "
            "HOMEs are not uid-isolated — cross-attendee access and attribution "
            "are NOT enforced. This is unsupported without ALLOW_SHARED_TOPOLOGY=true; "
            "the intended topology is one workspace (and instance) per attendee."
        )
    return None


__all__ = [
    "config_permits_multi_attendee",
    "validate_remote_omnigent",
    "AttendeeBinding",
    "AttendeeBindingConflict",
    "attendee_binding",
    "startup_warning",
    "second_attendee_warning",
]
