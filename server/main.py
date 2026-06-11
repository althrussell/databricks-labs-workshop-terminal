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

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import agents, config, user_content
from .admin import router as admin_router
from .auth import Principal, get_current_user, is_admin
from .bootstrap import install
from .content import content_service
from .credentials import CredentialError, credential_manager, ensure_user_credentials
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
    session_manager.attach_loop(loop)
    session_manager.output_observer = _observe_output
    event_hub.attach_loop(loop)
    os.makedirs(config.users_root(), exist_ok=True)
    if not config.local_dev():
        install.run_in_background()
        credential_manager.start()
    else:
        logger.info("LOCAL_DEV=1 — skipping CLI installers and credential rotation")
    logger.info("workshop terminal up (phase=%s)", content_service.phase)
    yield


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
        "shell": pack.shell.model_dump(),
        "phase": content_service.phase,
        "broadcast": (b.model_dump() if (b := content_service.active_broadcast()) else None),
        "limits": {
            "max_sessions_per_user": config.max_sessions_per_user(),
        },
        "credential": credential_manager.status(),
    }


@app.get("/api/setup-status")
def setup_status(_: Principal = Depends(get_current_user)):
    return install.status()


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
    return {"sessions": [s.to_dict() for s in session_manager.list_for(principal.name)]}


@app.post("/api/sessions")
def create_session(body: CreateSessionBody, principal: Principal = Depends(get_current_user)):
    agent = agents.get_agent(body.agent_id)
    if agent is None:
        raise HTTPException(status_code=404, detail=f"Unknown agent '{body.agent_id}'")

    requires = agent.get("requires", [])
    ready = install.status()["ready"]
    missing = [r for r in requires if not ready.get(r, False)]
    if missing:
        raise HTTPException(status_code=409, detail=f"{agent['label']} is still installing — try again in a moment")

    user = user_manager.get(principal.name)
    user.last_seen = time.time()
    if not user.first_seen:
        user.first_seen = time.time()

    # Write/refresh this user's CLI configs from the vended credential. Agent
    # CLIs hard-require it; bash degrades gracefully (shell works, databricks
    # CLI just isn't authenticated until the credential is configured).
    try:
        ensure_user_credentials(user)
    except CredentialError as e:
        if requires:
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
    return {"session": session.to_dict()}


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
    if user:
        if user.last_seen and now - user.last_seen > 600:
            triggers.add("idle_10m")
        live_topics = {t for t, at in user.topics.items() if now - at < TOPIC_TTL_SECONDS}
    return {
        "phase": content_service.phase,
        "nuggets": content_service.nuggets_for(triggers, live_topics),
    }


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
