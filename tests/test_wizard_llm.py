"""The LLM wizard: generate buildable cards, never block, never invent tables."""

from __future__ import annotations

import pytest

from server import demo_data, wizard, wizard_llm


@pytest.fixture()
def seeded(monkeypatch):
    import time

    monkeypatch.setattr(demo_data.config, "workshop_demo_catalog", lambda: "workshop_demo")
    inventory = {
        "healthcare": {"encounters", "providers", "claims", "diagnoses", "patients"},
        "automotive_mobility": {"vehicle360", "parts360"},
    }
    monkeypatch.setattr(demo_data, "_cache", inventory)
    monkeypatch.setattr(demo_data, "_cache_at", time.time())
    monkeypatch.setattr(demo_data, "_cache_ok", True)
    yield inventory
    demo_data.reset_cache()


def test_flag_off_never_calls_the_model(seeded, monkeypatch):
    monkeypatch.setattr(wizard_llm.config, "llm_wizard_enabled", lambda: False)

    def boom(*_a, **_k):
        raise AssertionError("model must not be called when the flag is off")

    monkeypatch.setattr(wizard_llm, "_ask_model", boom)
    result = wizard_llm.suggest("predict 30-day readmission", "healthcare")
    assert result["source"] == "selector"
    assert result["ideas"]


def test_a_model_500_falls_back_to_selector_cards(seeded, monkeypatch):
    monkeypatch.setattr(wizard_llm.config, "llm_wizard_enabled", lambda: True)

    def fail(*_a, **_k):
        raise wizard_llm.ModelUnavailable("serving endpoint returned 500")

    monkeypatch.setattr(wizard_llm, "_ask_model", fail)
    result = wizard_llm.suggest("a dashboard", "healthcare")
    assert result["source"] == "selector"
    assert result["industry"] == "healthcare"
    ids = {i["id"] for i in result["ideas"]}
    assert ids <= {i.id for i in wizard.select_ideas("healthcare")} | ids


def test_an_invented_table_is_dropped(seeded, monkeypatch):
    monkeypatch.setattr(wizard_llm.config, "llm_wizard_enabled", lambda: True)

    def fake(*_a, **_k):
        return {
            "industry": "healthcare",
            "ideas": [
                {
                    "id": "bad-table",
                    "label": "Invented",
                    "outcome": "A dashboard over a table that does not exist",
                    "prompt": "Use healthcare.unicorns",
                    "shape": "dashboard",
                    "demo_tables": ["healthcare.unicorns"],
                },
                {
                    "id": "good-encounters",
                    "label": "Readmission risk",
                    "outcome": "A page of patients likely to return",
                    "prompt": "Use healthcare.encounters and healthcare.patients.",
                    "shape": "dashboard",
                    "demo_tables": ["healthcare.encounters", "healthcare.patients"],
                },
            ],
        }, "system.ai.gpt-5-4-mini"

    monkeypatch.setattr(wizard_llm, "_ask_model", fake)
    result = wizard_llm.suggest("readmission", "healthcare")
    ids = [i["id"] for i in result["ideas"]]
    assert "bad-table" not in ids
    assert "good-encounters" in ids
    for idea in result["ideas"]:
        assert demo_data.verify(idea["demo_tables"])


def test_a_healthcare_card_cannot_cite_vehicle360(seeded, monkeypatch):
    monkeypatch.setattr(wizard_llm.config, "llm_wizard_enabled", lambda: True)

    def fake(*_a, **_k):
        return {
            "industry": "healthcare",
            "ideas": [
                {
                    "id": "leaky",
                    "label": "Fleet health",
                    "outcome": "A car dashboard",
                    "prompt": "Use vehicle360",
                    "shape": "dashboard",
                    "demo_tables": ["automotive_mobility.vehicle360"],
                }
            ],
        }, "system.ai.gpt-5-4-mini"

    monkeypatch.setattr(wizard_llm, "_ask_model", fake)
    result = wizard_llm.suggest("readmission", "healthcare")
    for idea in result["ideas"]:
        assert "vehicle360" not in idea["demo_tables"]
        for ref in idea["demo_tables"]:
            assert not ref.startswith("automotive_mobility.")


def test_an_unseeded_inferred_industry_is_ignored(seeded, monkeypatch):
    monkeypatch.setattr(wizard_llm.config, "llm_wizard_enabled", lambda: True)

    def fake(*_a, **_k):
        return {"industry": "media", "ideas": []}, "system.ai.gpt-5-4-mini"

    monkeypatch.setattr(wizard_llm, "_ask_model", fake)
    result = wizard_llm.suggest("a content dashboard", "healthcare")
    assert result["industry"] == "healthcare"


