"""Attendee raise-hand state and help delivery retained until CT acknowledges it."""

from __future__ import annotations

import copy
import logging
import threading
import time
from typing import Any, Callable
from uuid import uuid4

import requests

from . import config
from .capture_journal import CaptureHeldError
from .credentials import app_identity_bearer
from .events import event_hub

_log = logging.getLogger(__name__)
_lock = threading.RLock()
_MAX_MESSAGES = 200
_OUTBOX_PAGE = 20
_VOTES = ("up", "down")
# Control Tower sends ``sender_role: "assistant"`` only to terminals listing
# ``assistant_v1``; older ones get the answer as an operator's, signed as AI.
HELP_CAPABILITIES = ("assistant_v1",)
_capture_journal = None
_require_ready = None
_push_rejection = {"logged": False}


class HelpMessageNotFound(LookupError):
    """No message with that id is in this attendee's help thread."""


def _empty_state() -> dict[str, Any]:
    return {"raised": False, "note": "", "raised_at": 0.0, "messages": [],
            "help_request_id": None, "outbox": [], "seq": 0, "acked_through": 0,
            "history": {}, "history_floor": 0, "feedback": {}}


_state = _empty_state()


def _validate_state(state: Any) -> dict[str, Any]:
    if (not isinstance(state, dict) or type(state.get("raised")) is not bool
            or not isinstance(state.get("note"), str)
            or not isinstance(state.get("raised_at"), (int, float))
            or not isinstance(state.get("messages"), list)
            or not all(isinstance(m, dict) for m in state["messages"])
            or not isinstance(state.get("outbox"), list)
            or type(state.get("seq")) is not int or type(state.get("acked_through")) is not int
            or not 0 <= state["acked_through"] <= state["seq"]):
        raise CaptureHeldError("retained help state is invalid; capture requires recovery")
    previous = state["acked_through"]
    for entry in state["outbox"]:
        if (not isinstance(entry, dict) or type(entry.get("seq")) is not int
                or entry["seq"] != previous + 1 or entry["seq"] > state["seq"]):
            raise CaptureHeldError("retained help sequence is incomplete; capture requires recovery")
        previous = entry["seq"]
    if previous != state["seq"]:
        raise CaptureHeldError("retained help tail is missing; capture requires recovery")
    history, floor = state.get("history"), state.get("history_floor")
    if history is not None or floor is not None:
        if (not isinstance(history, dict) or type(floor) is not int
                or not 0 <= floor <= state["acked_through"]
                or set(history) != {str(n) for n in range(floor + 1, state["seq"] + 1)}
                or any(not isinstance(entry, dict) or type(entry.get("seq")) is not int
                       or entry["seq"] != int(seq) for seq, entry in history.items())
                or any(history.get(str(entry["seq"])) != entry for entry in state["outbox"])):
            raise CaptureHeldError("retained help inventory is inconsistent")
    # Releases before help assistant v1 retained no votes. Loading never adds
    # the key: a closing capture refuses any write that is not an ACK.
    if "feedback" in state:
        feedback = state["feedback"]
        if (not isinstance(feedback, dict)
                or any(not isinstance(message_id, str) or not message_id or vote not in _VOTES
                       for message_id, vote in feedback.items())):
            raise CaptureHeldError("retained help feedback is invalid; capture requires recovery")
    return state


def require_capture_ready(callback: Callable[[], None] | None) -> None:
    """Install the emitter guard before production capture initialization."""
    global _require_ready
    with _lock:
        _require_ready = callback


def _ensure_ready() -> None:
    # Initialization acquires emitter -> help -> journal locks. Never call its
    # guard while holding the help lock, including from a reentrant helper.
    callback = _require_ready
    if callback is not None:
        callback()


def _checked(journal, operation):
    try:
        return operation()
    except CaptureHeldError as error:
        journal.mark_held(error)
        raise


