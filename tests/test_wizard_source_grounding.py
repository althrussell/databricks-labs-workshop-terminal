"""Live source-grounding regressions, including computed fields and cold hints."""
from types import SimpleNamespace

import pytest

from server import demo_data, wizard_llm


def product_card(**changes):
    return {
        "id": "price-compare", "label": "Demo price versus cost",
        "outcome": "Compare sample selling and cost prices.",
        "prompt": "Compare unit_price with cost_price in the sample products.",
        "fit_reason": "A simple calculation suits the requested sample comparison.",
        "first_version": "Compute margin = unit_price - cost_price and margin_pct = margin / cost_price, then sort by margin_pct.",
        "shape": "app", "intents": ["business_problem"], "products": [],
        "technical": False, "demo_tables": ["cross_industry.products"], "data_mode": "demo",
        "required_columns": [{"table": "cross_industry.products", "columns": ["unit_price", "cost_price"]}],
        "assumptions": ["Use labelled workshop samples."], "unresolved": [], **changes,
    }


def test_explicit_calculated_identifier_does_not_become_an_invented_source_column(monkeypatch):
    # nf-38's actual card was rejected for margin_pct, despite a complete
    # arithmetic definition. This checks dependencies, not its metric label.
    monkeypatch.setattr(demo_data, "verify", lambda _: True)
    monkeypatch.setattr(demo_data, "supports", lambda *_a, **_k: True)
    card = product_card()
    coerced = wizard_llm._coerce_idea(card, "cross_industry")
    assert coerced is not None
    assert "margin_pct = margin / cost_price" in coerced.first_version
    assert coerced.required_columns == {"cross_industry.products": ["unit_price", "cost_price"]}


def test_parenthesised_calculation_keeps_its_verified_operands(monkeypatch):
    monkeypatch.setattr(demo_data, "verify", lambda _: True)
    monkeypatch.setattr(demo_data, "supports", lambda *_a, **_k: True)
    card = product_card(first_version="Calculate gross_margin_pct = (unit_price - cost_price) / unit_price * 100.")
    assert wizard_llm._coerce_idea(card, "") is not None


@pytest.mark.parametrize("formula", [
    "Show margin_pct for each product.",
    "Compute margin_pct = unit_price - hidden_cost.",
    "Compute margin_pct = price_lookup(unit_price).",
    "Compute margin_pct = margin_pct + unit_price.",
    "Compute margin_pct = unit_price + missing.",
    "Compute margin_pct = unit_price - products.",
])
def test_unbound_or_non_arithmetic_derived_identifier_still_fails(monkeypatch, formula):
    monkeypatch.setattr(demo_data, "verify", lambda _: True)
    monkeypatch.setattr(demo_data, "supports", lambda *_a, **_k: True)
    assert wizard_llm._coerce_idea(product_card(first_version=formula), "") is None


@pytest.mark.parametrize("query,source", [
    ("A council facility directory organised by ward", "public_sector.facilities"),
    ("List the council facilities by ward", "public_sector.facilities"),
    ("The devices used in sample website visits", "cross_industry.web_events"),
    ("Invent a pretend streaming plan and change its price", "media.subscriptions"),
])
def test_business_object_hints_get_cold_column_slots_before_unrelated_sources(monkeypatch, query, source):
    schema, _, table = source.partition(".")
    inventory = {"automotive_mobility": {f"unrelated_{n}" for n in range(30)},
                 "telco": {"plans"}, schema: {table}}
    monkeypatch.setattr(demo_data, "inventory", lambda **_k: inventory)
    monkeypatch.setattr(wizard_llm.content.content_service, "ideas", lambda: [])
    reads = []
    def columns(wanted, **_k):
        reads.extend(wanted)
        return {ref: ["verified_field"] for ref in wanted}
    monkeypatch.setattr(demo_data, "column_inventory", columns)
    wizard_llm._inventory_lines("", query=query)
    assert reads[0] == source
    assert len(reads) == 24


