"""The opening wizard: what it captures, what it refuses to capture, and what it
promises the attendee can be built.

Three things here would each be quietly expensive in production and none of them
is visible from a screenshot:

- A wizard record and an agent record for the same person arriving at Control
  Tower as two contradictory use cases.
- A record captured from someone who pressed Skip.
- An idea card offered for data this deployment never seeded.
"""

from __future__ import annotations

import random
import time
import types

import pytest

from server import content, demo_data, discovery, user_content, wizard


@pytest.fixture()
def user(tmp_path, monkeypatch):
    monkeypatch.setattr(discovery.config, "discovery_enabled", lambda: True)
    discovery.discovery_store.clear()
    yield types.SimpleNamespace(email="attendee@example.com", home=str(tmp_path))
    discovery.discovery_store.clear()


@pytest.fixture()
def seeded(monkeypatch):
    """A demo catalog with automotive and cross-industry data, as milestone 1 seeds."""
    monkeypatch.setattr(demo_data.config, "workshop_demo_catalog", lambda: "workshop_demo")
    inventory = {
        "automotive_mobility": {
            "vehicles", "customers", "dealers", "parts", "suppliers",
            "service_orders", "service_order_parts", "warranty_claims",
            "parts_inventory", "recall_campaigns", "campaign_vehicles",
            "telematics_events", "charging_sessions", "trips", "dtc_codes",
            "sales_transactions", "vehicle_ownership",
            "vehicle360", "customer360", "parts360",
        },
        "cross_industry": {"customers", "products", "sales", "web_events"},
    }
    monkeypatch.setattr(demo_data, "_cache", inventory)
    monkeypatch.setattr(demo_data, "_cache_at", time.time())
    monkeypatch.setattr(demo_data, "_cache_ok", True)
    yield inventory
    demo_data.reset_cache()


# -- brief -> discovery -----------------------------------------------------

def test_the_wizard_answer_becomes_a_high_confidence_discovery_record(user):
    """The attendee stated this themselves, unprompted by an agent's reading of
    a conversation. That is the strongest provenance any record in this system
    has, and a brief built on it should not hedge."""
    wizard.save(user, {
        "what_building": "A dashboard showing which parts fail early",
        "industry": "automotive_mobility",
        "intent": "business_problem",
        "current_stack": ["Snowflake", "Tableau"],
    })

    records = discovery.discovery_store.for_attendee(user.email)
    assert len(records) == 1
    record = records[0]
    assert record.confidence == "high"
    assert record.agent == "wizard"
    assert record.session_intent == "business_problem"
    assert record.industry == "automotive_mobility"
    assert record.use_case_title == "A dashboard showing which parts fail early"
    assert record.current_stack == ["Snowflake", "Tableau"]


def test_timeline_is_never_captured(user):
    """A workshop attendee has no authority over their employer's timeline, so a
    captured answer would read downstream as a commitment nobody made."""
    wizard.save(user, {"what_building": "x", "timeline": "before Q3"})
    assert discovery.discovery_store.for_attendee(user.email)[0].timeline == ""


def test_an_agent_refining_the_wizard_record_produces_one_record_not_two(user):
    """The whole point of minting the id in the wizard.

    Without this the same attendee reaches the account team as two unrelated use
    cases, and nobody downstream can tell which one they meant.
    """
    brief = wizard.save(user, {
        "what_building": "Warranty dashboard",
        "industry": "automotive_mobility",
    })

    # The agent, later, having learned more while building.
    discovery.record(user.email, {
        "record_id": brief.record_id,
        "confidence": "high",
        "use_case_title": "Warranty claims dashboard",
        "blockers": ["claims data lives in SAP"],
    })

    records = discovery.discovery_store.for_attendee(user.email)
    assert len(records) == 1
    assert records[0].revision == 2
    assert records[0].blockers == ["claims data lives in SAP"]


