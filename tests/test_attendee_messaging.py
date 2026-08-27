"""The tab rule has to reach an attendee three ways, or it reaches nobody.

Spoken once at the intro it is forgotten by the first break; printed only in the
UI it is missed by anyone who joined late; shown only when it breaks it arrives
after the damage. So: a floor script an operator reads out, a standing notice
while the rule applies, and a blocking banner the moment it is broken. These
tests pin all three, because losing any one of them is silent.
"""

from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "docs" / "attendee-messaging.md"
NOTICE = ROOT / "frontend" / "src" / "components" / "SignInNotice.tsx"
APP = ROOT / "frontend" / "src" / "App.tsx"


@pytest.fixture(scope="module")
def script() -> str:
    assert SCRIPT.exists(), "the floor script must exist for an operator to read"
    return SCRIPT.read_text()


def test_the_floor_script_states_the_rule_as_a_consequence(script):
    """'Please keep the tab open' is not remembered; 'or the agents stop' is."""
    assert "Leave the Workshop Terminal tab open" in script
    assert "stop working" in script


def test_the_floor_script_gives_the_fix_as_well_as_the_rule(script):
    assert "reload" in script.lower()
    assert "not lose your work" in script or "not lose any work" in script


def test_the_floor_script_names_the_tier_that_keeps_working(script):
    assert "Claude Code and Codex" in script or "Claude and Codex" in script


def test_the_script_does_not_ask_for_something_attendees_cannot_give(script):
    """Focus is not the requirement; a background tab renews fine."""
    assert "Do not** tell attendees to keep the tab *focused*" in script


def test_the_standing_notice_exists_and_is_not_dismissible():
    notice = NOTICE.read_text()
    assert "Keep this tab open" in notice
    assert "onDismiss" not in notice and "dismiss" not in notice.lower()


def test_the_blocking_banner_offers_the_one_action_that_fixes_it():
    notice = NOTICE.read_text()
    assert "sign-in has expired" in notice
    assert "onReload" in notice


def test_the_banner_never_covers_the_tier_that_still_works():
    """A modal would hide the bare CLIs — the only thing working at that moment."""
    notice = NOTICE.read_text()
    assert "modal" not in notice.lower() or "Never a modal" in notice
    assert "Claude and Codex keep working" in notice


def test_the_notice_is_actually_mounted():
    """A component nobody renders is a comment with extra steps."""
    app = APP.read_text()
    assert "<SignInNotice" in app
    assert "obo={config?.obo}" in app


def test_the_config_endpoint_carries_what_the_notice_reads(client):
    payload = client.get("/api/config").json()

    assert "obo" in payload
    for field in ("enabled", "present", "fresh", "expires_in"):
        assert field in payload["obo"]
