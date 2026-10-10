"""Recommendation faults must preserve a usable, truthful selection journey."""

import copy
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from server import bounded_work, demo_data, wizard, wizard_llm


def idea(**changes):
    return {"id": "orders", "label": "Pack bakery orders", "outcome": "Staff can pack orders in time",
            "prompt": "Build a bakery order queue with one packed action and remembered updates.",
            "shape": "app", "intents": ["business_problem"], "products": ["Databricks Apps"],
            "technical": False, "demo_tables": [], "data_mode": "generate", "required_columns": [],
            "fit_reason": "A staff queue makes the orders needing attention easy to find.",
            "first_version": "Today's orders, urgent first, with a packed action.",
            "assumptions": ["Label sample orders; explore prepared data first"],
            "unresolved": ["What makes an order need attention?"], **changes}


@pytest.mark.parametrize("field,value", [("demo_tables", 3), ("intents", "fun"), ("products", {}),
    ("assumptions", [3]), ("unresolved", None), ("technical", "false"), ("prompt", ""),
    ("fit_reason", None), ("data_mode", "demo"), ("shape", "unknown")])
def test_malformed_fields_are_rejected_without_raising(field, value):
    assert wizard_llm._coerce_idea(idea(**{field: value}), "") is None


@pytest.mark.parametrize("field,limit", [
    ("label", 80), ("outcome", 200), ("fit_reason", 240), ("first_version", 300),
    ("assumptions", 240), ("unresolved", 240),
])
def test_oversized_visible_copy_is_rejected_instead_of_cutting_the_task(field, limit):
    value = "A" * (limit - 5) + " keep this complete"
    changed = [value] if field in {"assumptions", "unresolved"} else value
    assert wizard_llm._coerce_idea(idea(**{field: changed}), "") is None


def test_decoder_capped_explanation_does_not_become_a_displayed_card():
    # Retained from the exact 8a3e617 deployed regression: valid JSON whose
    # explanation ends in a partial word at the configured string cap.
    reason = (
        "This matches the request for a phone-friendly app and a single useful action. "
        "Prepared source suitability was not established from the metadata shown, so "
        "the demo should use a generated sample unless verified stock fields are found in the 워"
    )
    assert len(reason) == 240
    assert wizard_llm._coerce_idea(idea(fit_reason=reason), "retail") is None


def test_prepared_sample_does_not_assume_rows_on_the_current_date(monkeypatch):
    monkeypatch.setattr(demo_data, "verify", lambda _: True)
    monkeypatch.setattr(demo_data, "supports", lambda *_a, **_k: True)
    card = idea(
        demo_tables=["retail.orders"], data_mode="demo",
        required_columns=[{"table": "retail.orders", "columns": ["order_id"]}],
        assumptions=["Today's orders means orders placed on the current date in the sample preview."],
    )
    assert wizard_llm._coerce_idea(card, "") is None


def test_current_period_warning_does_not_discard_a_useful_sample_card(monkeypatch):
    monkeypatch.setattr(demo_data, "verify", lambda _: True)
    monkeypatch.setattr(demo_data, "supports", lambda *_a, **_k: True)
    card = idea(
        demo_tables=["retail.orders"], data_mode="demo",
        required_columns=[{"table": "retail.orders", "columns": ["order_id"]}],
        assumptions=["Do not assume there are rows on the current date; choose a seeded day."],
    )
    assert wizard_llm._coerce_idea(card, "") is not None
    card["assumptions"][0] += " The preview uses orders from the current day."
    assert wizard_llm._coerce_idea(card, "") is None
    card["assumptions"] = ["Orders on the current date are used for this demo, not live shop records."]
    assert wizard_llm._coerce_idea(card, "") is None


def test_demo_period_binding_preserves_attributed_attendee_words(monkeypatch):
    monkeypatch.setattr(demo_data, "verify", lambda _: True)
    monkeypatch.setattr(demo_data, "supports", lambda *_a, **_k: True)
    quote = "The attendee asked for today's orders."
    card = idea(
        demo_tables=["retail.orders"], data_mode="demo",
        required_columns=[{"table": "retail.orders", "columns": ["order_id"]}],
        prompt=quote + " Propose a sample preview using a day present in the prepared rows.",
    )
    result = wizard_llm._coerce_idea(card, "")
    assert result is not None
    assert quote in result.prompt


def test_prepared_demo_period_also_binds_visible_assumptions(monkeypatch):
    monkeypatch.setattr(demo_data, "verify", lambda _: True)
    monkeypatch.setattr(demo_data, "supports", lambda *_a, **_k: True)
    card = idea(
        demo_tables=["retail.orders"], data_mode="demo",
        required_columns=[{"table": "retail.orders", "columns": ["order_id"]}],
        assumptions=["Today's orders can be represented with the prepared retail order sample."],
    )
    original = copy.deepcopy(card)
    result = wizard_llm._coerce_idea(card, "")
    assert result is not None and card == original
    assert "today" not in " ".join(result.assumptions).lower()
    assert "demo day" in " ".join(result.assumptions).lower()


@pytest.mark.parametrize("field", ["assumptions", "fit_reason", "unresolved", "prompt"])
@pytest.mark.parametrize("claim", [
    "Volunteer records are not available from prepared sources.",
    "No staffing or capacity table is available.",
    "There is no matching dataset in the prepared catalog.",
    "Prepared sources contain no volunteer records.",
])
def test_bounded_metadata_cannot_establish_source_absence(field, claim):
    value = [claim] if field in {"assumptions", "unresolved"} else claim
    assert wizard_llm._coerce_idea(idea(**{field: value}), "") is None