def test_source_comments_describe_rows_without_another_metadata_request(monkeypatch):
    demo_data.reset_cache()
    monkeypatch.setattr(demo_data.config, "workshop_demo_catalog", lambda: "prepared_demo")
    monkeypatch.setattr(demo_data, "inventory", lambda **_k: {"media": {"view_events"}})
    reads = []
    def read(*, full_name):
        reads.append(full_name)
        return SimpleNamespace(columns=[SimpleNamespace(name="device")],
            comment="Viewing sessions over nine months.\nNot website clickstream.")
    monkeypatch.setattr(demo_data.credentials, "workspace_client",
        lambda: SimpleNamespace(tables=SimpleNamespace(get=read)))
    try:
        rendered = wizard_llm._inventory_lines("", query="Compare viewing devices")
        assert "Viewing sessions over nine months." in rendered
        assert "Not website clickstream." in rendered
        assert reads == ["prepared_demo.media.view_events"]
        # Reset removes semantic metadata as well as the column cache.
        demo_data.reset_cache()
        monkeypatch.setattr(demo_data, "column_inventory", lambda *_a, **_k: {})
        assert "Viewing sessions" not in wizard_llm._inventory_lines("", query="Compare viewing devices")
    finally:
        demo_data.reset_cache()


def test_streaming_sensor_work_does_not_receive_a_media_domain_hint(monkeypatch):
    monkeypatch.setattr(demo_data, "inventory", lambda **_k: {
        "manufacturing": {"sensor_readings"}, "media": {"subscriptions"}})
    monkeypatch.setattr(wizard_llm.content.content_service, "ideas", lambda: [])
    reads = []
    def columns(wanted, **_k):
        reads.extend(wanted)
        return {}
    monkeypatch.setattr(demo_data, "column_inventory", columns)
    wizard_llm._inventory_lines("", query="Build streaming sensor readings")
    assert reads[0] == "manufacturing.sensor_readings"


def test_web_app_output_does_not_prioritise_clickstream_over_its_business_object(monkeypatch):
    monkeypatch.setattr(demo_data, "inventory", lambda **_k: {
        "financial_services": {"accounts"}, "cross_industry": {"web_events"}})
    monkeypatch.setattr(wizard_llm.content.content_service, "ideas", lambda: [])
    reads = []
    def columns(wanted, **_k):
        reads.extend(wanted)
        return {}
    monkeypatch.setattr(demo_data, "column_inventory", columns)
    wizard_llm._inventory_lines("", query="Build a web app to browse sample accounts")
    assert reads[0] == "financial_services.accounts"


@pytest.mark.parametrize("interpretation", [
    "Use service requests as a proposed stand-in for meeting-room booking requests.",
    "Treat maintenance requests as a proxy for room reservation requests.",
    "Use viewing events as a labelled substitute for website visits.",
])
def test_declaring_a_different_source_object_as_a_stand_in_does_not_establish_task_fit(monkeypatch, interpretation):
    # The live nf-40 card had real columns but explicitly replaced bookings
    # with citizen-service requests. Disclosure cannot make that workflow fit.
    monkeypatch.setattr(demo_data, "verify", lambda _: True)
    monkeypatch.setattr(demo_data, "supports", lambda *_a, **_k: True)
    card = product_card(
        id="booking-requests", label="Demo meeting-room request page",
        outcome="Share meeting-room booking requests.",
        prompt="Share booking requests using a prepared sample request list.",
        fit_reason="A small page lets the office share its requests.",
        first_version="List service requests with ward, status and SLA indicators.",
        demo_tables=["public_sector.service_requests"],
        required_columns=[{"table": "public_sector.service_requests", "columns": ["request_id", "ward", "status"]}],
        assumptions=[interpretation],
    )
    assert wizard_llm._coerce_idea(card, "") is None


