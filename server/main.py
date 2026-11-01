"""Workshop Terminal — multi-user coding-agent workbench for Databricks
training events.

Single uvicorn worker only: PTY fds, sessions, users, and content state are
process-local.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

import asyncio

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import (
    agents,
    cli_config,
    config,
    event_deadline,
    help as help_module,
    identity,
    models,
    model_policy,
    obo,
    observability,
    operational,
    readiness,
    selfheal,
    spend,
    supervisor,
    telemetry,
    user_content,
)
from .admin import router as admin_router
from .auth import Principal, get_current_user, is_admin
from .bootstrap import install
from .capture_journal import CaptureError
from .closeout_guard import CaptureClosedError
from .closeout_http import (
    CloseoutAdmissionMiddleware,
    retained_model_policy_pending_refusal,
)
from .content import content_service
from .workshop_lifecycle import CLOSING_DETAIL, UNAVAILABLE_DETAIL, attendee_status
from .credentials import (
    CredentialError,
    credential_manager,
    ensure_user_credentials,
    initialize_app_identity,
)
from .entitlements import entitlement_manager
from .event_emitter import event_emitter, flush_loop
from .events import event_hub
from .evaluation import router as evaluation_router
from .log_collector import install_app_error_journal, log_collector
from .network_check import router as network_check_router
from .omnigent_remote import remote_host_manager
from .sessions import SessionConfigurationError, SessionConflictError, session_manager
from .users import user_manager
from .ws import router as ws_router

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s"
)
observability.install_logging_redaction()
logger = logging.getLogger("workshop-terminal")

_STATIC_DIR = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "static"))
_transcript_topic_scan_at: dict[str, float] = {}

_DISCOVERY_PRODUCT_TO_TOPIC = {
    "lakebase": "lakebase",
    "postgres": "lakebase",
    "genie": "genie",
    "ai-bi": "dashboards",
    "aibi": "dashboards",
    "dashboards": "dashboards",
    "lakeview": "dashboards",
    "unity-catalog": "unity-catalog",
    "uc": "unity-catalog",
    "pipelines": "pipelines",
    "lakeflow": "pipelines",
    "dlt": "pipelines",
    "vector-search": "vector-search",
    "mlflow": "mlflow",
    "serving": "serving",
    "model-serving": "serving",
    "fmapi": "serving",
    "ai-functions": "ai-functions",
    "ai_functions": "ai-functions",
    "agent-bricks": "agent-bricks",
    "dbsql": "dbsql",
    "sql-warehouse": "dbsql",
    "apps": "apps",
    "databricks-apps": "apps",
    "jobs": "jobs",
}


def _observe_output(session, chunk: str) -> None:
    """Spot content-pack topics in terminal output to drive contextual
    insights. Only topic names are recorded — never the text itself."""
    if not config.topic_detection_enabled():
        return
    from .content import consume_topic_chunk

    prior_tail = getattr(session, "_topic_tail", "") or ""
    scannable, session._topic_tail = consume_topic_chunk(prior_tail, chunk)
    topics = content_service.scan_topics(scannable)
    if not topics:
        return
    user = user_manager.peek(session.owner_email)
    if user:
        now = time.time()
        should_notify = False
        for topic in topics:
            prev_at = user.topics.get(topic, 0.0)
            if now - prev_at > 30.0:
                should_notify = True
            user.topics[topic] = now
            user.topic_hits[topic] = user.topic_hits.get(topic, 0) + 1
        if content_service.advance_to_build_if_default():
            try:
                event_hub.publish({"t": "phase", "phase": "build"})
            except Exception:  # noqa: BLE001
                pass
        if should_notify:
            try:
                event_hub.publish({"t": "content_updated"})
            except Exception:  # noqa: BLE001
                pass


def _start_control_tower_threads(
    emitter,
    stop: threading.Event,
    *,
    thread_factory=threading.Thread,
) -> list[threading.Thread]:
    reporter = operational.OperationalHealthReporter(emitter)
    threads = []
    # The flusher is push-only: with no ingest endpoint it would wake every 15s to
    # POST at nothing. Control Tower collects the same buffer on its harvest.
    if emitter.can_push:
        threads.append(
            thread_factory(
                target=flush_loop,
                args=(emitter, stop),
                daemon=True,
                name="event-emitter-flush",
            )
        )
    threads.append(
        thread_factory(
            target=reporter.run,
            args=(stop,),
            daemon=True,
            name="operational-health",
        )
    )
    for thread in threads:
        thread.start()
    return threads


def _stop_control_tower_threads(
    stop: threading.Event,
    threads: list,
    *,
    join_timeout: float = 1.0,
) -> None:
    stop.set()
    for thread in threads:
        thread.join(timeout=join_timeout)


def _checkpoint_shutdown_users() -> dict:
    from .closeout_guard import admitted_writer, closeout_state

    journal = event_emitter.journal
    state = closeout_state(journal.read())
    if state is not None and state["phase"] != "OPEN":
        # Normal closeout already owns the final checkpoints/finalizer. An app
        # restart must neither reopen that barrier nor invent a second closeout.
        return {"complete": True, "issues": []}
    users = [user for user in user_manager.all() if user._capture_journal is journal]
    if users:
        # Save counters with durable failure evidence if persistence fails after
        # the PTY lifetimes have already completed. No existing ticket is erased.
        with admitted_writer(journal, "shutdown_checkpoint"):
            for user in users:
                user_manager.checkpoint(user)
    state = closeout_state(journal.read())
    issues = []
    if state is not None:
        if state["writers"]:
            issues.append("accepted_writers_pending")
        if state["writer_outcomes"]:
            issues.append("accepted_writer_failed")
    return {"complete": not issues, "issues": issues}


def _retain_capture() -> dict:
    """Flush accepted write-behind capture before the process exits."""
    event_emitter.journal.close()
    pending = event_emitter.journal.health().get("pending_operations", 0)
    return {"complete": not pending, "issues": ["capture_retention_pending"] if pending else []}


def _shutdown_runtime(emitter_stop, emitter_threads, *, timeout: float = 10.0) -> dict:
    """Drain ordinary app shutdown without sealing the ongoing workshop.

    Apps allows 15s after SIGTERM. Uvicorn has a separate 2s request drain; this
    one 10s budget includes every lifecycle stage. A stuck SDK call must not keep
    the server alive past it. Its daemon keeps the original durable reservation
    unresolved, so a replacement cannot silently declare that work preserved.
    """
    from . import insight_summary, ws

    deadline = time.monotonic() + timeout
    ws.freeze_capture_operations()
    if emitter_stop is not None:
        emitter_stop.set()
    result = {"complete": False, "issues": [], "outcomes": []}
    finished = threading.Event()

    def stop():
        stages = [
            # Accepted socket input must finish before stopping its PTY and
            # completing the terminal's existing lifetime ticket.
            ("websocket_capture", lambda: ws.drain_capture_operations(deadline=deadline)),
            # Stop periodic producers before checking the final writer inventory.
            # Existing credential clients remain usable after rotation stops.
            ("credential_rotation", credential_manager.stop),
            ("entitlement_reconciliation", entitlement_manager.stop),
            ("log_collector", log_collector.stop),
            ("obo_watcher", obo.obo_watcher.stop),
        ]
        if emitter_stop is not None:
            stages.append(("control_tower_reporters", lambda: _stop_control_tower_threads(
                emitter_stop, emitter_threads)))
        stages.extend([
            ("terminal_processes", lambda: session_manager.drain_for_closeout(
                deadline=deadline, reason="shutdown")),
            ("paired_host_processes", lambda: remote_host_manager.stop(deadline=deadline)),
            ("identity_workers", lambda: identity.drain_for_closeout(deadline=deadline)),
            ("summary_workers", lambda: insight_summary.drain_for_closeout(
                max(0.0, deadline - time.monotonic()))),
            # All terminal parents are stopped; wait for accepted detached saves
            # without creating the permanent workshop-closeout admission file.
            ("workspace_sync_workers", lambda: user_content.drain_sync_for_closeout(
                user_manager.all(), deadline=deadline)),
            ("attendee_checkpoints", _checkpoint_shutdown_users),
            # Write-behind capture is retained last, after every producer above.
            ("capture_retention", _retain_capture),
        ])
        try:
            for name, operation in stages:
                if time.monotonic() >= deadline:
                    result["issues"].append("shutdown_deadline_exceeded")
                    break
                try:
                    outcome = operation()
                    if outcome is None:
                        outcome = {"complete": True, "issues": []}
                    result["outcomes"].append({"kind": name, **outcome})
                    if not outcome.get("complete"):
                        result["issues"].extend(outcome.get("issues") or [name + "_unverified"])
                except Exception as error:  # noqa: BLE001 - keep other drains possible
                    result["issues"].append(f"{name}_failed:{type(error).__name__}")
            result["complete"] = not result["issues"]
        finally:
            finished.set()

    # A Workspace SDK operation has its own retry/network timeout. A daemon plus
    # a bounded join enforces this lifecycle deadline without pretending that a
    # blocked write was cancelled or removing its durable writer receipt.
    worker = threading.Thread(target=stop, name="workshop-shutdown", daemon=True)
    worker.start()
    if not finished.wait(timeout=max(0.0, deadline - time.monotonic())):
        return {"complete": False, "issues": ["shutdown_deadline_exceeded"], "outcomes": []}
    return result


@asynccontextmanager
async def lifespan(app: FastAPI):
    loop = asyncio.get_running_loop()
    previous_asyncio_handler = observability.install_asyncio_exception_handler(loop)
    # Restarts and blocked kills are seen by the supervisor, which has no
    # telemetry of its own; this process records them.
    supervisor.relay_events(observability.record)
    from . import topology, ws

    ws.start_capture_operations()

    # A multi-session override makes the UI lie and reintroduces the process
    # races this release removes. Fail the deployment before serving traffic.
    topology.validate_single_session()
    if not config.local_dev():
        # Capture must be durable before session restoration/startup producers.
        # This also scrubs the privileged environment before any PTY/installer.
        initialize_app_identity()
        from . import capture

        capture.configure(event_emitter)
    # P1-11: if a state path is configured, attach the metadata journal so
    # sessions survive a restart as surfaced ghosts (the live PTYs cannot).
    state_path = config.session_state_path()
    if state_path:
        from .session_store import SessionMetadataStore

        session_manager.configure_store(SessionMetadataStore(state_path))
    # C6: discovery records are the one attendee-authored thing worth surviving a
    # restart — a mid-workshop crash would otherwise lose everything the agent had
    # learned, and attendees don't repeat themselves. No journal, no capture, no
    # file: the path is only attached when capture is on.
    if config.local_dev() and config.insight_capture_enabled():
        from . import discovery

        restored = discovery.discovery_store.configure_journal(discovery.journal_path())
        if restored:
            logger.info("discovery journal restored (%d records)", restored)
    session_manager.attach_loop(loop)
    session_manager.output_observer = _observe_output
    event_hub.attach_loop(loop)
    os.makedirs(config.users_root(), exist_ok=True)
    emitter_stop = None
    emitter_threads = []
    try:
        remote_host_manager.start()
        # P4: the Omnigent process logs an attendee's failure lands in are on disk
        # either way. Sweeping them is the difference between an operator seeing
        # the traceback and an operator seeing a screenshot.
        if config.log_collector_enabled():
            log_collector.start()
        # And this app's own tracebacks, which land on stdout and so are absent
        # from every operator tool. An attendee reporting "it just says error"
        # may be reporting a 500 from here, not from Omnigent.
        install_app_error_journal()
        # P2: notice a credential approaching expiry while there is still a tab
        # able to renew it, rather than after every terminal has failed.
        obo.obo_watcher.start()
        if not config.local_dev():
            # Capture the platform-injected M2M secret into one explicit SDK
            # client, scrub it from process env, and harden the same-UID process
            # boundary before installers or attendee PTYs can start.
            install.run_in_background()
            credential_manager.start()
            # SP-driven entitlement reconciler: keeps the labuser able to use the
            # resources the app SP creates (no-op unless ENABLE_ENTITLEMENTS).
            entitlement_manager.start()
            # C3b: operational health sampling always runs — Control Tower reads it
            # off the buffer on its harvest. The push flusher only starts if CT
            # ingest happens to be configured, which the Apps proxy makes unlikely.
            emitter_stop = threading.Event()
            emitter_threads = _start_control_tower_threads(
                event_emitter,
                emitter_stop,
            )
            logger.info(
                "event emitter started (delivery=%s stream=%s)",
                "push" if event_emitter.can_push else "pull",
                event_emitter.delivery_status().get("stream_id") or "HELD",
            )
        else:
            logger.info("LOCAL_DEV=1 — skipping CLI installers and credential rotation")
        topo_warning = topology.startup_warning()
        if topo_warning:
            logger.warning("topology: %s", topo_warning)
        logger.info("workshop terminal up (phase=%s)", content_service.phase)
        yield
    finally:
        shutdown = _shutdown_runtime(emitter_stop, emitter_threads)
        if not shutdown["complete"]:
            logger.warning("workshop shutdown remains unverified (%s)", ",".join(shutdown["issues"]))
        loop.set_exception_handler(previous_asyncio_handler)


app = FastAPI(title="Databricks Workshop Terminal", lifespan=lifespan)

app.add_middleware(CloseoutAdmissionMiddleware)
app.include_router(admin_router)
app.include_router(evaluation_router)
app.include_router(network_check_router)
app.include_router(ws_router)


@app.exception_handler(CaptureError)
async def capture_unavailable(_request: Request, _error: CaptureError):
    detail = ("Workshop capture is held; retry when persistence is available."
              if _request.url.path.startswith("/api/admin/") else UNAVAILABLE_DETAIL)
    return JSONResponse(status_code=503, content={"detail": detail})


@app.exception_handler(CaptureClosedError)
async def capture_closed(_request: Request, _error: CaptureClosedError):
    return JSONResponse(status_code=409, content={"detail": CLOSING_DETAIL})


@app.middleware("http")
async def record_operational_http_status(request: Request, call_next):
    response = await call_next(request)
    try:
        operational.metrics.record_http(response.status_code)
    except Exception:  # noqa: BLE001 - telemetry must never break responses
        pass
    return response


# ---- public API (attendee-scoped) ----


def _effective_wizard_model() -> str:
    """The wizard model in force now, override included.

    Imported locally because ``wizard_llm`` imports ``wizard``, which imports
    this module's dependencies — the same reason the suggest endpoint below
    defers its import.
    """
    from . import wizard_llm

    return str(wizard_llm.effective_model()["model"])


@app.get("/api/config")
def get_config(principal: Principal = Depends(get_current_user)):
    pack = content_service.pack
    event_emitter._ensure_ready()
    return {
        "workshop": attendee_status(event_emitter.journal),
        "user": {"email": principal.name, "is_admin": is_admin(principal)},
        "branding": config.branding(),
        "workspace_url": config.databricks_host(),
        "shell": pack.shell.model_dump(),
        "phase": content_service.phase,
        "broadcast": (
            b.model_dump() if (b := content_service.active_broadcast()) else None
        ),
        "limits": {
            "max_sessions_per_user": config.max_sessions_per_user(),
        },
        "credential": credential_manager.status(),
        "obo": obo.obo_manager.status(principal.name),
        # One place to read "how long have I got, and what still works?" —
        # separately per plane, because the app credential and the attendee's
        # tab-bound OBO fail differently and cost different things.
        "durability": readiness.durability(
            credential_manager.status(),
            obo.obo_manager.status(principal.name),
            event_deadline.effective_environment(os.environ),
        ),
        "omnigent_remote": {
            "enabled": config.omnigent_remote_enabled(),
            "url": config.omnigent_app_url(),
        },
        # Carried here as well as on /api/wizard so Home can tell "the operator
        # switched this off" from "this attendee has already seen it" without a
        # second request — the two look identical in `should_show`, and only one
        # of them should also hide the recap's way back in.
        "onboarding_wizard": {"enabled": config.onboarding_wizard_enabled()},
        # The model actually in force, not the deployed pin: a live swap is
        # ephemeral, so reporting the pin here would tell an operator the room
        # is on a model it stopped using several minutes ago.
        "llm_wizard": {
            "enabled": config.llm_wizard_enabled(),
            "model": _effective_wizard_model(),
        },
        "entitlements": entitlement_manager.status(),
        # The model-comparison exercise, as the attendee runs it. Published from
        # the same resolution the generated Codex config uses, so what the UI
        # offers and what `codex --profile <name>` finds cannot drift apart.
        "model_comparison": model_comparison(),
        "help": help_module.snapshot(),
    }


@app.get("/api/workshop-status")
def get_workshop_status(_: Principal = Depends(get_current_user)):
    """Read the input barrier without admitting work or advancing closeout."""
    event_emitter._ensure_ready()
    return attendee_status(event_emitter.journal)


def model_comparison() -> list[dict]:
    """Comparison models this deployment will actually serve, in a fixed order.

    Carries the endpoint rather than a command to run. These models answer only
    on chat-completions, and codex-cli 0.144.6 removed that wire, so there is no
    harness command here we can promise works — see server/models for the whole
    story. Publishing the resolved set without a command keeps the UI honest
    instead of printing an invocation that exits non-zero in front of a room.

    One endpoint serves the whole set now rather than one per model: the
    gateway's unified chat-completions route takes the model in the body, so
    what distinguishes the entries is the ``model`` field alone. That is also
    what makes the exercise legible — the attendee changes one string and reads
    the difference in the bill.
    """
    from . import model_policy

    policy_catalogue = model_policy.direct_catalogue()
    resolved = models.comparison_models(
        policy_catalogue,
        strict=policy_catalogue is not None,
    )
    endpoint = cli_config.unified_chat_url()
    return [
        {
            "profile": name,
            "model": resolved[name],
            "label": label,
            "endpoint": endpoint,
        }
        for name, (_default, label) in models.COMPARISON_MODELS.items()
        if name in resolved
    ]


class HelpRaiseBody(BaseModel):
    note: str | None = None


class HelpMessageBody(BaseModel):
    body: str


@app.post("/api/help/raise")
def help_raise(body: HelpRaiseBody, _: Principal = Depends(get_current_user)):
    return help_module.raise_hand(body.note)


@app.post("/api/help/lower")
def help_lower(_: Principal = Depends(get_current_user)):
    return help_module.lower_hand()


@app.post("/api/help/messages")
def help_post_message(
    body: HelpMessageBody, principal: Principal = Depends(get_current_user)
):
    try:
        return help_module.post_attendee_message(body.body, sender=principal.name)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.get("/api/help/thread")
def help_thread(_: Principal = Depends(get_current_user)):
    return help_module.thread_snapshot()


class HelpFeedbackBody(BaseModel):
    value: str


@app.post("/api/help/messages/{message_id}/feedback")
def help_feedback(
    message_id: str, body: HelpFeedbackBody, _: Principal = Depends(get_current_user)
):
    """Rate a Workshop Assistant answer: once, and only an assistant's."""
    try:
        return help_module.record_feedback(message_id, body.value)
    except help_module.HelpMessageNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.post("/api/help/handoff")