def test_source_unknowns_and_exploration_warnings_remain_useful():
    card = idea(
        assumptions=["Do not assume there are no volunteer records in prepared sources."],
        unresolved=["Staffing or capacity data was not verified in the prepared sources."],
    )
    assert wizard_llm._coerce_idea(card, "") is not None
    card["assumptions"][0] += " Volunteer records are not available from prepared sources."
    assert wizard_llm._coerce_idea(card, "") is None


@pytest.mark.parametrize("limitation", [
    "No connected source is required for this workshop demo.",
    "No prepared sources have been verified for this proposal.",
    "No verified prepared dataset has been identified yet.",
    "No prepared table updates are allowed; use owned working storage.",
    "Make no changes to prepared tables.",
])
def test_source_verification_unknowns_and_write_restrictions_are_not_absence(limitation):
    assert wizard_llm._coerce_idea(idea(assumptions=[limitation]), "") is not None


def test_generated_scope_is_visibly_proposed_before_selection():
    # Actual a1e182b output specialized an unspecified team to vehicles/parts.
    # Every model offer must mark its scope as a proposal, regardless of how
    # confidently the model writes its fit explanation or assumptions.
    card = idea(
        outcome="Sample preview of the top 10 vehicles or parts to work on next.",
        fit_reason="The automotive sample has fields for a small ranked demo.",
        assumptions=["'What to work on next' means a priority queue for service or parts work."],
        unresolved=["The attendee's exact team and business object are not specified."],
    )
    result = wizard_llm._coerce_idea(card, "")
    assert result is not None
    assert result.fit_reason.startswith("One possible example: ")
    assert any("proposed example" in value.lower() for value in result.assumptions)
    assert "Adapt its scope to the attendee's team and goal" in result.prompt
    assert result.prompt.startswith(card["prompt"])


def test_generated_demo_handoff_requires_discovery_before_invention():
    card = idea(prompt="Build a small volunteer roster demo with proposed sample shifts.")
    result = wizard_llm._coerce_idea(card, "")
    assert result is not None
    assert result.prompt.startswith(card["prompt"])
    assert "Explore the prepared workshop catalog before creating sample data" in result.prompt
    assert "suitability has not been established" in result.prompt


def test_cold_configured_catalog_is_unverified_not_absent(monkeypatch):
    demo_data.reset_cache()
    monkeypatch.setattr(demo_data.config, "workshop_demo_catalog", lambda: "prepared_demo")
    release, finished = threading.Event(), threading.Event()
    def load():
        try:
            assert release.wait(2)
            return {"retail": {"inventory_daily"}}, {}
        finally:
            finished.set()
    monkeypatch.setattr(demo_data, "_load", load)
    try:
        prompt_inventory = wizard_llm._inventory_lines("retail", query="Show low stock")
        assert "no demo catalog" not in prompt_inventory.lower()
        assert "configured" in prompt_inventory.lower()
        assert "unverified" in prompt_inventory.lower()
        assert "absence" in prompt_inventory.lower()
        release.set()
        assert finished.wait(1)
        assert demo_data.inventory() == {"retail": {"inventory_daily"}}
    finally:
        release.set()
        finished.wait(1)
        demo_data.reset_cache()


def test_deliberate_suggestion_allows_a_short_cold_inventory_read(monkeypatch):
    demo_data.reset_cache()
    monkeypatch.setattr(demo_data.config, "workshop_demo_catalog", lambda: "prepared_demo")
    finished = threading.Event()
    def load():
        try:
            time.sleep(.45)
            return {"retail": {"inventory_daily"}}, {}
        finally:
            finished.set()
    monkeypatch.setattr(demo_data, "_load", load)
    monkeypatch.setattr(demo_data, "column_inventory",
        lambda tables, **_kwargs: {table: {"on_hand"} for table in tables})
    try:
        context = wizard_llm._inventory_lines("retail", query="Show low stock",
            deadline=time.monotonic() + 2)
        assert "retail.inventory_daily: on_hand" in context
    finally:
        assert finished.wait(2)
        demo_data.reset_cache()


def test_declarations_and_prompt_must_agree(monkeypatch):
    monkeypatch.setattr(demo_data, "verify", lambda _: True)
    monkeypatch.setattr(demo_data, "supports", lambda *_a, **_k: True)
    assert wizard_llm._coerce_idea(idea(prompt="Use retail.orders", demo_tables=[]), "") is None
    assert wizard_llm._coerce_idea(idea(demo_tables=["retail.orders"], data_mode="demo"), "retail") is None
    assert wizard_llm._coerce_idea(idea(prompt="Use retail.orders to pack orders.",
        demo_tables=["retail.orders"], data_mode="demo", required_columns=[{"table": "retail.orders", "columns": ["order_id"]}]), "retail") is not None


def test_compact_multi_table_handoff_is_preserved_without_truncation(monkeypatch):
    monkeypatch.setattr(demo_data, "verify", lambda _: True)
    monkeypatch.setattr(demo_data, "supports", lambda *_a, **_k: True)
    prompt = (
        "Build a read-only sample stock dashboard for store managers using retail.inventory_daily, "
        "retail.products and retail.stores. Start by querying the available snapshot dates; show "
        "that seeded date clearly rather than claiming current store records. Rank low stock by "
        "store and product, with on-hand and on-order counts, a store picker and a product filter. "
        "Inspect join keys and duplicate rows before calculating totals. Use a labelled demo "
        "threshold for the low-stock flag. This is a dashboard with no app, edit action or live feed."
    )
    assert 500 < len(prompt) <= wizard_llm._MAX_PROMPT_CHARS
    card = idea(prompt=prompt, shape="dashboard", products=["Databricks SQL"], data_mode="demo",
                demo_tables=["retail.inventory_daily", "retail.products", "retail.stores"],
                required_columns=[{"table": "retail.inventory_daily", "columns": ["store_id", "product_id", "snapshot_date", "on_hand"]},
                                  {"table": "retail.products", "columns": ["product_id", "product_name"]},
                                  {"table": "retail.stores", "columns": ["store_id", "store_name"]}])
    accepted = wizard_llm._coerce_idea(card, "retail")
    assert accepted is not None and accepted.prompt.startswith(prompt + "\n\n")
    assert "Only table/column metadata is checked" in accepted.prompt
    assert wizard_llm._coerce_idea(idea(prompt="x" * (wizard_llm._MAX_PROMPT_CHARS + 1)), "") is None


