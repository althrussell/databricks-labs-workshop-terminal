"""Operator/steering API — gated by ADMIN_GROUP membership (platform_admins).

Control Tower (or scripts/push_content.py, or the in-app operator panel)
drives the live workshop through these endpoints. See docs/admin-api.md
for the contract.
"""

from __future__ import annotations

import time

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from .auth import require_admin
from .content import Broadcast, ContentPack, content_service
from .credentials import credential_manager
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
            "sessions": [s.to_dict() for s in sessions],
        })
    return {
        "users": sorted(users, key=lambda u: u["email"]),
        "session_count": session_manager.count_all(),
        "credential": credential_manager.status(),
    }
