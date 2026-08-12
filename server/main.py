"""Workshop Terminal — multi-user coding-agent workbench for Databricks
training events.

Single uvicorn worker only: PTY fds, sessions, users, and content state are
process-local.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from contextlib import asynccontextmanager

import asyncio

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import (
    agents,
    config,
    help as help_module,
    identity,
    models,
    obo,
    operational,
    readiness,
    selfheal,
    spend,
    telemetry,
    user_content,
)
from .admin import router as admin_router
from .auth import Principal, get_current_user, is_admin
from .bootstrap import install
from .content import content_service
from .credentials import (
    CredentialError,
    credential_manager,
    ensure_user_credentials,
    initialize_app_identity,
)
from .entitlements import entitlement_manager
from .event_emitter import event_emitter, flush_loop
from .events import event_hub
from .log_collector import install_app_error_journal, log_collector
from .omnigent_remote import remote_host_manager
from .sessions import SessionLimitError, session_manager
from .users import user_manager
from .ws import router as ws_router

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
logger = logging.getLogger("workshop-terminal")

_STATIC_DIR = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "static"))


def _observe_output(session, chunk: str) -> None:
    """Spot content-pack topics in terminal output to drive contextual
    insights. Only topic names are recorded — never the text itself."""
    if not config.topic_detection_enabled():
        return
    topics = content_service.scan_topics(chunk)
    if not topics:
        return
    user = user_manager.peek(session.owner_email)
    if user:
        now = time.time()
        for topic in topics:
            user.topics[topic] = now
            user.topic_hits[topic] = user.topic_hits.get(topic, 0) + 1


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


@asynccontextmanager
async def lifespan(app: FastAPI):
    loop = asyncio.get_running_loop()
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
    if config.insight_capture_enabled():
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
            initialize_app_identity()
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
                event_emitter.stream_id,
            )
        else:
            logger.info("LOCAL_DEV=1 — skipping CLI installers and credential rotation")
        from . import topology

        topo_warning = topology.startup_warning()
        if topo_warning:
            logger.warning("topology: %s", topo_warning)
        logger.info("workshop terminal up (phase=%s)", content_service.phase)
        yield
    finally:
        if emitter_stop is not None:
            _stop_control_tower_threads(emitter_stop, emitter_threads)
        log_collector.stop()
        obo.obo_watcher.stop()
        remote_host_manager.stop()
        entitlement_manager.stop()


app = FastAPI(title="Databricks Workshop Terminal", lifespan=lifespan)
app.include_router(admin_router)
app.include_router(ws_router)


@app.middleware("http")
async def record_operational_http_status(request: Request, call_next):
    response = await call_next(request)
    try:
        operational.metrics.record_http(response.status_code)
    except Exception:  # noqa: BLE001 - telemetry must never break responses
        pass
    return response


# ---- public API (attendee-scoped) ----

@app.get("/api/config")
def get_config(principal: Principal = Depends(get_current_user)):
    pack = content_service.pack
    return {
        "user": {"email": principal.name, "is_admin": is_admin(principal)},
        "branding": config.branding(),
        "workspace_url": config.databricks_host(),
        "shell": pack.shell.model_dump(),
        "phase": content_service.phase,
        "broadcast": (b.model_dump() if (b := content_service.active_broadcast()) else None),
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
            os.environ,
        ),
        "omnigent_remote": {
            "enabled": config.omnigent_remote_enabled(),
            "url": config.omnigent_app_url(),
        },
        "entitlements": entitlement_manager.status(),
        # The model-comparison exercise, as the attendee runs it. Published from
        # the same resolution the generated Codex config uses, so what the UI
        # offers and what `codex --profile <name>` finds cannot drift apart.
        "model_comparison": model_comparison(),
        "help": help_module.snapshot(),
    }


def model_comparison() -> list[dict]:
    """Comparison models this deployment will actually serve, in a fixed order.

    Carries the endpoint rather than a command to run. These models answer only
    on chat-completions, and codex-cli 0.144.6 removed that wire, so there is no
    harness command here we can promise works — see server/models for the whole
    story. Publishing the resolved set without a command keeps the UI honest
    instead of printing an invocation that exits non-zero in front of a room.
    """
    resolved = models.comparison_models()
    return [
        {
            "profile": name,
            "model": resolved[name],
            "label": label,
            "endpoint": f"{config.databricks_host()}/serving-endpoints/{resolved[name]}/invocations",
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


def _callback_identity(body: _EmailBody, request: Request) -> Principal:
    """Authenticate a browser proxy call or attendee helper capability."""
    capability = (request.headers.get("x-workshop-capability") or "").strip()
    if capability:
        email = (body.email or "").strip().lower()
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
        raise HTTPException(status_code=403, detail="callback email does not match caller")
    return principal


@app.post("/api/obo/refresh")
def obo_refresh(body: _EmailBody, request: Request):
    """Reactive OBO self-heal (layer 2). Called by the ``databricks-me`` wrapper
    on a 401/403: captures a fresh forwarded token if the caller is the browser,
    force-writes the latest captured token to the ``me`` profile, and nudges any
    connected tab to deliver an even fresher token on its next poll."""
    principal = _callback_identity(body, request)
    email = principal.name
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
    result = selfheal.self_healer.recover(principal.name, "attendee pressed Recover", force=True)
    return {
        "recovered": bool(result.get("credential_fresh")),
        "actions": result.get("actions", []),
        "obo": obo.obo_manager.status(principal.name),
    }


@app.post("/api/entitlements/reconcile")
def reconcile_entitlements(body: _EmailBody, request: Request):
    """On-demand entitlement reconcile (the ``workshop-grant-me`` path): makes a
    just-created app/job/Lakebase instance usable by the labuser immediately
    instead of waiting for the next sweep."""
    principal = _callback_identity(body, request)
    return entitlement_manager.reconcile(email=principal.name)


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
    raw = body.model_dump(exclude={"email"})
    stored = discovery.record(principal.name, raw)
    if stored is None:
        # Withdrawn by the attendee, or the per-attendee cap is reached. Both are
        # deliberate refusals, not failures, and neither is the agent's business.
        return {"captured": False, "reason": "not_stored"}
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
        "records": [r.payload() for r in discovery.discovery_store.for_attendee(principal.name)],
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
    what_building: str = ""
    industry: str = ""
    intent: str = ""
    idea_id: str = ""
    current_stack: list[str] = Field(default_factory=list)
    persona: str = ""
    skipped: bool = False


@app.get("/api/wizard")
def get_wizard(industry: str = "", principal: Principal = Depends(get_current_user)):
    """Wizard state: whether to show it, and the ideas to show if so.

    Ideas are selected server-side because only the server can see which demo
    tables actually exist, and a card whose data was never seeded must never
    reach the grid. ``industry`` re-filters that grid for someone browsing before
    they have committed to anything, so it is a query parameter rather than
    something read back off the saved brief.
    """
    from . import wizard
    from .users import user_manager

    return wizard.state(user_manager.get(principal.name), industry)


@app.post("/api/wizard")
def save_wizard(body: _WizardBody, principal: Principal = Depends(get_current_user)):
    """Save what the attendee told the wizard, and hand back the first prompt.

    Returns the starter prompt rather than leaving the frontend to assemble it:
    a chosen idea card carries a prompt written to produce a good first build,
    and that text belongs with the catalogue rather than in the UI.
    """
    from . import user_content, wizard
    from .users import user_manager

    user = user_manager.get(principal.name)
    # Only the keys the attendee actually sent: the dismissal path posts nothing
    # but ``skipped``, and the defaults on the model would otherwise read as an
    # instruction to blank every answer they had already given.
    brief = wizard.save(user, body.model_dump(exclude_unset=True))
    # The brief is inlined into the agent's instructions, so they have to be
    # rewritten now. Sessions are almost always launched from the wizard's last
    # step, after this call, so the first agent to start already has it.
    user_content.set_wizard_brief(user, brief)
    return {
        "brief": brief.to_json(),
        "starter_prompt": wizard.starter_prompt(brief),
    }


@app.get("/api/agents")
def list_agents(principal: Principal = Depends(get_current_user)):
    ready = install.ready()
    catalog = []
    for agent in agents.load_catalog():
        requires = agent.get("requires", [])
        installed = all(ready.get(r, False) for r in requires)
        # A launch that cannot succeed is not offered. `blocked` separates
        # "this will fail on a stale credential" from "still installing" so the
        # card can say which one it is instead of spinning.
        blocked = agents.launch_block(agent, principal.name) if installed else ""
        # An install step that ended badly never retries, so the card would spin
        # for the rest of the workshop. Say what failed instead.
        install_error = "" if installed else install.failure_for(requires)
        catalog.append({
            **{k: agent[k] for k in ("id", "label", "description", "icon", "order")},
            "ready": installed and not blocked,
            "blocked": blocked,
            "install_error": install_error,
            "needs_credentials": bool(requires),
        })
    return {"agents": catalog, "credential": credential_manager.status()}


class CreateSessionBody(BaseModel):
    agent_id: str = "bash"


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
def create_session(body: CreateSessionBody, principal: Principal = Depends(get_current_user)):
    agent = agents.get_agent(body.agent_id)
    if agent is None:
        telemetry.session_create_failed(principal.name, body.agent_id, "unknown_agent")
        raise HTTPException(status_code=404, detail=f"Unknown agent '{body.agent_id}'")

    user = user_manager.get(principal.name)
    user.last_seen = time.time()
    if not user.first_seen:
        user.first_seen = time.time()

    # P1-16: operator kill-switch + per-attendee budget for LLM agents (bash is
    # always free). Gate first — a paused or over-budget attendee gets a clear,
    # cheap refusal before any readiness/credential/provision work, and isn't
    # told an agent is "still installing" when it's actually paused.
    try:
        spend.check_can_launch(user, agent)
    except spend.SpendBlocked as e:
        telemetry.session_create_failed(
            principal.name,
            agent["id"],
            "agents_paused" if e.status == 403 else "agent_budget_exhausted",
            e.message,
        )
        raise HTTPException(status_code=e.status, detail=e.message)

    requires = agent.get("requires", [])
    ready = install.ready()
    missing = [r for r in requires if not ready.get(r, False)]
    if missing:
        # The installer step that is still pending is the actionable half: an
        # operator sees which dependency is holding the room up, not just that
        # somebody's launch bounced.
        telemetry.session_create_failed(
            principal.name, agent["id"], "agent_installing", f"missing: {','.join(missing)}"
        )
        raise HTTPException(status_code=409, detail=f"{agent['label']} is still installing — try again in a moment")

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
        telemetry.session_create_failed(principal.name, agent["id"], "obo_stale", blocked)
        raise HTTPException(
            status_code=503,
            detail=(
                f"{agent['label']} is waiting for your Databricks sign-in to refresh. "
                "Reload this tab; Claude, Codex and Terminal work in the meantime."
            ),
        )

    # Write/refresh this user's CLI configs from the vended credential. Agent
    # CLIs hard-require it; bash degrades gracefully (shell works, databricks
    # CLI just isn't authenticated until the credential is configured).
    try:
        ensure_user_credentials(user)
    except CredentialError as e:
        if requires:
            user.errors += 1  # P1-14: failed agent launch (no credential)
            telemetry.session_create_failed(
                principal.name, agent["id"], "credential_unavailable", str(e)
            )
            raise HTTPException(status_code=503, detail=str(e))
        logger.warning("bash session for %s without credentials: %s", principal.name, e)

    # Instructions, subagents, skills links, git identity, workspace-sync hook.
    user_content.provision(user)

    try:
        session = session_manager.create(
            user, agent["id"], agents.launch_command(agent), agent["label"],
        )
    except SessionLimitError as e:
        telemetry.session_create_failed(
            principal.name, agent["id"], "session_limit", str(e)
        )
        raise HTTPException(status_code=429, detail=str(e))
    user.sessions_launched[agent["id"]] = user.sessions_launched.get(agent["id"], 0) + 1
    # C3b: emit a session.started event (no-op unless CT ingest is configured).
    event_emitter.emit("session.started", principal.name, {"agent": agent["id"]})
    # Record which principal each CLI surface resolves to on each plane, so a
    # resource created during this session can be attributed later. Backgrounded
    # and TTL'd — it must never be in the launch path.
    identity.observe(user)
    return {"session": session.to_dict()}


class TypeBody(BaseModel):
    text: str


@app.post("/api/sessions/{session_id}/type")
def type_into_session(session_id: str, body: TypeBody,
                      principal: Principal = Depends(get_current_user)):
    """Type text into the attendee's own PTY, UNSENT — the visible, user-
    initiated channel for ideation chips and insight-card prompts. The
    attendee presses Enter; we never submit on their behalf."""
    session = session_manager.get(session_id, principal.name)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")
    text = body.text.replace("\n", " ").replace("\r", " ")[:500]
    if not text.strip():
        raise HTTPException(status_code=422, detail="Nothing to type")
    try:
        session.write_input(text)
    except OSError:
        raise HTTPException(status_code=409, detail="Session is not accepting input")
    return {"status": "ok"}


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
    telemetry.attendee_error_seen(
        principal.name,
        body.code,
        {
            "detail": body.detail[:300],
            "agent": body.agent_id[:64],
            "session_id": body.session_id[:64],
        },
    )
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
    from .content import TOPIC_TTL_SECONDS

    now = time.time()
    triggers: set[str] = set()
    sessions = session_manager.list_for(principal.name)
    for session in sessions:
        triggers.add(f"{session.agent_id}_active")
    user = user_manager.peek(principal.name)
    live_topics: set[str] = set()
    idle_minutes = 0.0
    if user:
        live_topics = {t for t, at in user.topics.items() if now - at < TOPIC_TTL_SECONDS}
        # True idle = no input AND no terminal output. Session activity covers
        # both (the reader thread touches it on output), so an agent working
        # away on the attendee's behalf never counts as idle.
        signals = [s.last_activity for s in sessions]
        if user.last_seen:
            signals.append(user.last_seen)
        if signals:
            idle_minutes = max(0.0, (now - max(signals)) / 60)
    return {
        "phase": content_service.phase,
        "nuggets": content_service.nuggets_for(triggers, live_topics, idle_minutes),
        "prompts": content_service.prompts_for_phase(),
    }


@app.get("/api/certificate")
def certificate(name: str, principal: Principal = Depends(get_current_user)):
    """Brag certificate PDF — downloads straight to the attendee's laptop."""
    from fastapi.responses import Response

    from . import certificate as cert
    from . import stats

    display_name = " ".join(name.split())[:60]
    if not display_name:
        raise HTTPException(status_code=422, detail="A display name is required")
    user = user_manager.get(principal.name)
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
    report = readiness.evaluate_runtime()
    return JSONResponse(report, status_code=200 if report["ready"] else 503)


# ---- static frontend (committed Vite build) ----

if os.path.isdir(os.path.join(_STATIC_DIR, "assets")):
    app.mount("/assets", StaticFiles(directory=os.path.join(_STATIC_DIR, "assets")), name="assets")


@app.get("/{path:path}")
async def spa(path: str):
    candidate = os.path.normpath(os.path.join(_STATIC_DIR, path))
    if path and candidate.startswith(_STATIC_DIR) and os.path.isfile(candidate):
        return FileResponse(candidate)
    index = os.path.join(_STATIC_DIR, "index.html")
    if os.path.isfile(index):
        return FileResponse(index)
    return JSONResponse({"error": "frontend build missing — run make build-frontend"}, status_code=503)