@pytest.mark.parametrize("interpretation", [
    "Use sample products as a stand-in for connected products.",
    "Treat prepared products as a labelled proxy for live product records.",
    "Use sample cars as a stand-in for live vehicle records.",
    "Treat equipment records as a proxy for connected assets.",
    "Do not use service requests as a stand-in for booking requests.",
])
def test_same_object_samples_and_warnings_remain_usable(monkeypatch, interpretation):
    monkeypatch.setattr(demo_data, "verify", lambda _: True)
    monkeypatch.setattr(demo_data, "supports", lambda *_a, **_k: True)
    assert wizard_llm._coerce_idea(product_card(assumptions=[interpretation]), "") is not None


def test_labelled_working_samples_can_preserve_the_requested_object(monkeypatch):
    monkeypatch.setattr(demo_data, "verify", lambda _: True)
    card = product_card(
        id="booking-requests", label="Demo meeting-room request page",
        outcome="Share a labelled preview of meeting-room booking requests.",
        prompt="Explore prepared sources first, then share labelled booking requests in owned storage.",
        first_version="A small request list shows the room, requested time and proposed booking status.",
        demo_tables=[], required_columns=[], data_mode="generate",
        assumptions=["Use labelled room and time examples; source suitability was not established."],
    )
    assert wizard_llm._coerce_idea(card, "") is not None


@pytest.mark.parametrize("claim", [
    "The prepared source has verified genre values.",
    "The source includes verified device values for a simple comparison.",
    "The product values are confirmed.",
    "Use validated product rows in the preview.",
])
def test_verified_columns_do_not_establish_verified_row_values(monkeypatch, claim):
    # nf-51 called its genre values verified even though only the genre column
    # had been checked. Its own unresolved list said the values were uninspected.
    monkeypatch.setattr(demo_data, "verify", lambda _: True)
    monkeypatch.setattr(demo_data, "supports", lambda *_a, **_k: True)
    assert wizard_llm._coerce_idea(product_card(fit_reason=claim), "") is None


@pytest.mark.parametrize("wording", [
    "The listed device column supports the comparison; inspect its sample values first.",
    "The price columns are verified; row values are not verified.",
    "The source does not have verified product values.",
    "Inspect the product rows and verify their values before building the preview.",
])
def test_metadata_facts_and_future_row_checks_remain_usable(monkeypatch, wording):
    monkeypatch.setattr(demo_data, "verify", lambda _: True)
    monkeypatch.setattr(demo_data, "supports", lambda *_a, **_k: True)
    assert wizard_llm._coerce_idea(product_card(fit_reason=wording), "") is not None


def test_date_age_calculation_is_not_a_source_column_or_current_record_filter(monkeypatch):
    monkeypatch.setattr(demo_data, "verify", lambda _: True)
    monkeypatch.setattr(demo_data, "supports", lambda *_a, **_k: True)
    card = product_card(
        id="equipment-register", label="Demo equipment register",
        prompt="Group assets by asset_type and calculate their age from commissioned_on.",
        first_version="Group by asset_type, with age_years = floor((today - commissioned_on)/365).",
        demo_tables=["mining.assets"],
        required_columns=[{"table": "mining.assets", "columns": ["asset_type", "commissioned_on"]}],
    )
    result = wizard_llm._coerce_idea(card, "mining")
    assert result is not None
    assert "age_years = floor((today - commissioned_on)/365)" in result.first_version
    assert "calculate age" in result.prompt


@pytest.mark.parametrize("formula", [
    "price_gap = round(unit_price - cost_price, 2).",
    "price_gap = abs(unit_price - cost_price).",
])
def test_pure_rounding_functions_keep_declared_operands(monkeypatch, formula):
    monkeypatch.setattr(demo_data, "verify", lambda _: True)
    monkeypatch.setattr(demo_data, "supports", lambda *_a, **_k: True)
    assert wizard_llm._coerce_idea(product_card(first_version=formula), "") is not None


@pytest.mark.parametrize("formula", [
    "price_gap = floor(unit_price - hidden_cost).",
    "price_gap = unit_price + today.",
    "price_gap = unit_price + cost_price + hidden_cost.",
    "price_gap = round(unit_price - cost_price, digits=2).",
    "price_gap = unit_price + cost_price + price_lookup(unit_price).",
    "price_gap = (unit_price - cost_price).real.",
])
def test_calculation_extensions_do_not_admit_unknowns_or_arbitrary_calls(monkeypatch, formula):
    monkeypatch.setattr(demo_data, "verify", lambda _: True)
    monkeypatch.setattr(demo_data, "supports", lambda *_a, **_k: True)
    assert wizard_llm._coerce_idea(product_card(first_version=formula), "") is None


