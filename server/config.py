"""Runtime configuration.

Everything is read from the environment *at call time* — Control Tower edits
app.yaml env in place and injects per-deployment env_vars, so values must
never be cached at import or baked into the frontend build.
"""

from __future__ import annotations

import os
import re
from urllib.parse import urlsplit, urlunsplit


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


def control_tower_url() -> str:
    """Control Tower app base URL for app→app operator APIs (help raise/lower)."""
    return ensure_https(_env("CONTROL_TOWER_URL")).rstrip("/")


def control_tower_ingest_url() -> str:
    """Control Tower's event-ingest base URL (contract C3b). Empty disables emit."""
    return _env("CONTROL_TOWER_INGEST_URL")


def control_tower_ingest_token() -> str:
    """Shared X-Ingest-Token CT vends for the event-ingest endpoint."""
    return _env("CONTROL_TOWER_INGEST_TOKEN")


def workshop_run_id() -> str:
    """CT lab run id this terminal belongs to (injected at deploy)."""
    return _env("WORKSHOP_RUN_ID")


def workshop_unit_id() -> str:
    """CT lab unit id for this attendee instance (injected at deploy)."""
    return _env("WORKSHOP_UNIT_ID")


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
    return _env_int("MAX_SESSIONS_GLOBAL", max_sessions_per_user())


def allow_shared_topology() -> bool:
    """Opt-in acknowledgement that one instance may serve multiple attendees.

    Off by default (gap P1-11a): the supported model is one workspace per
    attendee. See server/topology.py.
    """
    return _env("ALLOW_SHARED_TOPOLOGY").strip().lower() in {
        "1", "true", "yes", "on", "enabled", "enable",
    }


def workshop_app_sp_id() -> str:
    """Numeric SCIM ID of this Databricks App's service principal."""
    return _env("WORKSHOP_APP_SP_ID")


def toolchain_mirror_raw() -> str:
    """``WORKSHOP_TOOLCHAIN_MIRROR_PATH`` exactly as Control Tower set it.

    Kept separate from the validated accessor so status reporting can tell
    "no mirror configured" apart from "a mirror was configured and rejected" —
    the two look identical once a bad path has been normalised to empty, and
    the second is the failure this feature exists to make visible.
    """
    return _env("WORKSHOP_TOOLCHAIN_MIRROR_PATH")


def toolchain_mirror_path() -> str:
    """UC Volume holding the pinned toolchain, or "" to download from source.

    Control Tower stages every manifest artifact into this volume keyed by its
    sha256 and patches the path in at deploy; empty keeps the internet path, so
    the mirror is entirely optional. Databricks Apps does not mount volumes into
    the container, so this is a Files API address rather than a readable
    directory — validated to the same absolute ``/Volumes/<catalog>/<schema>/
    <volume>`` shape ``probe_artifact_volume`` requires.
    """
    path = toolchain_mirror_raw().rstrip("/")
    if not path.startswith("/Volumes/"):
        return ""
    # ['', 'Volumes', catalog, schema, volume] — anything shorter cannot name a
    # volume, and a prefix like /Volumes/main would send every fetch to a 404.
    if len(path.split("/")) < 5 or not all(path.split("/")[2:5]):
        return ""
    return path


def toolchain_mirror_strict() -> bool:
    """Whether a mirror miss fails the install instead of reaching the internet.

    Off by default, so a half-staged volume costs a slow boot rather than a
    broken one. On for air-gapped events, where reaching the internet is itself
    the failure and falling back silently defeats the point.
    """
    return _env_bool("WORKSHOP_TOOLCHAIN_MIRROR_STRICT", False)


def session_idle_timeout() -> int:
    # 8 hours: long enough to survive a lunch break / closed laptop lid without
    # reaping the attendee's PTY out from under a returning session (the old
    # 1-hour default reaped a terminal an attendee stepped away from, and lined
    # up exactly with Omnigent's 1-hour runner idle timeout for a double hit).
    # Memory, not idle PTYs, is the real constraint on a per-attendee instance.
    return _env_int("SESSION_IDLE_TIMEOUT_SECONDS", 28800)