def test_the_record_id_survives_a_second_save(user):
    """An attendee who goes Back and edits must not mint a second record."""
    first = wizard.save(user, {"what_building": "A dashboard"})
    second = wizard.save(user, {"what_building": "A dashboard, but weekly"})
    assert second.record_id == first.record_id
    assert len(discovery.discovery_store.for_attendee(user.email)) == 1


def test_skip_records_nothing(user):
    """Skipping is an answer, and the answer is "leave me alone". Recording it
    anyway would make the record the one thing they explicitly declined."""
    brief = wizard.save(user, {"skipped": True})
    assert brief.skipped
    assert discovery.discovery_store.for_attendee(user.email) == []


def test_dismissing_the_agent_picker_does_not_erase_the_brief(user):
    """Skip and Escape fire from the third step too, where the attendee has
    already answered everything. Treating that as "I decline to tell you" would
    blank the home recap and the agent's instruction overlay for someone who had
    just finished filling the thing in — and leave the record already at Control
    Tower describing a brief the app no longer believes exists."""
    wizard.save(user, {
        "what_building": "A warranty dashboard",
        "industry": "automotive_mobility",
        "current_stack": ["Snowflake"],
    })

    wizard.save(user, {"skipped": True})

    brief = wizard.read_brief(user)
    assert brief.what_building == "A warranty dashboard"
    assert brief.industry == "automotive_mobility"
    assert brief.current_stack == ["Snowflake"]
    assert brief.skipped is False
    assert user_content._wizard_overlay(user) != ""


def test_a_dismissal_does_not_burn_a_discovery_revision(user):
    """Nobody touched the answers, so a second revision would make the record
    look like it was being actively refined."""
    wizard.save(user, {"what_building": "A warranty dashboard"})
    wizard.save(user, {"skipped": True})

    records = discovery.discovery_store.for_attendee(user.email)
    assert len(records) == 1
    assert records[0].revision == 1


def test_an_omitted_field_is_unchanged_but_a_cleared_one_is_cleared(user):
    """The distinction the merge turns on. Going Back and deleting the stack
    chips has to stick, or the attendee cannot correct themselves."""
    wizard.save(user, {"what_building": "A dashboard", "current_stack": ["Snowflake"]})

    wizard.save(user, {"current_stack": []})
    assert wizard.read_brief(user).what_building == "A dashboard"
    assert wizard.read_brief(user).current_stack == []


def test_an_empty_brief_records_nothing(user):
    """Clicking Next through an empty form is not a finding. A row saying an
    attendee exists and wants nothing is worse than no row, because it looks
    like one."""
    wizard.save(user, {"what_building": "   "})
    assert discovery.discovery_store.for_attendee(user.email) == []


def test_capture_off_stores_the_brief_but_records_nothing(user, monkeypatch):
    """The consent boundary. A deployment that never opted in holds no discovery
    data, and the wizard must not become a way around that — while still working
    as a launcher, which is its other job."""
    monkeypatch.setattr(discovery.config, "discovery_enabled", lambda: False)
    brief = wizard.save(user, {"what_building": "A dashboard", "industry": "retail"})

    assert discovery.discovery_store.for_attendee(user.email) == []
    # Still usable as a launcher: the brief itself is local state.
    assert wizard.read_brief(user).what_building == "A dashboard"
    assert wizard.starter_prompt(brief)


def test_seen_survives_a_reload(user):
    """Server-side, not localStorage: a reload, a second tab or the reconnect
    after a wifi flap must not re-present a modal someone already dealt with."""
    wizard.save(user, {"skipped": True})
    assert wizard.read_brief(user).seen is True
    assert wizard.state(user)["should_show"] is False


# -- instruction overlay ----------------------------------------------------

def test_the_overlay_carries_the_brief_and_the_record_id(user):
    wizard.save(user, {
        "what_building": "A dashboard showing which parts fail early",
        "industry": "automotive_mobility",
        "intent": "business_problem",
        "current_stack": ["Snowflake"],
    })
    overlay = user_content._wizard_overlay(user)

    assert "which parts fail early" in overlay
    assert "automotive mobility" in overlay
    assert "Snowflake" in overlay
    assert wizard.read_brief(user).record_id in overlay
    assert "do not ask them again" in overlay.lower()


