"""The three things an operator can do from the floor while a room waits.

Demote the Omnigent tier fleet-wide, recover one attendee's credential, and see
which install step failed instead of a card that spins forever.
"""

import pytest

from server import agents


@pytest.fixture(autouse=True)
def restore_tier():
    yield
    agents.reset_demotion()


@pytest.fixture
def omnigent_installed(monkeypatch):
    """A box where the Omnigent card would otherwise be launchable."""
    from server import obo
    from server.bootstrap import install

    monkeypatch.setenv("OMNIGENT_APP_URL", "https://omni.example.com")
    monkeypatch.setattr(
        install, "ready", lambda: {"omnigent": True, "claude": True, "codex": True}
    )
    monkeypatch.setattr(
        obo.obo_manager,
        "status",
        lambda email=None: {
            "enabled": True,
            "present": True,
            "fresh": True,
            "expires_in": 3600,
            "last_refresh": None,
        },
    )


def test_demoting_withdraws_omnigent_cards_from_every_attendee(
    client, as_admin, omnigent_installed
):
    before = client.get("/api/agents").json()["agents"]
    assert any(a["id"] == "omnigent" and not a["blocked"] for a in before)

    client.post("/api/admin/omnigent-tier", json={"enabled": False})

    after = {a["id"]: a for a in client.get("/api/agents").json()["agents"]}
    assert after["omnigent"]["blocked"] == "operator_demoted"
    assert not after["omnigent"]["ready"]


def test_the_fallback_tier_survives_a_demotion(client, as_admin, omnigent_installed):
    """The whole point of the lever: a demoted room keeps working."""
    client.post("/api/admin/omnigent-tier", json={"enabled": False})

    catalog = {a["id"]: a for a in client.get("/api/agents").json()["agents"]}
    for agent_id in ("claude", "codex", "bash"):
        if agent_id in catalog:
            assert not catalog[agent_id]["blocked"], f"{agent_id} must stay launchable"


def test_restoring_puts_the_tier_back(client, as_admin, omnigent_installed):
    client.post("/api/admin/omnigent-tier", json={"enabled": False})

    body = client.post("/api/admin/omnigent-tier", json={"enabled": True}).json()

    assert body["enabled"]
    catalog = {a["id"]: a for a in client.get("/api/agents").json()["agents"]}
    assert catalog["omnigent"]["blocked"] != "operator_demoted"


def test_every_open_tab_is_told_rather_than_left_on_a_dead_card(client, as_admin, monkeypatch):
    """A card that no longer works is exactly what the lever exists to end."""
    published = []
    from server import admin

    monkeypatch.setattr(admin.event_hub, "publish", published.append)

    client.post("/api/admin/omnigent-tier", json={"enabled": False})

    assert {"t": "agents_changed"} in published


def test_the_tier_view_shows_who_would_actually_be_able_to_launch(client, as_admin):
    payload = client.get("/api/admin/omnigent-tier").json()

    assert payload["enabled"] is True
    assert isinstance(payload["attendees"], list)
    for attendee in payload["attendees"]:
        assert "fresh" in attendee["obo"]
        assert "status" in attendee["host"]


def test_recovering_one_attendee_runs_the_same_steps_the_server_would(
    client, as_admin, monkeypatch
):
    from server import selfheal

    calls = []

    def fake_recover(email, reason, *, force=False):
        calls.append((email, reason, force))
        return {"attempted": True, "actions": ["re-mirrored"], "credential_fresh": True}

    monkeypatch.setattr(selfheal.self_healer, "recover", fake_recover)

    body = client.post("/api/admin/recover", json={"email": "alice@example.com"}).json()

    assert calls == [("alice@example.com", "operator recovery", True)]
    assert body["recovered"] == ["alice@example.com"]


def test_recovery_ignores_the_cooldown_because_an_operator_asked(client, as_admin, monkeypatch):
    """The cooldown exists to stop a log sweep thrashing, not to refuse a human."""
    from server import selfheal
    from server.users import user_manager

    user_manager.get("alice@example.com")
    seen = []
    monkeypatch.setattr(
        selfheal.self_healer,
        "recover",
        lambda email, reason, *, force=False: seen.append(force) or {"attempted": force},
    )

    client.post("/api/admin/recover", json={})

    assert seen and all(seen)


def test_a_recovery_that_cannot_help_says_so_rather_than_claiming_success(
    client, as_admin, monkeypatch
):
    from server import selfheal

    monkeypatch.setattr(
        selfheal.self_healer,
        "recover",
        lambda email, reason, *, force=False: {
            "attempted": True,
            "actions": [],
            "credential_fresh": False,
        },
    )

    body = client.post("/api/admin/recover", json={"email": "alice@example.com"}).json()

    assert body["recovered"] == []
    assert body["results"][0]["credential_fresh"] is False


def test_the_levers_are_refused_to_a_non_admin(client, as_non_admin):
    assert client.get("/api/admin/omnigent-tier").status_code == 403
    assert client.post("/api/admin/omnigent-tier", json={"enabled": False}).status_code == 403
    assert client.post("/api/admin/recover", json={}).status_code == 403


def test_a_failed_install_step_is_named_instead_of_spinning_forever(client, monkeypatch):
    """The card an attendee watches must not promise progress that will not come."""
    from server.bootstrap import install

    monkeypatch.setattr(
        install,
        "_state",
        {"omnigent": {"status": "error", "error": "npm install exited 1"}},
    )
    monkeypatch.setattr(install, "ready", lambda: {"omnigent": False, "claude": True})

    catalog = {a["id"]: a for a in client.get("/api/agents").json()["agents"]}

    assert catalog["omnigent"]["install_error"].startswith("omnigent: npm install exited 1")
    assert not catalog["omnigent"]["ready"]


def test_a_step_still_running_is_not_reported_as_a_failure(client, monkeypatch):
    from server.bootstrap import install

    monkeypatch.setattr(install, "_state", {"omnigent": {"status": "running", "error": None}})
    monkeypatch.setattr(install, "ready", lambda: {"omnigent": False, "claude": True})

    catalog = {a["id"]: a for a in client.get("/api/agents").json()["agents"]}

    assert catalog["omnigent"]["install_error"] == ""


def test_a_degraded_step_counts_as_failed_because_it_never_completes(client, monkeypatch):
    from server.bootstrap import install

    monkeypatch.setattr(
        install,
        "_state",
        {"tmux": {"status": "degraded", "error": "static build, unreviewed"}},
    )
    monkeypatch.setattr(install, "ready", lambda: {"omnigent": False})

    catalog = {a["id"]: a for a in client.get("/api/agents").json()["agents"]}

    assert "tmux: static build, unreviewed" in catalog["omnigent"]["install_error"]
