"""Which coding agents a workshop offers, chosen when the workshop was created.

``WORKSHOP_AGENTS`` is the operator's pick, arriving as a deploy-time env
override from Control Tower's create form. It answers a different question from
the two levers that already existed and must not be confused with either:
``AGENTS_ENABLED`` pauses launches for a room whose spend is running hot, and
``OMNIGENT_ENABLED`` says whether this deployment can run the harness at all.
This one says what an attendee is offered in the first place.
"""

from __future__ import annotations

import pytest

from server import agents, config
from server.bootstrap import install


@pytest.fixture(autouse=True)
def _no_selection(monkeypatch):
    """Start every test from an unconfigured deployment."""
    monkeypatch.delenv("WORKSHOP_AGENTS", raising=False)


def _ids(monkeypatch, selection: str | None) -> list[str]:
    if selection is not None:
        monkeypatch.setenv("WORKSHOP_AGENTS", selection)
    return [a["id"] for a in agents.load_catalog()]


def test_an_unconfigured_deployment_offers_everything(monkeypatch):
    """Empty means "nobody chose", not "choose nothing". A terminal deployed
    without Control Tower has to come up with a full launcher rather than one
    holding a single bash card."""
    assert set(_ids(monkeypatch, None)) >= {"omnigent", "claude", "codex", "bash"}
    assert set(_ids(monkeypatch, "")) >= {"omnigent", "claude", "codex", "bash"}


def test_an_agent_the_operator_did_not_pick_is_not_offered(monkeypatch):
    assert _ids(monkeypatch, "claude") == ["claude", "bash"]


def test_the_plain_terminal_survives_any_selection(monkeypatch):
    """Bash is where the runbook sends a room when an agent plane fails, so it
    is not an operator's to remove — and the create form never offered it as a
    choice, so its absence from the list is not an instruction to drop it."""
    assert "bash" in _ids(monkeypatch, "claude,codex")
    assert "bash" in _ids(monkeypatch, "omnigent")


def test_an_unrecognised_id_does_not_narrow_the_rest(monkeypatch):
    """Control Tower and this app ship separately. A CT that has learned a new
    agent id must not have the operator's whole selection second-guessed by an
    older terminal that has not."""
    assert _ids(monkeypatch, "claude,someagent") == ["claude", "bash"]
    assert config.workshop_agents() == ["claude", "someagent"]


def test_selection_is_case_and_whitespace_forgiving(monkeypatch):
    assert _ids(monkeypatch, " Claude , CODEX ") == ["claude", "codex", "bash"]


def test_a_deselected_agent_cannot_be_launched(client, monkeypatch):
    """The card is gone, so nothing in the UI offers it — but the launch path is
    what actually protects a run whose operator excluded an agent."""
    from tests.conftest import ALICE

    monkeypatch.setenv("WORKSHOP_AGENTS", "claude")

    listed = client.get("/api/agents", headers=ALICE).json()["agents"]
    assert [a["id"] for a in listed] == ["claude", "bash"]

    resp = client.post("/api/sessions", headers=ALICE, json={"agent_id": "codex"})
    assert resp.status_code == 404


def test_a_workshop_without_omnigent_does_not_install_it(monkeypatch):
    """Cold boot fetches the harness, tmux and pi. A run that will never launch
    Omnigent should not spend an attendee's first minutes on it."""
    monkeypatch.setenv("WORKSHOP_AGENTS", "claude,codex")

    assert config.omnigent_offered() is False
    specs = install._release_specs()
    assert specs["omnigent"][0] is False
    assert specs["pi"][0] is False


def test_picking_omnigent_does_not_override_the_platform_switch(monkeypatch):
    """Two independent decisions: the operator wants it, the deployment cannot
    run it. Either one saying no is enough."""
    monkeypatch.setenv("WORKSHOP_AGENTS", "omnigent,claude")
    monkeypatch.setenv("OMNIGENT_ENABLED", "false")

    assert config.omnigent_offered() is False
    assert "omnigent" not in _ids(monkeypatch, "omnigent,claude")
