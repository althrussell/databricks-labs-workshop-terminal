"""Help messages and raise-hand notes are redacted before they leave, but only under v2.

Before insight v2 the attendee's help text went to Control Tower exactly as typed.
That stays true with the flag off, because flag-off payloads must match today. With
``WORKSHOP_INSIGHT_V2`` on, the same text is secret- and PII-redacted before it is
stored, queued in the outbox, or pushed.
"""

from __future__ import annotations

import pytest

from server import help as help_module

EMAIL_NOTE = "I cannot reach the app, mail me at ada@example.com or call +61 412 345 678"


@pytest.fixture(autouse=True)
def _reset_help_state():
    help_module.reset_for_tests()
    yield
    help_module.reset_for_tests()


@pytest.fixture
def no_push(monkeypatch):
    """No Control Tower in these tests; the outbox is the record of what would leave."""
    monkeypatch.setattr(help_module, "_push_control_tower_message", lambda body, message_id="": False)
    monkeypatch.setattr(help_module, "_push_control_tower", lambda *args, **kwargs: False)


def _attendee_bodies() -> list[str]:
    return [m["body"] for m in help_module.thread_snapshot()["messages"] if m["sender_role"] == "attendee"]


def test_with_the_flag_off_help_text_is_stored_exactly_as_typed(client, no_push, monkeypatch):
    monkeypatch.delenv("WORKSHOP_INSIGHT_V2", raising=False)

    help_module.post_attendee_message(EMAIL_NOTE, sender="labuser001@x.com")

    assert _attendee_bodies() == [EMAIL_NOTE]


def test_with_the_flag_on_the_outbound_copy_is_redacted_and_the_thread_is_not(client, no_push, monkeypatch):
    monkeypatch.setenv("WORKSHOP_INSIGHT_CAPTURE", "true")
    monkeypatch.setenv("WORKSHOP_INSIGHT_V2", "true")

    help_module.post_attendee_message(EMAIL_NOTE, sender="labuser001@x.com")

    # What leaves the seat is redacted ...
    [queued] = [e["body"] for e in help_module.outbox_snapshot()]
    assert "ada@example.com" not in queued and "412 345 678" not in queued
    assert "[redacted-email]" in queued and "[redacted-phone]" in queued
    # ... and the attendee's own thread still shows what they typed.
    assert _attendee_bodies() == [EMAIL_NOTE]


def test_with_the_flag_on_a_raise_hand_note_is_redacted(client, no_push, monkeypatch):
    monkeypatch.setenv("WORKSHOP_INSIGHT_CAPTURE", "true")
    monkeypatch.setenv("WORKSHOP_INSIGHT_V2", "true")

    help_module.raise_hand("workspace https://dbc-9.cloud.databricks.com/?token=abc is down")

    [queued] = [e["body"] for e in help_module.outbox_snapshot()]
    assert "dbc-9" not in queued and "token=abc" not in queued
    assert "[redacted-url]" in queued


def test_with_the_flag_off_a_raise_hand_note_is_unchanged(client, no_push, monkeypatch):
    monkeypatch.delenv("WORKSHOP_INSIGHT_V2", raising=False)

    help_module.raise_hand("workspace https://dbc-9.cloud.databricks.com/?token=abc is down")

    notes = [m["body"] for m in help_module.thread_snapshot()["messages"]]
    assert any("https://dbc-9.cloud.databricks.com/?token=abc" in n for n in notes)
