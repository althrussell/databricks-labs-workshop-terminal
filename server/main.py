"""Workshop Terminal — multi-user coding-agent workbench for Databricks
training events.

Single uvicorn worker only: PTY fds, sessions, users, and content state are
process-local.
"""

from __future__ import annotations

import logging
import os
import time
from contextlib import asynccontextmanager

import asyncio

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import agents, config, obo, spend, user_content
from .admin import router as admin_router
from .auth import Principal, get_current_user, is_admin
from .bootstrap import install
from .content import content_service
from .credentials import CredentialError, credential_manager, ensure_user_credentials
from .entitlements import entitlement_manager
from .event_emitter import event_emitter, flush_loop
from .events import event_hub
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


@asynccontextmanager
async def lifespan(app: FastAPI):
    loop = asyncio.get_running_loop()
    # P1-11: if a state path is configured, attach the metadata journal so
    # sessions survive a restart as surfaced ghosts (the live PTYs cannot).
    state_path = config.session_state_path()
    if state_path:
        from .session_store import SessionMetadataStore

        session_manager.configure_store(SessionMetadataStore(state_path))
    session_manager.attach_loop(loop)
    session_manager.output_observer = _observe_output
    event_hub.attach_loop(loop)
    os.makedirs(config.users_root(), exist_ok=True)
    emitter_stop = None
    if not config.local_dev():
        install.run_in_background()
        credential_manager.start()
        # SP-driven entitlement reconciler: keeps the labuser able to use the
        # resources the app SP creates (no-op unless ENABLE_ENTITLEMENTS).
        entitlement_manager.start()
        # C3b: background flusher pushes buffered attendee events to Control
        # Tower. Only starts when CT ingest is configured (emitter.enabled).
        if event_emitter.enabled:
            import threading

            emitter_stop = threading.Event()
            threading.Thread(
                target=flush_loop, args=(event_emitter, emitter_stop),
                daemon=True, name="event-emitter-flush",
            ).start()
            logger.info("event emitter started (run_id=%s)", event_emitter.run_id)
    else:
        logger.info("LOCAL_DEV=1 — skipping CLI installers and credential rotation")
    from . import topology

    topo_warning = topology.startup_warning()
    if topo_warning:
        logger.warning("topology: %s", topo_warning)
    logger.info("workshop terminal up (phase=%s)", content_service.phase)
    yield
    if emitter_stop is not None:
        emitter_stop.set()
    entitlement_manager.stop()


app = FastAPI(title="Databricks Workshop Terminal", lifespan=lifespan)
app.include_router(admin_router)
app.include_router(ws_router)


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
        "entitlements": entitlement_manager.status(),
    }


@app.get("/api/setup-status")
def setup_status(_: Principal = Depends(get_current_user)):
    return install.status()


class _EmailBody(BaseModel):
    # Helpers running inside the attendee PTY have no proxy identity headers, so
    # they pass their own $WORKSHOP_USER_EMAIL. A browser caller carries the
    # forwarded headers instead and may omit this.
    email: str | None = None


@app.post("/api/obo/refresh")
def obo_refresh(body: _EmailBody, request: Request):
    """Reactive OBO self-heal (layer 2). Called by the ``databricks-me`` wrapper
    on a 401/403: captures a fresh forwarded token if the caller is the browser,
    force-writes the latest captured token to the ``me`` profile, and nudges any
    connected tab to deliver an even fresher token on its next poll."""
    email = (body.email or request.headers.get("x-forwarded-email") or "").strip().lower()
    if not email:
        raise HTTPException(status_code=422, detail="email required")
    token = (request.headers.get("x-forwarded-access-token") or "").strip()
    if token:
        obo.obo_manager.capture(email, token)
    written = obo.obo_manager.force_refresh(email)
    try:
        event_hub.publish({"t": "obo_refresh", "email": email})
    except Exception:  # noqa: BLE001 — nudge is best-effort
        pass
    return {"written": written, **obo.obo_manager.status(email)}


@app.post("/api/entitlements/reconcile")
def reconcile_entitlements(body: _EmailBody, request: Request):
    """On-demand entitlement reconcile (the ``workshop-grant-me`` path): makes a
    just-created app/job/Lakebase instance usable by the labuser immediately
    instead of waiting for the next sweep."""
    email = (body.email or request.headers.get("x-forwarded-email") or "").strip().lower() or None
    return entitlement_manager.reconcile(email=email)


@app.get("/api/agents")
def list_agents(_: Principal = Depends(get_current_user)):
    ready = install.status()["ready"]
    catalog = []
    for agent in agents.load_catalog():
        requires = agent.get("requires", [])
        installed = all(ready.get(r, False) for r in requires)
        catalog.append({
            **{k: agent[k] for k in ("id", "label", "description", "icon", "order")},
            "ready": installed,
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


@app.post("/api/sessions")
def create_session(body: CreateSessionBody, principal: Principal = Depends(get_current_user)):
    agent = agents.get_agent(body.agent_id)
    if agent is None:
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
        raise HTTPException(status_code=e.status, detail=e.message)

    requires = agent.get("requires", [])
    ready = install.status()["ready"]
    missing = [r for r in requires if not ready.get(r, False)]
    if missing:
        raise HTTPException(status_code=409, detail=f"{agent['label']} is still installing — try again in a moment")

    # Write/refresh this user's CLI configs from the vended credential. Agent
    # CLIs hard-require it; bash degrades gracefully (shell works, databricks
    # CLI just isn't authenticated until the credential is configured).
    try:
        ensure_user_credentials(user)
    except CredentialError as e:
        if requires:
            user.errors += 1  # P1-14: failed agent launch (no credential)
            raise HTTPException(status_code=503, detail=str(e))
        logger.warning("bash session for %s without credentials: %s", principal.name, e)

    # Instructions, subagents, skills links, git identity, workspace-sync hook.
    user_content.provision(user)

    try:
        session = session_manager.create(
            user, agent["id"], agents.launch_command(agent), agent["label"],
        )
    except SessionLimitError as e:
        raise HTTPException(status_code=429, detail=str(e))
    user.sessions_launched[agent["id"]] = user.sessions_launched.get(agent["id"], 0) + 1
    # C3b: emit a session.started event (no-op unless CT ingest is configured).
    event_emitter.emit("session.started", principal.name, {"agent": agent["id"]})
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