def configure_capture(journal) -> None:
    """Attach the shared retained journal before serving requests or collectors."""
    global _capture_journal, _state
    with _lock:
        def initialize(state):
            current = state.setdefault("help", copy.deepcopy(_state))
            # Previous producers retained the operation chain, but their latest
            # state omitted ACKed help entries. Do not manufacture that history.
            if "history" not in current:
                current["history"] = {str(entry["seq"]): copy.deepcopy(entry)
                                      for entry in current["outbox"]}
                current["history_floor"] = current["acked_through"]
            return copy.deepcopy(_validate_state(current))
        restored = _checked(journal, lambda: journal.transaction(initialize))
        _state = restored
        _capture_journal = journal


def _read_locked() -> dict[str, Any]:
    global _state
    if _capture_journal is not None:
        _state = _checked(_capture_journal, lambda: copy.deepcopy(
            _validate_state(_capture_journal.read().get("help"))))
    return _state


def _mutate_locked(operation: Callable[[dict[str, Any]], Any]):
    """Publish memory only after the retained transaction succeeds."""
    global _state
    if _capture_journal is None:
        candidate = copy.deepcopy(_state)
        result = operation(candidate)
        _state = _validate_state(candidate)
        return result

    def apply(state):
        current = _validate_state(state.get("help"))
        result = operation(current)
        _validate_state(current)
        return copy.deepcopy(result)
    result = _checked(_capture_journal, lambda: _capture_journal.transaction(apply))
    _read_locked()
    return result


def snapshot() -> dict[str, Any]:
    _ensure_ready()
    with _lock:
        state = _read_locked()
        return {"raised": state["raised"], "note": state["note"] or None,
                "raised_at": state["raised_at"] if state["raised"] else None,
                "message_count": len(state["messages"]),
                "help_request_id": state["help_request_id"]}


def presence_fields() -> dict[str, Any]:
    _ensure_ready()
    with _lock:
        state = _read_locked()
        return {"help_raised": state["raised"], "help_note": state["note"] or None,
                "help_raised_at": state["raised_at"] if state["raised"] else None,
                "help_open": state["raised"],
                "help_outbox": copy.deepcopy(state["outbox"][:_OUTBOX_PAGE]),
                "help_pending": len(state["outbox"]),
                "help_acked_through": state["acked_through"],
                "help_capabilities": list(HELP_CAPABILITIES)}


def _enqueue(state: dict[str, Any], kind: str, **fields: Any) -> None:
    state["seq"] += 1
    entry = {"seq": state["seq"], "kind": kind, **fields}
    state["outbox"].append(entry)
    if "history" not in state:
        state["history"] = {str(item["seq"]): copy.deepcopy(item) for item in state["outbox"]}
        state["history_floor"] = state["acked_through"]
    else:
        state["history"][str(entry["seq"])] = copy.deepcopy(entry)


def ack_outbox(through_seq: int) -> dict[str, Any]:
    """Persist a monotonic, bounded ACK; pending messages are never evicted."""
    if type(through_seq) is not int or through_seq < 0:
        raise ValueError("help acknowledgement must be a nonnegative integer")
    _ensure_ready()
    with _lock:
        def apply(state):
            if through_seq > state["seq"]:
                raise ValueError("help acknowledgement exceeds emitted sequence")
            state["acked_through"] = max(state["acked_through"], through_seq)
            state["outbox"] = [e for e in state["outbox"] if e["seq"] > state["acked_through"]]
            return {"acked_through": state["acked_through"], "pending": len(state["outbox"])}
        return _mutate_locked(apply)


def outbox_snapshot() -> list[dict[str, Any]]:
    _ensure_ready()
    with _lock:
        return copy.deepcopy(_read_locked()["outbox"])


def thread_snapshot() -> dict[str, Any]:
    _ensure_ready()
    with _lock:
        state = _read_locked()
        return {"raised": state["raised"], "note": state["note"] or None,
                "help_request_id": state["help_request_id"],
                "messages": copy.deepcopy(state["messages"]),
                "feedback": copy.deepcopy(state.get("feedback", {}))}


def _append_local(state: dict[str, Any], message: dict[str, Any]) -> None:
    state["messages"].append(message)
    state["messages"] = state["messages"][-_MAX_MESSAGES:]