def test_the_overlay_is_empty_when_they_skipped(user):
    """Leaves the agent exactly where it was before the wizard existed — free to
    ask, because nobody answered."""
    wizard.save(user, {"skipped": True})
    assert user_content._wizard_overlay(user) == ""


def test_the_overlay_omits_the_record_id_when_capture_is_off(user, monkeypatch):
    """Pointing an agent at a record id in a deployment that stores nothing would
    produce failed calls and an explanation the attendee should never hear."""
    monkeypatch.setattr(user_content.config, "discovery_enabled", lambda: False)
    monkeypatch.setattr(discovery.config, "discovery_enabled", lambda: False)
    wizard.save(user, {"what_building": "A dashboard"})
    overlay = user_content._wizard_overlay(user)

    assert "A dashboard" in overlay
    assert "discovery record" not in overlay


# -- demo data --------------------------------------------------------------

def test_the_manifest_leads_with_the_360s_and_is_capped(seeded):
    """The 360s are the point of the automotive schema, and an agent that finds
    vehicle360 will not hand-assemble it from six base tables. Twenty table names
    would crowd out everything else in the instruction file."""
    manifest = demo_data.manifest("automotive_mobility")
    lines = [line for line in manifest.splitlines() if line.startswith("- `")]

    assert len(lines) <= demo_data._MANIFEST_TABLE_CAP + 1  # +1 for the "and N more"
    assert "customer360" in lines[0]
    assert "parts360" in lines[1]
    assert "vehicle360" in lines[2]
    assert "and 6 more" in manifest


def test_an_unseeded_industry_falls_back_to_the_catalog_overview(seeded):
    """The agent still learns the catalog exists and can go looking, rather than
    concluding there is no data here."""
    manifest = demo_data.manifest("healthcare")
    assert "automotive_mobility" in manifest
    assert "cross_industry" in manifest


def test_no_manifest_at_all_without_a_catalog(monkeypatch):
    """A deployment that never ran the seed notebook must promise nothing."""
    monkeypatch.setattr(demo_data.config, "workshop_demo_catalog", lambda: "")
    demo_data.reset_cache()
    assert demo_data.manifest("automotive_mobility") == ""
    assert demo_data.enabled() is False


def test_verification_is_all_or_nothing(seeded):
    """Three tables out of four is not buildable; it just moves the failure from
    the wizard into the terminal, where it costs the attendee's time."""
    assert demo_data.verify(["automotive_mobility.parts360"]) is True
    assert demo_data.verify(
        ["automotive_mobility.parts360", "automotive_mobility.nonexistent"]
    ) is False
    assert demo_data.verify(["retail.orders"]) is False
    assert demo_data.verify([]) is True


def test_the_demo_overlay_is_scoped_to_the_attendees_industry(user, seeded):
    wizard.save(user, {"what_building": "x", "industry": "automotive_mobility"})
    overlay = user_content._demo_data_overlay(user)

    assert "vehicle360" in overlay
    assert "DEEP CLONE" in overlay
    assert "read-only" in overlay.lower()


# -- idea selection ---------------------------------------------------------

def test_every_offered_idea_is_actually_buildable(seeded):
    """The one unforgivable outcome. Somebody who clicked "show me ideas" has
    said they have no idea and is trusting the grid."""
    for industry in ("automotive_mobility", "retail", "healthcare", ""):
        for idea in wizard.select_ideas(industry, rng=random.Random(7)):
            assert demo_data.verify(idea.demo_tables), (industry, idea.id)