def test_date_intrinsic_stays_inside_its_source_bound_calculation(monkeypatch):
    monkeypatch.setattr(demo_data, "verify", lambda _: True)
    monkeypatch.setattr(demo_data, "supports", lambda *_a, **_k: True)
    result = wizard_llm._coerce_idea(product_card(
        first_version="Age in days uses age_days = current_date - commissioned_on. Show today's preview.",
        prompt="Calculate asset age from commissioned_on.",
        demo_tables=["mining.assets"],
        required_columns=[{"table": "mining.assets", "columns": ["commissioned_on"]}],
    ), "")
    assert result is not None
    assert "age_days = current_date - commissioned_on" in result.first_version
    assert "the demo day's preview" in result.first_version


def streaming_card():
    return product_card(
        id="stream-price", label="Demo streaming plan chooser",
        outcome="Choose options for a pretend streaming subscription.",
        prompt="Choose a streaming plan and see its monthly price change.",
        first_version="Pick a plan, included data/minutes, and compare its monthly price.",
        demo_tables=["telco.plans"],
        required_columns=[{"table": "telco.plans", "columns": ["included_data_gb", "included_minutes", "monthly_price"]}],
    )


def test_mobile_allowance_source_cannot_become_a_streaming_service_plan(monkeypatch):
    monkeypatch.setattr(demo_data, "verify", lambda _: True)
    monkeypatch.setattr(demo_data, "supports", lambda *_a, **_k: True)
    # Unlike the earlier disclosed stand-in, this actual defect labels the
    # mobile source as streaming without acknowledging the substitution.
    assert wizard_llm._coerce_idea(streaming_card(), "media") is None


def test_actual_mobile_plan_task_can_use_mobile_allowances(monkeypatch):
    monkeypatch.setattr(demo_data, "verify", lambda _: True)
    monkeypatch.setattr(demo_data, "supports", lambda *_a, **_k: True)
    assert wizard_llm._coerce_idea(streaming_card(), "media",
        query="Compare mobile plans including a streaming service bundle.") is not None


def test_original_task_prevents_an_unannounced_mobile_rewrite(monkeypatch):
    monkeypatch.setattr(demo_data, "verify", lambda _: True)
    monkeypatch.setattr(demo_data, "supports", lambda *_a, **_k: True)
    card = streaming_card() | {
        "label": "Demo monthly plan chooser", "outcome": "Compare monthly mobile prices.",
        "prompt": "Choose a mobile plan with data and minutes.",
    }
    assert wizard_llm._coerce_idea(card, "media",
        query="Let me play with a pretend streaming plan price.") is None


def test_selected_fun_intent_survives_schema_free_model_labels(monkeypatch):
    monkeypatch.setattr(demo_data, "industry_slug", lambda value: value)
    monkeypatch.setattr(demo_data, "verify", lambda _: True)
    monkeypatch.setattr(wizard_llm.config, "llm_wizard_enabled", lambda: True)
    monkeypatch.setattr(wizard_llm, "_fallback", lambda *_a, **_k: {"ideas": [], "source": "selector"})
    raw = product_card(
        demo_tables=[], required_columns=[], data_mode="generate",
        prompt="Play with a pretend streaming price.",
        first_version="Change a proposed number of screens and see the price update.",
        intents=["price comparison", "what-if analysis"],
    )
    monkeypatch.setattr(wizard_llm, "_ask_model", lambda *_a, **_k: ({"industry": "media", "ideas": [raw]}, "test-model"))
    result = wizard_llm.suggest("Play with a pretend streaming subscription price.", "media", industry_locked=True, intent="fun")
    assert result["source"] == "llm"
    assert result["ideas"][0]["intents"] == ["fun"]