def help_handoff(_: Principal = Depends(get_current_user)):
    """Ask for a member of the workshop team instead of the Workshop Assistant."""
    return help_module.request_person()


class HelpAckBody(BaseModel):
    message_id: str


@app.post("/api/help/ack")
def help_ack(body: HelpAckBody, _: Principal = Depends(get_current_user)):
    """The attendee saw an operator message — forward the receipt upstream.

    Attendee-facing rather than on the admin router: the browser calling this is
    the attendee's, and they are not an operator.
    """
    return {"acked": help_module.acknowledge_message(body.message_id)}


@app.get("/api/setup-status")
def setup_status(_: Principal = Depends(get_current_user)):
    return install.status()


@app.get("/api/omnigent-host")
def omnigent_host_status(principal: Principal = Depends(get_current_user)):
    """Sanitized verified remote-host readiness for the authenticated attendee."""
    return remote_host_manager.readiness(principal.name)


class _EmailBody(BaseModel):
    # Helpers running inside the attendee PTY have no proxy identity headers, so
    # they pass their own $WORKSHOP_USER_EMAIL. A browser caller carries the
    # forwarded headers instead and may omit this.
    email: str | None = None


class _EntitlementReconcileBody(_EmailBody):
    resource_type: str | None = None


class _AppDeployEventBody(_EmailBody):
    outcome: Literal["succeeded", "failed", "cancelled", "timed_out"]
    reason: str = Field(min_length=1, max_length=64)
    duration_ms: float = Field(ge=0, le=3_600_000)
    attempts: int = Field(ge=0, le=10_000)
    resumed: bool = False
    source: Literal["mcp", "cli"] = "mcp"