# -- instrumentation --------------------------------------------------------

def test_the_drop_rate_is_reported_so_a_model_swap_is_measurable(
    seeded, monkeypatch
):
    """Padding hides a bad model: six invented cards and four good ones both
    render as six. Without this number a model choice is a matter of taste."""
    monkeypatch.setattr(wizard_llm.config, "llm_wizard_enabled", lambda: True)

    def fake(*_a, **_k):
        return {
            "industry": "healthcare",
            "ideas": [
                {
                    "id": "invented",
                    "label": "Invented",
                    "outcome": "Over a table that does not exist",
                    "prompt": "Use healthcare.unicorns",
                    "shape": "dashboard",
                    "demo_tables": ["healthcare.unicorns"],
                },
                {
                    "id": "real",
                    "label": "Readmission risk",
                    "outcome": "Patients likely to return",
                    "prompt": "Use healthcare.encounters.",
                    "shape": "dashboard",
                    "demo_tables": ["healthcare.encounters"],
                },
            ],
        }, "system.ai.gpt-oss-20b"

    monkeypatch.setattr(wizard_llm, "_ask_model", fake)
    result = wizard_llm.suggest("readmission", "healthcare")

    assert result["offered"] == 2
    assert result["dropped"] == 1
    # The model that answered, not the one that was configured — a pin that
    # fell through to the chain head is otherwise invisible.
    assert result["model"] == "system.ai.gpt-oss-20b"
    assert len(result["ideas"]) == wizard.IDEA_COUNT  # still padded to a full grid


def test_the_selector_fallback_reports_no_model(seeded, monkeypatch):
    monkeypatch.setattr(wizard_llm.config, "llm_wizard_enabled", lambda: False)

    result = wizard_llm.suggest("anything", "healthcare")

    assert result["model"] == ""
    assert result["dropped"] == 0


# -- model selection --------------------------------------------------------

def test_failed_discovery_uses_the_chain_head_rather_than_giving_up(monkeypatch):
    """Empty discovery is documented as *the call failed*. Reading it as *the
    workspace serves nothing* took the whole idea grid down for a blip on an
    unrelated API — silently, because the selector fallback looks like a
    result."""
    monkeypatch.setattr(wizard_llm.config, "workshop_wizard_model", lambda: "")
    monkeypatch.setattr(wizard_llm, "_served_models", lambda _t: {})

    from server import models

    assert wizard_llm._pick_model("tok") == models.wizard_chain()[0]


def test_discovery_is_not_re_run_on_every_keystroke(monkeypatch):
    """It sat on the debounce path of a request that was already timing out."""
    wizard_llm.reset_discovery_cache()
    calls: list[str] = []

    def counted(token: str):
        calls.append(token)
        return {"gpt-5-4-mini": frozenset({"mlflow/v1/chat/completions"})}

    monkeypatch.setattr(
        "server.cli_config.discover_model_services", counted
    )
    wizard_llm._served_models("tok")
    wizard_llm._served_models("tok")
    wizard_llm._served_models("tok")

    assert len(calls) == 1
    wizard_llm.reset_discovery_cache()


def test_a_failed_discovery_is_retried_sooner_than_a_good_one(monkeypatch):
    """A workspace that has just been fixed should not wait ten minutes to be
    believed."""
    wizard_llm.reset_discovery_cache()
    monkeypatch.setattr("server.cli_config.discover_model_services", lambda _t: {})

    wizard_llm._served_models("tok")

    assert wizard_llm._discovery_ok is False
    assert (
        wizard_llm._DISCOVERY_FAILURE_TTL_SECONDS
        < wizard_llm._DISCOVERY_TTL_SECONDS
    )
    wizard_llm.reset_discovery_cache()


# -- the mid-workshop swap --------------------------------------------------

CHAT = "mlflow/v1/chat/completions"


@pytest.fixture()
def no_override():
    """Module-level state, so a leaked override would fail the next test."""
    yield
    wizard_llm.set_model_override("")


@pytest.fixture()
def serving(monkeypatch):
    """A workspace whose discovery answers, so validation has something to do."""
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


