"""Identity and group-based authorization.

Identity comes from the Databricks Apps proxy headers (X-Forwarded-Email /
X-Forwarded-User). Authorization is by **workspace group membership** —
never email allowlists:

- attendee access: optional ACCESS_GROUP (unset = any workspace user the
  app's own permission grants admit)
- operator/admin access: ADMIN_GROUP (default ``platform_admins``)

Group resolution prefers the caller's own credential — the forwarded OBO
access token for browser users, or the bearer token for service principals
calling the admin API — via SCIM ``/Me?attributes=groups``, so the app needs
no SCIM read grants of its own in the common path. Results are cached 5 min.
"""

from __future__ import annotations

import logging
import threading
import time

import requests
from fastapi import HTTPException, Request, WebSocket

from . import config

logger = logging.getLogger(__name__)

_CACHE_TTL = 300
_groups_cache: dict[str, tuple[float, set[str]]] = {}
_cache_lock = threading.Lock()


class Principal:
    """A resolved caller: browser user (via proxy headers) or SP (via bearer)."""

    def __init__(self, name: str, access_token: str | None = None):
        self.name = name  # email for users, application id / userName for SPs
        self.access_token = access_token

    def __repr__(self) -> str:  # never include the token
        return f"Principal({self.name})"


def _scim_me_groups(token: str) -> set[str] | None:
    """Resolve the calling principal's group display-names via SCIM /Me.

    Returns None on failure (treat as 'unknown', not 'no groups').
    """
    host = config.databricks_host()
    if not host or not token:
        return None
    try:
        resp = requests.get(
            f"{host}/api/2.0/preview/scim/v2/Me",
            headers={"Authorization": f"Bearer {token}"},
            params={"attributes": "groups,userName"},
            timeout=10,
        )
        if resp.status_code != 200:
            logger.warning("SCIM /Me failed (%s): %s", resp.status_code, resp.text[:200])
            return None
        groups = resp.json().get("groups", []) or []
        return {g.get("display", "") for g in groups if g.get("display")}
    except requests.RequestException as e:
        logger.warning("SCIM /Me request failed: %s", e)
        return None


def get_groups(principal: Principal) -> set[str]:
    """Group display-names for the principal, cached 5 minutes."""
    if config.local_dev():
        import os
        return set(filter(None, os.environ.get("DEV_GROUPS", "").split(",")))

    now = time.time()
    with _cache_lock:
        cached = _groups_cache.get(principal.name)
        if cached and now - cached[0] < _CACHE_TTL:
            return cached[1]

    groups = _scim_me_groups(principal.access_token or "")
    if groups is None:
        # Unknown — don't poison the cache; deny-by-default falls out at the
        # membership checks below.
        return set()
    with _cache_lock:
        _groups_cache[principal.name] = (now, groups)
    return groups


def _principal_from_headers(headers) -> Principal:
    email = (headers.get("x-forwarded-email") or headers.get("x-forwarded-user") or "").strip().lower()
    if not email and config.local_dev():
        import os
        email = os.environ.get("DEV_FAKE_EMAIL", "dev@example.com")
    if not email:
        raise HTTPException(status_code=403, detail="No identity headers — access via Databricks Apps only")
    token = (headers.get("x-forwarded-access-token") or "").strip() or None
    if not token and config.local_dev():
        import os
        token = os.environ.get("DEV_FAKE_TOKEN") or None
    return Principal(email, token)


def _check_access(principal: Principal) -> None:
    group = config.access_group()
    if group and group not in get_groups(principal):
        raise HTTPException(status_code=403, detail=f"Access requires membership in the '{group}' group")


def get_current_user(request: Request) -> Principal:
    """FastAPI dependency: identify + authorize the attendee for any route."""
    principal = _principal_from_headers(request.headers)
    _check_access(principal)
    return principal


async def get_ws_user(websocket: WebSocket) -> Principal | None:
    """Authorize a websocket BEFORE accept(). Returns None (and closes 4403) on failure."""
    try:
        principal = _principal_from_headers(websocket.headers)
        _check_access(principal)
        return principal
    except HTTPException:
        await websocket.close(code=4403)
        return None


def require_admin(request: Request) -> Principal:
    """FastAPI dependency for /api/admin/*: ADMIN_GROUP membership required.

    Accepts either a browser user (proxy headers + forwarded token) or a
    direct service-principal call (Authorization: Bearer) — both resolve
    groups through their own credential.
    """
    bearer = (request.headers.get("authorization") or "").removeprefix("Bearer ").strip()
    has_proxy_identity = bool(request.headers.get("x-forwarded-email") or request.headers.get("x-forwarded-user"))
    if bearer and not has_proxy_identity:
        principal = Principal(f"sp:{bearer[:8]}", bearer)
    else:
        principal = _principal_from_headers(request.headers)

    if config.admin_group() not in get_groups(principal):
        raise HTTPException(
            status_code=403,
            detail=f"Operator access requires membership in the '{config.admin_group()}' group",
        )
    return principal


def is_admin(principal: Principal) -> bool:
    return config.admin_group() in get_groups(principal)
