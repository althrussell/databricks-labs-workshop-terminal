"""Emergency agent control and launch activity."""

import pytest

from server import config, spend
from .conftest import ALICE


@pytest.fixture(autouse=True)
def _reset_kill_switch():
    spend.reset()
    yield
    spend.reset()


CLAUDE = {"id": "claude", "label": "Claude", "requires": ["claude"]}
BASH = {"id": "bash", "label": "Terminal", "requires": []}


class _FakeUser:
    def __init__(self, launched):
        self.email = "a@example.com"
        self.sessions_launched = launched


# ── classification ────────────────────────────────────────────────────────────


def test_bash_is_never_metered_or_blocked():
    assert spend.is_llm_agent(BASH) is False
    spend.set_kill_switch(True)
    # Even with agents killed, a non-agent helper would pass.
    spend.check_can_launch(_FakeUser({"bash": 99}), BASH)  # no raise


def test_claude_is_an_llm_agent():
    assert spend.is_llm_agent(CLAUDE) is True


# ── kill-switch ────────────────────────────────────────────────────────────────


def test_kill_switch_blocks_llm_agents_with_403():
    spend.set_kill_switch(True)
    assert spend.agents_enabled() is False
    with pytest.raises(spend.SpendBlocked) as ei:
        spend.check_can_launch(_FakeUser({}), CLAUDE)
    assert ei.value.status == 403
    # resume
    spend.set_kill_switch(False)
    assert spend.agents_enabled() is True
    spend.check_can_launch(_FakeUser({}), CLAUDE)  # no raise


def test_default_follows_config(monkeypatch):
    spend.reset()
    monkeypatch.setattr(config, "agents_enabled_default", lambda: False)
    assert spend.agents_enabled() is False  # honours deploy default when no override
    spend.set_kill_switch(False)
    assert spend.agents_enabled() is True   # operator override wins


# ── activity is not budget ───────────────────────────────────────────────────


def test_launch_activity_never_blocks_a_gateway_governed_agent():
    spend.check_can_launch(_FakeUser({"claude": 500, "codex": 500}), CLAUDE)


def test_metering_reports_activity_without_budget_language():
    m = spend.metering(_FakeUser({"bash": 3, "claude": 2, "codex": 1}))
    assert m["agent_launches"] == 3       # bash excluded
    assert "budget" not in m and "remaining" not in m
    assert m["by_agent"] == {"claude": 2, "codex": 1}


# ── endpoint enforcement (live app) ─────────────────────────────────────────────


def test_create_session_blocked_when_agents_killed(client, monkeypatch):
    # An LLM agent is offered; with agents killed the launch is refused 403 —
    # and because the spend gate runs first, this needs no readiness/credentials.
    import server.main as m
    monkeypatch.setattr(m.agents, "get_agent",
                        lambda aid: CLAUDE if aid == "claude" else None)
    spend.set_kill_switch(True)
    r = client.post("/api/sessions", json={"agent_id": "claude"}, headers=ALICE)
    assert r.status_code == 403
    assert "paused" in r.json()["detail"].lower()


def test_admin_control_reports_reason_and_is_idempotent(client, as_admin):
    first = client.post(
        "/api/admin/agent-controls",
        json={"enabled": False, "reason": "gateway incident"},
    )
    second = client.post(
        "/api/admin/agent-controls",
        json={"enabled": False, "reason": "gateway incident"},
    )
    assert first.json()["changed"] is True
    assert second.json()["changed"] is False
    assert second.json()["reason"] == "gateway incident"