def test_verified_dependencies_are_bound_before_the_card_is_offered(monkeypatch):
    monkeypatch.setattr(demo_data, "verify", lambda _: True)
    monkeypatch.setattr(demo_data, "supports", lambda *_a, **_k: True)
    raw = idea(prompt="Build a sample bakery order queue using prepared retail sources.",
               data_mode="demo", demo_tables=["retail.orders", "retail.order_items"],
               required_columns=[{"table": "retail.orders", "columns": ["order_id"]},
                                 {"table": "retail.order_items", "columns": ["order_id", "quantity"]}])
    original = copy.deepcopy(raw)
    accepted = wizard_llm._coerce_idea(raw, "retail")
    assert accepted is not None and raw == original
    assert accepted.prompt.startswith(raw["prompt"] + "\n\n")
    assert all(table in accepted.prompt for table in raw["demo_tables"])
    assert "Prepared read-only workshop sources:" in accepted.prompt
    assert "owned working storage for updates" in accepted.prompt
    assert len(accepted.prompt) <= wizard_llm._MAX_HANDOFF_CHARS
    assert accepted.label == raw["label"] and accepted.shape == raw["shape"]
    # Binding never legitimises an undeclared source or unverified dependency.
    assert wizard_llm._coerce_idea({**raw, "prompt": "Use retail.unknown for these orders."}, "retail") is None
    monkeypatch.setattr(demo_data, "supports", lambda *_a, **_k: False)
    assert wizard_llm._coerce_idea(raw, "retail") is None


def test_confirmed_industry_is_independent_of_prepared_source_schema(monkeypatch):
    monkeypatch.setattr(wizard_llm.config, "llm_wizard_enabled", lambda: True)
    monkeypatch.setattr(demo_data, "verify", lambda _: True)
    monkeypatch.setattr(demo_data, "supports", lambda *_a, **_k: True)
    monkeypatch.setattr(wizard_llm, "_fallback", lambda *_a, **_k: {"ideas": []})
    raw = idea(label="Bakery order demo", outcome="Pack sample bakery orders",
               prompt="Adapt prepared sample orders into a bakery packing queue.",
               data_mode="demo", demo_tables=["retail.orders"],
               required_columns=[{"table": "retail.orders", "columns": ["order_id"]}])
    monkeypatch.setattr(wizard_llm, "_ask_model", lambda *_a, **_k: (
        {"industry": "retail", "ideas": [raw]}, "approved-model"))
    result = wizard_llm.suggest("An app to pack my bakery orders", "bakery", industry_locked=True)
    assert result["source"] == "llm" and result["industry"] == "bakery"
    assert result["ideas"][0]["industries"] == ["bakery"]
    assert result["ideas"][0]["demo_tables"] == ["retail.orders"]
    assert "retail.orders" in result["ideas"][0]["prompt"]
    # Removing the industry/schema coupling keeps dependency validation intact.
    mixed = raw | {"demo_tables": ["retail.orders", "healthcare.patients"]}
    assert wizard_llm._coerce_idea(mixed, "bakery") is None
    monkeypatch.setattr(demo_data, "supports", lambda *_a, **_k: False)
    assert wizard_llm._coerce_idea(raw, "bakery") is None


@pytest.mark.parametrize("locked", [False, True])
def test_known_industry_context_does_not_reject_verified_task_sources(monkeypatch, locked):
    monkeypatch.setattr(wizard_llm.config, "llm_wizard_enabled", lambda: True)
    monkeypatch.setattr(demo_data, "verify", lambda _: True)
    monkeypatch.setattr(demo_data, "supports", lambda *_a, **_k: True)
    monkeypatch.setattr(wizard_llm, "_fallback", lambda *_a, **_k: {"ideas": []})
    raw = idea(label="Hospital demand demo", outcome="See sample department demand",
               prompt="Summarise sample hospital encounters by department; inspect dates and joins first.",
               data_mode="demo", demo_tables=["healthcare.encounters"],
               required_columns=[{"table": "healthcare.encounters", "columns": ["encounter_id"]}])
    monkeypatch.setattr(wizard_llm, "_ask_model", lambda *_a, **_k: (
        {"industry": "automotive_mobility", "ideas": [raw]}, "approved-model"))
    result = wizard_llm.suggest("Show hospital department demand, even though this room is about car sales.",
                                "automotive_mobility", industry_locked=locked)
    assert result["source"] == "llm"
    assert result["industry"] == "automotive_mobility"
    assert result["ideas"][0]["demo_tables"] == ["healthcare.encounters"]
    assert "healthcare.encounters (encounter_id)" in result["ideas"][0]["prompt"]
    assert wizard_llm._coerce_idea(raw | {"demo_tables": ["healthcare.invented"]}, "automotive_mobility") is None
    monkeypatch.setattr(demo_data, "supports", lambda *_a, **_k: False)
    assert wizard_llm._coerce_idea(raw, "automotive_mobility") is None


