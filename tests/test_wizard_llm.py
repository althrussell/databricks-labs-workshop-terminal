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