def raise_hand(note: str | None = None) -> dict[str, Any]:
    _ensure_ready()
    note_clean = (note or "").strip()[:280] or None
    note_id = str(uuid4()) if note_clean else ""
    now = time.time()
    created_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    with _lock:
        def apply(state):
            state.update(raised=True, note=note_clean or "", raised_at=now)
            if note_clean:
                local = {"message_id": note_id, "help_request_id": state["help_request_id"],
                         "sender_role": "attendee", "sender": "", "body": note_clean,
                         "created_at": created_at}
                _append_local(state, local)
                _enqueue(state, "message", **local)
        _mutate_locked(apply)
    pushed = _push_control_tower("raise", note_clean, message_id=note_id)
    event_hub.publish({"t": "help_state", "raised": True, "note": note_clean})
    return {"raised": True, "pushed": pushed, "note": note_clean}


def _lower_state(state):
    had_open = state["raised"]
    state.update(raised=False, note="", raised_at=0.0)
    return had_open


def lower_hand() -> dict[str, Any]:
    _ensure_ready()
    with _lock:
        had_open = _mutate_locked(_lower_state)
    pushed = _push_control_tower("lower", None) if had_open else False
    event_hub.publish({"t": "help_state", "raised": False})
    return {"raised": False, "pushed": pushed}


def clear_hand() -> None:
    """Operator resolve clears the raised state and preserves conversation history."""
    _ensure_ready()
    with _lock:
        _mutate_locked(_lower_state)
    event_hub.publish({"t": "help_state", "raised": False})


def reset_for_tests() -> None:
    global _state, _capture_journal, _require_ready
    with _lock:
        _capture_journal = None
        _require_ready = None
        _state = _empty_state()
        _push_rejection["logged"] = False


def post_attendee_message(body: str, *, sender: str = "") -> dict[str, Any]:
    text = (body or "").strip()[:2000]
    if not text:
        raise ValueError("message body is required")
    _ensure_ready()
    message_id = str(uuid4())
    now = time.time()
    created_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    with _lock:
        def apply(state):
            if not state["raised"]:
                state.update(raised=True, raised_at=now, note=text[:280])
            local = {"message_id": message_id, "help_request_id": state["help_request_id"],
                     "sender_role": "attendee", "sender": sender, "body": text,
                     "created_at": created_at}
            _append_local(state, local)
            _enqueue(state, "message", **local)
            return local
        local = _mutate_locked(apply)
    pushed = _push_control_tower_message(text, message_id=local["message_id"])
    if not pushed and not _push_rejection["logged"]:
        _push_control_tower("raise", text[:280], message_id=local["message_id"])
    event_hub.publish({"t": "help_message", **local, "surface": "none", "request_ack": False})
    event_hub.publish({"t": "help_state", "raised": True})
    return {"message": local, "pushed": pushed, "raised": True}


