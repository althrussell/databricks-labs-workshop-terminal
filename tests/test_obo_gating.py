"""Gate rather than fail: a launch that cannot succeed is not offered.

Everything behind the Omnigent host inherits the attendee's tab-bound OBO
mirror. When that mirror goes stale, every Omnigent session dies with an auth
error the attendee cannot act on — so the card stops being launchable and says
why, while the bare CLIs the runbook falls back to stay untouched.
"""

from __future__ import annotations

import time

import pytest

from .test_obo import make_jwt


REMOTE_URL = "https://alice-omnigent.example.databricksapps.com"
ALICE = {"X-Forwarded-Email": "alice@example.com"}


@pytest.fixture
def remote_omnigent(monkeypatch, tmp_path):
    """A remote-Omnigent instance with every installer complete, and no mirror.

    The OBO store is a process-wide singleton whose captures outlive the test
    that made them, so "no token captured" is only true here if no earlier test
    in the run captured one for this attendee. That made
    ``test_no_captured_token_is_reported_as_absent_not_as_installing`` pass in
    file order and fail in others — an order-dependent red that would cost real
    debugging during event week. Each test starts from an empty store instead.
    """
    from server import config, obo
    from server.bootstrap import install as install_mod

    monkeypatch.setenv("OMNIGENT_APP_URL", REMOTE_URL)
    monkeypatch.setenv("ENABLE_OBO", "true")
    monkeypatch.setattr(config, "users_root", lambda: str(tmp_path / "users"))
    monkeypatch.setattr(obo, "obo_manager", obo.OboManager())
    monkeypatch.setattr(
        install_mod,
        "ready",
        lambda: {"bash": True, "claude": True, "codex": True, "omnigent": True},
    )


@pytest.fixture
def emitted(monkeypatch):
    from server import event_emitter as emitter_module

    records: list[tuple[str, str, dict]] = []
    monkeypatch.setattr(
        emitter_module.event_emitter,
        "emit",
        lambda event_type, attendee, payload=None, **_k: records.append(
            (event_type, attendee, payload or {})
        ),
    )
    return records


def _capture(monkeypatch, expires_in: float) -> None:
    """Put a token with the given remaining life in the store the fixture made."""
    from server import obo
    from server.users import user_manager

    user_manager.get("alice@example.com")
    obo.obo_manager.capture("alice@example.com", make_jwt(time.time() + expires_in))


def _agents(client) -> dict[str, dict]:
    body = client.get("/api/agents", headers=ALICE).json()
    return {agent["id"]: agent for agent in body["agents"]}


def test_a_fresh_mirror_leaves_every_card_launchable(client, remote_omnigent, monkeypatch):
    _capture(monkeypatch, expires_in=3600)

    agents = _agents(client)

    assert agents["omnigent"]["ready"] is True
    assert agents["omnigent"]["blocked"] == ""


def test_a_stale_mirror_withdraws_omnigent_and_leaves_the_fallbacks_alone(
    client, remote_omnigent, monkeypatch
):
    _capture(monkeypatch, expires_in=5)

    agents = _agents(client)

    assert agents["omnigent"]["ready"] is False
    assert agents["omnigent"]["blocked"] == "credential_stale"
    # The whole point of the fallback tier: it does not touch this plane.
    assert agents["claude"]["ready"] is True
    assert agents["codex"]["ready"] is True


def test_no_captured_token_is_reported_as_absent_not_as_installing(
    client, remote_omnigent
):
    """"Installing" would send an attendee off to wait for something that has
    already finished. The credential is the problem, so say so."""
    agents = _agents(client)

    assert agents["omnigent"]["blocked"] == "credential_absent"
    assert agents["claude"]["ready"] is True


def test_a_local_instance_is_never_gated_on_the_mirror(client, monkeypatch, tmp_path):
    """Without a remote host there is no mirror to be stale about."""
    from server import config
    from server.bootstrap import install as install_mod

    monkeypatch.delenv("OMNIGENT_APP_URL", raising=False)
    monkeypatch.setattr(config, "users_root", lambda: str(tmp_path / "users"))
    monkeypatch.setattr(
        install_mod,
        "ready",
        lambda: {"bash": True, "claude": True, "codex": True, "omnigent": True},
    )

    assert _agents(client)["omnigent"]["ready"] is True


def test_launching_omnigent_stale_is_refused_with_the_working_alternatives(
    client, remote_omnigent, monkeypatch, emitted
):
    _capture(monkeypatch, expires_in=5)

    response = client.post("/api/sessions", json={"agent_id": "omnigent"}, headers=ALICE)

    assert response.status_code == 503
    detail = response.json()["detail"]
    assert "Reload this tab" in detail
    assert "Claude and Codex" in detail
    failure = next(
        payload for kind, _who, payload in emitted if kind == "session.create_failed"
    )
    assert failure["code"] == "obo_stale"
    assert failure["detail"] == "credential_stale"


def test_the_fallback_clis_still_launch_while_the_mirror_is_stale(
    client, remote_omnigent, monkeypatch, launchable_agents
):
    _capture(monkeypatch, expires_in=5)

    response = client.post("/api/sessions", json={"agent_id": "claude"}, headers=ALICE)

    assert response.status_code == 200


def test_config_reports_each_credential_plane_separately(
    client, remote_omnigent, monkeypatch
):
    _capture(monkeypatch, expires_in=1800)

    durability = client.get("/api/config", headers=ALICE).json()["durability"]

    assert durability["attendee_obo_expires_in"] == pytest.approx(1800, abs=5)
    assert durability["attendee_obo_present"] is True
    assert durability["omnigent_launchable"] is True


def test_readyz_reports_durability_alongside_its_verdict(client):
    body = client.get("/readyz").json()

    assert set(body["durability"]) >= {
        "app_credential_expires_in",
        "attendee_obo_expires_in",
        "event_ends_in",
        "omnigent_launchable",
        "outlasts_event",
    }


def test_durability_answers_whether_the_credentials_outlast_the_event():
    from server import readiness

    env = {"WORKSHOP_EVENT_ENDS_AT": str(time.time() + 7200)}

    comfortable = readiness.durability(
        {"token_expires_in": 9000}, {"expires_in": 9000, "present": True}, env
    )
    short = readiness.durability(
        {"token_expires_in": 600}, {"expires_in": 9000, "present": True}, env
    )
    unbounded = readiness.durability(
        {"token_expires_in": 600}, {"expires_in": 600, "present": True}, {}
    )

    assert comfortable["outlasts_event"] is True
    assert short["outlasts_event"] is False
    # No declared end time is not a failure — it is an unanswerable question.
    assert unbounded["outlasts_event"] is None