def test_sample_period_and_verified_columns_are_bound_before_offer(monkeypatch):
    monkeypatch.setattr(demo_data, "verify", lambda _: True)
    monkeypatch.setattr(demo_data, "supports", lambda *_a, **_k: True)
    raw = idea(label="Today's order demo", outcome="See today's sample orders",
               fit_reason="Show today’s orders in a sample preview.",
               first_version="A queue for today", prompt="Build today's sample order queue.",
               data_mode="demo", demo_tables=["retail.orders"],
               required_columns=[{"table": "retail.orders", "columns": ["order_id", "order_date"]}])
    original = copy.deepcopy(raw)
    accepted = wizard_llm._coerce_idea(raw, "bakery")
    assert accepted is not None and raw == original
    assert accepted.outcome == "See the demo day's sample orders"
    assert accepted.first_version == "A queue for the demo day"
    assert all("today" not in getattr(accepted, field).lower()
               for field in ("label", "outcome", "fit_reason", "first_version"))
    assert accepted.prompt.startswith(raw["prompt"])
    assert "choose a date present in the inspected sample rows" in accepted.prompt
    assert "retail.orders (order_id, order_date)" in accepted.prompt
    assert "Inspect rows, seeded dates, joins and execution permissions" in accepted.prompt
    # A new input-driven demo can use today's date; it has no seeded period to
    # mistake for the attendee's current records.
    generated = raw | {"data_mode": "generate", "demo_tables": [], "required_columns": []}
    assert wizard_llm._coerce_idea(generated, "bakery").outcome == raw["outcome"]


def test_full_offered_counts_and_concept_duplicates():
    raw = [idea(id=f"orders-{n}") for n in range(6)]
    accepted, offered = wizard_llm._verified_ideas(raw, "")
    assert offered == 6 and len(accepted) == 1


def test_valid_but_unshown_cards_do_not_inflate_rejection_counts():
    raw = [idea(id=f"orders-{n}", label=f"Useful order variation {n}") for n in range(6)]
    stats = {}
    accepted, offered = wizard_llm._verified_ideas(raw, "", stats=stats)
    assert offered == 6 and len(accepted) == 3
    assert stats == {"valid": 6, "accepted": 3, "rejected": 0, "not_shown": 3, "unchecked": 0}


def test_column_check_uses_the_app_client_and_rejects_missing_join_columns(monkeypatch):
    calls = []
    demo_data.reset_cache()
    monkeypatch.setattr(demo_data.config, "workshop_demo_catalog", lambda: "prepared_demo")
    def get(*, full_name):
        calls.append(full_name)
        return SimpleNamespace(columns=[SimpleNamespace(name="order_id"), SimpleNamespace(name="due_at")])
    monkeypatch.setattr(demo_data.credentials, "workspace_client", lambda: SimpleNamespace(tables=SimpleNamespace(get=get)))
    assert demo_data.supports({"retail.orders": ["order_id", "due_at"]})
    assert not demo_data.supports({"retail.orders": ["customer_id"]})
    assert calls == ["prepared_demo.retail.orders"]
    demo_data.reset_cache()


def test_cold_verified_columns_can_use_remaining_request_budget(monkeypatch):
    demo_data.reset_cache()
    monkeypatch.setattr(demo_data.config, "workshop_demo_catalog", lambda: "prepared_demo")
    monkeypatch.setattr(demo_data, "verify", lambda _: True)
    def get(*, full_name):
        time.sleep(.35)
        return SimpleNamespace(columns=[SimpleNamespace(name="order_id")])
    monkeypatch.setattr(demo_data.credentials, "workspace_client", lambda: SimpleNamespace(tables=SimpleNamespace(get=get)))
    raw = idea(data_mode="demo", demo_tables=["retail.orders"],
               required_columns=[{"table": "retail.orders", "columns": ["order_id"]}])
    try:
        accepted = wizard_llm._coerce_idea(raw, "retail", deadline=time.monotonic() + 1.5)
        assert accepted is not None
        # The completed metadata read cannot legitimise an absent source field.
        assert wizard_llm._coerce_idea(raw | {"required_columns": [
            {"table": "retail.orders", "columns": ["missing_join_key"]}]}, "retail") is None
    finally:
        deadline = time.monotonic() + 1
        while demo_data._capabilities.active and time.monotonic() < deadline:
            time.sleep(.01)
        demo_data.reset_cache()


def test_explicit_app_request_rejects_other_shapes_and_keeps_failure_counts():
    stats = {}
    accepted, offered = wizard_llm._verified_ideas([
        idea(id="chart", label="Chart orders", shape="dashboard"),
        idea(id="orders", label="Pack orders", shape="app")], "", required_shape="app", stats=stats)
    assert offered == 2 and [item.id for item in accepted] == ["orders"]
    assert stats["rejected"] == 1 and stats["accepted"] == 1


@pytest.mark.parametrize("words,expected", [
    ("Build a bakery app", "app"), ("Make a website for volunteers", "app"),
    ("A dashboard page for stock levels", ""), ("A tool for exploring customer spending", ""),
    ("A dashboard, not an app", "no_app"), ("I don't want a separate web app", "no_app"),
    ("Use a dashboard instead of an app", "no_app")])
def test_app_format_choice_is_not_inferred_from_generic_or_negated_words(words, expected):
    assert wizard.app_intent(words) == expected