def test_an_unseeded_industry_never_leaks_a_foreign_industry(seeded):
    """Degrades to generic cards, which need no demo data. Never empty, and
    never a random automotive ML card to fill a shape the generics do not
    cover — that was how a healthcare attendee saw a car-parts model."""
    ideas = wizard.select_ideas("healthcare", rng=random.Random(3))
    assert ideas
    for idea in ideas:
        assert not idea.industries or "healthcare" in idea.industries
        assert "automotive_mobility" not in idea.industries


def test_an_empty_industry_stays_on_generic_cards(seeded):
    """No generic has shape ml. Filling that hole from a random industry is
    the leak the filter exists to close."""
    ideas = wizard.select_ideas("", rng=random.Random(11))
    assert ideas
    for idea in ideas:
        assert idea.industries == []


def test_the_six_cards_span_at_least_four_shapes(seeded):
    """Six dashboards tells someone who wanted to build an app that this workshop
    is not for them."""
    for industry in ("automotive_mobility", "cross_industry", ""):
        ideas = wizard.select_ideas(industry, rng=random.Random(11))
        assert len({i.shape for i in ideas}) >= 4, industry


def test_industry_matches_lead_the_grid(seeded):
    ideas = wizard.select_ideas("automotive_mobility", rng=random.Random(5))
    assert "automotive_mobility" in ideas[0].industries


def test_without_a_demo_catalog_the_whole_catalogue_is_offered(monkeypatch):
    """Filtering on tables nobody has would empty the grid. Without demo data the
    agent generates what it needs, so every card is reachable."""
    monkeypatch.setattr(demo_data.config, "workshop_demo_catalog", lambda: "")
    demo_data.reset_cache()
    ideas = wizard.select_ideas("automotive_mobility", rng=random.Random(2))

    assert len(ideas) == wizard.IDEA_COUNT
    assert any(i.demo_tables for i in ideas)


def test_an_attendee_keeps_the_grid_they_were_given(user, seeded):
    """The grid is asked for again on every reconnect, second tab and pressed
    filter chip. An unseeded selector answers differently each time, so six new
    cards appear under someone halfway through reading the old ones — and, once,
    wiped the sentence they were typing beside them."""
    first = [i["id"] for i in wizard.state(user)["ideas"]]
    again = [i["id"] for i in wizard.state(user)["ideas"]]

    assert first == again


def test_two_attendees_do_not_get_the_same_six(seeded):
    """The reason the shuffle exists: a room that all sees the same grid in the
    same order builds the same thing. Stability is per attendee, not global."""
    grids = {
        email: tuple(
            i["id"]
            for i in wizard.state(
                types.SimpleNamespace(email=email, home="/nonexistent")
            )["ideas"]
        )
        for email in (f"labuser{n}@example.com" for n in range(12))
    }

    assert len(set(grids.values())) > 1, grids


def test_the_guided_grid_never_offers_a_game(seeded):
    """A game is a legitimate thing to build here — `fun` stays a session intent
    a discovery record can carry — but it is not a path we steer someone down.
    Somebody who clicked "show me ideas" is asking what this platform is for."""
    assert not [i for i in content.content_service.ideas() if i.shape == "fun"]


def test_the_shipped_pack_covers_every_industry_it_names(seeded):
    """A pack that names an industry with one idea in one shape produces a grid
    padded out with generics, which reads as "nothing here for you"."""
    ideas = content.content_service.ideas()
    by_industry: dict[str, set[str]] = {}
    for idea in ideas:
        for industry in idea.industries:
            by_industry.setdefault(industry, set()).add(idea.shape)

    assert by_industry, "the shipped pack has no tagged ideas"
    for industry, shapes in by_industry.items():
        assert len(shapes) >= 3, f"{industry} only offers {shapes}"


def test_a_known_industry_is_preselected_even_when_this_catalog_lacks_it(
    seeded, monkeypatch
):
    """It used to require live inventory, so an unseeded — or briefly
    unreadable — catalog silently discarded the operator's choice. The chip
    steers the agent and the discovery record; only the demo tables need the
    data to exist, and those are filtered separately."""
    monkeypatch.setattr(content.content_service, "default_industry", lambda: "healthcare")
    assert wizard.default_industry() == "healthcare"

    monkeypatch.setattr(
        content.content_service, "default_industry", lambda: "automotive_mobility"
    )
    assert wizard.default_industry() == "automotive_mobility"


