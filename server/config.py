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


_TRUE = ("1", "true", "yes", "on", "enabled", "enable")


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in _TRUE


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


def allow_shared_topology() -> bool:
    """Opt-in acknowledgement that one instance may serve multiple attendees.

    Off by default (gap P1-11a): the supported model is one workspace per
    attendee. See server/topology.py.
    """
    return _env("ALLOW_SHARED_TOPOLOGY").strip().lower() in {
        "1", "true", "yes", "on", "enabled", "enable",
    }


def session_idle_timeout() -> int:
    # 8 hours: long enough to survive a lunch break / closed laptop lid without
    # reaping the attendee's PTY out from under a returning session (the old
    # 1-hour default reaped a terminal an attendee stepped away from, and lined
    # up exactly with Omnigent's 1-hour runner idle timeout for a double hit).
    # Memory, not idle PTYs, is the real constraint on a per-attendee instance.
    return _env_int("SESSION_IDLE_TIMEOUT_SECONDS", 28800)


def session_state_path() -> str:
    """File path for the session-metadata journal (P1-11), or "" to disable.

    When set, the SessionManager journals live-session metadata + a scrollback
    tail so that after a server restart the attendee sees which terminals they
    had (ended on restart) with replay, instead of a silent blank. Empty keeps
    the manager fully in-memory.
    """
    return _env("SESSION_STATE_PATH")


def agents_enabled_default() -> bool:
    """Deploy-time default for whether coding (LLM) agents may launch (P1-16).

    The operator kill-switch (see server/spend.py) overrides this at runtime.
    """
    return _env("AGENTS_ENABLED", "true").lower() not in ("false", "0", "no", "off")


def max_agent_launches_per_user() -> int:
    """Per-attendee lifetime cap on coding-agent launches; 0 = unlimited (P1-16).

    WT can't see model-serving tokens (agents run as CLI subprocesses in PTYs),
    so the controllable spend proxy is how many LLM-agent sessions an attendee
    starts. Bash sessions are free and never counted.
    """
    return _env_int("MAX_AGENT_LAUNCHES_PER_USER", 0)


def omnigent_enabled() -> bool:
    """Whether the Omnigent meta-harness session type is offered (default ON).

    Omnigent is GA on public PyPI, so the default deploy installs it (latest
    `omnigent`) alongside tmux, the catalog leads with the Omnigent session, and
    per-user omnigent configs are written. Operators opt OUT with
    OMNIGENT_ENABLED=false (e.g. air-gapped events where the PyPI install can't
    reach out, or to fall back to bare claude/codex only).
    """
    return _env("OMNIGENT_ENABLED", "true").lower() not in ("false", "0", "no", "off")


def omnigent_runner_idle_timeout() -> int:
    """Seconds Omnigent's background runner stays alive with no active work
    before self-terminating (written as ``runner.idle_timeout_s``).

    Omnigent's own default is 3600s (1 hour) — but on a disposable
    one-workspace-per-attendee instance that is far too short: an attendee who
    steps away for lunch returns to a dead runner ("Runner disconnected
    unexpectedly" / "turn failed") even though their PTY and tmux session are
    still there. ``0`` disables the watchdog entirely; the default here (0)
    keeps the runner alive for the life of the container, matching the "surviving
    the PTY is a feature" policy. Operators can set a finite value if runner
    memory on long idle instances becomes a concern."""
    return _env_int("OMNIGENT_RUNNER_IDLE_TIMEOUT_S", 0)


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


def obo_enabled() -> bool:
    """Whether the attendee's forwarded on-behalf-of-user (OBO) token is
    persisted to a second databricks CLI profile (default ``[me]``) so the agent
    can run ``databricks --profile me ...`` as the *attendee* and see exactly the
    Unity Catalog objects the attendee is governed by.

    Off by default (``ENABLE_OBO``) for staged rollout — the SP ``[DEFAULT]``
    profile keeps powering all build/deploy plumbing regardless.
    """
    return _env_bool("ENABLE_OBO", False)


def obo_profile_name() -> str:
    """databricks CLI profile backed by the attendee's OBO token (default ``me``)."""
    return _env("OBO_PROFILE_NAME", "me") or "me"


def obo_scopes() -> str:
    """Documentation / health hint only.

    The app cannot set its own OBO scopes — they live on the *app resource*
    (Control Tower enables user authorization + ``user_api_scopes``). Baseline
    ``catalog.catalogs:read,catalog.schemas:read,catalog.tables:read,sql`` lets
    the ``me`` profile list the attendee's catalogs/schemas/tables (UC metadata)
    and query their UC-governed data via a warehouse. There is no
    ``unity-catalog`` scope — use the granular ``catalog.*:read`` scopes. Other
    valid scopes: ``genie``, ``postgres``, ``model-serving``, ``files``,
    ``ai-gateway``, ``vector-search``, ``workspace.workspace``,
    ``catalog.connections``, ``mcp.external``, ``mcp.functions``.
    """
    return _env(
        "OBO_SCOPES",
        "catalog.catalogs:read,catalog.schemas:read,catalog.tables:read,sql",
    )


def entitlements_enabled() -> bool:
    """Whether the SP-driven entitlement reconciler runs (``ENABLE_ENTITLEMENTS``).

    Default ON. The agent builds and deploys as the app service principal (the
    reliable identity for long/idle/cross-1h work), so without reconciliation
    the *attendee* has no access to anything the agent creates — they open the
    app they just deployed and get denied. This loop closes that gap by granting
    the labuser ALL_PRIVILEGES on ``WORKSHOP_CATALOG`` (inherited by every object
    the SP creates inside it) and ``CAN_MANAGE`` on non-UC resources
    (apps/jobs/pipelines/db-instances/serving). It is idempotent and a no-op when
    ``WORKSHOP_CATALOG`` is unset and no SP-created resources exist, so leaving it
    on is safe. Set ``ENABLE_ENTITLEMENTS=false`` to opt out."""
    return _env_bool("ENABLE_ENTITLEMENTS", True)


def workshop_catalog() -> str:
    """Per-attendee Unity Catalog the agent must create objects inside, so the
    labuser's inherited ``ALL PRIVILEGES`` grant makes them instantly usable.
    Empty disables the catalog grant/verify step."""
    return _env("WORKSHOP_CATALOG")


def workshop_schema() -> str:
    """Optional default schema within ``WORKSHOP_CATALOG`` for the agent to target."""
    return _env("WORKSHOP_SCHEMA")


def entitlement_reconcile_interval() -> int:
    """Seconds between SP-driven entitlement reconciliation sweeps."""
    return _env_int("ENTITLEMENT_RECONCILE_INTERVAL", 300)


def entitlement_transfer_ownership() -> bool:
    """Also transfer SP-created UC catalog ownership to the labuser (default off —
    grant-based ``ALL PRIVILEGES`` is enough for usability; ownership transfer is
    only needed if the labuser must drop/alter SP-created objects as owner)."""
    return _env_bool("ENTITLEMENT_TRANSFER_OWNERSHIP", False)


def branding() -> dict:
    return {
        "brand_name": _env("BRAND_NAME") or "Databricks",
        "brand_logo_url": _env("BRAND_LOGO_URL"),
        "brand_primary_color": _env("BRAND_PRIMARY_COLOR"),
        "event_name": _env("EVENT_NAME"),
        "cobranded": bool(_env("BRAND_NAME")),
    }