def _callback_identity(body: _EmailBody, request: Request) -> Principal:
    """Authenticate a browser proxy call or attendee helper capability."""
    capability = (request.headers.get("x-workshop-capability") or "").strip()
    if capability:
        email = (body.email or "").strip().lower()
        if not email:
            email = user_content.callback_capability_owner(capability) or ""
        if not email:
            raise HTTPException(status_code=422, detail="email required")
        if not user_content.verify_callback_capability(email, capability):
            raise HTTPException(status_code=403, detail="invalid callback capability")
        return Principal(email)

    # Browser requests are authenticated by the Databricks Apps proxy. Do not
    # use auth's LOCAL_DEV identity fallback here: a headerless external/direct
    # request must remain denied even in a development process.
    has_proxy_identity = bool(
        request.headers.get("x-forwarded-email")
        or request.headers.get("x-forwarded-user")
    )
    if not has_proxy_identity:
        raise HTTPException(status_code=403, detail="callback authentication required")
    principal = get_current_user(request)
    requested = (body.email or "").strip().lower()
    if requested and requested != principal.name:
        raise HTTPException(
            status_code=403, detail="callback email does not match caller"
        )
    return principal


@app.get("/api/auth/keepalive")
def auth_keepalive(principal: Principal = Depends(get_current_user)):
    """Silent same-origin keepalive loaded in a hidden iframe.

    Browser ``fetch()`` cannot follow cross-origin OIDC 302 redirects to the
    workspace IdP when the Apps proxy session cookie nears expiry, whereas a
    hidden iframe navigation completes the silent OIDC redirect and delivers a
    fresh ``x-forwarded-access-token`` to ``get_current_user``.
    """
    from fastapi.responses import HTMLResponse

    return HTMLResponse(
        content="<!doctype html><meta charset=utf-8><title>ok</title>",
        headers={"Cache-Control": "no-store"},
    )


@app.post("/api/obo/refresh")
def obo_refresh(body: _EmailBody, request: Request):
    """Reactive OBO self-heal (layer 2). Called by the ``databricks-me`` wrapper
    on a 401/403: captures a fresh forwarded token if the caller is the browser,
    force-writes the latest captured token to the ``me`` profile, and nudges any
    connected tab to deliver an even fresher token on its next poll."""
    principal = _callback_identity(body, request)
    email = principal.name
    forwarded_obo = (request.headers.get("x-workshop-forwarded-obo-token") or "").strip()
    if forwarded_obo and not principal.access_token:
        obo.obo_manager.capture(email, forwarded_obo)
    written = obo.obo_manager.force_refresh(email)
    try:
        # Attendee-neutral nudge: every tab may refresh its own authenticated
        # identity, but no tab should learn another attendee's email.
        event_hub.publish({"t": "obo_refresh"})
    except Exception:  # noqa: BLE001 — nudge is best-effort
        pass
    return {"written": written, **obo.obo_manager.status(email)}


@app.post("/api/recover")
def recover(principal: Principal = Depends(get_current_user)):
    """The attendee-facing Recover button: re-mirror, wake the host, re-ask the tab.

    Same three actions the server takes automatically when it notices a
    credential failure. Exposed because an attendee looking at a banner should
    have something to press that is not "reload and hope", and because pressing
    it is the fastest signal we get that somebody is stuck.
    """
    result = selfheal.self_healer.recover(
        principal.name, "attendee pressed Recover", force=True
    )
    return {
        "recovered": bool(result.get("credential_fresh")),
        "actions": result.get("actions", []),
        "obo": obo.obo_manager.status(principal.name),
    }


