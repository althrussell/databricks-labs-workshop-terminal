"""Identity and group-based authorization.

Identity comes from the Databricks Apps proxy headers (X-Forwarded-Email /
X-Forwarded-User). Authorization is by **workspace group membership** —
never email allowlists:

- attendee access: optional ACCESS_GROUP (unset = any workspace user the
  app's own permission grants admit)
- operator/admin access: ADMIN_GROUP (default ``platform_admins``)

An instance also carries one attendee identity, resolved by ``server/attendee``
and enforced here: every other identity is refused. An unbound instance binds
itself to the first non-operator caller rather than refusing everyone.

Group resolution uses the caller's own credential when present (bearer-token
service principals via SCIM ``/Me``); for browser users it looks the user up
by email via SCIM with the Control-Tower-vended workspace credential
(WORKSHOP_PAT). Results are cached 5 min.
"""

from __future__ import annotations

from collections import OrderedDict
import logging
import hashlib
import threading
import time

import requests
from fastapi import HTTPException, Request, WebSocket

from . import attendee as attendee_binding
from . import config

logger = logging.getLogger(__name__)

_CACHE_TTL = 300
_GROUPS_CACHE_MAX = 4096
_TOKEN_PRINCIPAL_CACHE_MAX = 1024
_groups_cache: OrderedDict[str, tuple[float, set[str]]] = OrderedDict()
_token_principal_cache: OrderedDict[str, tuple[float, str]] = OrderedDict()
_cache_lock = threading.Lock()


class Principal:
    """A resolved caller: browser user (via proxy headers) or SP (via bearer)."""

    def __init__(self, name: str, access_token: str | None = None):
        self.name = name  # email for users, application id / userName for SPs
        self.access_token = access_token

    def __repr__(self) -> str:  # never include the token
        return f"Principal({self.name})"


def _scim_me_identity(token: str) -> tuple[set[str], str | None] | None:
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
            params={"attributes": "id,groups,userName"},
            timeout=10,
        )
        if resp.status_code != 200:
            logger.warning("SCIM /Me failed with status %s", resp.status_code)
            return None
        payload = resp.json()
        groups = payload.get("groups", []) or []
        principal_id = str(payload.get("id") or "").strip() or None
        return ({g.get("display", "") for g in groups if g.get("display")}, principal_id)
    except requests.RequestException as e:
        logger.warning("SCIM /Me request failed: %s", e)
        return None


def _scim_me_groups(token: str) -> set[str] | None:
    identity = _scim_me_identity(token)
    return identity[0] if identity is not None else None


def scim_token_valid(token: str) -> bool:
    """True if ``token`` authenticates against SCIM /Me.

    Used by the credential self-probe to distinguish a valid-but-can't-mint
    credential (degraded) from a rejected/expired one (unhealthy).
    """
    return bool(token) and _scim_me_groups(token) is not None


def _scim_lookup_groups_by_email(email: str) -> set[str] | None:
    """Resolve a user's groups by email using the app's own credential.

    Prefers the app service-principal OAuth identity (auto-refreshed, no expiry
    clock); the vended PAT is an emergency-only fallback so operator/attendee
    group resolution does not silently depend on an expiring static token.
    """
    from .credentials import app_identity_bearer, vended_pat

    host = config.databricks_host()
    token = app_identity_bearer() or vended_pat()
    if not host or not token:
        return None
    try:
        resp = requests.get(
            f"{host}/api/2.0/preview/scim/v2/Users",
            headers={"Authorization": f"Bearer {token}"},
            params={"filter": f'userName eq "{email}"', "attributes": "groups,userName"},
            timeout=10,
        )
        if resp.status_code != 200:
            logger.warning("SCIM Users lookup failed with status %s", resp.status_code)
            return None
        resources = resp.json().get("Resources", []) or []
        if not resources:
            return set()
        groups = resources[0].get("groups", []) or []
        return {g.get("display", "") for g in groups if g.get("display")}
    except requests.RequestException as e:
        logger.warning("SCIM Users lookup request failed: %s", e)
        return None


