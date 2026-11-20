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


def test_with_the_flag_on_help_text_is_redacted_before_it_is_stored(client, no_push, monkeypatch):
    monkeypatch.setenv("WORKSHOP_INSIGHT_CAPTURE", "true")
    monkeypatch.setenv("WORKSHOP_INSIGHT_V2", "true")

    help_module.post_attendee_message(EMAIL_NOTE, sender="labuser001@x.com")

    [body] = _attendee_bodies()
    assert "ada@example.com" not in body
    assert "412 345 678" not in body
    assert "[redacted-email]" in body and "[redacted-phone]" in body


def test_with_the_flag_on_a_raise_hand_note_is_redacted(client, no_push, monkeypatch):
    monkeypatch.setenv("WORKSHOP_INSIGHT_CAPTURE", "true")
    monkeypatch.setenv("WORKSHOP_INSIGHT_V2", "true")

    help_module.raise_hand("workspace https://dbc-9.cloud.databricks.com/?token=abc is down")

    notes = [m["body"] for m in help_module.thread_snapshot()["messages"]]
    assert notes and all("dbc-9" not in n and "token=abc" not in n for n in notes)


def test_with_the_flag_off_a_raise_hand_note_is_unchanged(client, no_push, monkeypatch):
    monkeypatch.delenv("WORKSHOP_INSIGHT_V2", raising=False)

    help_module.raise_hand("workspace https://dbc-9.cloud.databricks.com/?token=abc is down")

    notes = [m["body"] for m in help_module.thread_snapshot()["messages"]]
    assert any("https://dbc-9.cloud.databricks.com/?token=abc" in n for n in notes)
