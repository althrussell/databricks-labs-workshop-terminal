"""Operator/steering API — gated by ADMIN_GROUP membership (platform_admins).

Control Tower (or scripts/push_content.py, or the in-app operator panel)
drives the live workshop through these endpoints. See docs/admin-api.md
for the contract.
"""

from __future__ import annotations

import time

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from . import config, obo, spend
from .auth import require_admin
from .content import Broadcast, ContentPack, content_service
from .credentials import credential_manager
from .entitlements import entitlement_manager
from .events import event_hub
from .sessions import session_manager
from .users import user_manager

router = APIRouter(prefix="/api/admin", dependencies=[Depends(require_admin)])


class PhaseBody(BaseModel):
    phase: str


@router.get("/state")
def admin_state():
    pack = content_service.pack
    return {
        "phase": content_service.phase,
        "phases": pack.phases,
        "nugget_count": len(pack.nuggets),
        "broadcast": (b.model_dump() if (b := content_service.active_broadcast()) else None),
        "started_at": content_service.started_at,
    }


class AgentControlBody(BaseModel):
    enabled: bool


@router.get("/agent-controls")
def agent_controls():
    """P1-16: kill-switch state + per-attendee LLM-agent spend metering.

    Lets an operator see who's consuming agent sessions and pause new launches
    fleet-wide if spend runs hot. Bash sessions are free and excluded.
    """
    return {
        "agents_enabled": spend.agents_enabled(),
        "max_agent_launches_per_user": config.max_agent_launches_per_user(),
        "attendees": sorted(
            (spend.metering(u) for u in user_manager.all()),
            key=lambda m: m["agent_launches"],
            reverse=True,
        ),
    }


@router.post("/agent-controls")
def set_agent_controls(body: AgentControlBody):
    """Operator kill-switch: pause (``enabled=false``) or resume new LLM-agent
    launches across the whole instance, effective immediately."""
    spend.set_kill_switch(killed=not body.enabled)
    return {"agents_enabled": spend.agents_enabled()}


@router.post("/content-pack")
def set_content_pack(pack: ContentPack):
    content_service.set_pack(pack)
    event_hub.publish({"t": "content_updated"})
    return {"status": "ok", "nuggets": len(pack.nuggets), "phases": pack.phases}


@router.post("/phase")
def set_phase(body: PhaseBody):
    if body.phase not in content_service.pack.phases:
        raise HTTPException(
            status_code=422,
            detail=f"Unknown phase '{body.phase}' — pack defines {content_service.pack.phases}",
        )
    content_service.set_phase(body.phase)
    event_hub.publish({"t": "phase", "phase": body.phase})
    return {"status": "ok", "phase": body.phase}


@router.post("/broadcast")
def broadcast(body: Broadcast):
    content_service.set_broadcast(body)
    event_hub.publish({"t": "broadcast", **body.model_dump()})
    return {"status": "ok"}


@router.get("/stats")
def harvest_stats():
    """Harvest endpoint for Control Tower: per-attendee build stats (cached
    code stats) + one instance-level workspace census + instance meta.
    Persisted into CT's Lakebase for durable event-impact reporting."""
    from . import stats

    payload = stats.gather_all(user_manager.all())
    payload["instance"] = {
        "phase": content_service.phase,
        "started_at": content_service.started_at,
        "session_count": session_manager.count_all(),
    }
    return payload


@router.get("/presence")
def presence():
    now = time.time()
    users = []
    for user in user_manager.all():
        sessions = session_manager.list_for(user.email)
        users.append({
            "email": user.email,
            "online": (now - user.last_seen) < 60 if user.last_seen else False,
            "last_seen": user.last_seen,
            "first_seen": user.first_seen,
            "cli_ready": bool(user.cli_ready),
            "obo": obo.obo_manager.status(user.email),
            "sessions": [s.to_dict() for s in sessions],
        })
    return {
        "users": sorted(users, key=lambda u: u["email"]),
        "session_count": session_manager.count_all(),
        "credential": credential_manager.status(),
        "entitlements": entitlement_manager.status(),
    }
