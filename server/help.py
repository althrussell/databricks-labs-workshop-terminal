"""Attendee raise-hand state + threaded help messages + CT push."""

from __future__ import annotations

import logging
import threading
import time
from typing import Any
from uuid import uuid4

import requests

from . import config
from .credentials import app_identity_bearer
from .events import event_hub

_log = logging.getLogger(__name__)
_lock = threading.Lock()
_open = False
_note = ""
_raised_at = 0.0
# In-memory mirror of the active conversation (CT is source of truth).
_messages: list[dict[str, Any]] = []
_help_request_id: str | None = None
_MAX_MESSAGES = 200

# Attendee-authored help traffic waiting for Control Tower to collect it.
#
# The direct push below cannot authenticate: this app's OAuth token is minted
# against its own attendee workspace, and Control Tower sits behind the Apps
# proxy of a different workspace, which rejects that token with a 401 before
# Control Tower's code runs. Collection is the direction that does work — CT
# polls every terminal a few seconds apart — so what the attendee writes waits
# here on ``GET /api/admin/presence`` until CT has it and acks.
#
# ``help_raised``/``help_note`` already ride on presence and CT already
# reconciles them, so the outbox carries only what those fields cannot express:
# each message with the id it was given locally, and each read receipt.
_outbox: list[dict[str, Any]] = []
_outbox_seq = 0
# A backlog only grows while CT is not collecting, which is also when nobody is
# reading it. Keep the recent end of it and bound the presence payload.
_OUTBOX_MAX = 50
_OUTBOX_PAGE = 20

# Whether this process has already explained a refused push. Repeating it per
# raise, per message and per read receipt buried the one line that says what
# happened under hundreds that say it again.
_push_rejection = {"logged": False}


def snapshot() -> dict[str, Any]:
    """Current help state for config/presence surfaces."""
    with _lock:
        return {
            "raised": _open,
            "note": _note or None,
            "raised_at": _raised_at if _open else None,
            "message_count": len(_messages),
            "help_request_id": _help_request_id,
        }


def presence_fields() -> dict[str, Any]:
    """Fields merged into ``GET /api/admin/presence`` for CT reconcile."""
    with _lock:
        return {
            "help_raised": _open,
            "help_note": _note or None,
            "help_raised_at": _raised_at if _open else None,
            "help_open": _open,
            # Present even when empty: it is how Control Tower tells a terminal
            # that hands it messages from one that cannot, and so whether to
            # keep deriving the opening message from ``help_note``.
            "help_outbox": _outbox[:_OUTBOX_PAGE],
        }


def _enqueue_locked(kind: str, **fields: Any) -> None:
    """Queue one attendee-side event for collection. Caller holds ``_lock``."""
    global _outbox_seq
    _outbox_seq += 1
    _outbox.append({"seq": _outbox_seq, "kind": kind, **fields})
    if len(_outbox) > _OUTBOX_MAX:
        del _outbox[: len(_outbox) - _OUTBOX_MAX]


def ack_outbox(through_seq: int) -> dict[str, Any]:
    """Drop what Control Tower has applied, up to and including ``through_seq``."""
    with _lock:
        global _outbox
        _outbox = [e for e in _outbox if int(e["seq"]) > through_seq]
        return {"acked_through": through_seq, "pending": len(_outbox)}


def outbox_snapshot() -> list[dict[str, Any]]:
    """Everything still waiting on Control Tower (tests and diagnostics)."""
    with _lock:
        return list(_outbox)


def thread_snapshot() -> dict[str, Any]:
    with _lock:
        return {
            "raised": _open,
            "note": _note or None,
            "help_request_id": _help_request_id,
            "messages": list(_messages),
        }