def get_groups(principal: Principal) -> set[str]:
    """Group display-names for the principal, cached 5 minutes."""
    if config.local_dev():
        import os
        return set(filter(None, os.environ.get("DEV_GROUPS", "").split(",")))

    now = time.time()
    token_digest = (
        hashlib.sha256(principal.access_token.encode("utf-8")).hexdigest()
        if principal.access_token
        else None
    )
    with _cache_lock:
        expired_group_keys = [
            key
            for key, (cached_at, _) in _groups_cache.items()
            if now - cached_at >= _CACHE_TTL
        ]
        for key in expired_group_keys:
            _groups_cache.pop(key, None)
        expired = [
            digest
            for digest, (cached_at, _) in _token_principal_cache.items()
            if now - cached_at >= _CACHE_TTL
        ]
        for digest in expired:
            _token_principal_cache.pop(digest, None)
        principal_key = principal.name
        if token_digest:
            mapping = _token_principal_cache.get(token_digest)
            if mapping and now - mapping[0] < _CACHE_TTL:
                _token_principal_cache.move_to_end(token_digest)
                principal_key = f"principal:{mapping[1]}"
            else:
                principal_key = f"token:{token_digest}"
        cached = _groups_cache.get(principal_key)
        if cached and now - cached[0] < _CACHE_TTL:
            _groups_cache.move_to_end(principal_key)
            return cached[1]

    # Caller's own token first (SPs, or browsers when user authorization is
    # enabled); otherwise look the user up with the vended app credential.
    identity = _scim_me_identity(principal.access_token or "")
    groups = identity[0] if identity is not None else None
    validated_principal_id = identity[1] if identity is not None else None
    if groups is None and "@" in principal.name:
        groups = _scim_lookup_groups_by_email(principal.name)
    if groups is None:
        # Unknown — don't poison the cache; deny-by-default falls out at the
        # membership checks below.
        return set()
    with _cache_lock:
        cache_key = principal.name
        if token_digest:
            if validated_principal_id:
                cache_key = f"principal:{validated_principal_id}"
                for digest, (_, mapped_id) in list(
                    _token_principal_cache.items()
                ):
                    if mapped_id == validated_principal_id and digest != token_digest:
                        _token_principal_cache.pop(digest, None)
                _token_principal_cache[token_digest] = (now, validated_principal_id)
                _token_principal_cache.move_to_end(token_digest)
                while len(_token_principal_cache) > _TOKEN_PRINCIPAL_CACHE_MAX:
                    _token_principal_cache.popitem(last=False)
            else:
                cache_key = f"token:{token_digest}"
        _groups_cache[cache_key] = (now, groups)
        _groups_cache.move_to_end(cache_key)
        while len(_groups_cache) > _GROUPS_CACHE_MAX:
            _groups_cache.popitem(last=False)
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
    try:
        attendee = attendee_binding.resolved_email()
        remote = config.omnigent_remote_enabled()
    except ValueError as exc:
        # A misconfigured remote instance must fail closed, not surface a 500.
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    # Remote mode mirrors one attendee's OBO token under a shared Unix uid, so
    # the configured owner is enforced even where shared topology was
    # acknowledged for a trusted group.
    shared_ok = config.allow_shared_topology() and not remote
    if not attendee and not config.local_dev() and not config.allow_shared_topology():
        # Control Tower's injected binding is missing. Rather than lock the
        # attendee out of a workspace provisioned solely for them, bind the
        # instance to the first non-operator identity that arrives. Operators
        # must not become the attendee, so they are still refused here.
        if is_admin(principal):
            raise HTTPException(
                status_code=403,
                detail=(
                    "This instance has no attendee yet and an operator identity "
                    "cannot claim it; the attendee must open it first."
                ),
            )
        try:
            attendee = attendee_binding.bind(principal.name)
        except ValueError:
            # A non-email principal (e.g. a bearer-token service principal)
            # cannot own an instance; fall through to the unbound refusal.
            attendee = ""
    if attendee and principal.name != attendee and not shared_ok:
        raise HTTPException(
            status_code=403,
            detail=(
                f"This workshop instance is assigned to {attendee}; "
                "request a separate attendee instance."
            ),
        )
    if not attendee and not config.local_dev() and not config.allow_shared_topology():
        raise HTTPException(
            status_code=403,
            detail=(
                "This workshop instance has no attendee identity and none could "
                "be bound for this caller"
            ),
        )
    group = config.access_group()
    if group and group not in get_groups(principal):
        raise HTTPException(status_code=403, detail=f"Access requires membership in the '{group}' group")


def _capture_obo(principal: Principal) -> None:
    """Feed the attendee's forwarded OBO token to the OBO manager so the ``me``
    CLI profile stays fresh. Guarded — capture must never break a request."""
    try:
        from . import obo

        obo.obo_manager.capture(principal.name, principal.access_token)
    except Exception:  # noqa: BLE001 — never fail a request on OBO bookkeeping
        pass


def get_current_user(request: Request) -> Principal:
    """FastAPI dependency: identify + authorize the attendee for any route."""
    principal = _principal_from_headers(request.headers)
    _check_access(principal)
    _capture_obo(principal)
    return principal


async def get_ws_user(websocket: WebSocket) -> Principal | None:
    """Authorize an ALREADY-ACCEPTED websocket. Returns None and closes 4403 on
    failure.

    The caller MUST ``await websocket.accept()`` first. A close during the
    opening handshake (pre-accept) reaches browsers only as a generic 1006 with
    no application code, so the client can't tell "auth failed" / "session gone"
    from a transient network blip and reconnects blindly forever. Accepting
    first means the 4403/4404 close code is delivered and the client can stop.
    """
    try:
        principal = _principal_from_headers(websocket.headers)
        _check_access(principal)
        _capture_obo(principal)
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
        principal = Principal("service-principal", bearer)
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