def session_state_path() -> str:
    """File path for the session-metadata journal (P1-11), or "" to disable.

    When set, the SessionManager journals metadata only so that after a server
    restart the attendee sees which terminals ended and can relaunch them.
    Raw terminal output remains in memory and is never persisted.
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


def normalize_omnigent_app_url(
    raw: str, *, allow_loopback_http: bool = False
) -> str:
    """Validate and canonicalize an Omnigent server URL."""
    raw = raw.strip()
    if not raw:
        return ""
    parsed = urlsplit(raw)
    if not parsed.scheme or not parsed.netloc:
        raise ValueError("OMNIGENT_APP_URL must be an absolute URL")
    if parsed.username or parsed.password:
        raise ValueError("OMNIGENT_APP_URL must not contain credentials")
    if parsed.query or parsed.fragment:
        raise ValueError("OMNIGENT_APP_URL must not contain query or fragment")
    scheme = parsed.scheme.lower()
    hostname = (parsed.hostname or "").lower()
    if not hostname:
        raise ValueError("OMNIGENT_APP_URL must include a host")
    loopback = hostname in {"localhost", "127.0.0.1", "::1"}
    if scheme != "https" and not (
        allow_loopback_http and scheme == "http" and loopback
    ):
        raise ValueError(
            "OMNIGENT_APP_URL must use https (loopback http is local-dev only)"
        )
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("OMNIGENT_APP_URL contains an invalid port") from exc
    host_for_netloc = f"[{hostname}]" if ":" in hostname else hostname
    default_port = (scheme == "https" and port == 443) or (
        scheme == "http" and port == 80
    )
    netloc = (
        host_for_netloc
        if not port or default_port
        else f"{host_for_netloc}:{port}"
    )
    path = parsed.path.rstrip("/")
    return urlunsplit((scheme, netloc, path, "", ""))


def omnigent_app_url() -> str:
    """Normalized dedicated Omnigent App URL; empty keeps local behavior.

    Production must use TLS. Local development may target a loopback HTTP
    server for integration tests, but never a non-loopback cleartext host.
    """
    return normalize_omnigent_app_url(
        _env("OMNIGENT_APP_URL"), allow_loopback_http=local_dev()
    )


def omnigent_remote_enabled() -> bool:
    return bool(omnigent_app_url())


_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def workshop_attendee_email() -> str:
    """Control Tower's attendee hint for this instance, empty when unset.

    An empty value is not an error: ``server/attendee.py`` resolves the
    effective binding, falling back to a persisted or self-bound identity.
    """
    raw = _env("WORKSHOP_ATTENDEE_EMAIL")
    if not raw:
        return ""
    normalized = raw.lower()
    if not _EMAIL_RE.fullmatch(normalized):
        raise ValueError("WORKSHOP_ATTENDEE_EMAIL must be a valid email address")
    return normalized


def valid_attendee_email(value: str) -> bool:
    """True when ``value`` is a well-formed attendee identity."""
    return bool(_EMAIL_RE.fullmatch(value.strip().lower()))


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


def omnigent_host_stable_runtime() -> int:
    """Runtime that resets remote-host crash backoff after a healthy stretch."""
    return max(1, _env_int("OMNIGENT_HOST_STABLE_RUNTIME_S", 30))


def omnigent_stale_grace() -> int:
    """Seconds a running Omnigent host tolerates a stale OBO mirror.

    Past this it is stood down into ``waiting_for_token`` — visibly gated and
    recoverable — rather than left running and failing every session with an
    auth error the attendee cannot act on."""
    return max(5, _env_int("OMNIGENT_STALE_GRACE_S", 60))


def obo_renew_lead() -> int:
    """How far ahead of OBO expiry to start nudging the tab for a fresh token.

    The app holds no refresh token, so renewal is *pulled* from a live browser
    tab. Ten minutes of lead means an ordinary poll refreshes it long before an
    attendee could notice, and a tab that has stopped polling is identified as a
    problem while there is still time to say so."""
    return max(60, _env_int("OBO_RENEW_LEAD_S", 600))


def obo_watch_interval() -> float:
    """Seconds between OBO freshness samples."""
    return max(5.0, float(_env_int("OBO_WATCH_INTERVAL_S", 30)))


def omnigent_host_log_level() -> str:
    """``OMNIGENT_LOG_LEVEL`` for the per-attendee host process.

    DEBUG by default. The host is one supervised process per attendee, not a hot
    path, and the records that explain a failed start (``exc_info`` on the
    connect and spec-resolution paths) are below INFO. The volume it costs is
    bounded by the rotating capture in ``server/diagnostics.py``; the volume it
    saves is an operator reconstructing an incident from a screenshot."""
    return _env("OMNIGENT_HOST_LOG_LEVEL", "DEBUG").strip().upper() or "DEBUG"


def omnigent_host_log_max_bytes() -> int:
    """Ceiling for one attendee's captured host stdout/stderr log."""
    return max(64 * 1024, _env_int("OMNIGENT_HOST_LOG_MAX_BYTES", 2 * 1024 * 1024))