def ingest_operator_message(payload: dict[str, Any]) -> dict[str, Any]:
    """Keep a reply from Control Tower: an operator's or the Workshop Assistant's.

    ``sender_role`` is stored as sent. Replies are never enqueued; Control
    Tower wrote them and already has them.
    """
    body = (payload.get("body") or "").strip()
    if not body:
        raise ValueError("body required")
    _ensure_ready()
    local = {"message_id": str(payload.get("message_id") or uuid4()),
             "help_request_id": payload.get("help_request_id"),
             "sender_role": payload.get("sender_role") or "operator",
             "sender": payload.get("sender") or "", "body": body[:2000],
             "created_at": payload.get("created_at")
             or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    with _lock:
        def apply(state):
            if local["help_request_id"]:
                state["help_request_id"] = str(local["help_request_id"])
            if any(m.get("message_id") == local["message_id"] for m in state["messages"]):
                return False
            _append_local(state, local)
            return True
        added = _mutate_locked(apply)
    if added:
        event_hub.publish({"t": "help_message", **local,
                           "surface": payload.get("surface") or "toast",
                           "durability": payload.get("durability") or "sticky",
                           "request_ack": bool(payload.get("request_ack", True))})
    return local


def acknowledge_message(message_id: str) -> bool:
    mid = (message_id or "").strip()
    if not mid:
        return False
    _ensure_ready()
    with _lock:
        _mutate_locked(lambda state: _enqueue(state, "seen", message_id=mid))
    base = config.control_tower_url()
    run_id = config.workshop_run_id()
    unit_id = config.workshop_unit_id()
    if not base or not run_id or not unit_id:
        return False
    return _ct_post(f"{base.rstrip('/')}/api/help/messages/{mid}/seen",
                    {"run_id": run_id, "unit_id": unit_id})


def record_feedback(message_id: str, value: str) -> dict[str, Any]:
    """The attendee's one vote on a Workshop Assistant answer."""
    if value not in _VOTES:
        raise ValueError("feedback must be 'up' or 'down'")
    if not isinstance(message_id, str) or not message_id:
        raise HelpMessageNotFound("help message not found")
    _ensure_ready()
    created_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    with _lock:
        def apply(state):
            message = next((m for m in state["messages"] if m.get("message_id") == message_id), None)
            if message is None:
                raise HelpMessageNotFound("help message not found")
            if message.get("sender_role") != "assistant":
                raise ValueError("only Workshop Assistant answers can be rated")
            existing = state.get("feedback", {}).get(message_id)
            if existing is not None:
                return {"recorded": False, "value": existing}
            state.setdefault("feedback", {})[message_id] = value
            _enqueue(state, "feedback", message_id=message_id, value=value, created_at=created_at)
            return {"recorded": True, "value": value}
        result = _mutate_locked(apply)
    if result["recorded"]:
        event_hub.publish({"t": "help_feedback", "message_id": message_id, "value": value})
    return result


def request_person() -> dict[str, Any]:
    """The attendee asked for a member of the workshop team, not the assistant."""
    _ensure_ready()
    now = time.time()
    created_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    handoff_id = str(uuid4())
    with _lock:
        def apply(state):
            if not state["raised"]:
                state.update(raised=True, note="", raised_at=now)
            # However often it is pressed, Control Tower needs one handoff per
            # collection; a press after the ACK is a new request.
            pending = next((e for e in state["outbox"] if e.get("kind") == "handoff"), None)
            if pending is not None:
                return pending["handoff_id"]
            _enqueue(state, "handoff", handoff_id=handoff_id,
                     help_request_id=state["help_request_id"], created_at=created_at)
            return handoff_id
        handoff = _mutate_locked(apply)
    event_hub.publish({"t": "help_state", "raised": True})
    return {"raised": True, "handoff_id": handoff}


def replace_thread_from_ct(messages: list[dict[str, Any]], *, help_request_id: str | None) -> None:
    _ensure_ready()
    with _lock:
        def apply(state):
            state["help_request_id"] = help_request_id
            state["messages"] = copy.deepcopy(messages[-_MAX_MESSAGES:])
        _mutate_locked(apply)


def _push_control_tower(
    action: str, note: str | None, *, message_id: str = ""
) -> bool:
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
        if message_id:
            payload["message_id"] = message_id
    return _ct_post(url, payload)


def _push_control_tower_message(body: str, *, message_id: str = "") -> bool:
    base = config.control_tower_url()
    run_id = config.workshop_run_id()
    unit_id = config.workshop_unit_id()
    if not base or not run_id or not unit_id:
        return False
    url = f"{base.rstrip('/')}/api/help/messages"
    payload = {"run_id": run_id, "unit_id": unit_id, "body": body}
    # The same id this message has locally and in the outbox. Both delivery
    # paths are live in a same-workspace deployment, and without a shared id the
    # push would file the text under Control Tower's own id and the collection
    # would file it again under ours — the attendee's sentence twice in the
    # operator's thread, with only one of them markable as seen.
    if message_id:
        payload["message_id"] = message_id
    return _ct_post(url, payload)


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
    # Control Tower's Apps proxy refuses this app's token before any Control
    # Tower code runs, so once it has said so every later push would be refused
    # too (28 Oct E2E: a 401 for every raise and every seen receipt). Help and
    # receipts already reach Control Tower through its 10 s collection, so stop
    # posting for the rest of this process instead of repeating the 401.
    if _push_rejection["logged"]:
        return False
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
    except Exception:  # noqa: BLE001
        hid = None
    if hid:
        _ensure_ready()
        request_id = str(hid)
        with _lock:
            _mutate_locked(lambda state: state.update(help_request_id=request_id))
    return True
