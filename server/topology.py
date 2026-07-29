"""Topology guard (gap P1-11a).

Workshop Terminal's security model assumes **one disposable workspace per
attendee**: the credential manager is instance-level (one vended token for the
whole app), per-user HOMEs are not uid-isolated, and git-sync writes as a single
identity. Running multiple distinct attendees on one instance therefore collapses
credential isolation and attribution.

Static session-cap checks remain defense in depth, but they do not establish
identity: several principals can use sessions sequentially or below the cap.
When remote Omnigent is enabled, ``validate_remote_omnigent`` fails startup on
any multi-attendee configuration, and ``auth`` rejects every principal other
than ``WORKSHOP_ATTENDEE_EMAIL`` before HOME creation or credential writes.
"""

from __future__ import annotations

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
    "startup_warning",
    "second_attendee_warning",
]