@pytest.mark.parametrize("words", [
    "Show which devices appear most in the recorded sample website visits.",
    "Compare app usage across the sample regions.",
    "Show a dashboard of website traffic.",
    "Help me explore app crashes in the recorded samples.",
])
def test_app_source_metrics_do_not_require_an_app_output(words):
    assert wizard.app_intent(words) == ""


def test_app_output_can_use_website_visits_as_its_source():
    assert wizard.app_intent("Build an app to compare sample website visits.") == "app"
    assert wizard.app_intent("A dashboard of website visits, not an app.") == "no_app"


def test_server_proposal_note_does_not_reject_valid_complete_copy():
    sentence = "This proposed queue helps staff find the orders needing attention and record their next action. "
    reason = sentence + "The source suitability remains unverified; explore prepared samples before choosing the working data for this small first version."
    assert 240 - len(wizard_llm._PROPOSAL_PREFIX) < len(reason) <= 240
    result = wizard_llm._coerce_idea(idea(fit_reason=reason), "")
    assert result is not None
    assert result.fit_reason == wizard_llm._PROPOSAL_PREFIX + reason
    assert reason in result.fit_reason


def test_declining_an_app_keeps_curated_and_generated_alternatives(monkeypatch):
    monkeypatch.setattr(demo_data.config, "workshop_demo_catalog", lambda: "")
    assert all(card.shape != "app" for card in wizard.select_ideas(query="A simple dashboard, not an app"))
    accepted, _ = wizard_llm._verified_ideas([
        idea(id="app", label="Order app"), idea(id="dashboard", label="Order dashboard", shape="dashboard", products=["dashboards"])],
        "", excluded_shape="app")
    assert [card.shape for card in accepted] == ["dashboard"]


def test_an_app_refusal_also_excludes_disguised_app_dependencies():
    accepted, offered = wizard_llm._verified_ideas([
        idea(id="doc", label="Document assistant", shape="ai", products=["serving", "apps"]),
        idea(id="dashboard", label="Order dashboard", shape="dashboard", products=["dashboards"])],
        "", excluded_shape="app")
    assert offered == 2 and [card.id for card in accepted] == ["dashboard"]


def test_invalid_generation_cannot_retarget_the_fallback_industry(monkeypatch):
    monkeypatch.setattr(wizard_llm.config, "llm_wizard_enabled", lambda: True)
    monkeypatch.setattr(demo_data, "enabled", lambda: True)
    monkeypatch.setattr(demo_data, "has_industry", lambda _: True)
    monkeypatch.setattr(wizard_llm, "_ask_model", lambda *_a, **_k: (
        {"industry": "financial_services", "ideas": [idea(data_mode="demo")]}, "test-model"))
    monkeypatch.setattr(wizard, "select_ideas", lambda industry, **_k: [] if not industry else pytest.fail("An invalid result retargeted the task"))
    result = wizard_llm.suggest("A read-only stock dashboard, not an app")
    assert result["industry"] == "" and result["ideas"] == []
    assert result["offered"] == result["rejected"] == 1
    assert "could not be verified" in result["fallback_reason"]


def test_curated_fallback_does_not_replace_an_unmatched_task(monkeypatch):
    monkeypatch.setattr(demo_data.config, "workshop_demo_catalog", lambda: "")
    assert wizard.select_ideas(query="Help my bakery staff keep track of today's orders") == []


def test_generic_team_priority_words_cannot_select_a_sector_pipeline(monkeypatch):
    monkeypatch.setattr(demo_data.config, "workshop_demo_catalog", lambda: "prepared_demo")
    monkeypatch.setattr(demo_data, "verify", lambda _: True)
    monkeypatch.setattr(demo_data, "supports", lambda *_a, **_k: True)
    assert wizard.select_ideas(query="I want an easy way to know what our team should work on next.") == []


def test_readings_summary_fallback_preserves_the_requested_action(monkeypatch):
    monkeypatch.setattr(demo_data.config, "workshop_demo_catalog", lambda: "prepared_demo")
    monkeypatch.setattr(demo_data, "verify", lambda _: True)
    monkeypatch.setattr(demo_data, "supports", lambda *_a, **_k: True)
    cards = wizard.select_ideas(query="Help me try turning some prepared readings into a useful summary.", intent="learning")
    assert cards and cards[0].id in {"mining-telemetry-pipeline", "energy-meter-stream", "mfg-sensor-stream"}
    assert all(card.id != "auto-telematics-landing" for card in cards)


def test_an_unstated_sector_can_find_task_matched_prepared_data(monkeypatch):
    monkeypatch.setattr(demo_data.config, "workshop_demo_catalog", lambda: "prepared_demo")
    monkeypatch.setattr(demo_data, "verify", lambda _: True)
    monkeypatch.setattr(demo_data, "supports", lambda *_a, **_k: True)
    cards = wizard.select_ideas(query="A read-only stock dashboard, not an app")
    assert cards and cards[0].id == "retail-stockouts"
    assert all(not wizard.uses_app(card) for card in cards)


def test_requested_device_changes_the_first_task_matched_fallback(monkeypatch):
    monkeypatch.setattr(demo_data.config, "workshop_demo_catalog", lambda: "prepared_demo")
    monkeypatch.setattr(demo_data, "verify", lambda _: True)
    monkeypatch.setattr(demo_data, "supports", lambda *_a, **_k: True)
    ordinary = wizard.select_ideas("retail", query="View stock running low")
    phone = wizard.select_ideas("retail", query="Give store managers a phone-friendly view of stock running low")
    assert ordinary[0].id == "retail-stockouts"
    assert phone[0].id == "retail-store-app"
    assert "phone-friendly" in phone[0].prompt
    assert wizard._task_tokens("stock running low") & wizard._task_terms(phone[0])