def log_collector_enabled() -> bool:
    """Whether the background sweep of Omnigent process logs runs."""
    return _env("WORKSHOP_LOG_COLLECTOR", "true").lower() not in (
        "false",
        "0",
        "no",
        "off",
    )


def log_collector_interval() -> float:
    """Seconds between collector sweeps.

    Five seconds because the operator use case is "an attendee just told me it
    broke" — the answer has to be there by the time an operator opens the panel,
    and the read is a few kilobytes of tail per attendee."""
    return max(1.0, float(_env_int("WORKSHOP_LOG_COLLECTOR_INTERVAL_S", 5)))


def log_journal_capacity() -> int:
    """Distinct classified errors retained across restarts."""
    return max(20, _env_int("WORKSHOP_LOG_JOURNAL_CAPACITY", 500))


def workshop_phase_default() -> str:
    return _env("WORKSHOP_PHASE", "intro")


def content_pack_path() -> str:
    return _env("CONTENT_PACK_PATH")


def lab_coach_enabled() -> bool:
    return _env("LAB_COACH", "true").lower() not in ("false", "0", "no", "off")


def auto_mode_enabled() -> bool:
    """Lab 'auto mode': agents run without permission prompts. Attendees work
    in isolated per-user HOMEs inside a disposable workshop container, so the
    zero-prompt flow is the right default for a time-boxed lab."""
    return _env("WORKSHOP_AUTO_MODE", "true").lower() not in ("false", "0", "no", "off")


def topic_detection_enabled() -> bool:
    """Coarse keyword spotting on terminal output to drive contextual
    insights. Only topic flags are recorded — terminal content is never
    stored or transmitted."""
    return _env("TOPIC_DETECTION", "true").lower() not in ("false", "0", "no", "off")


def insight_capture_enabled() -> bool:
    """Master switch for workshop insight capture (``WORKSHOP_INSIGHT_CAPTURE``).

    Off by default, and deliberately so: this is the one feature that sends
    attendee-authored content off the instance (discovery answers, artifact
    titles), reversing the "content never leaves" property the rest of the app
    holds. Consent is handled out-of-band in the event's registration terms, and
    an operator who hasn't arranged that consent must get capture-off by doing
    nothing. See ``docs/workshop-insight-contract.md``.

    With this off, the signal rollup, the discovery endpoint, the CLI helper, the
    agent instructions and the wrap-phase harvest are all inert — the attendee's
    terminal behaves exactly as it did before the feature existed.
    """
    return _env_bool("WORKSHOP_INSIGHT_CAPTURE", False)


def discovery_enabled() -> bool:
    """Whether the *agent-elicited* discovery tier runs, within capture.

    Subordinate to ``WORKSHOP_INSIGHT_CAPTURE`` on purpose. The two tiers carry
    very different consent weight: the behavioural rollup is derived counters the
    app already keeps, while a discovery record is the attendee describing their
    company's plans in their own words. An operator who wants the anonymous
    signal without the conversational capture sets ``DISCOVERY_ENABLED=false``
    and keeps the rest.
    """
    return insight_capture_enabled() and _env_bool("DISCOVERY_ENABLED", True)


def insight_summary_model() -> str:
    """Serving endpoint for the edge summary (``INSIGHT_SUMMARY_MODEL``).

    Empty means "discover a ready endpoint", which is the right default because
    model availability is regional and an operator should not have to know which
    Claude generation their workspace serves. Pinning matters when an event runs
    on a workspace whose default chain is unavailable or expensive.
    """
    return _env("INSIGHT_SUMMARY_MODEL").strip()


def insight_summary_min_interval_seconds() -> int:
    """Floor between edge summaries for one attendee
    (``INSIGHT_SUMMARY_MIN_INTERVAL_MINUTES``).

    The summary regenerates off the harvest Control Tower already makes rather
    than waiting for a phase an operator may never flip. That harvest runs every
    ~10 minutes, so without a floor a long session would spend a model call per
    poll per attendee. Twenty minutes keeps a brief current to within one
    content phase while costing at most every second harvest.

    Zero disables the floor, which is for tests rather than events.
    """
    return max(0, _env_int("INSIGHT_SUMMARY_MIN_INTERVAL_MINUTES", 20)) * 60


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
