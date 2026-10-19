"""Read-only attendee lifecycle over the existing durable admission barrier."""

from .closeout_guard import closeout_state


def attendee_status(journal) -> dict:
    """No capture identifiers, attendee data or operator diagnostics leave here."""
    operation = closeout_state(journal.read())
    phase = operation["phase"] if operation else "OPEN"
    state = "open" if phase == "OPEN" else "closed" if phase == "SEALED" else "closing"
    return {"state": state, "accepting_input": state == "open"}


CLOSING_DETAIL = {"code": "workshop_closing", "message": "This workshop is closing. New input is disabled."}
UNAVAILABLE_DETAIL = "Workshop is temporarily unavailable. Please retry shortly."