def test_model_rejection_records_only_a_bounded_diagnostic_reason(monkeypatch, caplog):
    monkeypatch.setattr(demo_data, "verify", lambda _: True)
    monkeypatch.setattr(demo_data, "supports", lambda *_a, **_k: False)
    with caplog.at_level("INFO", logger="server.wizard_llm"):
        rejected = wizard_llm._coerce_idea(idea(
            label="Private synthetic attendee words", prompt="Use retail.orders for a sample queue.",
            demo_tables=["retail.orders"], data_mode="demo",
            required_columns=[{"table": "retail.orders", "columns": ["order_id"]}]), "retail")
    assert rejected is None
    assert "unverified_prepared_columns" in caplog.text
    assert "Private synthetic attendee words" not in caplog.text
    assert "retail.orders" not in caplog.text


def test_model_inventory_uses_actual_columns_and_omits_unknown_names(monkeypatch):
    demo_data.reset_cache()
    monkeypatch.setattr(demo_data.config, "workshop_demo_catalog", lambda: "prepared_demo")
    monkeypatch.setattr(demo_data, "verify", lambda _: True)
    monkeypatch.setattr(demo_data, "inventory", lambda **_kwargs: {"retail": {"orders"}})
    monkeypatch.setattr(demo_data.credentials, "workspace_client", lambda: SimpleNamespace(tables=SimpleNamespace(
        get=lambda **_k: SimpleNamespace(columns=[SimpleNamespace(name="ordered_at"), SimpleNamespace(name="order_id")]))))
    text = wizard_llm._inventory_lines("retail", query="Show orders")
    assert "retail.orders: order_id, ordered_at" in text
    assert "order_date" not in text
    demo_data.reset_cache()


def test_task_ranked_inventory_stays_ordered_across_sectors(monkeypatch):
    monkeypatch.setattr(demo_data, "inventory", lambda **_kwargs: {
        "financial_services": {"accounts", "customers"},
        "retail": {"inventory_daily", "products", "stores"},
    })
    monkeypatch.setattr(demo_data, "column_inventory", lambda tables, **_k: {
        table: ["verified_column"] for table in tables
    })
    lines = wizard_llm._inventory_lines("", query="Give me a read-only stock dashboard, not an app.").splitlines()
    assert lines[0].startswith("- retail.inventory_daily:")
    assert any(line.startswith("- financial_services.accounts:") for line in lines)
    # Without a stated task an industry can narrow the inventory. A task may
    # use suitable sources elsewhere without changing the industry context.
    assert all(line.startswith("- financial_services.")
               for line in wizard_llm._inventory_lines("financial_services").splitlines())
    assert wizard_llm._inventory_lines("financial_services", query="stock").splitlines()[0].startswith("- retail.inventory_daily:")


def test_bounded_column_inventory_keeps_other_source_names_visible(monkeypatch):
    tables = {f"table_{n:02d}" for n in range(25)}
    monkeypatch.setattr(demo_data, "inventory", lambda **_k: {"example": tables})
    read = []
    def columns(wanted, **_k):
        read.extend(wanted)
        return {name: ["verified_field"] for name in wanted}
    monkeypatch.setattr(demo_data, "column_inventory", columns)
    rendered = wizard_llm._inventory_lines("", query="Help me compare samples")
    assert len(read) == 24
    assert "example.table_24" in rendered
    assert "columns and task fit not verified here" in rendered
    assert "example.table_24: verified_field" not in rendered


@pytest.mark.parametrize("query,source", [
    ("Compare viewing sessions by device", "media.view_events"),
    ("A small equipment register", "mining.assets"),
    ("Filter sample orders by day and channel", "retail.orders"),
    ("A film catalogue for our cinema club", "media.content"),
])
def test_named_task_sources_are_not_displaced_by_unrelated_curated_dependencies(monkeypatch, query, source):
    # Unrelated curated dependencies used to consume every column slot before
    # the directly relevant source. These are inventory hints, not a fit verdict.
    unrelated = [f"unrelated.table_{n:02d}" for n in range(30)]
    from server.content import WizardIdea
    cards = [WizardIdea(id=str(n), label="Fleet warranty lookup", outcome="Inspect a vehicle warranty",
                        prompt="Look up vehicle warranty records", demo_tables=[table])
             for n, table in enumerate(unrelated)]
    schema, _, table = source.partition(".")
    monkeypatch.setattr(demo_data, "inventory", lambda **_k: {
        "unrelated": {ref.split(".")[1] for ref in unrelated}, schema: {table},
    })
    monkeypatch.setattr(wizard_llm.content.content_service, "ideas", lambda: cards)
    read = []
    def columns(wanted, **_k):
        read.extend(wanted)
        return {name: ["verified_field"] for name in wanted}
    monkeypatch.setattr(demo_data, "column_inventory", columns)
    rendered = wizard_llm._inventory_lines("", query=query)
    assert read[0] == source
    assert len(read) == 24
    assert rendered.splitlines()[0] == f"- {source}: verified_field"


def test_structured_data_mode_is_limited_to_supported_choices():
    field = wizard_llm._RESPONSE_FORMAT["json_schema"]["schema"]["properties"]["ideas"]["items"]["properties"]["data_mode"]
    assert set(field.get("enum", [])) == {"demo", "generate"}