def test_an_industry_no_notebook_has_ever_heard_of_is_still_ignored(
    seeded, monkeypatch
):
    """Never silently automotive: a typo in the create form preselects nothing
    rather than the nearest thing to it."""
    monkeypatch.setattr(content.content_service, "default_industry", lambda: "halthcare")
    assert wizard.default_industry() == ""


# -- starter prompt ---------------------------------------------------------

def test_a_chosen_card_supplies_its_own_prompt(user):
    """The card's prompt was written to produce a good first build; the sentence
    was written to describe an ambition."""
    idea = next(i for i in content.content_service.ideas() if i.demo_tables)
    brief = wizard.save(user, {"idea_id": idea.id, "what_building": idea.outcome})
    assert wizard.starter_prompt(brief) == idea.prompt


def test_a_typed_sentence_is_framed_so_the_agent_starts_building(user):
    brief = wizard.save(user, {"what_building": "A warranty dashboard"})
    prompt = wizard.starter_prompt(brief)

    assert prompt.startswith("A warranty dashboard")
    assert "Start building this with me now" in prompt


def test_skipping_produces_no_starter_prompt(user):
    assert wizard.starter_prompt(wizard.save(user, {"skipped": True})) == ""


# -- API --------------------------------------------------------------------

def test_the_industry_filter_actually_refilters(user, seeded):
    """The chip is pressed before anything is saved, so the brief cannot answer
    which industry the grid is for. Read off the brief instead and the filter
    visibly does nothing, which reads as a broken control."""
    ideas = wizard.state(user, "automotive_mobility")["ideas"]
    assert "automotive_mobility" in ideas[0]["industries"]

    ideas = wizard.state(user, "cross_industry")["ideas"]
    assert "cross_industry" in ideas[0]["industries"]


def test_a_dismissal_over_http_keeps_the_saved_brief(client):
    """The wire format matters as much as the merge: a model whose fields all
    default would arrive as an instruction to blank every one of them."""
    # Its own attendee: the data root is session-scoped, so writing a brief for
    # a shared one leaks into every later test that asks whether to show it.
    who = {"X-Forwarded-Email": "dismisser@example.com"}

    client.post("/api/wizard", headers=who, json={"what_building": "A dashboard"})
    client.post("/api/wizard", headers=who, json={"skipped": True})

    brief = client.get("/api/wizard", headers=who).json()["brief"]
    assert brief["what_building"] == "A dashboard"
    assert brief["skipped"] is False


def test_the_wizard_endpoint_round_trips(client):
    from tests.conftest import ALICE

    initial = client.get("/api/wizard", headers=ALICE).json()
    assert initial["should_show"] is True
    assert len(initial["ideas"]) == wizard.IDEA_COUNT

    saved = client.post(
        "/api/wizard",
        headers=ALICE,
        json={"what_building": "A warranty dashboard", "industry": "automotive_mobility"},
    ).json()
    assert saved["brief"]["record_id"]
    assert saved["starter_prompt"]

    assert client.get("/api/wizard", headers=ALICE).json()["should_show"] is False


# -- the operator's switch --------------------------------------------------

def test_a_workshop_can_be_created_without_the_wizard(user, monkeypatch):
    """Some formats walk the whole room through the first build together, and a
    modal asking what each person intends is noise in front of that."""
    monkeypatch.setattr(wizard.config, "onboarding_wizard_enabled", lambda: False)
    state = wizard.state(user)

    assert state["enabled"] is False
    assert state["should_show"] is False


