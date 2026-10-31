"""The 31 Oct sweep's instruction wording, checked in what each agent receives.

Each rule is pinned in every channel that must carry it: home Claude, home Codex
and the project template (Omni's Codex worker reads only the project template).
Text is compared whitespace-normalised and lower-cased, so a line wrap cannot
hide or fake a rule.
"""

from __future__ import annotations

import uuid

import pytest

from .test_teammate_config import _channels, _home, _open, _ready

HOME_CHANNELS = ("claude", "codex")
ALL_CHANNELS = ("claude", "codex", "project")


@pytest.fixture()
def rendered(client, monkeypatch):
    """The three channels for a fresh attendee, lower-cased and whitespace-normalised."""
    monkeypatch.setenv("WORKSHOP_INSIGHT_CAPTURE", "true")
    monkeypatch.setenv("DISCOVERY_ENABLED", "true")
    person = {"X-Forwarded-Email": f"wording-{uuid.uuid4().hex[:8]}@example.com"}
    _ready(monkeypatch)
    _open(client, person, "claude", has_starter_prompt=True)
    return {name: " ".join(text.split()).lower() for name, text in _channels(_home(person)).items()}


def _has(rendered, channels, *phrases):
    for channel in channels:
        for phrase in phrases:
            assert phrase.lower() in rendered[channel], f"{channel} lacks {phrase!r}"


# --- plan approval (§2) -------------------------------------------------------------


def test_claude_starts_in_plan_mode_and_codex_uses_the_workflow_gate(rendered):
    _has(rendered, ("claude",), "you start in plan mode", "AskUserQuestion", "ExitPlanMode",
         "discover data read-only", "build after approval")
    _has(rendered, ("codex", "project"), "Start building this?", "request_user_input",
         "workshop-workflow plan --approved --source attendee", "then scaffold")


def test_every_channel_says_init_refuses_before_approval_and_rapid_is_not_approval(rendered):
    _has(rendered, ALL_CHANNELS, "refuse", "approve")
    _has(rendered, ALL_CHANNELS, "A rapid prototype, please.")
    _has(rendered, ("claude", "codex"), "only an explicit yes", "not approval")


# --- meeting and industry ----------------------------------------------------------------


def test_the_meeting_offers_every_seeded_industry_or_the_confident_suggestion(rendered):
    _has(rendered, HOME_CHANNELS, "Is <industry> the right demo data?", "Something else",
         "every seeded industry there")
    _has(rendered, HOME_CHANNELS, "How they want to work with you")


def test_a_form_that_closes_unanswered_is_asked_again_in_text(rendered):
    _has(rendered, HOME_CHANNELS, "If a form closes unanswered, ask the same question in text")


# --- working style (STYLE-1) and preferences ---------------------------------------------


def test_a_change_of_working_style_is_saved_with_prefs(rendered):
    _has(rendered, HOME_CHANNELS, "Just build the rest for me", "save it with `prefs`", "confirm in one line")


# --- data honesty -------------------------------------------------------------------------


def test_plans_name_the_demo_tables_and_numbers_are_labelled(rendered):
    _has(rendered, ALL_CHANNELS, "workshop_demo` tables")
    _has(rendered, HOME_CHANNELS, "label synthetic or assumed numbers")


def test_the_strongest_signal_is_reported_and_no_signal_is_not_invented(rendered):
    _has(rendered, HOME_CHANNELS, "report the strongest planted signal, not the first",
         "never read a signal into data that has none")
    _has(rendered, ("project",), "report the strongest planted signal")


def test_the_demo_catalog_is_read_in_place_and_never_granted(rendered):
    _has(rendered, ALL_CHANNELS, "never grant on")


# --- hygiene ----------------------------------------------------------------------------------


def test_deploys_are_typechecked_and_failures_are_named(rendered):
    _has(rendered, HOME_CHANNELS, "run the typecheck and tests before each deploy",
         "name it in the wrap")
    _has(rendered, ("project",), "run the typecheck", "name any failure in the wrap")


def test_test_records_are_asked_about_and_cleaned_up(rendered):
    _has(rendered, HOME_CHANNELS, "ask before writing one into their app data", "clean it up")


def test_the_preview_link_comes_before_the_records(rendered):
    _has(rendered, HOME_CHANNELS, "post the preview link first, then record")


def test_mid_build_changes_are_offered_not_made_silently(rendered):
    _has(rendered, HOME_CHANNELS, "never change them silently")


def test_rapid_mode_states_the_design_direction_in_one_line(rendered):
    _has(rendered, HOME_CHANNELS, "state the design direction in one line")


def test_agent_bricks_missing_is_said_plainly_with_a_named_alternative(rendered):
    _has(rendered, ALL_CHANNELS, "MODEL is not enabled")
    _has(rendered, HOME_CHANNELS, "say so plainly and offer Genie or a simpler search by name",
         "never ship a lesser chat under the agent bricks name")
    _has(rendered, ("project",), "offer Genie or a simple search")


# --- voice ---------------------------------------------------------------------------------------


def test_no_channel_carries_the_coffee_opener_or_a_catchphrase(rendered):
    for channel, text in rendered.items():
        assert "coffee" not in text, channel
        assert "just... good" not in text, channel
        assert "2am" not in text, channel


def test_every_channel_forbids_stock_lines_and_swearing(rendered):
    _has(rendered, HOME_CHANNELS, "never swear", "no stock lines")
    _has(rendered, ("project",), "no swearing", "no stock openers")


def test_brevity_is_first_in_every_channel(rendered):
    _has(rendered, HOME_CHANNELS, "brevity is mandatory")
    _has(rendered, ("project",), "brevity first")


def test_one_unprompted_data_finding_follows_the_first_preview(rendered):
    _has(rendered, HOME_CHANNELS, "after the first preview, share one thing the data shows")


# --- Omni continuity and discovery -------------------------------------------------------------


def test_terminal_commits_are_the_attendees_own_work(rendered):
    _has(rendered, HOME_CHANNELS, "workshop terminal commits are their own work", 'never "someone"')
    _has(rendered, ("project",), "workshop terminal commits are theirs", 'never "someone"')


def test_one_build_keeps_one_discovery_record_across_agents(rendered):
    _has(rendered, HOME_CHANNELS, "one build, one id", "only new build starts another")
    _has(rendered, ("project",), "one build, one `record_id`", "only new build starts another")