@pytest.mark.parametrize("field", ["prompt", "outcome", "fit_reason", "first_version", "assumptions", "unresolved"])
def test_undeclared_column_in_card_prose_is_rejected(monkeypatch, field):
    monkeypatch.setattr(demo_data, "verify", lambda _: True)
    monkeypatch.setattr(demo_data, "supports", lambda *_a, **_k: True)
    raw = idea(data_mode="demo", demo_tables=["cross_industry.sales"],
               required_columns=[{"table": "cross_industry.sales", "columns": ["order_id", "amount"]}])
    raw[field] = ["Treat store_id as a bakery location."] if field in {"assumptions", "unresolved"} else "Show store_id for these orders."
    assert wizard_llm._coerce_idea(raw, "") is None


def test_verified_source_fields_and_plain_workflow_defaults_remain_usable(monkeypatch):
    monkeypatch.setattr(demo_data, "verify", lambda _: True)
    monkeypatch.setattr(demo_data, "supports", lambda *_a, **_k: True)
    raw = idea(data_mode="demo", demo_tables=["cross_industry.sales"],
               required_columns=[{"table": "cross_industry.sales", "columns": ["order_id", "amount"]}],
               prompt="Use cross_industry.sales to show order_id and amount. Store a new packing status in owned working storage.",
               assumptions=["New packing status starts as unpacked in owned working storage."])
    assert wizard_llm._coerce_idea(raw, "") is not None


def test_column_inventory_keeps_verified_metadata_when_another_read_fails(monkeypatch):
    demo_data.reset_cache()
    monkeypatch.setattr(demo_data.config, "workshop_demo_catalog", lambda: "prepared_demo")
    monkeypatch.setattr(demo_data, "verify", lambda _: True)
    def read(*, full_name):
        if full_name.endswith("missing"):
            raise RuntimeError("metadata unavailable")
        return SimpleNamespace(columns=[SimpleNamespace(name="order_id")])
    monkeypatch.setattr(demo_data.credentials, "workspace_client", lambda: SimpleNamespace(tables=SimpleNamespace(get=read)))
    assert demo_data.column_inventory(["retail.orders", "retail.missing"]) == {"retail.orders": ["order_id"]}
    demo_data.reset_cache()


def test_curated_dependencies_match_the_prepared_schema_and_prompt_contract():
    import json
    from pathlib import Path
    from server import content
    schema = json.loads((Path(__file__).parent / "fixtures" / "wizard-prepared-columns.json").read_text())
    for card in content.content_service.ideas():
        if card.demo_tables:
            assert set(card.required_columns) == set(card.demo_tables), card.id
            for table, columns in card.required_columns.items():
                assert table in card.prompt, (card.id, table)
                assert set(columns) <= set(schema[table]), (card.id, table)


def test_curated_table_presence_is_insufficient_without_needed_columns(monkeypatch):
    from server import content
    monkeypatch.setattr(demo_data, "verify", lambda _: True)
    monkeypatch.setattr(demo_data, "supports", lambda *_a, **_k: False)
    curated = next(card for card in content.content_service.ideas() if card.id == "retail-store-app")
    assert not wizard._buildable(curated)


def test_fallback_is_stable_and_respects_explicit_app_goal(monkeypatch):
    monkeypatch.setattr(demo_data.config, "workshop_demo_catalog", lambda: "")
    first = wizard_llm._fallback("retail", query="I want an app for staff")
    assert first == wizard_llm._fallback("retail", query="I want an app for staff")
    assert first["ideas"] and all(item["shape"] == "app" for item in first["ideas"])
    assert first["padded"] == 0


def test_room_default_is_not_a_confirmed_model_constraint():
    prompt = wizard_llm._prompt("Help my bakery staff", "automotive_mobility")
    assert "room industry is a suggestion" in prompt
    assert "confirmed this industry" not in prompt
    assert "Fun and learning goals are valid" in prompt


def test_end_to_end_deadline_bounds_slow_work_without_duplicate_calls(monkeypatch):
    calls = []
    release = threading.Event()
    monkeypatch.setattr(wizard_llm, "_generation_flights", bounded_work.Singleflight(1))
    monkeypatch.setattr(wizard_llm, "_TIMEOUT_SECONDS", 0.04)
    monkeypatch.setattr(wizard_llm.config, "llm_wizard_enabled", lambda: True)
    monkeypatch.setattr(demo_data.config, "workshop_demo_catalog", lambda: "")
    def model(*args, **kwargs):
        calls.append(1)
        release.wait(1)
        return {"ideas": [idea()]}, "test-model"
    monkeypatch.setattr(wizard_llm, "_ask_model", model)
    try:
        started = time.monotonic()
        with ThreadPoolExecutor(6) as pool:
            results = list(pool.map(lambda _: wizard_llm.suggest("Bakery staff orders", attendee_key="same-attendee"), range(6)))
        assert time.monotonic() - started < 0.25
        assert calls == [1]
        assert all(result["source"] == "selector" for result in results)
        assert wizard_llm._generation_flights.active == 1
    finally:
        release.set()
    deadline = time.monotonic() + 1
    while wizard_llm._generation_flights.active and time.monotonic() < deadline:
        time.sleep(.01)
    assert wizard_llm._generation_flights.active == 0


def test_cold_inventory_load_is_singleflight_and_does_not_block_other_calls(monkeypatch):
    calls = []
    release = threading.Event()
    demo_data.reset_cache()
    monkeypatch.setattr(demo_data, "_loads", bounded_work.Singleflight())
    monkeypatch.setattr(demo_data.config, "workshop_demo_catalog", lambda: "test-catalog")
    def load():
        calls.append(1)
        release.wait(1)
        return {"retail": {"orders"}}, {}
    monkeypatch.setattr(demo_data, "_load", load)
    try:
        with ThreadPoolExecutor(6) as pool:
            results = list(pool.map(lambda _: demo_data.inventory(timeout=.02), range(6)))
        assert calls == [1] and results == [{}] * 6
    finally:
        release.set()
    deadline = time.monotonic() + 1
    while demo_data._loads.active and time.monotonic() < deadline:
        time.sleep(.01)
    assert demo_data.inventory() == {"retail": {"orders"}}
    demo_data.reset_cache()