def test_the_switch_is_the_servers_answer_not_the_browsers(client, monkeypatch):
    """Answered where a reload, a second tab and a reconnect all have to agree —
    the same reason `seen` lives on the server."""
    from server import config as server_config
    from tests.conftest import BOB

    monkeypatch.setattr(server_config, "onboarding_wizard_enabled", lambda: False)

    assert client.get("/api/wizard", headers=BOB).json()["should_show"] is False
    assert (
        client.get("/api/config", headers=BOB).json()["onboarding_wizard"]["enabled"]
        is False
    )


def test_the_wizard_is_offered_by_default(client):
    """An instance nobody configured still avoids dropping someone at a blinking
    cursor with no idea what to type."""
    from tests.conftest import BOB

    assert (
        client.get("/api/config", headers=BOB).json()["onboarding_wizard"]["enabled"]
        is True
    )


def test_the_pack_default_is_not_a_stated_industry(user, seeded, monkeypatch):
    """Preselecting the chip is a suggestion. Typing a healthcare sentence
    without touching it must not tell the agent, or Control Tower, that they
    are automotive."""
    monkeypatch.setattr(
        content.content_service, "default_industry", lambda: "automotive_mobility"
    )
    wizard.save(user, {"what_building": "predict 30-day readmission"})

    assert discovery.discovery_store.for_attendee(user.email)[0].industry == ""
    overlay = user_content._demo_data_overlay(user)
    assert "do not assume automotive" in overlay.lower()


def test_picking_a_retail_card_scopes_the_overlay_to_retail(user, seeded, monkeypatch):
    """The card owns the schema. A chip left on automotive must not win."""
    inventory = {
        **seeded,
        "retail": {"orders", "order_items", "products", "stores", "inventory_daily"},
    }
    monkeypatch.setattr(demo_data, "_cache", inventory)
    idea = next(
        i for i in content.content_service.ideas() if "retail" in i.industries
    )
    wizard.save(user, {"idea_id": idea.id, "what_building": idea.outcome})

    overlay = user_content._demo_data_overlay(user)
    assert "workshop_demo.retail" in overlay
    assert "vehicle360" not in overlay
    assert discovery.discovery_store.for_attendee(user.email)[0].industry == "retail"


def test_industry_aliases_resolve_to_the_seeded_schema(seeded):
    assert demo_data.normalize_industry("automotive mobility") == "automotive_mobility"
    assert demo_data.normalize_industry("Automotive-Mobility") == "automotive_mobility"
    assert demo_data.normalize_industry("healthcare") == ""  # not seeded here


def test_the_env_default_industry_wins_over_the_pack(seeded, monkeypatch):
    monkeypatch.setattr(
        content.content_service, "default_industry", lambda: "automotive_mobility"
    )
    monkeypatch.setattr(
        wizard.config, "workshop_default_industry", lambda: "cross_industry"
    )
    assert wizard.default_industry() == "cross_industry"


def test_an_unseeded_env_default_still_names_the_room(seeded, monkeypatch):
    """The operator said the room is healthcare. Whether this metastore got the
    healthcare schema is a separate question, answered by seeded_industries."""
    monkeypatch.setattr(content.content_service, "default_industry", lambda: "")
    monkeypatch.setattr(
        wizard.config, "workshop_default_industry", lambda: "healthcare"
    )
    assert wizard.default_industry() == "healthcare"


def test_state_offers_every_known_industry_and_badges_the_seeded_ones(
    seeded, user
):
    """The picker's whole failure mode was rendering only seeded schemas, so an
    unset catalog removed the question instead of the answer."""
    state = wizard.state(user)

    assert "healthcare" in state["industries"], "known industries must be offered"
    assert "healthcare" not in state["seeded_industries"]
    assert "automotive_mobility" in state["seeded_industries"]
    assert state["industry_labels"]["financial_services"] == "Financial services"


def test_state_offers_industries_with_no_catalog_at_all(user, monkeypatch):
    """The reported bug: WORKSHOP_DEMO_CATALOG unset meant zero chips."""
    monkeypatch.setattr(demo_data.config, "workshop_demo_catalog", lambda: "")
    demo_data.reset_cache()

    state = wizard.state(user)

    assert state["industries"] == list(demo_data.KNOWN_INDUSTRIES)
    assert state["seeded_industries"] == []
    assert state["demo_data_available"] is False


