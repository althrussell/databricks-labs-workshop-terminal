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


def test_an_unseeded_industry_still_gets_a_full_grid(seeded):
    """Degrades to generic cards, which need no demo data. Never empty: an empty
    grid is worst for the attendee least able to recover from it."""
    ideas = wizard.select_ideas("healthcare", rng=random.Random(3))
    assert len(ideas) == wizard.IDEA_COUNT


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


def test_default_industry_is_ignored_when_it_is_not_seeded(seeded, monkeypatch):
    """A pack naming an industry the notebook never created would otherwise
    preselect a filter that quietly empties the grid."""
    monkeypatch.setattr(content.content_service, "default_industry", lambda: "healthcare")
    assert wizard.default_industry() == ""

    monkeypatch.setattr(
        content.content_service, "default_industry", lambda: "automotive_mobility"
    )
    assert wizard.default_industry() == "automotive_mobility"


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
