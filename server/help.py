"""Attendee raise-hand state + optional push to Control Tower help APIs."""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

import requests

from . import config
from .credentials import app_identity_bearer

_log = logging.getLogger(__name__)
_lock = threading.Lock()
_open = False
_note = ""
_raised_at = 0.0


def snapshot() -> dict[str, Any]:
    """Current help state for config/presence surfaces."""
    with _lock:
        return {
            "raised": _open,
            "note": _note or None,
            "raised_at": _raised_at if _open else None,
        }


def presence_fields() -> dict[str, Any]:
    """Fields merged into ``GET /api/admin/presence`` for CT reconcile."""
    with _lock:
        return {
            "help_raised": _open,
            "help_note": _note or None,
            "help_raised_at": _raised_at if _open else None,
            "help_open": _open,
        }


def raise_hand(note: str | None = None) -> dict[str, Any]:
    note_clean = (note or "").strip()[:280] or None
    with _lock:
        global _open, _note, _raised_at
        _open = True
        _note = note_clean or ""
        _raised_at = time.time()
    pushed = _push_control_tower("raise", note_clean)
    return {"raised": True, "pushed": pushed, "note": note_clean}


def lower_hand() -> dict[str, Any]:
    with _lock:
        global _open, _note, _raised_at
        had_open = _open
        _open = False
        _note = ""
        _raised_at = 0.0
    pushed = _push_control_tower("lower", None) if had_open else False
    return {"raised": False, "pushed": pushed}


def clear_hand() -> None:
    """Clear local raised state (operator resolve); does not push to CT."""
    with _lock:
        global _open, _note, _raised_at
        _open = False
        _note = ""
        _raised_at = 0.0


def _push_control_tower(action: str, note: str | None) -> bool:
    base = config.control_tower_url()
    run_id = config.workshop_run_id()
    unit_id = config.workshop_unit_id()
    if not base or not run_id or not unit_id:
        return False
    path = "/api/help/raise" if action == "raise" else "/api/help/lower"
    url = f"{base.rstrip('/')}{path}"
    payload: dict[str, str] = {"run_id": run_id, "unit_id": unit_id}
    if action == "raise" and note:
        payload["note"] = note
    bearer = app_identity_bearer()
    if not bearer:
        _log.debug("help.ct_push_skipped: no app identity bearer")
        return False
    try:
        response = requests.post(
            url,
            json=payload,
            headers={
                "Authorization": f"Bearer {bearer}",
                "Content-Type": "application/json",
            },
            timeout=10,
        )
    except requests.RequestException as exc:
        _log.warning("help.ct_push_failed: %s", exc)
        return False
    if response.status_code >= 400:
        _log.warning(
            "help.ct_push_rejected status=%s body=%s",
            response.status_code,
            response.text[:200],
        )
        return False
    return True
