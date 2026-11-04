"""Enrol attendee-created Apps into the seat's account group (Control Tower option 2).

Control Tower creates one account group per seat, nests it in the group that
carries the workshop model grants, and gives this Workshop Terminal's service
principal Manage on it. Whenever an App is deployed in the seat, its service
principal must join that group. Enrolment is idempotent, runs with the WT SP
token, and never fails the caller: an enrolment failure is returned as data
and recorded, not raised.

The group id arrives as ``WORKSHOP_ATTENDEE_APP_GROUP_ID``. Unset, every
function here is a no-op, so this ships ahead of Control Tower.

Unverified until the live spike: the SCIM ``members`` value is the numeric SCIM
id of the App's service principal (``service_principal_id`` in the Apps API),
not its application id.
"""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, urlsplit

import requests

GROUP_ENV = "WORKSHOP_ATTENDEE_APP_GROUP_ID"
PATCH_OP_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:PatchOp"
ATTEMPT_DELAYS = (1.0, 2.0, 4.0)  # four attempts in total
RETRY_AFTER_CAP_SECONDS = 30.0
REQUEST_TIMEOUT_SECONDS = 15
RETRYABLE_STATUSES = frozenset({429, 500, 502, 503, 504})
# A successful add is re-proven this often (a cheap PATCH), so a membership
# removed by hand or by a SCIM sync is restored without a process restart.
ENROLLED_TTL_SECONDS = 900.0
# After a failed add, the same (group, member) waits this long before the next
# attempt, so a broken SCIM path cannot stall every reconcile pass.
FAILED_COOLDOWN_SECONDS = 300.0

_enrolled_lock = threading.Lock()
# (group, member) -> monotonic time the success stops being trusted
_enrolled: dict[tuple[str, str], float] = {}
# (group, member) -> monotonic time before which the add is not retried
_failed_until: dict[tuple[str, str], float] = {}
_status: dict[str, Any] = {"enabled": False, "enrolled": 0, "failed": 0, "last_error": ""}


@dataclass(frozen=True)
class Enrolment:
    ok: bool
    attempts: int
    status: int | None
    error: str = ""
    skipped: str = ""


def group_id_from_env(env: Mapping[str, str] | None = None) -> str:
    source = os.environ if env is None else env
    return str(source.get(GROUP_ENV, "") or "").strip()


def member_path(group_id: str) -> str:
    return f"/api/2.0/account/scim/v2/Groups/{quote(group_id, safe='')}"


def add_member_body(member_scim_id: str) -> dict[str, Any]:
    return {
        "schemas": [PATCH_OP_SCHEMA],
        "Operations": [
            {"op": "add", "path": "members", "value": [{"value": str(member_scim_id)}]}
        ],
    }


def is_control_tower_managed(
    app: Mapping[str, Any],
    *,
    own_client_id: str = "",
    own_app_name: str = "",
    omnigent_url: str = "",
) -> bool:
    """WT itself and Omnigent are CT-managed and never join an attendee group."""
    client_id = str(app.get("service_principal_client_id") or "")
    if own_client_id and client_id == own_client_id:
        return True
    name = str(app.get("name") or app.get("app_name") or "")
    if own_app_name and name == own_app_name:
        return True
    url = str(app.get("url") or "")
    if omnigent_url and url and urlsplit(url).netloc == urlsplit(omnigent_url).netloc:
        return True
    return False


def _retry_after(response: Any) -> float:
    try:
        value = float(response.headers.get("Retry-After", 0) or 0)
    except (AttributeError, TypeError, ValueError):
        return 0.0
    return min(max(value, 0.0), RETRY_AFTER_CAP_SECONDS)


def enrol(
    *,
    host: str,
    bearer: str,
    group_id: str,
    member_scim_id: str,
    sleep: Callable[[float], None] = time.sleep,
    patch: Callable[..., Any] = requests.patch,
) -> Enrolment:
    """Add one App SP to the group. Bounded retry on throttling and 5xx; never raises."""
    if not group_id:
        return Enrolment(ok=False, attempts=0, status=None, skipped="unset")
    if not member_scim_id:
        return Enrolment(ok=False, attempts=0, status=None, error="app has no service principal id")
    url = host.rstrip("/") + member_path(group_id)
    body = add_member_body(member_scim_id)
    last_status: int | None = None
    last_error = ""
    for attempt in range(1, len(ATTEMPT_DELAYS) + 2):
        try:
            response = patch(
                url,
                headers={"Authorization": f"Bearer {bearer}"},
                json=body,
                timeout=REQUEST_TIMEOUT_SECONDS,
            )
        except requests.RequestException as exc:
            last_status, last_error = None, str(exc)[:200]
        else:
            last_status = response.status_code
            if 200 <= response.status_code < 300 or response.status_code == 409:
                # 409: the member is already in the group.
                return Enrolment(ok=True, attempts=attempt, status=last_status)
            last_error = f"HTTP {response.status_code}"
            if response.status_code not in RETRYABLE_STATUSES:
                return Enrolment(ok=False, attempts=attempt, status=last_status, error=last_error)
        if attempt <= len(ATTEMPT_DELAYS):
            delay = ATTEMPT_DELAYS[attempt - 1]
            if last_status == 429:
                delay = max(delay, _retry_after(response))
            sleep(delay)
    return Enrolment(ok=False, attempts=len(ATTEMPT_DELAYS) + 1, status=last_status, error=last_error)


def enrol_once(
    *,
    host: str,
    bearer: str,
    group_id: str,
    member_scim_id: str,
    app_name: str = "",
    sleep: Callable[[float], None] = time.sleep,
    patch: Callable[..., Any] = requests.patch,
) -> Enrolment:
    """``enrol`` with a success memo (re-proven after a TTL) and a failure cooldown."""
    key = (group_id, str(member_scim_id))
    now = time.monotonic()
    with _enrolled_lock:
        if _enrolled.get(key, 0.0) > now:
            return Enrolment(ok=True, attempts=0, status=None, skipped="already_enrolled")
        if _failed_until.get(key, 0.0) > now:
            return Enrolment(ok=False, attempts=0, status=None, skipped="cooling_down")
    result = enrol(host=host, bearer=bearer, group_id=group_id, member_scim_id=member_scim_id,
                   sleep=sleep, patch=patch)
    with _enrolled_lock:
        _status["enabled"] = True
        if result.ok:
            if not result.skipped:
                _status["enrolled"] = int(_status["enrolled"]) + 1
            _enrolled[key] = now + ENROLLED_TTL_SECONDS
            _failed_until.pop(key, None)
        elif result.skipped != "unset":
            _failed_until[key] = now + FAILED_COOLDOWN_SECONDS
            _status["failed"] = int(_status["failed"]) + 1
            _status["last_error"] = f"{app_name or member_scim_id}: {result.error or result.skipped}"[:300]
    return result


def status() -> dict[str, Any]:
    with _enrolled_lock:
        return dict(_status)


def reset_for_tests() -> None:
    with _enrolled_lock:
        _enrolled.clear()
        _failed_until.clear()
        _status.update({"enabled": False, "enrolled": 0, "failed": 0, "last_error": ""})


__all__ = [
    "GROUP_ENV",
    "Enrolment",
    "add_member_body",
    "enrol",
    "enrol_once",
    "group_id_from_env",
    "is_control_tower_managed",
    "member_path",
    "reset_for_tests",
    "status",
]
