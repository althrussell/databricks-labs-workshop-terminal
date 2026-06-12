"""P1-16: LLM-agent spend controls — kill-switch, per-attendee budget, metering."""

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
    # Even with agents killed and any budget, bash must pass.
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


# ── per-attendee budget ─────────────────────────────────────────────────────────


def test_budget_blocks_when_cap_reached_with_429(monkeypatch):
    monkeypatch.setattr(config, "max_agent_launches_per_user", lambda: 2)
    # bash launches don't count toward the agent budget
    spend.check_can_launch(_FakeUser({"bash": 5, "claude": 1}), CLAUDE)  # 1 < 2, ok
    with pytest.raises(spend.SpendBlocked) as ei:
        spend.check_can_launch(_FakeUser({"claude": 1, "codex": 1}), CLAUDE)  # 2 >= 2
    assert ei.value.status == 429


def test_budget_zero_is_unlimited(monkeypatch):
    monkeypatch.setattr(config, "max_agent_launches_per_user", lambda: 0)
    spend.check_can_launch(_FakeUser({"claude": 50}), CLAUDE)  # no raise


def test_metering_reports_usage_and_remaining(monkeypatch):
    monkeypatch.setattr(config, "max_agent_launches_per_user", lambda: 5)
    m = spend.metering(_FakeUser({"bash": 3, "claude": 2, "codex": 1}))
    assert m["agent_launches"] == 3       # bash excluded
    assert m["budget"] == 5 and m["remaining"] == 2
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


def test_create_session_budget_exhausted_returns_429(client, monkeypatch):
    import server.main as m
    monkeypatch.setattr(m.agents, "get_agent",
                        lambda aid: CLAUDE if aid == "claude" else None)
    monkeypatch.setattr(config, "max_agent_launches_per_user", lambda: 1)
    # Pre-load the attendee at the cap.
    m.user_manager.get("alice@example.com").sessions_launched["claude"] = 1
    r = client.post("/api/sessions", json={"agent_id": "claude"}, headers=ALICE)
    assert r.status_code == 429
