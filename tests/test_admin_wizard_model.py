"""Changing the wizard's model with a room already in front of you.

The deployed pin is written to both the deployment env and ``app.yaml``, so a
console redeploy cannot silently revert it. That is the right shape for a
decision made before the room exists and the wrong shape for the failure it
addresses — a model answering badly with forty people waiting, which needs an
action measured in seconds.
"""

from __future__ import annotations

import pytest

from server import wizard_llm

CHAT = "mlflow/v1/chat/completions"


@pytest.fixture(autouse=True)
def clean_override():
    """Module-level state: a leak would silently change the next test's model."""
    yield
    wizard_llm.set_model_override("")


@pytest.fixture()
def serving(monkeypatch):
    def install(*names: str):
        monkeypatch.setattr(
            wizard_llm,
            "_served_models",
            lambda _t: {n: frozenset({CHAT}) for n in names},
        )
        monkeypatch.setattr(
            "server.credentials.credential_manager.token", lambda: "tok"
        )

    return install


def test_an_operator_can_swap_the_model_without_a_redeploy(
    client, as_admin, serving, monkeypatch
):
    serving("gpt-5-4-mini", "gpt-oss-20b")
    monkeypatch.setattr(
        wizard_llm.config, "workshop_wizard_model", lambda: "system.ai.gpt-5-4-mini"
    )

    resp = client.post("/api/admin/wizard-model", json={"model": "gpt-oss-20b"})

    assert resp.status_code == 200
    body = resp.json()
    assert body["applied"] == "system.ai.gpt-oss-20b"
    assert body["source"] == "override"
    assert wizard_llm._pick_model("tok") == "system.ai.gpt-oss-20b"


def test_an_empty_model_clears_the_swap(client, as_admin, serving, monkeypatch):
    serving("gpt-5-4-mini", "gpt-oss-20b")
    monkeypatch.setattr(
        wizard_llm.config, "workshop_wizard_model", lambda: "system.ai.gpt-5-4-mini"
    )
    client.post("/api/admin/wizard-model", json={"model": "gpt-oss-20b"})

    body = client.post("/api/admin/wizard-model", json={"model": ""}).json()

    assert body["source"] == "deployed"
    assert body["model"] == "system.ai.gpt-5-4-mini"


def test_a_model_this_workspace_does_not_serve_is_refused(
    client, as_admin, serving, monkeypatch
):
    """422 now beats a room whose idea grid quietly went static."""
    serving("gpt-5-4-mini")
    monkeypatch.setattr(wizard_llm.config, "workshop_wizard_model", lambda: "")

    resp = client.post(
        "/api/admin/wizard-model", json={"model": "system.ai.imaginary"}
    )

    assert resp.status_code == 422
    assert wizard_llm.model_override() == ""


def test_a_discovery_blip_does_not_take_the_control_away(
    client, as_admin, monkeypatch
):
    """Empty discovery means the call failed, not that nothing is served.

    Refusing here would remove the operator's last remaining action at exactly
    the moment the room is already unhappy.
    """
    monkeypatch.setattr(wizard_llm, "_served_models", lambda _t: {})
    monkeypatch.setattr(
        "server.credentials.credential_manager.token", lambda: "tok"
    )

    resp = client.post(
        "/api/admin/wizard-model", json={"model": "system.ai.gpt-oss-20b"}
    )

    assert resp.status_code == 200
    assert wizard_llm.model_override() == "system.ai.gpt-oss-20b"


def test_the_state_endpoint_reports_which_model_is_actually_in_force(
    client, as_admin, serving, monkeypatch
):
    """Because the override is ephemeral, a restart reverts it.

    If ``/state`` did not say so, the operator would go on believing the room
    is running the model they picked long after it stopped.
    """
    serving("gpt-oss-20b")
    monkeypatch.setattr(
        wizard_llm.config, "workshop_wizard_model", lambda: "system.ai.gpt-5-4-mini"
    )

    before = client.get("/api/admin/state").json()["wizard_model"]
    assert before["source"] == "deployed"
    assert before["override"] == ""

    client.post("/api/admin/wizard-model", json={"model": "gpt-oss-20b"})
    after = client.get("/api/admin/state").json()["wizard_model"]

    assert after["source"] == "override"
    assert after["model"] == "system.ai.gpt-oss-20b"
    # What a restart would revert to, stated rather than implied.
    assert after["deployed"] == "system.ai.gpt-5-4-mini"


def test_the_state_endpoint_names_the_chain_when_nothing_is_pinned(
    client, as_admin, monkeypatch
):
    monkeypatch.setattr(wizard_llm.config, "workshop_wizard_model", lambda: "")

    state = client.get("/api/admin/state").json()["wizard_model"]

    assert state["source"] == "chain"
    assert state["model"] == ""
    assert state["chain"], "an operator choosing a model needs to see the default"


def test_the_attendee_surfaces_name_the_swapped_model_too(
    client, as_admin, serving, monkeypatch
):
    """``/api/config`` and ``/api/wizard`` both publish the wizard's model.

    Both read the deployed pin before this change, so a swap left two places
    naming a model the room had stopped using — and those are the two places an
    operator checks when an attendee says the ideas look wrong.
    """
    serving("gpt-oss-20b")
    monkeypatch.setattr(
        wizard_llm.config, "workshop_wizard_model", lambda: "system.ai.gpt-5-4-mini"
    )
    client.post("/api/admin/wizard-model", json={"model": "gpt-oss-20b"})

    assert (
        client.get("/api/config").json()["llm_wizard"]["model"]
        == "system.ai.gpt-oss-20b"
    )
    assert (
        client.get("/api/wizard").json()["llm_wizard"]["model"]
        == "system.ai.gpt-oss-20b"
    )


def test_an_attendee_cannot_swap_the_room_onto_another_model(
    client, as_non_admin
):
    resp = client.post(
        "/api/admin/wizard-model", json={"model": "system.ai.gpt-oss-20b"}
    )

    assert resp.status_code in (401, 403)
    assert wizard_llm.model_override() == ""
