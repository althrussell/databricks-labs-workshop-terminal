"""Deterministic deep-readiness checks for event admission.

The checks consume explicit status snapshots and environment mappings so unit
tests never need a live Databricks workspace. ``evaluate_runtime`` is the thin
adapter used by the HTTP endpoint.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
import os
import re
import tempfile
import time

from . import models
from .obo import FRESH_MARGIN as OBO_FRESH_MARGIN


EXPECTED_OBO_SCOPES = frozenset(
    {
        "catalog.catalogs:read",
        "catalog.schemas:read",
        "catalog.tables:read",
        "sql",
    }
)
_TRUE = frozenset({"1", "true", "yes", "on", "enabled", "enable"})
_BRANCH_TIPS = frozenset({"", "main", "master", "head", "latest"})
CREDENTIAL_SUCCESS_MAX_AGE = 600
ENTITLEMENT_PROOF_MAX_AGE = 600
OBO_VALIDATION_MAX_AGE = 300


def _bool(env: Mapping[str, str], name: str, default: bool = False) -> bool:
    raw = env.get(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in _TRUE


def _int(env: Mapping[str, str], name: str, default: int) -> int:
    try:
        return int(env.get(name, "") or default)
    except ValueError:
        return default


def _omnigent_offered(env: Mapping[str, str]) -> bool:
    """Whether this deployment installs Omnigent, and so whether to require it.

    Mirrors ``config.omnigent_offered`` on a plain mapping, because readiness has
    to judge the install the bootstrap actually ran. Reading ``OMNIGENT_ENABLED``
    alone made a workshop created without Omnigent permanently unready: nothing
    installed tmux or the harness, readiness still demanded both, and the room
    was held out of admission for a harness it was never going to launch.
    """
    if not _bool(env, "OMNIGENT_ENABLED", True):
        return False
    selected = [
        part.strip().lower()
        for part in env.get("WORKSHOP_AGENTS", "").split(",")
        if part.strip()
    ]
    return not selected or "omnigent" in selected


def event_ends_in(env: Mapping[str, str], now: float | None = None) -> int | None:
    """Seconds left in the event, from ``WORKSHOP_EVENT_ENDS_AT`` (epoch seconds).

    Unset on most deployments, and that is fine — it exists so an operator can
    ask the question that actually matters before a room fills up: will these
    credentials outlive the workshop?
    """
    raw = (env.get("WORKSHOP_EVENT_ENDS_AT") or "").strip()
    if not raw:
        return None
    try:
        ends_at = float(raw)
    except ValueError:
        return None
    return max(0, int(ends_at - (now if now is not None else time.time())))


def sustainability(
    credential_status: Mapping[str, object],
    env: Mapping[str, str],
    remaining: int | None,
    *,
    obo_renewing: bool,
) -> dict:
    """Can each plane be *kept* alive for the rest of the event?

    Deliberately not "does this token outlive the event". No attendee OBO ever
    does — they are minted for about an hour — so a gate built on raw expiry
    would be red on every instance from the first minute and turned off by the
    end of the first event. What must outlast the event is the machinery: the
    app credential rotating server-side, and the freshness watcher pulling a new
    OBO from the tab before the old one dies.

    That leaves two failures this can actually catch, both of which end a
    workshop mid-session and neither of which is visible until it does: a static
    token configured with an expiry inside the event window, and a deployment
    wired to remote Omnigent with nothing renewing the attendee credential.
    """
    rotating = str(credential_status.get("state") or "") == "rotating"
    app_expires_in = credential_status.get("token_expires_in")
    app_durable = rotating or _outlives(app_expires_in, remaining)
    remote = bool((env.get("OMNIGENT_APP_URL") or "").strip())
    return {
        "app_plane_durable": app_durable,
        "app_plane_rotating": rotating,
        "attendee_plane_renewing": (not remote) or obo_renewing,
        "sustainable": app_durable and ((not remote) or obo_renewing),
    }


def _outlives(expires_in: object, remaining: int | None) -> bool:
    if remaining is None or expires_in is None:
        return True  # unanswerable, and an unanswerable question is not a failure
    try:
        return int(expires_in) >= remaining  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return True


def durability(
    credential_status: Mapping[str, object],
    obo_status: Mapping[str, object],
    env: Mapping[str, str],
    now: float | None = None,
    *,
    obo_renewing: bool | None = None,
) -> dict:
    """How long each credential plane has left, and what that costs right now.

    Two planes fail differently and must be read separately. The app service
    principal's token rotates server-side and takes the whole instance with it;
    the attendee's OBO is pulled from a live browser tab and takes only the
    Omnigent harnesses. Reporting one number for "credentials" hid exactly that
    distinction during the incident this exists to prevent.
    """
    app_expires_in = credential_status.get("token_expires_in")
    obo_expires_in = obo_status.get("expires_in")
    obo_present = obo_status.get("present") is True
    remote = bool((env.get("OMNIGENT_APP_URL") or "").strip())
    obo_live = obo_present and (
        obo_expires_in is None or int(obo_expires_in) > OBO_FRESH_MARGIN
    )
    remaining = event_ends_in(env, now)
    if obo_renewing is None:
        from .obo import obo_watcher

        obo_renewing = obo_watcher.running
    return {
        **sustainability(credential_status, env, remaining, obo_renewing=obo_renewing),
        "app_credential_expires_in": app_expires_in,
        "attendee_obo_expires_in": obo_expires_in,
        "attendee_obo_present": obo_present,
        "event_ends_in": remaining,
        # The single question an operator asks mid-event: can this attendee
        # start an Omnigent session right now, or only the bare CLIs?
        "omnigent_launchable": (not remote) or obo_live,
        "outlasts_event": (
            None
            if remaining is None
            else all(
                value is None or int(value) >= remaining
                for value in (app_expires_in, obo_expires_in if remote else None)
            )
        ),
    }


def _check(ok: bool, detail: str, **extra: object) -> dict:
    return {"ok": ok, "state": "green" if ok else "red", "detail": detail, **extra}


def _soft(ok: bool, state: str, detail: str, **extra: object) -> dict:
    """A reported-but-not-gating check.

    Soft checks are excluded from the ``ready`` verdict, so an operator whose
    optional feature is misconfigured still gets a serving workshop. They exist
    to make the configuration provable after the event rather than to block it.
    """
    return {"ok": ok, "state": state, "detail": detail, "soft": True, **extra}


def _insight_capture(
    env: Mapping[str, str], delivery: Mapping[str, object] | None = None
) -> dict:
    """What this instance collects, how it leaves, and whether any was lost.

    Delivery is by collection: Control Tower reads the buffer on the harvest it
    already makes, so unlike every other integration here there is nothing to
    configure and nothing that can be configured wrong. Push (a token POST to CT's
    ingest endpoint) stays reported because a deployment may still set it, but the
    Apps proxy in front of Control Tower means it does not currently reach.

    What *can* go wrong is loss: the buffer is bounded, so a collector that stops
    collecting eventually costs events. ``dropped`` is the only honest evidence of
    that, and it is why this check can still be red.
    """
    capture = _bool(env, "WORKSHOP_INSIGHT_CAPTURE", False)
    discovery = capture and _bool(env, "DISCOVERY_ENABLED", True)
    push_configured = all(
        env.get(name, "").strip()
        for name in (
            "CONTROL_TOWER_INGEST_URL",
            "CONTROL_TOWER_INGEST_TOKEN",
            "WORKSHOP_RUN_ID",
        )
    )
    status = dict(delivery or {})
    mode = str(status.get("delivery") or ("push" if push_configured else "pull"))
    collections = int(status.get("collections") or 0)
    dropped = int(status.get("dropped") or 0)
    pending = int(status.get("pending") or 0)
    if not capture:
        requested = "off"
    elif discovery:
        requested = "signal+discovery"
    else:
        requested = "signal"
    lossless = not capture or dropped == 0
    return {
        "enabled": capture,
        "discovery": discovery,
        "delivery": mode,
        "push_configured": push_configured,
        "collections": collections,
        "collected": collections > 0,
        "pending": pending,
        "dropped": dropped,
        "requested": requested,
        "effective": requested if lossless else "lossy",
    }


def _path_writable(path: str) -> bool:
    """Exercise journal-style atomic write/read on a disposable sibling."""
    if not path:
        return False
    if os.path.isdir(path):
        return False
    parent = os.path.dirname(os.path.abspath(path))
    if not os.path.isdir(parent):
        return False
    probe_path = ""
    try:
        fd, probe_path = tempfile.mkstemp(dir=parent, prefix=".readyz-journal-")
        os.close(fd)
        os.unlink(probe_path)
        from .session_store import SessionMetadataStore

        store = SessionMetadataStore(probe_path)
        sentinel = {"id": "readyz-probe", "exited": True}
        store.upsert(sentinel)
        return store.load().get("readyz-probe") == sentinel
    except OSError:
        return False
    finally:
        if probe_path and os.path.exists(probe_path):
            os.unlink(probe_path)


def _app_service_principal_grants(entitlement_status: Mapping[str, object]) -> dict:
    """Summarise the app-service-principal catalog grants from the handoff ledger.

    An app the attendee built runs as a service principal that has to be granted
    on the catalog separately, and when that grant fails the app reads nothing
    while every other check stays green. ``last_error`` alone truncates to five
    messages across all resource types, so name the apps here: at a hundred
    instances the useful question is which app is blocked, not that something is.
    """
    handoff = entitlement_status.get("handoff")
    details = handoff.get("details") if isinstance(handoff, Mapping) else None
    granted: list[str] = []
    failed: list[dict] = []
    for entry in details or []:
        if not isinstance(entry, Mapping):
            continue
        if entry.get("resource_type") != "app-service-principals":
            continue
        name = str(entry.get("resource_id") or "")
        if entry.get("state") == "handed_off":
            granted.append(name)
        elif entry.get("state") == "failed":
            failed.append({"app": name, "error": entry.get("error")})
    return {
        "granted": sorted(granted),
        "failed": sorted(failed, key=lambda f: f["app"]),
        "ok": not failed,
    }


def evaluate(
    *,
    env: Mapping[str, str],
    credential_status: Mapping[str, object],
    installer_status: Mapping[str, object],
    entitlement_status: Mapping[str, object],
    obo_status: Mapping[str, object],
    secret_protection_status: Mapping[str, object],
    attendee_binding: Mapping[str, str] | None = None,
    delivery_status: Mapping[str, object] | None = None,
    gateway_status: Mapping[str, object] | None = None,
    workspace_sync: Mapping[str, object] | None = None,
    otel_status: Mapping[str, object] | None = None,
    writable_probe: Callable[[str], bool] = _path_writable,
    obo_renewing: bool | None = None,
    now: float | None = None,
) -> dict:
    """Return the complete, secret-free readiness report."""
    current_time = time.time() if now is None else now
    if obo_renewing is None:
        from .obo import obo_watcher

        obo_renewing = obo_watcher.running
    event_remaining = event_ends_in(env, current_time)
    sustainable = sustainability(
        credential_status, env, event_remaining, obo_renewing=obo_renewing
    )
    gateway_status = gateway_status or {}
    otel_status = otel_status or {
        "enabled": False,
        "configured": False,
        "state": "amber",
        "protocol": None,
        "collector_endpoint_present": False,
        "service_name_present": False,
        "required_resource_attributes": [],
        "missing_resource_attributes": [],
    }
    sync = {
        "state": "never",
        "at": None,
        "exit": None,
        "detail": "",
        **(workspace_sync or {}),
    }
    per_user = _int(env, "MAX_SESSIONS_PER_USER", 1)
    global_cap = _int(env, "MAX_SESSIONS_GLOBAL", 1)
    topology_ok = (
        not _bool(env, "ALLOW_SHARED_TOPOLOGY") and per_user == 1 and global_cap == 1
    )
    # The effective binding, which may be self-bound rather than injected by
    # Control Tower; the raw env var is only the hint.
    binding = attendee_binding or {}
    attendee_email = (
        str(binding.get("email") or env.get("WORKSHOP_ATTENDEE_EMAIL", ""))
        .strip()
        .lower()
    )
    attendee_identity_ok = (
        bool(attendee_email)
        and "@" in attendee_email
        and not any(char.isspace() for char in attendee_email)
    )
    attendee_binding_source = str(
        binding.get("source") or ("control-tower" if attendee_email else "unbound")
    )

    has_pat = bool(env.get("WORKSHOP_PAT", "").strip())
    last_credential_success = credential_status.get("last_successful_at")
    credential_recent = isinstance(last_credential_success, (int, float)) and (
        0 <= current_time - float(last_credential_success) <= CREDENTIAL_SUCCESS_MAX_AGE
    )
    credential_ok = (
        not has_pat
        and credential_status.get("state") == "rotating"
        and credential_status.get("rotating") is True
        and credential_status.get("healthy") is True
        and credential_status.get("source") == "app_identity_oauth"
        and credential_recent
    )
    expected_app_sp_id = env.get("WORKSHOP_APP_SP_ID", "").strip()
    expected_app_client_id = env.get("DATABRICKS_CLIENT_ID", "").strip()
    validation_diagnostic = credential_status.get("validation_diagnostic")
    validation_diagnostic = (
        validation_diagnostic if isinstance(validation_diagnostic, Mapping) else {}
    )
    observed_app_sp_id = str(
        validation_diagnostic.get("observed_service_principal_id") or ""
    )
    observed_app_client_id = str(
        validation_diagnostic.get("observed_application_id") or ""
    )
    app_sp_binding_ok = (
        bool(expected_app_client_id)
        and bool(re.fullmatch(r"[0-9]+", expected_app_sp_id))
        and validation_diagnostic.get("result") == "matched"
        and validation_diagnostic.get("expected_application_id")
        == expected_app_client_id
        and observed_app_client_id == expected_app_client_id
        and validation_diagnostic.get("expected_service_principal_id")
        == expected_app_sp_id
        and observed_app_sp_id == expected_app_sp_id
    )
    secret_protection_ok = (
        secret_protection_status.get("initialized") is True
        and secret_protection_status.get("env_scrubbed") is True
        and secret_protection_status.get("non_dumpable") is True
        and secret_protection_status.get("ok") is True
    )

    steps = installer_status.get("steps")
    steps = steps if isinstance(steps, Mapping) else {}
    required_steps = ["node", "claude", "codex", "databricks", "skills"]
    if _omnigent_offered(env):
        required_steps.extend(["tmux", "omnigent"])
    missing_installers = [
        name
        for name in required_steps
        if not isinstance(steps.get(name), Mapping)
        or steps[name].get("status") != "complete"
    ]
    # A degraded step produced something usable without meeting its reviewed
    # contract (skills served from the vendored fallback). It is not ready, and
    # naming it separately keeps an operator from reading it as "still going".
    degraded_installers = [
        name
        for name in required_steps
        if isinstance(steps.get(name), Mapping)
        and steps[name].get("status") == "degraded"
    ]
    agents_ready = installer_status.get("ready")
    agents_ready = agents_ready if isinstance(agents_ready, Mapping) else {}
    installed_harnesses = [
        name for name in ("claude", "codex") if agents_ready.get(name) is True
    ]
    artifact_manifest = installer_status.get("artifact_manifest")
    artifact_manifest = (
        artifact_manifest if isinstance(artifact_manifest, Mapping) else {}
    )
    artifact_proof = installer_status.get("artifact_proof")
    artifact_proof = artifact_proof if isinstance(artifact_proof, Mapping) else {}
    supply_chain_ok = (
        artifact_manifest.get("ok") is True and artifact_proof.get("reusable") is True
    )
    # Reported, never gated on: falling back to the internet is checksum-verified
    # and therefore safe, just slow. It fails an event, not a readiness probe.
    mirror = installer_status.get("toolchain_mirror")
    mirror = mirror if isinstance(mirror, Mapping) else {}

    state_path = env.get("SESSION_STATE_PATH", "").strip()
    state_writable = bool(state_path and writable_probe(state_path))
    catalog = env.get("WORKSHOP_CATALOG", "").strip()

    entitlement_enabled = _bool(env, "ENABLE_ENTITLEMENTS")
    last_verified_at = entitlement_status.get("last_verified_at")
    # A verified no-change pass deliberately enters a longer idle cadence.
    # Keep that last-known-good proof valid through one idle window plus a
    # capped rate-limit retry; otherwise /readyz would turn red merely because
    # the reconciler successfully became quiet.
    proof_max_age = max(
        ENTITLEMENT_PROOF_MAX_AGE,
        int(entitlement_status.get("idle_interval") or 0)
        + max(300, int(entitlement_status.get("backoff_seconds") or 0)),
    )
    entitlement_recent = isinstance(last_verified_at, (int, float)) and (
        0 <= current_time - float(last_verified_at) <= proof_max_age
    )
    attendee_verified = bool(entitlement_status.get("verified_email"))
    catalog_verified = entitlement_status.get("verified_catalog") == catalog
    reconciler_available = (
        entitlement_status.get("thread_alive") is True
        or entitlement_status.get("verification_source") == "on_demand"
    )
    entitlement_ok = (
        entitlement_enabled
        and entitlement_status.get("enabled") is True
        and entitlement_status.get("ok") is True
        and bool(catalog)
        and attendee_verified
        and catalog_verified
        and entitlement_recent
        and reconciler_available
    )
    catalog_ok = (
        bool(catalog) and attendee_verified and catalog_verified and entitlement_recent
    )
    app_sp_grants = _app_service_principal_grants(entitlement_status)

    configured_scopes = {
        scope.strip() for scope in env.get("OBO_SCOPES", "").split(",") if scope.strip()
    }
    observed_scopes = {
        str(scope)
        for scope in obo_status.get("verified_scopes", []) or []
        if str(scope)
    }
    missing_configured_scopes = EXPECTED_OBO_SCOPES - configured_scopes
    missing_observed_scopes = EXPECTED_OBO_SCOPES - observed_scopes
    missing_scopes = sorted(missing_configured_scopes | missing_observed_scopes)
    obo_validation_state = str(obo_status.get("validation_state") or "pending")
    obo_validated_at = obo_status.get("validated_at")
    obo_recent = isinstance(obo_validated_at, (int, float)) and (
        0 <= current_time - float(obo_validated_at) <= OBO_VALIDATION_MAX_AGE
    )
    obo_ok = (
        _bool(env, "ENABLE_OBO")
        and obo_status.get("enabled") is True
        and obo_status.get("present") is True
        and obo_status.get("fresh") is True
        and obo_validation_state == "verified"
        and not missing_scopes
        and obo_recent
    )

    # An unset profile is the expected state and means `balanced`; a set-but-
    # unknown one is an operator asking for something this release cannot give.
    requested_profile = env.get("WORKSHOP_MODEL_PROFILE", "").strip().lower()
    profile_known = not requested_profile or requested_profile in models.PROFILES
    active_profile = (
        requested_profile
        if profile_known and requested_profile
        else models.DEFAULT_PROFILE
    )

    # What each role intends to run, as the head of its chain — which is what it
    # resolves to when the workspace serves everything, and what a pin displaces
    # when one is set. Computed without discovery on purpose: /readyz is not the
    # place to add a network round-trip, and "what this deployment is asking
    # for" is the question an operator reading it has. What the workspace
    # actually served is visible per attendee in the generated configs.
    intended_models = {
        role: models.resolve(role, ())
        for role in ("driver", "frontier", "standard", "fast", "codex", "insight")
    }

    # What must be pinned is the code an attendee runs, so every binary boot
    # installs belongs here. Model names deliberately do not: a model service is
    # a service that changes under a deployment whether or not its name is
    # written down, and requiring one contradicts the profile —
    # WORKSHOP_MODEL_PROFILE exists so an event names a cost posture and lets
    # role chains pick the model. A required ANTHROPIC_MODEL would put a copy of
    # a chain head in every event's deployment, to go stale there, which is the
    # drift server/models.py was written to end. Pins are still reported below.
    pin_names = [
        "CLAUDE_CODE_VERSION",
        "CODEX_CLI_VERSION",
        "DATABRICKS_CLI_VERSION",
        "NODE_VERSION",
    ]
    omnigent_enabled = _bool(env, "OMNIGENT_ENABLED", True)
    if omnigent_enabled:
        pin_names.append("OMNIGENT_VERSION")
    missing_pins = [name for name in pin_names if not env.get(name, "").strip()]
    # Unset is the expected state: the reviewed tag is repo-owned. Only an
    # explicitly configured branch tip is a missing pin.
    skills_ref = env.get("SKILLS_REF", "").strip()
    if skills_ref and (
        skills_ref.lower() in _BRANCH_TIPS or skills_ref.startswith("refs/heads/")
    ):
        missing_pins.append("SKILLS_REF")
    raw_manifest = installer_status.get("release_manifest")
    raw_manifest = raw_manifest if isinstance(raw_manifest, Mapping) else {}
    release_manifest: dict[str, dict[str, object]] = {}
    mismatched_tools: list[str] = []
    # Every required pin appears here too. A pin proves nothing on its own: an
    # operator can raise NODE_VERSION without a reinstall, and the terminal would
    # keep running the version already on disk while readiness reported the new
    # number. Pairing each pin with what bootstrap actually installed is what
    # makes the pin a claim about the running system.
    env_names = {
        "claude": "CLAUDE_CODE_VERSION",
        "codex": "CODEX_CLI_VERSION",
        "databricks": "DATABRICKS_CLI_VERSION",
        "node": "NODE_VERSION",
        "omnigent": "OMNIGENT_VERSION",
        "databricks_agent_skills": "SKILLS_REF",
    }
    omnigent_tools = {"omnigent"}
    for tool, env_name in env_names.items():
        entry = raw_manifest.get(tool)
        entry = entry if isinstance(entry, Mapping) else {}
        enabled = bool(
            entry.get(
                "enabled",
                tool not in omnigent_tools or _bool(env, "OMNIGENT_ENABLED", True),
            )
        )
        expected = str(entry.get("expected") or "")
        actual = str(entry.get("actual") or "")
        configured_expected = env.get(env_name, "").strip()
        if tool == "databricks_agent_skills" and not configured_expected:
            # The skills ref is repo-owned in assets/artifacts/manifest.json, so
            # an unset SKILLS_REF is the normal case, not a missing pin. Setting
            # it still has to agree with what bootstrap installed.
            configured_expected = expected
        match = (
            enabled
            and bool(expected)
            and expected == configured_expected
            and actual == expected
            and entry.get("match") is True
        )
        if tool == "databricks_agent_skills":
            match = (
                match
                and entry.get("source") in {"network", "prewarmed"}
                and bool(entry.get("resolved_commit"))
                and bool(entry.get("checksum"))
            )
        release_manifest[tool] = {
            "enabled": enabled,
            "expected": expected or None,
            "actual": actual or None,
            "match": match if enabled else None,
        }
        if tool == "databricks_agent_skills":
            release_manifest[tool]["source"] = entry.get("source")
            release_manifest[tool]["resolved_commit"] = entry.get("resolved_commit")
            release_manifest[tool]["checksum"] = entry.get("checksum")
        if enabled and not match:
            mismatched_tools.append(tool)

    # Capture state rides in the release manifest because that is the block
    # Control Tower records per run: "was insight capture on for this workshop"
    # is asked months later, by someone reading a brief rather than a log.
    insight = _insight_capture(env, delivery_status)
    insight_delivering = insight["requested"] == insight["effective"]
    release_manifest["insight_capture"] = {
        "enabled": insight["enabled"],
        "expected": insight["requested"],
        "actual": insight["effective"],
        "match": insight_delivering,
        "delivery": insight["delivery"],
    }

    checks = {
        "topology": _check(
            topology_ok,
            "single-session limits enforced"
            if topology_ok
            else "MAX_SESSIONS_PER_USER and MAX_SESSIONS_GLOBAL must both equal 1",
            max_sessions_per_user=per_user,
            max_sessions_global=global_cap,
        ),
        "attendee_identity": _check(
            attendee_identity_ok,
            "instance is bound to one attendee identity"
            if attendee_identity_ok
            else "no valid attendee identity is bound to this instance",
            configured=bool(attendee_email),
            source=attendee_binding_source,
        ),
        "credentials": _check(
            credential_ok,
            "direct app-identity OAuth is recently validated and auto-refreshing"
            if credential_ok
            else "direct app-identity OAuth is stale/unhealthy or a static fallback is configured",
            credential_state=str(credential_status.get("state") or "unknown"),
            source=str(credential_status.get("source") or "unknown"),
            last_successful_at=last_credential_success,
            max_age_seconds=CREDENTIAL_SUCCESS_MAX_AGE,
        ),
        # Hard, and therefore an admission gate: Control Tower blocks on a 503
        # from here. An instance that cannot keep its credentials alive for the
        # rest of the event is one that will strand an attendee mid-session,
        # which is worse than never admitting them to it.
        "credential_durability": _check(
            sustainable["sustainable"],
            (
                "both credential planes can be kept alive for the rest of the event"
                if sustainable["sustainable"]
                else "the app credential is static and expires before the event ends"
                if not sustainable["app_plane_durable"]
                else "remote Omnigent is configured but nothing is renewing the "
                "attendee credential — every Omnigent harness will fail together "
                "when the first token expires"
            ),
            event_ends_in=event_remaining,
            app_plane_durable=sustainable["app_plane_durable"],
            app_plane_rotating=sustainable["app_plane_rotating"],
            attendee_plane_renewing=sustainable["attendee_plane_renewing"],
        ),
        "app_sp_binding": _check(
            app_sp_binding_ok,
            "app client UUID and numeric service-principal identity are authoritatively verified"
            if app_sp_binding_ok
            else "app client UUID or WORKSHOP_APP_SP_ID is absent, invalid, or not authoritatively verified",
            expected_application_id=expected_app_client_id or None,
            observed_application_id=observed_app_client_id or None,
            expected_service_principal_id=expected_app_sp_id or None,
            observed_service_principal_id=observed_app_sp_id or None,
            validation_result=str(validation_diagnostic.get("result") or "unverified"),
        ),
        "secret_protection": _check(
            secret_protection_ok,
            "OAuth client secret was captured before PTYs, scrubbed from env, and the server is non-dumpable"
            if secret_protection_ok
            else "OAuth client-secret process boundary is not hardened",
            initialized=secret_protection_status.get("initialized") is True,
            env_scrubbed=secret_protection_status.get("env_scrubbed") is True,
            non_dumpable=secret_protection_status.get("non_dumpable") is True,
        ),
        "installers": _check(
            not missing_installers,
            "all enabled agents and support tools are ready"
            if not missing_installers
            else (
                "serving a degraded fallback instead of the reviewed install"
                if degraded_installers
                else "one or more enabled installers are incomplete"
            ),
            missing=missing_installers,
            degraded=degraded_installers,
            # Installed agent CLIs for operators / older CT revisions. Workshop
            # Polly economy/balanced/frontier tiers (which needed
            # WORKSHOP_HARNESSES on the Omnigent App) are removed; stock polly
            # plus optional Auto · smart routing remain.
            harnesses=installed_harnesses,
        ),
        "supply_chain": _check(
            supply_chain_ok,
            "reviewed bootstrap artifact manifest is complete"
            if supply_chain_ok
            else "reviewed bootstrap artifact manifest is missing or invalid",
            artifact_count=artifact_manifest.get("artifact_count"),
            error=artifact_manifest.get("error"),
            # "default" is the repo-owned contract; "override" means an operator
            # redirected sources at a mirror.
            source=artifact_manifest.get("source"),
            persistent_proof_reusable=artifact_proof.get("reusable") is True,
            toolchain_mirror_configured=mirror.get("configured") is True,
            toolchain_mirror_path=mirror.get("path") or None,
            # True when a mirror was configured and artifacts still came over
            # the internet -- the silent degradation, surfaced.
            toolchain_mirror_bypassed=mirror.get("bypassed") is True,
            toolchain_mirror_served=mirror.get("served"),
            toolchain_mirror_error=mirror.get("error"),
        ),
        "session_state": _check(
            state_writable,
            "session journal destination is writable"
            if state_writable
            else "SESSION_STATE_PATH is unset or not writable",
            configured=bool(state_path),
        ),
        "catalog": _check(
            catalog_ok,
            "attendee catalog access was recently verified"
            if catalog_ok
            else "catalog is unset or lacks recent attendee grant/owner proof",
            configured=bool(catalog),
            verified=catalog_verified and attendee_verified and entitlement_recent,
        ),
        "entitlements": _check(
            entitlement_ok,
            "entitlement reconciliation is enabled and healthy"
            if entitlement_ok
            else (
                "apps the attendee built cannot read the catalog: "
                + "; ".join(
                    f"{f['app']} ({f['error']})" for f in app_sp_grants["failed"][:3]
                )
                if not app_sp_grants["ok"]
                else "entitlement reconciliation is disabled, unhealthy, or missing its catalog"
            ),
            enabled=entitlement_enabled,
            healthy=entitlement_status.get("ok") is True,
            state=entitlement_status.get("state"),
            deferred_reason=entitlement_status.get("deferred_reason"),
            next_attempt_at=entitlement_status.get("next_attempt_at"),
            thread_alive=entitlement_status.get("thread_alive") is True,
            last_verified_at=last_verified_at,
            verification_source=entitlement_status.get("verification_source"),
            # The reconciler already knows why it failed. Not reporting it left
            # an operator with "unhealthy" and no way to tell a missing grant
            # from an unreachable catalog without shelling into the box.
            catalog=entitlement_status.get("catalog"),
            last_reconcile=entitlement_status.get("last_reconcile"),
            last_error=entitlement_status.get("last_error"),
            app_service_principals=app_sp_grants,
        ),
        "obo": _check(
            obo_ok,
            "OBO is enabled with required scopes"
            if obo_ok
            else "OBO is disabled"
            if not _bool(env, "ENABLE_OBO")
            else "no attendee has opened this instance yet, so no OBO token has "
            "been forwarded to validate scopes against"
            if obo_status.get("present") is not True
            else f"required scopes are missing: {', '.join(missing_scopes)}"
            if missing_scopes
            else "the last OBO validation is stale; scopes verified at "
            f"{obo_validated_at}, older than {OBO_VALIDATION_MAX_AGE}s"
            if not obo_recent
            else f"OBO token validation is {obo_validation_state}, not verified"
            if obo_validation_state != "verified"
            # Reached only when present is True but stale or unfresh in a way
            # the branches above do not name.
            else "the forwarded OBO token is not usable",
            # The one hard check nothing on this instance can turn green on its
            # own: scope verification needs a real attendee token, and a token
            # arrives only when a browser forwards one. So a perfectly
            # provisioned instance is red here until its attendee shows up,
            # which is exactly when Control Tower is deciding whether to hand
            # it to them. Flagged rather than softened: once an attendee *has*
            # arrived, this check failing is a genuine reason to pull the
            # instance, and demoting it to soft would lose that. Admission is
            # documented against the flag in
            # docs/control-tower-implementation.md §1.
            attendee_dependent=True,
            enabled=_bool(env, "ENABLE_OBO"),
            missing=missing_scopes,
            validation_state=obo_validation_state,
            external_validation_pending=obo_validation_state == "pending",
            present=obo_status.get("present") is True,
            fresh=obo_status.get("fresh") is True,
            validated_at=obo_validated_at,
            max_age_seconds=OBO_VALIDATION_MAX_AGE,
        ),
        "release_pins": _check(
            not missing_pins and not mismatched_tools,
            "all release inputs are pinned and installed versions match"
            if not missing_pins and not mismatched_tools
            else "release inputs are unpinned or installed versions differ",
            missing=sorted(missing_pins),
            mismatched=sorted(mismatched_tools),
        ),
        # This used to be a governance-only warning, on the grounds that a
        # deployment with no gateway still reached every model through
        # <host>/serving-endpoints. That is no longer true. The legacy
        # per-model endpoints have been retired in favour of Unity Catalog
        # model services, /serving-endpoints/anthropic/v1/messages answers 404,
        # and the AI Gateway is the only surface left — so an unresolved gateway
        # is now a broken event rather than an ungoverned one.
        #
        # It stays soft anyway, for the same reason it always was: /readyz
        # gating an attendee out of a workshop is a worse outcome than letting
        # them in to find out. What changed is the wording and the bar. Amber
        # now means "resolved but Omnigent will not route through it", and
        # unresolved means no workspace host is configured at all, since the
        # workspace-hosted form is derivable from a host alone.
        "model_gateway": _soft(
            gateway_status.get("resolved") is True
            and gateway_status.get("omnigent_gateway_form") is True,
            (
                "green"
                if gateway_status.get("resolved")
                and gateway_status.get("omnigent_gateway_form")
                else "amber"
            ),
            (
                "Unity AI Gateway resolved in the form Omnigent recognises"
                if gateway_status.get("resolved")
                and gateway_status.get("omnigent_gateway_form")
                else "Unity AI Gateway resolved, but not in a form Omnigent "
                "treats as the gateway, so Omnigent derives its own paths from "
                "the workspace host instead"
                if gateway_status.get("resolved")
                else "no Unity AI Gateway and no workspace host to derive one "
                "from, so no model is reachable: the legacy serving-endpoints "
                "models this used to fall back to have been retired. Set "
                "DATABRICKS_HOST, or name a gateway with "
                "DATABRICKS_GATEWAY_HOST"
            ),
            **{
                key: gateway_status.get(key)
                for key in (
                    "source",
                    "gateway_host_set",
                    "workspace_id_set",
                    "workspace_id_derivable",
                    "omnigent_gateway_form",
                )
            },
        ),
        # Reported so an operator can see which cost posture an event is running
        # without reading the deployment, and amber on a name we don't know
        # because that is the one case where what an operator asked for and what
        # they got differ. Not a hard gate: the value it falls back to is the one
        # every event ran before profiles existed, so a typo here degrades to the
        # old behaviour rather than to a broken one.
        "model_profile": _soft(
            profile_known,
            "green" if profile_known else "amber",
            (
                f"model profile {active_profile}"
                if profile_known
                else f"WORKSHOP_MODEL_PROFILE={requested_profile!r} is not a "
                f"profile this release knows, so {active_profile} is in force. "
                f"Valid: {', '.join(sorted(models.PROFILES))}"
            ),
            profile=active_profile,
            requested=requested_profile,
            # Reported rather than required (see the pin_names note above), and
            # worth reporting because a pin and a profile can disagree: a pinned
            # Opus driver under the economy profile is legal, deliberate for a
            # pool of paid attendees, and otherwise invisible.
            pins={
                name: value
                for name in ("ANTHROPIC_MODEL", "CODEX_MODEL", "INSIGHT_SUMMARY_MODEL")
                if (value := env.get(name, "").strip())
            },
            # Fully-qualified model service names, so an operator can compare
            # what this deployment intends against what the workspace lists
            # without working the profile and the pins out by hand.
            models=intended_models,
        ),
        # Never a hard gate: insight capture serves the sales follow-up, not the
        # attendee, and no attendee should lose a workshop over it.
        "insight_capture": _soft(
            insight_delivering,
            (
                "green"
                if insight["enabled"] and insight_delivering
                else "amber"
                if not insight["enabled"]
                else "red"
            ),
            (
                "insight capture is off"
                if not insight["enabled"]
                else f"{insight['dropped']} events were dropped before Control "
                "Tower collected them — the buffer overflowed"
                if not insight_delivering
                else "capturing; pushing to Control Tower"
                if insight["delivery"] == "push"
                else f"capturing; Control Tower has collected {insight['collections']} times"
                if insight["collected"]
                else "capturing; awaiting Control Tower's first collection"
            ),
            requested=insight["requested"],
            effective=insight["effective"],
            discovery=insight["discovery"],
            delivery=insight["delivery"],
            push_configured=insight["push_configured"],
            collected=insight["collected"],
            collections=insight["collections"],
            pending=insight["pending"],
            dropped=insight["dropped"],
        ),
        # Native Apps telemetry is configured on the App resource by Control
        # Tower, not by this process. Missing export remains soft so a preview
        # outage never blocks an attendee after fleet preflight; the exact
        # secret-free state is still visible to CT on every readiness sweep.
        "app_telemetry": _soft(
            otel_status.get("configured") is True,
            str(otel_status.get("state") or "amber"),
            (
                "Databricks Apps OTLP export and workshop identity are configured"
                if otel_status.get("configured") is True
                else "Databricks Apps OTLP export is disabled or missing workshop identity"
            ),
            enabled=otel_status.get("enabled") is True,
            configured=otel_status.get("configured") is True,
            protocol=otel_status.get("protocol"),
            collector_endpoint_present=otel_status.get("collector_endpoint_present")
            is True,
            service_name_present=otel_status.get("service_name_present") is True,
            required_resource_attributes=otel_status.get(
                "required_resource_attributes", []
            ),
            missing_resource_attributes=otel_status.get(
                "missing_resource_attributes", []
            ),
        ),
        # Soft because the container's copy under DATA_ROOT is not lost when
        # this fails -- what is lost is the attendee's ability to reach their
        # work from outside the terminal. Refusing to serve would cost them the
        # workshop to protect a copy. Reported at all because this failed on
        # every commit of a live event while announcing it nowhere but a log
        # file in a container nobody can open.
        "workspace_sync": _soft(
            sync["state"] != "failed",
            "green"
            if sync["state"] == "ok"
            else "amber"
            if sync["state"] == "never"
            else "red",
            (
                "committed work is syncing to the attendee's Workspace home"
                if sync["state"] == "ok"
                else "no commits yet, so nothing has been synced"
                if sync["state"] == "never"
                else "committed work is NOT reaching the attendee's Workspace "
                f"home (databricks sync exited {sync['exit']}) — it exists only "
                "inside the terminal"
            ),
            state_detail=sync["detail"],
            last_attempt_at=sync["at"],
            exit_code=sync["exit"],
        ),
    }
    ready = all(check["ok"] for check in checks.values() if not check.get("soft"))
    return {
        "status": "ready" if ready else "not_ready",
        "ready": ready,
        "checks": checks,
        "durability": durability(
            credential_status,
            obo_status,
            env,
            now=current_time,
            obo_renewing=obo_renewing,
        ),
        "release_manifest": release_manifest,
    }


def _bound_workspace_sync() -> dict | None:
    """The bound attendee's last sync outcome, or None if the instance is unbound.

    One app serves one attendee, so the instance-level report can speak for that
    attendee. Read straight off disk rather than through the user registry: the
    registry is process-local and empty until the attendee's next request, while
    the record survives on ``DATA_ROOT``, so going through it would paper over a
    recorded failure with "nothing committed yet" for the whole window after a
    restart. Creates nothing -- a readiness probe has no business provisioning
    anyone.
    """
    from . import attendee, user_content

    email = attendee.resolved_email()
    if not email:
        return None
    return user_content.workspace_sync_status_for_email(email)


def evaluate_runtime() -> dict:
    from . import attendee, event_deadline
    from .bootstrap import install
    from .cli_config import gateway_status
    from .credentials import credential_manager, secret_protection_status
    from .entitlements import entitlement_manager
    from .event_emitter import event_emitter
    from .obo import obo_manager, obo_watcher
    from .telemetry import otel_health

    return evaluate(
        env=event_deadline.effective_environment(os.environ),
        gateway_status=gateway_status(),
        credential_status=credential_manager.status(),
        installer_status=install.status(include_proof=True),
        entitlement_status=entitlement_manager.status(),
        obo_status=obo_manager.status(),
        secret_protection_status=secret_protection_status(),
        attendee_binding=attendee.binding(),
        delivery_status=event_emitter.delivery_status(),
        workspace_sync=_bound_workspace_sync(),
        otel_status=otel_health(os.environ),
        obo_renewing=obo_watcher.running,
    )


__all__ = ["EXPECTED_OBO_SCOPES", "evaluate", "evaluate_runtime"]
