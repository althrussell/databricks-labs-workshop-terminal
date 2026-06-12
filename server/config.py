"""Runtime configuration.

Everything is read from the environment *at call time* — Control Tower edits
app.yaml env in place and injects per-deployment env_vars, so values must
never be cached at import or baked into the frontend build.
"""

from __future__ import annotations

import os


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


def ensure_https(url: str) -> str:
    if url and not url.startswith(("http://", "https://")):
        return f"https://{url}"
    return url


def control_tower_ingest_url() -> str:
    """Control Tower's event-ingest base URL (contract C3b). Empty disables emit."""
    return _env("CONTROL_TOWER_INGEST_URL")


def control_tower_ingest_token() -> str:
    """Shared X-Ingest-Token CT vends for the event-ingest endpoint."""
    return _env("CONTROL_TOWER_INGEST_TOKEN")


def workshop_run_id() -> str:
    """CT lab run id this terminal belongs to (injected at deploy)."""
    return _env("WORKSHOP_RUN_ID")


def workspace_id() -> str:
    return _env("DATABRICKS_WORKSPACE_ID")


def databricks_host() -> str:
    return ensure_https(_env("DATABRICKS_HOST").rstrip("/"))


def local_dev() -> bool:
    return _env("LOCAL_DEV") == "1"


def admin_group() -> str:
    return _env("ADMIN_GROUP", "platform_admins")


def access_group() -> str:
    return _env("ACCESS_GROUP")


def data_root() -> str:
    return _env("DATA_ROOT", "/app/python/source_code/data")


def users_root() -> str:
    return os.path.join(data_root(), "users")


def shared_prefix() -> str:
    """Shared install prefix for node + agent CLIs (one copy for all users)."""
    return os.path.join(data_root(), "shared")


def max_sessions_per_user() -> int:
    return _env_int("MAX_SESSIONS_PER_USER", 3)


def max_sessions_global() -> int:
    return _env_int("MAX_SESSIONS_GLOBAL", 30)


def session_idle_timeout() -> int:
    return _env_int("SESSION_IDLE_TIMEOUT_SECONDS", 3600)


def session_state_path() -> str:
    """File path for the session-metadata journal (P1-11), or "" to disable.

    When set, the SessionManager journals live-session metadata + a scrollback
    tail so that after a server restart the attendee sees which terminals they
    had (ended on restart) with replay, instead of a silent blank. Empty keeps
    the manager fully in-memory.
    """
    return _env("SESSION_STATE_PATH")


def workshop_phase_default() -> str:
    return _env("WORKSHOP_PHASE", "intro")


def content_pack_path() -> str:
    return _env("CONTENT_PACK_PATH")


def lab_coach_enabled() -> bool:
    return _env("LAB_COACH", "true").lower() not in ("false", "0", "no", "off")


def auto_mode_enabled() -> bool:
    """Lab 'auto mode': agents run without permission prompts. Attendees work
    in isolated per-user HOMEs inside a disposable workshop container, so the
    zero-prompt flow is the right default (CoDA lab-profile behaviour)."""
    return _env("WORKSHOP_AUTO_MODE", "true").lower() not in ("false", "0", "no", "off")


def topic_detection_enabled() -> bool:
    """Coarse keyword spotting on terminal output to drive contextual
    insights. Only topic flags are recorded — terminal content is never
    stored or transmitted."""
    return _env("TOPIC_DETECTION", "true").lower() not in ("false", "0", "no", "off")


def enable_public_mcp() -> bool:
    """Whether to wire public MCP servers (DeepWiki, Exa) into the agent.

    Off by default (gap P1-21): the agents run autonomously with a live
    workspace token, so fetching from public MCP servers is an indirect
    prompt-injection egress path. Operators opt in per event when the content
    warrants it. With this off, the agent's MCP server list is empty
    (workspace-internal only)."""
    return _env("ENABLE_PUBLIC_MCP", "false").lower() in ("1", "true", "yes", "on")


def branding() -> dict:
    return {
        "brand_name": _env("BRAND_NAME") or "Databricks",
        "brand_logo_url": _env("BRAND_LOGO_URL"),
        "brand_primary_color": _env("BRAND_PRIMARY_COLOR"),
        "event_name": _env("EVENT_NAME"),
        "cobranded": bool(_env("BRAND_NAME")),
    }
