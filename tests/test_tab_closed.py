"""The attendee shuts their laptop at lunch and comes back two hours later.

This is the scenario that took a room down. The app holds no refresh token, so
a closed tab genuinely cannot be rescued — the honest goal is not "it keeps
working" but "it degrades into something explained, survivable and self-healing
the moment the tab comes back". Every assertion here is one of those four
properties, walked as one timeline rather than as isolated states, because the
failure was in the transitions.
"""

from __future__ import annotations

import time

import pytest

from .test_obo import make_jwt

REMOTE_URL = "https://alice-omnigent.example.databricksapps.com"
ALICE = {"X-Forwarded-Email": "alice@example.com"}
LUNCH = 2 * 60 * 60


@pytest.fixture
def workshop(monkeypatch, tmp_path):
    """A working instance: remote Omnigent, everything installed, tab open."""
    from server import config, obo
    from server.bootstrap import install as install_mod
    from server.users import user_manager

    monkeypatch.setenv("OMNIGENT_APP_URL", REMOTE_URL)
    monkeypatch.setenv("ENABLE_OBO", "true")
    monkeypatch.setattr(config, "users_root", lambda: str(tmp_path / "users"))
    monkeypatch.setattr(
        install_mod,
        "ready",
        lambda: {"bash": True, "claude": True, "codex": True, "omnigent": True},
    )
    user_manager.get("alice@example.com")
    manager = obo.OboManager()
    monkeypatch.setattr(obo, "obo_manager", manager)
    manager.capture("alice@example.com", make_jwt(time.time() + 3600))
    return manager


def close_the_tab(manager, seconds: float = LUNCH) -> None:
    """Age the captured token by ``seconds`` without capturing a new one.

    Which is exactly what a closed tab is: the mirror on disk is the same file
    it always was, and nothing exists that can renew it.
    """
    with manager._lock:  # noqa: SLF001 — the point is to forge elapsed time
        record = manager._by_email["alice@example.com"]  # noqa: SLF001
        record.exp = time.time() - seconds
        record.last_capture_exp = record.exp


def reopen_the_tab(client, manager):
    """The attendee comes back: one request carrying a fresh proxy token.

    A different expiry from the one the fixture captured, because the proxy
    would be handing over a genuinely new token — and an identical string is
    deliberately throttled to a no-op, which would make this prove nothing.
    """
    return client.get(
        "/api/config",
        headers={**ALICE, "X-Forwarded-Access-Token": make_jwt(time.time() + 7200)},
    )


def agents(client) -> dict[str, dict]:
    body = client.get("/api/agents", headers=ALICE).json()
    return {agent["id"]: agent for agent in body["agents"]}


def test_before_lunch_everything_works(client, workshop):
    assert agents(client)["omnigent"]["ready"] is True


def test_the_attendee_is_never_shown_a_harness_code_for_a_closed_tab(client, workshop):
    close_the_tab(workshop)

    response = client.post("/api/sessions", json={"agent_id": "omnigent"}, headers=ALICE)

    assert response.status_code == 503
    detail = response.json()["detail"]
    assert "Reload this tab" in detail
    for code in ("spec_resolver_failed", "native_terminal_start_failed", "runner logs"):
        assert code not in detail


def test_the_workshop_is_still_usable_two_hours_later(client, workshop):
    """Degrading cleanly means the attendee can still do the workshop."""
    close_the_tab(workshop)

    catalog = agents(client)

    assert catalog["claude"]["ready"] is True
    assert catalog["codex"]["ready"] is True
    assert catalog["bash"]["ready"] is True
    assert client.post("/api/sessions", json={"agent_id": "bash"}, headers=ALICE).status_code == 200


def test_the_dead_card_is_withdrawn_rather_than_left_to_fail_on_click(client, workshop):
    close_the_tab(workshop)

    omnigent = agents(client)["omnigent"]

    assert omnigent["ready"] is False
    assert omnigent["blocked"] == "credential_stale"
    # Not an install problem, and saying so would send them off to wait.
    assert omnigent["install_error"] == ""


def test_the_attendee_is_told_what_to_do_before_they_click_anything(client, workshop):
    """The notice is driven off /api/config; it must carry the stale state."""
    close_the_tab(workshop)

    payload = client.get("/api/config", headers=ALICE).json()

    assert payload["obo"]["present"] is True
    assert payload["obo"]["fresh"] is False
    assert payload["durability"]["omnigent_launchable"] is False


def test_an_operator_can_see_it_without_the_attendee_saying_anything(
    client, as_admin, workshop, monkeypatch
):
    emitted: list[tuple] = []
    from server import event_emitter as emitter_module

    monkeypatch.setattr(
        emitter_module.event_emitter,
        "emit",
        lambda kind, who, payload=None, **_k: emitted.append((kind, who, payload or {})),
    )
    close_the_tab(workshop)

    workshop.note_health("alice@example.com")

    health = [payload for kind, _who, payload in emitted if kind == "obo.health"]
    assert health and health[-1]["state"] == "stale"
    panel = client.get("/api/admin/omnigent-tier").json()
    row = next(a for a in panel["attendees"] if a["email"] == "alice@example.com")
    assert row["obo"]["fresh"] is False


def test_recovery_admits_it_cannot_help_a_tab_that_is_still_closed(
    client, as_admin, workshop
):
    """The one thing worse than failing here is claiming success."""
    close_the_tab(workshop)

    body = client.post("/api/admin/recover", json={"email": "alice@example.com"}).json()

    assert body["recovered"] == []
    assert body["results"][0]["obo"]["fresh"] is False


def test_reopening_the_tab_fixes_it_with_nobody_being_asked_for_help(client, workshop):
    close_the_tab(workshop)
    assert agents(client)["omnigent"]["ready"] is False

    reopen_the_tab(client, workshop)

    assert agents(client)["omnigent"]["ready"] is True
    assert workshop.status("alice@example.com")["fresh"] is True


def test_the_host_is_woken_on_the_way_back_rather_than_left_behind_a_dead_mirror(
    client, workshop, monkeypatch
):
    """A host still reporting `running` against an expired mirror fails every
    launch afterwards, which reads as "the reload did not work"."""
    from server import omnigent_remote

    woken: list[str] = []
    monkeypatch.setattr(
        omnigent_remote.remote_host_manager, "notify", lambda email: woken.append(email)
    )
    close_the_tab(workshop)

    response = reopen_the_tab(client, workshop)

    assert response.status_code == 200, response.text
    assert woken == ["alice@example.com"]


def test_a_closed_tab_does_not_take_the_instance_out_of_service(client, workshop):
    """One attendee's browser is not an instance health problem, and marking it
    one would have Control Tower pulling healthy instances mid-event."""
    close_the_tab(workshop)

    body = client.get("/readyz").json()

    assert body["durability"]["omnigent_launchable"] is False
    assert "attendee" not in " ".join(body.get("blocking", []))