@pytest.fixture
def wire(monkeypatch):
    responses = []
    calls = []
    monkeypatch.setattr("server.cli_config.unified_chat_url", lambda: "https://gateway.invalid/chat")
    monkeypatch.setattr("server.credentials.credential_manager.token", lambda: "not-a-real-token")
    monkeypatch.setattr(wizard_llm, "_pick_model", lambda _: "approved-model")
    monkeypatch.setattr(wizard_llm, "_structured_unsupported", set())
    def post(*args, **kwargs):
        calls.append(copy.deepcopy(kwargs))
        return responses.pop(0)
    monkeypatch.setattr("requests.post", post)
    return responses, calls


def response(status, body):
    return SimpleNamespace(status_code=status, json=lambda: body, headers={})


@pytest.mark.parametrize("status", [400, 429, 500])
def test_unrelated_error_never_disables_schema_or_substitutes_a_model(wire, status):
    responses, calls = wire
    responses.append(response(status, {"error": {"message": "budget or unrelated bad request"}}))
    with pytest.raises(wizard_llm.ModelUnavailable):
        wizard_llm._ask_model("Bakery orders", "")
    assert len(calls) == 1
    assert "approved-model" not in wizard_llm._structured_unsupported


def test_only_explicit_schema_support_rejection_retries_with_remaining_budget(wire):
    responses, calls = wire
    responses.extend([response(400, {"error": "response_format json_schema not supported"}),
        response(200, {"choices": [{"message": {"content": '{"ideas": []}'}, "finish_reason": "stop"}]})])
    wizard_llm._ask_model("Bakery orders", "", deadline=time.monotonic() + .4)
    assert len(calls) == 2
    assert "response_format" in calls[0]["json"] and "response_format" not in calls[1]["json"]
    assert all(call["timeout"].total <= .41 for call in calls)


@pytest.mark.parametrize("cached", [False, True])
def test_schema_less_generation_keeps_the_complete_card_contract(wire, monkeypatch, cached):
    responses, calls = wire
    card = idea()
    if cached:
        monkeypatch.setattr(wizard_llm, "_structured_unsupported", {"approved-model"})
    else:
        responses.append(response(400, {"error": "response_format json_schema not supported"}))
    responses.append(response(200, {"choices": [{"message": {
        "content": json.dumps({"industry": "", "ideas": [card]})}, "finish_reason": "stop"}]}))
    raw, model = wizard_llm._ask_model("Pack bakery orders", "")
    if not cached:
        assert "JSON contract (all required fields and types):" not in calls[0]["json"]["messages"][0]["content"]
    request = calls[-1]["json"]
    assert "response_format" not in request and request["model"] == model == "approved-model"
    supplied = json.loads(request["messages"][0]["content"].split("JSON contract (all required fields and types):\n", 1)[1])
    assert supplied == wizard_llm._RESPONSE_FORMAT["json_schema"]["schema"]
    assert set(supplied["properties"]["ideas"]["items"]["required"]) == set(card)
    verified, offered = wizard_llm._verified_ideas(raw["ideas"], raw["industry"])
    assert offered == 1 and len(verified) == 1


def test_warm_connection_keeps_unused_connect_allowance_for_generation(wire):
    responses, calls = wire
    responses.append(response(200, {"choices": [{"message": {"content": '{"ideas": []}'}, "finish_reason": "stop"}]}))
    wizard_llm._ask_model("Bakery orders", "", deadline=time.monotonic() + 12)
    timeout = calls[0]["timeout"]
    assert timeout.connect_timeout == 2
    timeout.start_connect()
    assert 11 < timeout.read_timeout <= timeout.total <= 12


def test_reasoning_service_accepts_the_full_wizard_request_without_sampling_override(wire, monkeypatch):
    _responses, calls = wire
    monkeypatch.setattr(wizard_llm, "_pick_model", lambda _: "system.ai.gpt-6-1-sol")
    def reasoning_service(_url, **kwargs):
        calls.append(copy.deepcopy(kwargs))
        if kwargs["json"].get("temperature", 1) != 1:
            return response(400, {"error": {"param": "temperature", "code": "unsupported_value",
                "message": "Only the default (1) value is supported."}})
        return response(200, {"choices": [{"message": {"content": '{"industry":"","ideas":[]}'}, "finish_reason": "stop"}]})
    monkeypatch.setattr("requests.post", reasoning_service)
    raw, model = wizard_llm._ask_model("Track bakery orders", "")
    assert model == "system.ai.gpt-6-1-sol" and raw["ideas"] == []
    assert len(calls) == 1 and "response_format" in calls[0]["json"]
    assert calls[0]["json"]["reasoning_effort"] == "low"


def test_truncated_generation_is_recoverable(wire):
    responses, calls = wire
    responses.append(response(200, {"choices": [{"message": {"content": '{}'}, "finish_reason": "length"}]}))
    with pytest.raises(wizard_llm.ModelUnavailable, match="truncated"):
        wizard_llm._ask_model("Bakery orders", "")


@pytest.mark.parametrize("choice", [1, {"message": {"content": [{"text": 12}]}}])
def test_bad_gateway_message_shapes_are_recoverable(wire, choice):
    responses, _ = wire
    responses.append(response(200, {"choices": [choice]}))
    with pytest.raises(wizard_llm.ModelUnavailable):
        wizard_llm._ask_model("Bakery orders", "")