@app.post("/api/entitlements/reconcile")
def reconcile_entitlements(body: _EntitlementReconcileBody, request: Request):
    """On-demand entitlement reconcile (the ``workshop-grant-me`` path): makes a
    just-created app/job/Lakebase instance usable by the labuser immediately
    instead of waiting for the next sweep."""
    principal = _callback_identity(body, request)
    try:
        return entitlement_manager.reconcile(
            email=principal.name, resource_type=body.resource_type
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.post("/api/tools/app-deploy/event")
def record_app_deploy_event(body: _AppDeployEventBody, request: Request):
    """Receive the local helper's bounded result and bridge it into OTel/CT.

    The callback capability binds the record to this attendee. The request has
    deliberately no app name, URL, command output, project path, or credential.
    """
    principal = _callback_identity(body, request)
    captured = telemetry.app_deploy(
        principal.name,
        outcome=body.outcome,
        reason=body.reason,
        duration_ms=body.duration_ms,
        attempts=body.attempts,
        resumed=body.resumed,
        source=body.source,
    )
    if not captured:
        raise HTTPException(status_code=503, detail=UNAVAILABLE_DETAIL)
    if config.topic_detection_enabled():
        deploy_user = user_manager.peek(principal.name)
        if deploy_user is not None:
            now_ts = time.time()
            deploy_user.topics["apps"] = now_ts
            deploy_user.topic_hits["apps"] = deploy_user.topic_hits.get("apps", 0) + 1
            if body.outcome == "succeeded":
                deploy_user.topics["build-complete"] = now_ts
                deploy_user.topic_hits["build-complete"] = (
                    deploy_user.topic_hits.get("build-complete", 0) + 1
                )
            try:
                event_hub.publish({"t": "content_updated"})
            except Exception:  # noqa: BLE001
                pass
    if body.outcome == "succeeded":
        def _bg_screenshot(owner_email: str) -> None:
            try:
                from . import browser_evidence, projects

                if not browser_evidence.browser_binary():
                    return
                user = user_manager.get(owner_email)
                root = projects.root(user)
                if root.is_dir():
                    for child in sorted(root.iterdir()):
                        if child.is_dir() and not child.is_symlink() and not child.name.startswith("."):
                            browser_evidence.capture_project_preview_screenshot(user, child)
            except Exception:  # noqa: BLE001
                pass

        threading.Thread(
            target=_bg_screenshot,
            args=(principal.name,),
            daemon=True,
            name="app-deploy-screenshot",
        ).start()
    return {"recorded": True}


class _AppLogsBody(_EmailBody):
    app_name: str = Field(..., min_length=1, max_length=120)
    tail_lines: int = Field(default=200, ge=0, le=5000)
    search: str = Field(default="", max_length=300)


@app.post("/api/apps/logs")
def stream_app_logs_snapshot(body: _AppLogsBody, request: Request):
    """Fetch recent log lines from a deployed Databricks App's /logz/stream WebSocket.

    The Databricks CLI ``apps logs`` subcommand rejects token-backed profiles
    with ``OAuth Token not supported for current auth type pat``. Using the
    Workshop Terminal's app-identity OAuth bearer and ``Origin`` header against
    ``wss://<app-url>/logz/stream`` returns the real JSON log entries.
    """
    from datetime import datetime, timezone
    import urllib.error
    import urllib.request
    from websockets.sync.client import connect

    _callback_identity(body, request)
    host = config.databricks_host().rstrip("/")
    tok = credential_manager.token()
    if not host or not tok:
        raise HTTPException(status_code=503, detail="Workshop credential is not ready")

    app_req = urllib.request.Request(
        f"{host}/api/2.0/apps/{body.app_name.strip()}",
        headers={"Authorization": f"Bearer {tok}", "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(app_req, timeout=15) as resp:
            info = json.load(resp)
    except urllib.error.HTTPError as exc:
        # Only a real "no such app" is a 404; a refused or failing Apps API is
        # the platform's problem, not the attendee's app name.
        status = 404 if exc.code == 404 else 502
        raise HTTPException(status_code=status, detail=f"Could not read app {body.app_name!r}: {exc}") from exc
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=502, detail=f"Could not read app {body.app_name!r}: {exc}") from exc

    app_url = str(info.get("url") or "").rstrip("/")
    if not app_url.startswith("https://"):
        raise HTTPException(status_code=409, detail=f"App {body.app_name!r} has no HTTPS URL yet")

    ws_url = app_url.replace("https://", "wss://", 1) + "/logz/stream"
    lines: list[str] = []
    try:
        with connect(
            ws_url,
            additional_headers={"Authorization": f"Bearer {tok}"},
            origin=app_url,
            open_timeout=10,
        ) as sock:
            sock.send(body.search or "")
            deadline = time.monotonic() + 3.0
            while time.monotonic() < deadline:
                try:
                    raw = sock.recv(timeout=0.5)
                except TimeoutError:
                    break
                if raw == "\x00":
                    break
                try:
                    entry = json.loads(raw)
                    ts = entry.get("timestamp")
                    iso = (
                        datetime.fromtimestamp(float(ts), tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                        if isinstance(ts, (int, float)) and ts > 0
                        else ""
                    )
                    src = entry.get("source") or "APP"
                    msg = (entry.get("message") or "").rstrip("\r\n")
                    lines.append(f"{iso} [{src}] {msg}".strip())
                except Exception:  # noqa: BLE001
                    text = str(raw or "").strip()
                    if text:
                        lines.append(text)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Log stream failed for {body.app_name!r}: {exc}") from exc

    if body.tail_lines > 0:
        lines = lines[-body.tail_lines :]
    return {"app_name": body.app_name, "lines": lines}


class _DiscoveryBody(_EmailBody):
    """One agent-elicited discovery record (contract C6).

    Extra keys are permitted: the agent composes this from an instruction file and
    will improvise a field eventually. ``server/discovery.py`` ignores what it
    doesn't know, so a hallucinated key costs that key rather than the record —
    and the attendee, who already said the thing, doesn't get asked twice.
    """

    model_config = {"extra": "allow"}


@app.post("/api/discovery")
def submit_discovery(body: _DiscoveryBody, request: Request):
    """Record what the agent learned about the attendee's use case.

    Called by the ``workshop-discovery`` helper from inside the attendee's PTY, so
    it authenticates through the same capability path as the OBO and entitlement
    helpers — the caller has no proxy identity headers, and the capability token
    binds the submission to one attendee so a shared instance can't cross-attribute.

    Returns ``captured: false`` rather than an error when capture is disabled: the
    agent must not retry, and it must not tell the attendee something failed.
    """
    from . import discovery

    principal = _callback_identity(body, request)
    if not config.discovery_enabled():
        return {"captured": False, "reason": "disabled"}
    raw = body.model_dump(exclude={"email"}, exclude_unset=True)
    stored = discovery.record(principal.name, raw)
    if stored is None:
        # Withdrawn by the attendee, or the per-attendee cap is reached. Both are
        # deliberate refusals, not failures, and neither is the agent's business.
        return {"captured": False, "reason": "not_stored"}
    if config.topic_detection_enabled() and getattr(stored, "agent", "") != "wizard":
        user = user_manager.peek(principal.name)
        if user:
            now = time.time()
            should_notify = False
            for prod in getattr(stored, "databricks_products", ()) or ():
                slug = str(prod).strip().lower().replace(" ", "-")
                mapped = _DISCOVERY_PRODUCT_TO_TOPIC.get(slug)
                if mapped:
                    if now - user.topics.get(mapped, 0.0) > 30.0:
                        should_notify = True
                    user.topics[mapped] = now
                    user.topic_hits[mapped] = user.topic_hits.get(mapped, 0) + 1
            if should_notify:
                try:
                    event_hub.publish({"t": "content_updated"})
                except Exception:  # noqa: BLE001
                    pass
    return {
        "captured": True,
        "record_id": stored.record_id,
        "redactions": stored.redactions,
        "records": discovery.discovery_store.count_for(principal.name),
    }


@app.get("/api/discovery")
def my_discovery(principal: Principal = Depends(get_current_user)):
    """The attendee's own records, so capture is inspectable by its subject."""
    from . import discovery

    if not config.discovery_enabled():
        return {"enabled": False, "records": []}
    return {
        "enabled": True,
        "records": [
            r.payload() for r in discovery.discovery_store.for_attendee(principal.name)
        ],
    }


class _RedactBody(BaseModel):
    record_id: str


@app.post("/api/discovery/redact")
def redact_discovery(
    body: _RedactBody, principal: Principal = Depends(get_current_user)
):
    """Withdraw one of the caller's own records.

    Browser-authenticated only — a PTY capability token must not be able to
    withdraw records, or an agent could quietly erase what it captured. Scoped to
    the caller's own attendee identity, so this cannot reach another attendee's
    records on a shared instance.
    """
    from . import discovery

    if not config.discovery_enabled():
        raise HTTPException(status_code=404, detail="discovery capture is disabled")
    removed = discovery.withdraw(principal.name, body.record_id)
    if not removed:
        raise HTTPException(status_code=404, detail="record not found")
    return {"redacted": True, "record_id": body.record_id}


class _PersonaBody(BaseModel):
    persona: str


@app.post("/api/persona")
def set_persona(body: _PersonaBody, principal: Principal = Depends(get_current_user)):
    """Record whether the attendee is technical or business-oriented.

    Asked on the landing page rather than by the agent on the first turn. The
    agent needs the answer to pitch everything it says, but making it ask cost a
    full round trip before the attendee saw anything happen — and it is a
    strange first question to be asked by a machine you have just met.
    """
    from . import user_content
    from .users import user_manager

    user = user_manager.get(principal.name)
    try:
        persona = user_content.set_persona(user, body.persona)
    except ValueError:
        raise HTTPException(status_code=400, detail="unknown persona") from None
    return {"persona": persona}


class _WizardBody(BaseModel):
    expected_revision: int | None = Field(default=None, ge=0)
    operation: str = "complete"
    selection_token: str = ""
    what_building: str = ""
    industry: str = ""
    industry_stated: bool = False
    intent: str = ""
    idea_id: str = ""
    current_stack: list[str] = Field(default_factory=list)
    persona: str = ""
    skipped: bool = False
    # "rapid" or "guided" when they chose how to start this idea (W2).
    engagement_path: str = ""
    # Optional, and the only field here that is about the person rather than
    # what they are building. Attendees regularly land on a workspace nobody
    # assigned to them, so the name they type is the first evidence we have of
    # who is actually sitting there.
    display_name: str = ""


class _ProfileBody(BaseModel):
    expected_revision: int = Field(ge=0)
    help_preference: str | None = None
    working_approach: str | None = None
    explanation_detail: str | None = None
    engagement_path: str | None = None
    background: str | None = None
    industry: str | None = None


class _AgentProfileBody(_EmailBody):
    """A direct-conversation preference save from ``workshop-workflow prefs``.

    No ``changes`` reads the profile; otherwise ``expected_revision`` is
    required, exactly as for the browser, so a stale write is refused.
    """
    expected_revision: int | None = Field(default=None, ge=0)
    changes: dict[str, str] = Field(default_factory=dict)


@app.get("/api/profile")
def get_profile(principal: Principal = Depends(get_current_user)):
    from . import attendee_profile
    from .users import user_manager

    try:
        return attendee_profile.read(user_manager.get(principal.name)).to_json()
    except attendee_profile.ProfileReadError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.post("/api/profile")
def save_profile(body: _ProfileBody, principal: Principal = Depends(get_current_user)):
    from . import attendee_profile, user_content
    from .users import user_manager

    user = user_manager.get(principal.name)
    changes = {name: value for name, value in (("working_approach", body.working_approach),
                                                ("explanation_detail", body.explanation_detail),
                                                ("engagement_path", body.engagement_path),
                                                ("background", body.background),
                                                ("industry", body.industry)) if value is not None}
    try:
        profile = attendee_profile.save(user, body.help_preference, expected_revision=body.expected_revision,
                                        changes=changes, choice_source="ui")
    except attendee_profile.ProfileConflict as exc:
        raise HTTPException(status_code=409, detail={"message": str(exc), "profile": exc.profile.to_json()}) from exc
    except attendee_profile.ProfileReadError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    try:
        user_content._write_instructions(user)
    except Exception as exc:
        # Durable acceptance already succeeded. Generated instruction files are
        # refreshed by the existing launch/bootstrap path; a cache failure must
        # not tell the attendee that the retained preference was rejected.
        logger.warning("saved profile instruction refresh unavailable (%s)", type(exc).__name__)
    return profile.to_json()


@app.post("/api/agent/profile")
def agent_profile(body: _AgentProfileBody, request: Request):
    """Read or save preferences an attendee stated in conversation (W2).

    Same revision-checked service as the browser; the save is acknowledged
    only once it is durable, so an agent never claims to remember a choice
    that was not kept.
    """
    from . import attendee_profile, user_content
    from .users import user_manager

    principal = _callback_identity(body, request)
    user = user_manager.get(principal.name)
    try:
        if not body.changes:
            return attendee_profile.read(user).to_json()
        if body.expected_revision is None:
            raise HTTPException(status_code=422, detail="expected_revision is required to save")
        profile = attendee_profile.save(user, None, expected_revision=body.expected_revision,
                                        changes=body.changes, choice_source="chat")
    except attendee_profile.ProfileConflict as exc:
        raise HTTPException(status_code=409, detail={"message": str(exc), "profile": exc.profile.to_json()}) from exc
    except attendee_profile.ProfileReadError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    try:
        user_content._write_instructions(user)
    except Exception as exc:
        logger.warning("agent profile instruction refresh unavailable (%s)", type(exc).__name__)
    try:
        # New projects commit this template; keep it on the saved industry (H2, F-007).
        user_content.write_project_memory_template(user)
    except Exception as exc:
        logger.warning("project-memory template refresh unavailable (%s)", type(exc).__name__)
    saved = profile.to_json()
    if "industry" in body.changes:
        # The running session does not reread its instructions, so the agent
        # that saved the industry is told here which demo schema it is.
        from . import demo_data

        try:
            saved["industry_demo"] = demo_data.resolution(profile.industry)
        except Exception as exc:  # noqa: BLE001 — the save is already durable
            logger.warning("industry demo-data resolution unavailable (%s)", type(exc).__name__)
    return saved


class _BrowserStatusBody(_EmailBody):
    pass


class _BrowserInspectBody(_EmailBody):
    url: str = Field(max_length=2000)
    scope: Literal["local", "deployed"] = "local"
    viewports: list[str] = Field(default_factory=lambda: ["desktop"], max_length=2)
    actions: list[str] = Field(default_factory=list, max_length=12)
    project: str | None = None


def _owned_project(user, requested: str | None) -> Path:
    """The attendee's own project: given, or the selected one; never another's."""
    from . import projects

    root = projects.root(user).resolve()
    if requested:
        path = Path(requested).resolve()
    else:
        active = projects.read(user)["active"]
        if not active:
            raise HTTPException(status_code=409, detail="no build is selected yet")
        path = projects.project_path(user, active).resolve()
    if path != root and root not in path.parents:
        raise HTTPException(status_code=403, detail="evidence is recorded only for your own projects")
    if not path.is_dir():
        raise HTTPException(status_code=404, detail="that project does not exist yet")
    return path


@app.post("/api/browser/status")
def browser_status(body: _BrowserStatusBody, request: Request):
    from . import browser_evidence

    _callback_identity(body, request)
    return browser_evidence.status()


@app.post("/api/browser/inspect")
def browser_inspect(body: _BrowserInspectBody, request: Request):
    """Render the attendee's own app and keep real screenshots as evidence (W3).

    A gap (no browser, sign-in blocked, page never loaded) is recorded as a
    gap and returned with ``ok: false``; it is never reported as a pass.
    """
    from . import browser_evidence

    principal = _callback_identity(body, request)
    user = user_manager.get(principal.name)
    project = _owned_project(user, body.project)
    token = browser_evidence.attendee_token(user)[1] if body.scope == "deployed" else ""
    inspect_request = browser_evidence.InspectRequest(url=body.url, scope=body.scope, viewports=body.viewports,
                                                      actions=body.actions, project=project, token=token)
    try:
        result = browser_evidence.inspect(inspect_request)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except browser_evidence.Gap as gap:
        entry = {**gap.as_dict(), "scope": body.scope, "url": body.url}
        revision = browser_evidence.record(project, [entry])
        return {"ok": False, "gap": entry, "revision": revision}
    revision = browser_evidence.record(project, result["artifacts"])
    return {"ok": True, **result, "revision": revision}


class _BuildRequestBody(BaseModel):
    kind: Literal["change", "explain"]
    text: str = Field(min_length=1, max_length=1000)


class _BuildDecisionBody(BaseModel):
    question_id: str | None = Field(default=None, max_length=16)
    answer: str | None = Field(default=None, max_length=2000)
    confirm_direction: bool = False
    path: Literal["rapid", "guided"] | None = None


def _active_session_for(user):
    session = session_manager.active()
    return session if session is not None and session.owner_email == user.email else None


@app.get("/api/build")
def get_build(principal: Principal = Depends(get_current_user)):
    """The attendee's own "Your build" view (W4): goal, stage, next decision, preview, changes."""
    from . import journey

    user = user_manager.get(principal.name)
    return journey.view(user, session=_active_session_for(user))


def _build_project(user) -> Path:
    from . import journey

    project = journey.active_project(user)
    if project is None:
        raise HTTPException(status_code=409, detail="Start or choose a build first.")
    return project


@app.post("/api/build/request")
def build_request(body: _BuildRequestBody, principal: Principal = Depends(get_current_user)):
    """Queue a change or an explanation for the agent's next safe boundary; never types into the terminal."""
    from . import journey

    user = user_manager.get(principal.name)
    try:
        entry = journey.add_request(_build_project(user), body.kind, body.text)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    return {"request": entry, "delivery": "next_safe_boundary",
            "detail": "Your agent picks this up at its next step. To interrupt it now, use Stop current step."}


@app.post("/api/build/decide")
def build_decide(body: _BuildDecisionBody, principal: Principal = Depends(get_current_user)):
    """Resolve a pending decision into the same record a chat answer writes, once."""
    from . import design_workflow, journey

    user = user_manager.get(principal.name)
    try:
        record = journey.decide(_build_project(user), question_id=body.question_id, answer=body.answer,
                                confirm_direction=body.confirm_direction, path=body.path)
    except (ValueError, design_workflow.WorkflowError) as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return {"revision": record["revision"], "phase": record["phase"]}


@app.post("/api/build/stop")
def build_stop(principal: Principal = Depends(get_current_user)):
    """A real interrupt of the agent's current step, reported as exactly that."""
    from . import journey

    user = user_manager.get(principal.name)
    return journey.stop(_active_session_for(user))


class AttachImageBody(BaseModel):
    filename: str = Field(default="screenshot.png", max_length=120)
    data_base64: str = Field(min_length=4, max_length=15_000_000)
    project_name: str | None = Field(default=None, max_length=63)


@app.post("/api/projects/attach-image")
def attach_project_image(
    body: AttachImageBody,
    principal: Principal = Depends(get_current_user),
):
    """Save a pasted or uploaded screenshot into the attendee's project."""
    import base64 as _b64

    from . import projects

    user = user_manager.get(principal.name)
    state = projects.read(user)
    target_name = (body.project_name or state.get("active") or "default").strip()
    root = projects.root(user)
    root.mkdir(parents=True, exist_ok=True)
    if not state.get("active"):
        try:
            projects.select(user, target_name, new=True)
        except Exception:  # noqa: BLE001
            pass
    project_dir = projects.project_path(user, target_name)
    project_dir.mkdir(parents=True, exist_ok=True)
    attachments_dir = project_dir / ".workshop" / "attachments"
    attachments_dir.mkdir(parents=True, exist_ok=True)

    raw_b64 = body.data_base64.strip()
    if "," in raw_b64 and raw_b64.lower().startswith("data:"):
        raw_b64 = raw_b64.split(",", 1)[1]
    try:
        decoded = _b64.b64decode(raw_b64, validate=True)
    except Exception as exc:
        raise HTTPException(status_code=422, detail="Invalid base64 image data") from exc
    if not decoded or len(decoded) > 10 * 1024 * 1024:
        raise HTTPException(status_code=422, detail="Image must be under 10 MB")

    safe_stem = re.sub(r"[^A-Za-z0-9_.-]+", "-", os.path.basename(body.filename or "screenshot.png")).strip("-.") or "screenshot.png"
    if not any(safe_stem.lower().endswith(ext) for ext in (".png", ".jpg", ".jpeg", ".webp", ".gif")):
        safe_stem += ".png"
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    dest = attachments_dir / f"{stamp}-{safe_stem}"
    dest.write_bytes(decoded)
    return {
        "path": str(dest),
        "relative_path": str(dest.relative_to(project_dir)),
        "bytes": len(decoded),
        "project": target_name,
    }


@app.post("/api/export")
def export_code(principal: Principal = Depends(get_current_user)):
    """Export my code (WT-P6): the ZIP streams to the browser; the volume copy stays."""
    from fastapi.responses import FileResponse

    from . import journey

    user = user_manager.get(principal.name)
    try:
        result = journey.export(user, reason="attendee")
    except (RuntimeError, OSError, ValueError, subprocess.TimeoutExpired) as error:
        raise HTTPException(status_code=503, detail=f"Export did not finish: {error}") from error
    return FileResponse(result["local_zip"], media_type="application/zip", filename=result["filename"],
                        headers={"X-Workshop-Export-Path": str(result.get("path") or ""),
                                 "X-Workshop-Export-Files": str(result.get("files", ""))})


@app.get("/api/wizard")
def get_wizard(
    industry: str | None = None,
    q: str = "",
    principal: Principal = Depends(get_current_user),
):
    """Wizard state: whether to show it, and the ideas to show if so.

    Ideas are selected server-side because only the server can see which demo
    tables actually exist, and a card whose data was never seeded must never
    reach the grid. ``industry`` re-filters that grid for someone browsing before
    they have committed to anything, so it is a query parameter rather than
    something read back off the saved brief.
    """
    from . import wizard
    from .users import user_manager

    try:
        return wizard.state(user_manager.get(principal.name), industry, query=q)
    except wizard.BriefReadError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.post("/api/wizard")
def save_wizard(body: _WizardBody, principal: Principal = Depends(get_current_user)):
    """Save what the attendee told the wizard, and hand back the first prompt.

    Returns the starter prompt rather than leaving the frontend to assemble it:
    a chosen idea card carries a prompt written to produce a good first build,
    and that text belongs with the catalogue rather than in the UI.
    """
    from . import attendee_names, user_content, wizard
    from .users import user_manager

    user = user_manager.get(principal.name)
    payload = body.model_dump(exclude_unset=True)
    if payload.get("display_name"):
        attendee_names.capture(
            principal.name,
            user.home,
            payload["display_name"],
            attendee_names.SOURCE_WIZARD,
        )
    # Only the keys the attendee actually sent: the dismissal path posts nothing
    # but ``skipped``, and the defaults on the model would otherwise read as an
    # instruction to blank every answer they had already given.
    try:
        brief = wizard.save(user, payload)
    except wizard.BriefConflict as exc:
        raise HTTPException(status_code=409, detail={"message": str(exc), "brief": exc.brief.to_json()}) from exc
    except wizard.BriefReadError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    # The brief is inlined into the agent's instructions, so they have to be
    # rewritten now. Sessions are almost always launched from the wizard's last
    # step, after this call, so the first agent to start already has it.
    try:
        user_content.set_wizard_brief(user, brief)
    except Exception as exc:
        # Keep the accepted revision observable even when disposable generated
        # instructions cannot refresh; primary journal errors still propagate.
        logger.warning("saved goal instruction refresh unavailable (%s)", type(exc).__name__)
    return {
        "brief": brief.to_json(),
        "starter_prompt": wizard.starter_prompt(brief),
    }


class _WizardSuggestBody(BaseModel):
    text: str = Field(default="", max_length=2000)
    industry: str = Field(default="", max_length=64)
    industry_locked: bool = False
    intent: str = ""


@app.post("/api/wizard/suggest")
def suggest_wizard(
    body: _WizardSuggestBody, principal: Principal = Depends(get_current_user)
):
    """Replace the idea grid with model-written, table-verified cards.

    The attendee frontend calls this, never Control Tower. A timeout or a
    switched-off flag returns the deterministic selector so the modal never
    waits on a model that is not answering.
    """
    from . import wizard_llm, wizard_selections
    from .users import user_manager

    result = wizard_llm.suggest(
        body.text, body.industry, industry_locked=body.industry_locked,
        intent=body.intent, attendee_key=principal.name,
    )
    user = user_manager.get(principal.name)
    result["ideas"] = [wizard_selections.offer(user, idea, source=idea.get("source", "catalog"), model=result.get("model", "")) for idea in result["ideas"]]
    return result


@app.get("/api/wizard/surprise")
def surprise_wizard(
    industry: str = "", principal: Principal = Depends(get_current_user)
):
    """One verified idea, for the Surprise me button."""
    from . import wizard, wizard_selections
    from .users import user_manager

    idea = wizard.surprise(industry)
    return {"idea": wizard_selections.offer(user_manager.get(principal.name), wizard.idea_payload(idea), source="catalog") if idea else None}


@app.get("/api/agents")
def list_agents(principal: Principal = Depends(get_current_user)):
    ready = install.ready()
    remote_url = config.omnigent_app_url() if config.omnigent_remote_enabled() else ""
    catalog = []
    for agent in agents.load_catalog():
        requires = agent.get("requires", [])
        installed = all(ready.get(r, False) for r in requires)
        # A launch that cannot succeed is not offered. `blocked` separates
        # "this will fail on a stale credential" from "still installing" so the
        # card can say which one it is instead of spinning.
        blocked = agents.launch_block(agent, principal.name) if installed else ""
        # A failed step needs explicit recovery; show a setup retry instead of
        # spinning or trying to launch the broken harness.
        install_error = "" if installed else install.failure_for(requires)
        entry = {
            **{
                k: agent[k] for k in ("id", "label", "description", "icon", "order")
            },
            "ready": installed and not blocked,
            "blocked": blocked,
            "install_error": install_error,
            "needs_credentials": bool(requires),
        }
        if agent.get("id") == "omnigent" and remote_url:
            entry["external_url"] = remote_url
        catalog.append(entry)
    return {"agents": catalog, "credential": credential_manager.status()}


@app.post("/api/agents/{agent_id}/retry-setup")
def retry_agent_setup(agent_id: str, principal: Principal = Depends(get_current_user)):
    agent = agents.get_agent(agent_id)
    if agent is None:
        raise HTTPException(status_code=404, detail="Unknown or unavailable agent")
    # Setup recovery is independent of session switching, credentials and
    # onboarding. In particular, it must not stop an already running harness.
    return {"retrying": install.retry_failed(agent.get("requires", []))}


class CreateSessionBody(BaseModel):
    agent_id: str
    replaces_agent_id: str | None = Field(default=None, max_length=32)
    project_name: str | None = Field(default=None, max_length=63)
    # The wizard, an idea chip or a switch is about to type its own first
    # prompt: never add the workshop opener on top of it.
    has_starter_prompt: bool = False


class SelectProjectBody(BaseModel):
    name: str = Field(min_length=1, max_length=63)
    goal: str | None = Field(default=None, max_length=2000)
    new: bool = False


@app.get("/api/projects")
def list_projects(principal: Principal = Depends(get_current_user)):
    from . import build_restore, projects

    try:
        user = user_manager.get(principal.name)
        with user.content_lock:
            value = projects.inventory(user)
            session_manager.update_project(user, value["active"])
    except projects.ProjectError as error:
        raise HTTPException(status_code=503, detail=str(error)) from error

    def prepare() -> None:
        ensure_user_credentials(user)
        user_content.provision(user)

    def accepting() -> bool:
        # Restoring commits into the attendee's builds; once closeout begins the
        # instance is read-only and a restore could only add work to seal.
        try:
            event_emitter._ensure_ready()
            return attendee_status(event_emitter.journal)["accepting_input"]
        except Exception:  # noqa: BLE001 — unknown lifecycle: do not restore
            return False

    # A fresh container (after an Apps restart) brings saved builds back in the
    # background; this response never waits for it.
    restore = build_restore.restorer.observe(user, value, prepare=prepare,
                                             busy=lambda: bool(session_manager.active()),
                                             accepting=accepting)
    if restore is not None:
        value["restore"] = restore
    return value


@app.get("/api/projects/saved")
def saved_projects(principal: Principal = Depends(get_current_user)):
    """List saved Workspace copies. A read, so closeout never waits on it as a writer.

    WT checks saved copies on its own when the build list is empty. As a POST
    that check was an accepted writer, and a refusal (no credentials yet) left
    a failed writer that blocked the seal.
    """
    return _saved_projects(principal)


@app.post("/api/projects/saved")
def list_saved_projects(principal: Principal = Depends(get_current_user)):
    """The same list, for tabs loaded before the read existed."""
    return _saved_projects(principal)


def _saved_projects(principal: Principal):
    from . import projects

    user = user_manager.get(principal.name)
    try:
        ensure_user_credentials(user)
        user_content.provision(user)
        return projects.saved_inventory(user)
    except (projects.ProjectError, user_content.PreparationError) as error:
        raise HTTPException(status_code=503, detail=str(error)) from error
    except (CredentialError, model_policy.ModelPolicyConfigurationError) as error:
        raise HTTPException(status_code=503, detail="Saved builds are waiting for workshop credentials. Reload WT or ask the facilitator to check setup.") from error


@app.post("/api/projects/select")
def select_project(body: SelectProjectBody, principal: Principal = Depends(get_current_user)):
    from . import projects

    user = user_manager.get(principal.name)
    try:
        with user.content_lock:
            current = projects.read(user)["active"]
            if session_manager.active() and (body.new or current != body.name):
                raise HTTPException(status_code=409, detail="Close the current agent before switching builds. Its written files stay in place.")
            value = projects.select(user, body.name, goal=body.goal, new=body.new)
            user_content.provision(user)
            if content_service.advance_to_build_if_default():
                event_hub.publish({"t": "phase", "phase": "build"})
            return value
    except projects.ProjectError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except user_content.PreparationError as error:
        raise HTTPException(status_code=503, detail=str(error)) from error
    except OSError as error:
        raise HTTPException(status_code=503, detail="Build context could not be saved. Your source files are retained; retry after checking workshop storage.") from error


@app.post("/api/projects/{name}/recover")
def recover_project(name: str, principal: Principal = Depends(get_current_user)):
    from . import projects

    user = user_manager.get(principal.name)
    try:
        with user.content_lock:
            if session_manager.active():
                raise HTTPException(status_code=409, detail="Close the current agent before recovering a build. Its written files stay in place.")
            ensure_user_credentials(user)
            user_content.provision(user)
            value = projects.recover(user, name)
            user_content.provision(user)
            return value
    except projects.ProjectError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except user_content.PreparationError as error:
        raise HTTPException(status_code=503, detail=str(error)) from error
    except (CredentialError, model_policy.ModelPolicyConfigurationError) as error:
        raise HTTPException(status_code=503, detail="Recovery is waiting for workshop credentials. Reload WT or ask the facilitator to check setup.") from error


@app.get("/api/sessions")
def list_sessions(principal: Principal = Depends(get_current_user)):
    # Live sessions plus any ended-on-restart ghosts (P1-11), so the attendee
    # sees terminals lost to a server restart instead of a silent blank.
    live = [s.to_dict() for s in session_manager.list_for(principal.name)]
    prior = [
        {
            "id": g.get("id"),
            "agent_id": g.get("agent_id"),
            "label": g.get("label"),
            "project_name": g.get("project_name", ""),
            "created_at": g.get("created_at"),
            "last_activity": g.get("last_activity"),
            "exited": True,
            "exit_reason": g.get("exit_reason", "server_restarted"),
        }
        for g in session_manager.prior_for(principal.name)
    ]
    return {"sessions": live, "prior_sessions": prior}


@app.delete("/api/sessions/prior/{session_id}")
def acknowledge_prior_session(
    session_id: str,
    principal: Principal = Depends(get_current_user),
):
    if not session_manager.acknowledge_prior(principal.name, session_id):
        raise HTTPException(status_code=404, detail="Prior session not found")
    return {"status": "ok"}


@app.post("/api/sessions")
def create_session(
    body: CreateSessionBody, principal: Principal = Depends(get_current_user)
):
    agent = agents.get_agent(body.agent_id)
    if agent is None:
        telemetry.session_create_failed(principal.name, body.agent_id, "unknown_agent")
        raise HTTPException(status_code=404, detail=f"Unknown agent '{body.agent_id}'")

    if config.model_policy_required() and not model_policy.store.snapshot().revision:
        retained = telemetry.session_create_failed(
            principal.name, agent["id"], "model_policy_pending"
        )
        if retained:
            retained_model_policy_pending_refusal()
        raise HTTPException(
            status_code=503,
            detail="Workshop model permissions are still syncing — try again shortly",
        )

    # Refuse an occupied slot before install, credential, or provisioning work.
    # The SessionManager repeats this check atomically at insertion for races.
    # Check the caller's build before returning a same-agent conflict: a stale
    # tab must not focus that agent and deliver a prompt for a different build.
    if body.project_name is not None:
        from . import projects

        try:
            user = user_manager.get(principal.name)
            with user.content_lock:
                if projects.read(user)["active"] != body.project_name:
                    raise HTTPException(status_code=409, detail="The active build changed in another tab. Refresh My builds before opening an agent.")
        except projects.ProjectError as error:
            raise HTTPException(status_code=503, detail=str(error)) from error
    if active := session_manager.active():
        conflict = SessionConflictError(active)
        telemetry.session_create_failed(
            principal.name, agent["id"], "session_conflict", str(conflict)
        )
        raise HTTPException(
            status_code=409,
            detail={
                "code": "session_conflict",
                "message": str(conflict),
                "active_session": conflict.active_session,
            },
        )

    user = user_manager.get(principal.name)
    user.last_seen = user.last_active = time.time()
    if not user.first_seen:
        user.first_seen = time.time()
    user_manager.checkpoint(user)

    # Emergency pause gate first, before credential or provisioning work.
    try:
        spend.check_can_launch(user, agent)
    except spend.SpendBlocked as e:
        telemetry.session_create_failed(
            principal.name,
            agent["id"],
            "agents_paused",
            e.message,
        )
        raise HTTPException(status_code=e.status, detail=e.message)

    requires = agent.get("requires", [])
    ready = install.ready()
    missing = [r for r in requires if not ready.get(r, False)]
    if missing:
        failure = install.failure_for(requires)
        if failure:
            install.retry_failed(requires)
            raise HTTPException(
                status_code=503,
                detail=f"{agent['label']} setup failed ({failure}). Retrying setup; try opening it again shortly.",
            )
        # The installer step that is still pending is the actionable half: an
        # operator sees which dependency is holding the room up, not just that
        # somebody's launch bounced.
        telemetry.session_create_failed(
            principal.name,
            agent["id"],
            "agent_installing",
            f"missing: {','.join(missing)}",
        )
        raise HTTPException(
            status_code=409,
            detail=f"{agent['label']} is still installing — try again in a moment",
        )

    # Refuse rather than fail: every Omnigent session started behind a stale
    # mirror dies with an auth error the attendee cannot act on, and the bare
    # CLIs below are unaffected and still launchable.
    blocked = agents.launch_block(agent, principal.name)
    if blocked:
        # Try to fix it before saying anything: most of the time the mirror is
        # merely behind a token the tab has already delivered, and re-writing it
        # turns a refusal into a launch nobody had to think about.
        selfheal.self_healer.recover(
            principal.name, f"launch {agent['id']}: {blocked}", force=True
        )
        blocked = agents.launch_block(agent, principal.name)
    if blocked:
        telemetry.session_create_failed(
            principal.name, agent["id"], "obo_stale", blocked
        )
        raise HTTPException(
            status_code=503,
            detail=(
                f"{agent['label']} is waiting for your Databricks sign-in to refresh. "
                "Reload this tab; Claude and Codex work in the meantime."
            ),
        )

    # Write/refresh this user's CLI configs from the vended credential. Every
    # supported CLI requires it.
    try:
        ensure_user_credentials(user)
    except model_policy.ModelPolicyConfigurationError as e:
        user.errors += 1
        user_manager.checkpoint(user)
        telemetry.session_create_failed(
            principal.name,
            agent["id"],
            "model_policy_configuration",
            str(e),
        )
        raise HTTPException(
            status_code=503,
            detail=(
                "Workshop model permissions are incomplete — ask the workshop "
                "facilitator"
            ),
        ) from e
    except CredentialError as e:
        user.errors += 1  # P1-14: failed agent launch (no credential)
        user_manager.checkpoint(user)
        telemetry.session_create_failed(
            principal.name, agent["id"], "credential_unavailable", str(e)
        )
        raise HTTPException(status_code=503, detail=str(e))

    # Instructions, subagents, skills links, git identity, workspace-sync hook.
    try:
        user_content.provision(user)
    except user_content.PreparationError as e:
        telemetry.session_create_failed(principal.name, agent["id"], "preparation_failed", str(e))
        raise HTTPException(
            status_code=503,
            detail="Workshop instructions or tools could not be prepared. Try opening this harness again; if it keeps failing, ask your facilitator to check setup.",
        ) from e

    try:
        with user.content_lock, spend.launch_guard(user, agent):
            from . import projects

            if body.project_name is not None and projects.read(user)["active"] != body.project_name:
                raise HTTPException(status_code=409, detail="The active build changed in another tab. Refresh My builds before opening an agent.")
            if projects.read(user)["active"]:
                user_content._write_instructions(user)
            # Every fresh or switched session without a starter prompt gets an
            # opener (meeting opener on first visit, return opener on later
            # sessions, or switch opener when changing agents).
            opener = (
                agents.claim_opener(
                    user,
                    agent,
                    has_starter_prompt=body.has_starter_prompt,
                    replaces_agent_id=body.replaces_agent_id,
                )
                or ""
            )
            try:
                launch_agent = agent
                if agent["id"] == "claude":
                    # A build whose plan is not yet approved opens in Claude Code's
                    # plan mode; an approved build launches in full auto, passed as
                    # a flag since the user settings carry none.
                    from . import build_plan

                    approved = build_plan.approved_for_active(
                        user.home, projects.read(user)["active"], projects.root(user)
                    )
                    launch_agent = agents.with_launch_flags(
                        agent,
                        agents.claude_plan_flags(config.auto_mode_enabled())
                        if not approved
                        else agents.claude_auto_flags(config.auto_mode_enabled()),
                    )
                launched = agents.with_first_message(launch_agent, opener)
                session = session_manager.create(user, agent["id"], agents.launch_command(launched),
                                                 agent["label"])
            except BaseException:
                if opener == agents.WORKSHOP_OPENER:
                    agents.release_opener(user)
                raise
    except spend.SpendBlocked as e:
        telemetry.session_create_failed(
            principal.name, agent["id"], "agents_paused", e.message
        )
        raise HTTPException(status_code=e.status, detail=e.message)
    except SessionConflictError as e:
        telemetry.session_create_failed(
            principal.name, agent["id"], "session_conflict", str(e)
        )
        raise HTTPException(
            status_code=409,
            detail={
                "code": "session_conflict",
                "message": str(e),
                "active_session": e.active_session,
            },
        )
    except SessionConfigurationError as e:
        telemetry.session_create_failed(
            principal.name, agent["id"], "session_configuration", str(e)
        )
        raise HTTPException(status_code=503, detail=str(e))
    except projects.ProjectError as e:
        raise HTTPException(status_code=503, detail=str(e)) from e
    user.sessions_launched[agent["id"]] = user.sessions_launched.get(agent["id"], 0) + 1
    user_manager.checkpoint(user)
    if content_service.advance_to_build_if_default():
        event_hub.publish({"t": "phase", "phase": "build"})
    telemetry.session_started(principal.name, agent["id"], session.id)
    entitlement_manager.wake("session_start")
    if body.replaces_agent_id and body.replaces_agent_id != agent["id"]:
        telemetry.session_switched(
            principal.name,
            body.replaces_agent_id,
            agent["id"],
            session.id,
        )
    # Record which principal each CLI surface resolves to on each plane, so a
    # resource created during this session can be attributed later. Backgrounded
    # and TTL'd — it must never be in the launch path.
    identity.observe(user)
    return {"session": session.to_dict()}


class TypeBody(BaseModel):
    text: str = Field(max_length=12000)
    delivery_id: str = Field(default="", max_length=160)


@app.post("/api/sessions/{session_id}/type")
def type_into_session(
    session_id: str, body: TypeBody, principal: Principal = Depends(get_current_user)
):
    """Type text into the attendee's own PTY, UNSENT — the visible, user-
    initiated channel for ideation chips and insight-card prompts. The
    attendee presses Enter; we never submit on their behalf."""
    session = session_manager.get(session_id, principal.name)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")
    # Flatten line breaks to keep the prompt UNSENT. Reject oversized requests
    # in the schema rather than reporting success after silently cutting a task.
    text = body.text.replace("\n", " ").replace("\r", " ")
    if not text.strip():
        raise HTTPException(status_code=422, detail="Nothing to type")
    try:
        if body.delivery_id:
            return session.deliver_prompt(text, body.delivery_id)
        session.write_input(text)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except OSError:
        raise HTTPException(status_code=409, detail="Session is not accepting input")
    import hashlib

    return {"status": "ok", "typed_characters": len(text),
            "typed_sha256": hashlib.sha256(text.encode()).hexdigest()}


@app.delete("/api/sessions/{session_id}")
def close_session(session_id: str, principal: Principal = Depends(get_current_user)):
    session = session_manager.get(session_id, principal.name)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")
    session_manager.terminate(session)
    return {"status": "ok"}


class AttendeeErrorBody(BaseModel):
    code: str
    detail: str = ""
    agent_id: str = ""
    session_id: str = ""


@app.post("/api/telemetry/error")
def report_attendee_error(
    body: AttendeeErrorBody, principal: Principal = Depends(get_current_user)
):
    """The attendee's browser reporting what it just put on their screen.

    The one signal nothing server-side can produce, and the one that closes the
    loop the incident exposed: it proves the code the attendee saw and the
    diagnostics an operator reads describe the same moment. Fire-and-forget by
    design — the UI must never depend on the reply.
    """
    captured = telemetry.attendee_error_seen(
        principal.name,
        body.code,
        {
            "detail": body.detail[:300],
            "agent": body.agent_id[:64],
            "session_id": body.session_id[:64],
        },
    )
    if not captured:
        raise HTTPException(status_code=503, detail=UNAVAILABLE_DETAIL)
    # An error on the attendee's screen is also the earliest arriving signal we
    # get. `retry` tells the browser whether trying again is worth it, so the
    # attendee is not asked to reload for something already fixed.
    healed = {"attempted": False}
    if selfheal.is_auth_error(body.code, body.detail):
        healed = selfheal.self_healer.recover(
            principal.name, f"attendee saw {body.code}"[:120]
        )
    return {"status": "ok", "retry": bool(healed.get("credential_fresh"))}


@app.get("/api/nuggets")
def get_nuggets(principal: Principal = Depends(get_current_user)):
    from . import artifacts
    from .content import TOPIC_TTL_SECONDS

    now = time.time()
    triggers: set[str] = set()
    sessions = session_manager.list_for(principal.name)
    for session in sessions:
        triggers.add(f"{session.agent_id}_active")
    user = user_manager.peek(principal.name)
    live_topics: set[str] = set()
    idle_minutes = 0.0
    has_building_activity = bool(sessions)
    if user:
        if config.topic_detection_enabled():
            # Scan new agent transcript tool targets periodically (covers Claude, Codex, Omnigent)
            last_scan = _transcript_topic_scan_at.get(principal.name, 0.0)
            if now - last_scan >= 20.0:
                try:
                    _transcript_topic_scan_at[principal.name] = now
                    cursors = getattr(user, "_transcript_topic_cursors", None)
                    if cursors is None:
                        cursors = {}
                        user._transcript_topic_cursors = cursors
                    new_targets = artifacts.scan_new_tool_targets(user.home, cursors)
                    if new_targets:
                        # Agent tool calls, Omnigent workers' included. An
                        # Omni-only seat has no WT session, so without this it
                        # stayed in intro until wrap.
                        user.last_active = now
                        has_building_activity = True
                        for topic in content_service.scan_topics(
                            "\n".join(new_targets)
                        ):
                            user.topics[topic] = now
                            user.topic_hits[topic] = (
                                user.topic_hits.get(topic, 0) + 1
                            )
                except Exception:  # noqa: BLE001
                    pass

            live_topics = {
                t for t, at in user.topics.items() if now - at < TOPIC_TTL_SECONDS
            }

        # True idle = no input AND no agent output. ``last_io`` covers both
        # (the reader thread sets it on output), so an agent working away on the
        # attendee's behalf never counts as idle. Keepalive pings are not
        # activity: they refresh ``last_seen`` and ``last_activity`` every 20 s,
        # which held idle at zero for as long as the tab was open.
        signals = [s.last_io for s in sessions]
        signals.append(user.last_active or user.first_seen)
        signals = [at for at in signals if at]
        if signals:
            idle_minutes = max(0.0, (now - max(signals)) / 60)

    if has_building_activity and content_service.advance_to_build_if_default():
        try:
            event_hub.publish({"t": "phase", "phase": "build"})
        except Exception:  # noqa: BLE001
            pass
    effective_phase = content_service.phase

    return {
        "phase": effective_phase,
        "nuggets": content_service.nuggets_for(
            triggers,
            live_topics,
            idle_minutes,
            phase_override=effective_phase,
            topic_recency=dict(user.topics) if user else None,
        ),
        "prompts": content_service.prompts_for_phase(phase_override=effective_phase),
    }


@app.get("/api/certificate")
def certificate(name: str, principal: Principal = Depends(get_current_user)):
    """Brag certificate PDF — downloads straight to the attendee's laptop."""
    from fastapi.responses import Response

    from . import attendee_names
    from . import certificate as cert
    from . import stats

    display_name = attendee_names.clean_name(name)
    if not display_name:
        raise HTTPException(status_code=422, detail="A display name is required")
    user = user_manager.get(principal.name)
    # The strongest name evidence this system gets: nobody types someone else's
    # name onto a certificate they are about to show people.
    attendee_names.capture(
        principal.name, user.home, display_name, attendee_names.SOURCE_CERTIFICATE
    )
    pdf = cert.build_pdf(display_name, stats.gather(user))
    filename = "databricks-workshop-certificate.pdf"
    return Response(
        content=pdf,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/healthz")
def healthz():
    return {"status": "ok"}


@app.get("/readyz")
def readyz():
    started_at = time.monotonic()
    report = readiness.evaluate_runtime()
    telemetry.readiness_result(report, started_at)
    return JSONResponse(report, status_code=200 if report["ready"] else 503)


# ---- static frontend (committed Vite build) ----

if os.path.isdir(os.path.join(_STATIC_DIR, "assets")):
    app.mount(
        "/assets",
        StaticFiles(directory=os.path.join(_STATIC_DIR, "assets")),
        name="assets",
    )


@app.get("/{path:path}")
async def spa(path: str):
    candidate = os.path.normpath(os.path.join(_STATIC_DIR, path))
    if path and candidate.startswith(_STATIC_DIR) and os.path.isfile(candidate):
        return FileResponse(candidate)
    index = os.path.join(_STATIC_DIR, "index.html")
    if os.path.isfile(index):
        return FileResponse(index)
    return JSONResponse(
        {"error": "frontend build missing — run make build-frontend"}, status_code=503
    )