def raise_hand(note: str | None = None) -> dict[str, Any]:
    note_clean = (note or "").strip()[:280] or None
    with _lock:
        global _open, _note, _raised_at
        _open = True
        _note = note_clean or ""
        _raised_at = time.time()
        if note_clean:
            local = {
                "message_id": str(uuid4()),
                "help_request_id": _help_request_id,
                "sender_role": "attendee",
                "sender": "",
                "body": note_clean,
                "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            }
            _append_local(local)
            _enqueue_locked("message", **local)
    pushed = _push_control_tower("raise", note_clean)
    event_hub.publish({"t": "help_state", "raised": True, "note": note_clean})
    return {"raised": True, "pushed": pushed, "note": note_clean}


def lower_hand() -> dict[str, Any]:
    with _lock:
        global _open, _note, _raised_at
        had_open = _open
        _open = False
        _note = ""
        _raised_at = 0.0
    pushed = _push_control_tower("lower", None) if had_open else False
    event_hub.publish({"t": "help_state", "raised": False})
    return {"raised": False, "pushed": pushed}


def clear_hand() -> None:
    """Clear local raised state (operator resolve); keep chat history."""
    with _lock:
        global _open, _note, _raised_at
        _open = False
        _note = ""
        _raised_at = 0.0
    event_hub.publish({"t": "help_state", "raised": False})


def reset_for_tests() -> None:
    """Wipe raised state and message buffer (test fixture only)."""
    clear_hand()
    with _lock:
        global _messages, _help_request_id, _outbox, _outbox_seq
        _messages = []
        _help_request_id = None
        _outbox = []
        _outbox_seq = 0
        _push_rejection["logged"] = False


def post_attendee_message(body: str, *, sender: str = "") -> dict[str, Any]:
    text = (body or "").strip()[:2000]
    if not text:
        raise ValueError("message body is required")
    with _lock:
        global _open, _note, _raised_at
        if not _open:
            _open = True
            _raised_at = time.time()
            _note = text[:280]
        local = {
            "message_id": str(uuid4()),
            "help_request_id": _help_request_id,
            "sender_role": "attendee",
            "sender": sender,
            "body": text,
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        _append_local(local)
        _enqueue_locked("message", **local)
    pushed = _push_control_tower_message(text)
    if not pushed and not _push_rejection["logged"]:
        # Fall back to raise so CT still opens a queue row when the message API
        # is unavailable on an older Control Tower. Pointless once the proxy has
        # refused us — it is the same caller against the same door, and it was
        # doubling the failure in the log for every message.
        _push_control_tower("raise", text[:280])
    # The attendee typed this, so they have already seen it — no toast, no ack.
    event_hub.publish(
        {"t": "help_message", **local, "surface": "none", "request_ack": False}
    )
    event_hub.publish({"t": "help_state", "raised": True})
    return {"message": local, "pushed": pushed, "raised": True}


def ingest_operator_message(payload: dict[str, Any]) -> dict[str, Any]:
    """CT fan-out: durable operator (or synced) message into local buffer.

    Publishes exactly one event. This used to publish two — a real
    ``help_message`` plus a synthetic ``broadcast`` that hijacked the banner so
    an attendee with the help panel closed would notice a reply. That produced
    the worst of both surfaces: the banner has one slot, so a second reply
    silently overwrote the first, and a single ``ttl_s`` dismissed a direct
    answer on the same clock as a pacing announcement. Replies are toasts now,
    which stack and wait, and the banner is left to persistent state.
    """
    body = (payload.get("body") or "").strip()
    if not body:
        raise ValueError("body required")
    local = {
        "message_id": str(payload.get("message_id") or uuid4()),
        "help_request_id": payload.get("help_request_id"),
        "sender_role": payload.get("sender_role") or "operator",
        "sender": payload.get("sender") or "",
        "body": body[:2000],
        "created_at": payload.get("created_at")
        or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    with _lock:
        global _help_request_id
        if local["help_request_id"]:
            _help_request_id = str(local["help_request_id"])
        # Deduplicate by message_id
        if any(m.get("message_id") == local["message_id"] for m in _messages):
            return local
        _append_local(local)
    event_hub.publish(
        {
            "t": "help_message",
            **local,
            # A direct answer to a raised hand must not disappear on a timer.
            "surface": payload.get("surface") or "toast",
            "durability": payload.get("durability") or "sticky",
            "request_ack": bool(payload.get("request_ack", True)),
        }
    )
    return local


def acknowledge_message(message_id: str) -> bool:
    """Tell Control Tower the attendee actually saw this message.

    Delivered is not read. Without this the operator cannot distinguish an
    answer that landed and was acted on from one sitting unseen behind a
    full-screen terminal, which is exactly the case where the right next action
    is to walk over rather than type again.
    """
    mid = (message_id or "").strip()
    if not mid:
        return False
    with _lock:
        _enqueue_locked("seen", message_id=mid)
    base = config.control_tower_url()
    run_id = config.workshop_run_id()
    unit_id = config.workshop_unit_id()
    if not base or not run_id or not unit_id:
        return False
    url = f"{base.rstrip('/')}/api/help/messages/{mid}/seen"
    return _ct_post(url, {"run_id": run_id, "unit_id": unit_id})


def replace_thread_from_ct(messages: list[dict[str, Any]], *, help_request_id: str | None) -> None:
    with _lock:
        global _messages, _help_request_id
        _help_request_id = help_request_id
        _messages = list(messages)[-_MAX_MESSAGES:]


def _append_local(msg: dict[str, Any]) -> None:
    global _messages
    _messages.append(msg)
    if len(_messages) > _MAX_MESSAGES:
        _messages = _messages[-_MAX_MESSAGES:]


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
    return _ct_post(url, payload)


def _push_control_tower_message(body: str) -> bool:
    base = config.control_tower_url()
    run_id = config.workshop_run_id()
    unit_id = config.workshop_unit_id()
    if not base or not run_id or not unit_id:
        return False
    url = f"{base.rstrip('/')}/api/help/messages"
    return _ct_post(url, {"run_id": run_id, "unit_id": unit_id, "body": body})


def _log_push_rejection(status: int) -> None:
    """Say once that the push is refused, and that nothing is lost by it."""
    if _push_rejection["logged"]:
        _log.debug("help.ct_push_rejected status=%s (already reported)", status)
        return
    _push_rejection["logged"] = True
    _log.warning(
        "help.ct_push_rejected status=%s — Control Tower's Apps proxy will not "
        "accept this app's token, which is minted against its own workspace. "
        "Attendee help traffic is delivered by collection instead: it waits in "
        "the presence outbox until Control Tower polls for it. Not logging "
        "further rejections from this process.",
        status,
    )


def _ct_post(url: str, payload: dict[str, str]) -> bool:
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
    if response.status_code in (401, 403):
        _log_push_rejection(response.status_code)
        return False
    if response.status_code >= 400:
        _log.warning(
            "help.ct_push_rejected status=%s body=%s",
            response.status_code,
            response.text[:200],
        )
        return False
    # Capture help_request_id from raise/message responses when present.
    try:
        data = response.json()
        hid = data.get("help_request_id") or (data.get("message") or {}).get(
            "help_request_id"
        )
        if hid:
            with _lock:
                global _help_request_id
                _help_request_id = str(hid)
    except Exception:  # noqa: BLE001
        pass
    return True