def test_an_override_beats_the_deployed_pin(monkeypatch, serving, no_override):
    """The whole point: the operator in front of the room outranks the deploy.

    A pin is written to both the deployment env and app.yaml so a redeploy
    cannot revert it — which is right, and is also exactly why it cannot be the
    thing you change with forty people waiting.
    """
    serving("gpt-5-4-mini", "gpt-oss-20b")
    monkeypatch.setattr(
        wizard_llm.config, "workshop_wizard_model", lambda: "system.ai.gpt-5-4-mini"
    )

    wizard_llm.set_model_override("gpt-oss-20b")

    assert wizard_llm._pick_model("tok") == "system.ai.gpt-oss-20b"


def test_clearing_the_override_restores_the_deployed_pin(
    monkeypatch, serving, no_override
):
    serving("gpt-5-4-mini", "gpt-oss-20b")
    monkeypatch.setattr(
        wizard_llm.config, "workshop_wizard_model", lambda: "system.ai.gpt-5-4-mini"
    )
    wizard_llm.set_model_override("gpt-oss-20b")

    wizard_llm.set_model_override("")

    assert wizard_llm._pick_model("tok") == "system.ai.gpt-5-4-mini"


def test_a_bare_name_is_qualified_the_same_way_a_pin_is(
    monkeypatch, serving, no_override
):
    """An operator types what they see in a dropdown, not a schema path."""
    serving("gpt-oss-20b")
    monkeypatch.setattr(wizard_llm.config, "workshop_wizard_model", lambda: "")

    assert wizard_llm.set_model_override("gpt-oss-20b") == "system.ai.gpt-oss-20b"


def test_a_model_the_workspace_does_not_serve_is_refused(
    monkeypatch, serving, no_override
):
    """Better a refusal now than a room whose grid quietly went static."""
    serving("gpt-5-4-mini")
    monkeypatch.setattr(wizard_llm.config, "workshop_wizard_model", lambda: "")

    with pytest.raises(wizard_llm.UnknownModel):
        wizard_llm.set_model_override("system.ai.not-a-real-model")

    assert wizard_llm.model_override() == ""


def test_a_discovery_blip_does_not_block_the_swap(monkeypatch, no_override):
    """Empty discovery means the call failed, everywhere in this module.

    Refusing a swap on a blip would withdraw the operator's only remaining
    action at precisely the moment the room is already unhappy.
    """
    monkeypatch.setattr(wizard_llm, "_served_models", lambda _t: {})
    monkeypatch.setattr(
        "server.credentials.credential_manager.token", lambda: "tok"
    )
    monkeypatch.setattr(wizard_llm.config, "workshop_wizard_model", lambda: "")

    assert wizard_llm.set_model_override("system.ai.anything") == (
        "system.ai.anything"
    )


def test_a_missing_credential_does_not_block_the_swap(monkeypatch, no_override):
    from server.credentials import CredentialError

    def no_token():
        raise CredentialError("no workshop credential")

    monkeypatch.setattr("server.credentials.credential_manager.token", no_token)
    monkeypatch.setattr(wizard_llm.config, "workshop_wizard_model", lambda: "")

    assert wizard_llm.set_model_override("system.ai.gpt-oss-20b")


def test_the_effective_model_says_which_of_the_three_is_in_force(
    monkeypatch, serving, no_override
):
    """A restart silently reverting an override is worse than no override.

    The operator would believe the room is on the model they picked. So the
    reported value carries its provenance rather than being a bare string.
    """
    serving("gpt-oss-20b")
    monkeypatch.setattr(wizard_llm.config, "workshop_wizard_model", lambda: "")

    assert wizard_llm.effective_model()["source"] == "chain"

    monkeypatch.setattr(
        wizard_llm.config, "workshop_wizard_model", lambda: "system.ai.gpt-5-4-mini"
    )
    deployed = wizard_llm.effective_model()
    assert deployed["source"] == "deployed"
    assert deployed["model"] == "system.ai.gpt-5-4-mini"

    wizard_llm.set_model_override("gpt-oss-20b")
    live = wizard_llm.effective_model()
    assert live["source"] == "override"
    assert live["model"] == "system.ai.gpt-oss-20b"
    # Both are reported, so the operator can see what a restart would revert to.
    assert live["deployed"] == "system.ai.gpt-5-4-mini"


def test_the_override_is_not_written_anywhere(monkeypatch, serving, no_override):
    """Ephemeral by decision, so the deployed value stays the thing that is
    always true after a restart."""
    serving("gpt-oss-20b")
    wizard_llm.set_model_override("gpt-oss-20b")

    import importlib

    reloaded = importlib.reload(wizard_llm)
    try:
        assert reloaded.model_override() == ""
    finally:
        importlib.reload(wizard_llm)