def test_an_unreadable_catalog_does_not_withdraw_every_data_backed_card(
    user, monkeypatch
):
    """A permission error or a cold warehouse used to read as "no tables exist",
    which silently thinned the grid to generics with nothing saying why."""
    monkeypatch.setattr(
        demo_data.config, "workshop_demo_catalog", lambda: "workshop_demo"
    )
    # Configured, but every read fails — the transient case, not the unset one.
    monkeypatch.setattr(demo_data, "_load", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    demo_data.reset_cache()

    assert demo_data.enabled() is True
    assert demo_data.readable() is False

    ideas = wizard.state(user, "automotive_mobility")["ideas"]

    assert any(i["demo_tables"] for i in ideas), "data-backed cards were withdrawn"
    # ...but nothing claims the data is there, because nobody could check.
    assert all(i["data_ready"] is False for i in ideas)


def test_a_catalog_that_reads_clean_and_holds_nothing_is_not_unreadable(
    user, monkeypatch
):
    """An empty catalog is a successful read, and the two must not be conflated.

    Inferring readability from whether the inventory came back non-empty put
    this case on the unreadable branch, where ``_buildable`` stops filtering
    because it cannot check — for a catalog it had checked perfectly well. Cards
    naming tables that demonstrably did not exist were offered anyway.
    """
    monkeypatch.setattr(
        demo_data.config, "workshop_demo_catalog", lambda: "workshop_demo"
    )
    monkeypatch.setattr(demo_data, "_load", lambda: {})
    demo_data.reset_cache()

    assert demo_data.readable() is True, "the read succeeded; it just found nothing"

    ideas = wizard.state(user, "automotive_mobility")["ideas"]

    assert ideas, "generic cards need no demo data and must survive"
    assert not any(i["demo_tables"] for i in ideas), (
        "a card naming tables this catalog does not have was still offered"
    )


def test_the_data_badge_is_a_fact_about_this_catalog(seeded, user):
    """Shown only for cards whose tables were verified against live inventory."""
    ideas = wizard.state(user, "automotive_mobility")["ideas"]
    backed = [i for i in ideas if i["demo_tables"]]

    assert backed, "expected at least one data-backed card for a seeded industry"
    assert all(i["data_ready"] for i in backed)
    # A card that needs no demo data makes no promise about it either.
    assert all(not i["data_ready"] for i in ideas if not i["demo_tables"])


def test_a_typed_industry_nobody_seeded_survives_into_the_brief(seeded, user):
    """It used to be resolved against seeded schemas and dropped when it did not
    match, which lost the single most useful field on the record for every
    attendee whose industry the notebook had not created."""
    wizard.save(
        user,
        {
            "what_building": "a route planner",
            "industry": "Shipping & Logistics",
            "industry_stated": True,
        },
    )
    brief = wizard.read_brief(user)

    assert brief.industry == "shipping_logistics"
    assert brief.stated_industry == "shipping_logistics"
    assert wizard.to_discovery(brief)["industry"] == "shipping_logistics"


def test_a_known_industry_still_normalises_to_its_schema(seeded, user):
    """Free text must not fork the vocabulary: "Financial Services" typed into
    the Other box is the same industry as the chip."""
    wizard.save(user, {"what_building": "x", "industry": "Financial Services"})

    assert wizard.read_brief(user).industry == "financial_services"


def test_industry_slug_prefers_a_seeded_schema_then_a_known_one(seeded):
    assert demo_data.industry_slug("automotive mobility") == "automotive_mobility"
    assert demo_data.industry_slug("Financial-Services") == "financial_services"
    assert demo_data.industry_slug("  ") == ""
    assert demo_data.industry_slug("Deep Sea Mining") == "deep_sea_mining"


def test_the_shipped_pack_names_only_seed_manifest_schemas():
    """Renaming a seed-notebook schema used to empty the grid with no error.
    The manifest is the contract with that notebook: every pack demo_tables
    schema must be in it."""
    import json
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    pack = json.loads((root / "content" / "default_pack.json").read_text())
    manifest = json.loads(
        (root / "content" / "demo_seed_manifest.json").read_text()
    )
    allowed = {schema: set(tables) for schema, tables in manifest["schemas"].items()}
    missing: list[str] = []
    for idea in pack["ideas"]:
        for ref in idea.get("demo_tables") or []:
            schema, _, table = ref.partition(".")
            if schema not in allowed or table not in allowed[schema]:
                missing.append(ref)
    assert missing == [], missing
    assert pack.get("default_industry", "") == ""


def test_the_seed_manifest_ships_with_the_app():
    """It moved out of tests/fixtures precisely so the running app can read it.
    An empty KNOWN_INDUSTRIES means the asset did not make it into the deploy,
    which downgrades the picker to the bug this list exists to fix."""
    assert demo_data.KNOWN_INDUSTRIES, "seed manifest missing from content/"
    assert "retail" in demo_data.KNOWN_INDUSTRIES
    assert all(demo_data.industry_label(i) for i in demo_data.KNOWN_INDUSTRIES)


def test_every_known_industry_has_a_human_label():
    """The chips show the label; a slug leaking into the UI reads as a bug."""
    assert demo_data.industry_label("financial_services") == "Financial services"
    # Unknown slugs still render as something a person can read.
    assert demo_data.industry_label("space_mining") == "Space Mining"


def test_the_llm_wizard_is_on_by_default(client):
    from tests.conftest import BOB

    cfg = client.get("/api/config", headers=BOB).json()
    assert cfg["llm_wizard"]["enabled"] is True
    state = client.get("/api/wizard", headers=BOB).json()
    assert state["llm_wizard"]["enabled"] is True


def test_capture_being_off_does_not_withdraw_the_wizard(user, monkeypatch):
    """Capture decides whether the answer leaves the instance; the wizard decides
    whether the question is asked. A run that records nothing still wants the
    attendee to start with something running."""
    monkeypatch.setattr(wizard.config, "discovery_enabled", lambda: False)
    state = wizard.state(user)

    assert state["capture_enabled"] is False
    assert state["enabled"] is True


def test_one_attendees_brief_is_not_another_attendees(client):
    from tests.conftest import ALICE, BOB

    client.post("/api/wizard", headers=ALICE, json={"what_building": "Alice's build"})
    assert client.get("/api/wizard", headers=BOB).json()["should_show"] is True
    assert client.get("/api/wizard", headers=BOB).json()["brief"]["what_building"] == ""


def test_the_wizard_ui_makes_industry_a_choice_and_does_not_skip_on_backdrop():
    """Grep-style, same as the persona tests: there is no Wizard.tsx runner, and
    these strings are the contract the plan asked for."""
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1] / "frontend" / "src" / "components" / "Wizard.tsx").read_text()
    # The chips render on their own, not behind a length check on live
    # inventory: that gate is what removed the picker on an unset catalog.
    assert "industries.length > 0 && (" in src
    assert "state.seeded_industries" in src
    # An industry nobody seeded is still an industry someone works in.
    assert "otherOpen" in src
    assert "Tell us your industry" in src
    assert "industryOf(idea)" in src
    assert "works with any data" in src
    assert "A little context (optional)" in src
    assert "Press Enter to start" in src
    assert 'className="modal-backdrop" onClick={skip}' not in src
    assert "wizardSuggest" in src
    assert "wizardSurprise" in src
    # The reported bug: cards arrived and landed behind a collapsed panel, so
    # the spinner was the only evidence the LLM path existed.
    assert "if (reveal && res.ideas.length > 0) setShowIdeas(true);" in src
    # The badge is a fact from the server, not an inference from card shape.
    assert "idea.data_ready &&" in src
