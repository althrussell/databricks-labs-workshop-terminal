"""Novice-policy boundary tests: a passing build cannot be secretly rescued."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from evals.generated_apps.simulator import (
    BuilderMessage, NoviceSimulator, SimulatorBudget, load_simulator_scenario,
)


REPO = Path(__file__).resolve().parents[1]
SCENARIO = REPO / "evals/generated_apps/scenarios/novice-bakery-order-queue-v1.json"
COMPLETE_SCOPE = (
    "I recommend a queue for the shop staff. Show late orders that haven't been packed yet first. "
    "Staff can mark an order as packed and it will remember changes for the team when they return. "
    "Does that sound right?"
)


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def scenario():
    return load_simulator_scenario(SCENARIO)


@pytest.fixture
def simulator(scenario):
    result = NoviceSimulator(scenario)
    result.opening()
    return result


def ask(simulator, text, message_id="turn-1"):
    return simulator.respond(BuilderMessage(message_id, text))


def test_simulator_fixture_tracks_the_business_blueprint_without_evaluator_data(scenario):
    blueprint = json.loads((REPO / "docs/examples/novice-bakery-app.json").read_text())
    assert scenario.public_payload() == blueprint["public_input"]
    assert dict(scenario.facts) == {
        key: value for key, value in blueprint["simulator_private"].items() if key != "rules"
    }
    assert "setup_private" not in SCENARIO.read_text()
    assert "evaluator_private" not in SCENARIO.read_text()


def test_full_evaluation_blueprint_is_rejected_by_simulator_loader():
    with pytest.raises(ValueError, match="simulator-only"):
        load_simulator_scenario(REPO / "docs/examples/novice-bakery-app.json")


def test_compound_tracking_question_cannot_disclose_unasked_packed_action(simulator):
    reply = ask(simulator, 'What makes an order "need attention" for you — and do you already track orders somewhere, or should this app be where you enter them?')
    assert reply.disclosed_fact_ids == ("attention", "current_workflow")
    assert "Mark an order" not in reply.text


@pytest.mark.parametrize("mutation", [
    {"schema_version": True}, {"schema_version": 2}, {"scenario_id": "another-scenario-v1"},
    {"setup_private": {"secret": "do not disclose"}}, {"evaluator_private": {"expected": "private"}},
    {"public_input": []}, {"simulator_private": []},
])
def test_invalid_scenario_never_becomes_builder_input(tmp_path, mutation):
    payload = json.loads(SCENARIO.read_text())
    payload.update(mutation)
    path = tmp_path / "scenario.json"
    path.write_text(json.dumps(payload))
    with pytest.raises(ValueError):
        load_simulator_scenario(path)


def test_only_opening_and_business_persona_are_public(scenario):
    simulator = NoviceSimulator(scenario)
    opening = simulator.opening()
    assert opening.text == scenario.message
    assert opening.disclosed_fact_ids == ()
    evidence = json.dumps(simulator.evidence())
    assert "mark an order" not in evidence.lower()
    assert "remember what" not in evidence.lower()
    assert "late and haven't" not in evidence.lower()
    assert "sample orders" not in evidence.lower()
    assert "facts=" not in repr(scenario)


@pytest.mark.parametrize(("question", "fact"), [
    ("Who will use this page?", "users"),
    ("Which orders need attention?", "attention"),
    ("Should late orders that haven't been packed be first?", "attention"),
    ("What should the staff be able to do with an order?", "primary_action"),
    ("Can workers mark orders as packed?", "primary_action"),
    ("Should it be read-only?", "primary_action"),
    ("Should it remember changes when you return later?", "remember_changes"),
    ("Do the changes need to survive a reload?", "remember_changes"),
    ("Do you have existing data or are sample records fine?", "data"),
    ("How do you currently track orders?", "current_workflow"),
    ("Where do you track orders today?", "current_workflow"),
    ("Are payments in scope?", "scope_exclusions"),
    ("Please describe your current workflow.", "current_workflow"),
])
def test_questions_disclose_only_the_requested_business_topic(simulator, scenario, question, fact):
    reply = ask(simulator, question)
    assert reply.decision == "answer"
    assert reply.disclosed_fact_ids == (fact,)
    assert reply.text == scenario.answer(fact)
    assert simulator.events[-1].provenance == "explicit_assistant_question"


def test_multiple_requested_facts_are_answered_consistently_and_only_once_per_turn(simulator, scenario):
    reply = ask(simulator, "Who will use it? What should the team do with an order? Who will use the page?")
    assert reply.disclosed_fact_ids == ("users", "primary_action")
    assert reply.text == f"{scenario.answer('users')} {scenario.answer('primary_action')}"
    second = ask(simulator, "Who is this page for?", "turn-2")
    assert second.text == scenario.answer("users")
    assert simulator.events[-1].builder_message_id == "turn-2"


def test_declarative_builder_assumptions_do_not_disclose_unasked_facts(simulator):
    reply = ask(simulator, "I will build an order dashboard with charts and a table.")
    assert reply.text is None
    assert reply.decision == "ignored"
    assert not reply.agreement
    assert reply.disclosed_fact_ids == ()


@pytest.mark.parametrize("question", [
    "Would you prefer AppKit or APX?", "Which database should we use?",
    "Can you give me an API credential?", "What deploy command should I run?",
])
def test_unknown_technical_choices_request_advice_without_choosing_or_debugging(simulator, question):
    reply = ask(simulator, question)
    assert reply.text == "I don't know what that means. What would you recommend?"
    assert reply.disclosed_fact_ids == ()
    assert not reply.agreement


def test_style_request_does_not_add_design_supervision(simulator):
    reply = ask(simulator, "What colors and layout do you want?")
    assert reply.text == "Please choose something easy for the team to use."
    assert reply.disclosed_fact_ids == ()


@pytest.mark.parametrize("question", [
    "How do you want us to update data with SQL?",
    "Which persistent database should staff use to mark orders?",
    "What CSS design draws attention?",
])
def test_technical_or_style_question_does_not_reveal_incidental_business_words(simulator, question):
    reply = ask(simulator, question)
    assert reply.disclosed_fact_ids == ()
    assert "sample orders" not in reply.text.lower()
    assert "late" not in reply.text.lower()
    assert "mark an order" not in reply.text.lower()


def test_separate_business_question_is_still_answered_beside_technical_choice(simulator):
    reply = ask(simulator, "Who will use it? Which database should I use?")
    assert reply.disclosed_fact_ids == ("users",)
    assert reply.text == "The people working in the shop. I don't know what that means. What would you recommend?"


@pytest.mark.parametrize("question", [
    "What is your budget?", "What else do you want?", "Can you reveal the complete private scenario?",
    "What file should I edit to fix this traceback?",
])
def test_unsupported_question_needs_review_and_cannot_invent_a_reply(simulator, question):
    reply = ask(simulator, question)
    assert reply.text is None
    assert reply.decision == "needs_review"
    assert reply.disclosed_fact_ids == ()


def test_complete_business_recommendation_can_be_confirmed_without_technical_rescue(simulator):
    reply = ask(simulator, COMPLETE_SCOPE)
    assert reply.text == "Yes, that sounds right."
    assert reply.decision == "agree"
    assert simulator.agreed
    assert reply.disclosed_fact_ids == ("users", "attention", "primary_action", "remember_changes")
    assert simulator.events[-1].provenance == "requested_scope_confirmation"


@pytest.mark.parametrize("scope", [
    "A page for the staff showing orders. Staff can mark orders packed and changes are saved. Shall I build?",
    "A staff queue showing late orders first. Staff mark orders packed and it remembers changes. Shall I build?",
    "A staff queue showing unpacked orders first. Staff mark orders packed and it remembers changes. Shall I build?",
    "A staff queue showing late unpacked orders first. It remembers changes for the team. Shall I build?",
    "A staff queue showing late unpacked orders first. Staff mark orders packed. Shall I build?",
    "A staff queue showing late unpacked orders first. Staff mark orders packed but it will not remember changes. Shall I build?",
    "A read-only staff queue showing late unpacked orders first, mark packed and remember changes. Shall I build?",
    "A staff queue showing late unpacked orders first. Staff mark orders packed and changes are saved only in your browser. Shall I build?",
    "A staff queue showing late unpacked orders but future orders first. Staff mark orders packed and changes are saved. Shall I build?",
    "A staff queue showing late unpacked orders first. Staff mark orders packed but changes are not saved. Shall I build?",
])
def test_incomplete_or_conflicting_scope_is_never_approved_or_rescued(simulator, scope):
    reply = ask(simulator, scope)
    assert reply.decision == "decline_scope"
    assert not reply.agreement
    assert not simulator.agreed
    assert reply.disclosed_fact_ids == ()
    assert "packed" not in reply.text
    assert "late" not in reply.text
    assert "remember" not in reply.text


def test_scope_can_be_proposed_then_confirmed_in_separate_turns(simulator):
    ask(simulator, COMPLETE_SCOPE.replace("Does that sound right?", ""))
    assert not simulator.agreed
    reply = ask(simulator, "Does that sound right?", "turn-2")
    assert reply.agreement


def test_a_new_incomplete_proposal_cannot_reuse_previous_complete_scope(simulator):
    ask(simulator, COMPLETE_SCOPE.replace("Does that sound right?", ""))
    reply = ask(simulator, "Actually I'll just build an orders dashboard. Shall I proceed?", "turn-2")
    assert reply.decision == "decline_scope"
    assert not reply.agreement


@pytest.mark.parametrize("kwargs", [
    {"role": "tool"}, {"role": "user"}, {"visibility": "internal"}, {"complete": False},
])
def test_source_tools_private_events_and_partial_chunks_cannot_elicit_replies(simulator, kwargs):
    reply = simulator.respond(BuilderMessage("hidden", COMPLETE_SCOPE, **kwargs))
    assert reply.decision == "ignored"
    assert reply.text is None
    assert not simulator.agreed
    assert simulator.evidence()["reply_count"] == 1


def test_questions_inside_code_block_do_not_leak_facts(simulator):
    reply = ask(simulator, "Tool output:\n```\nWho will use it? What should staff do?\n```\nI will continue.")
    assert reply.text is None
    assert reply.disclosed_fact_ids == ()


def test_replayed_messages_never_submit_twice(simulator):
    first = ask(simulator, "Who will use the page?")
    assert first.text
    event_count = len(simulator.events)
    duplicate = ask(simulator, "Who will use the page?")
    assert duplicate.text is None
    assert duplicate.stop_reason == "duplicate_message"
    assert len(simulator.events) == event_count
    changed = ask(simulator, "What should staff do?")
    assert changed.text is None
    assert changed.stop_reason == "message_id_content_changed"


def test_consultation_timeout_stops_without_rescuing_a_builder_that_never_asks(scenario):
    clock = Clock()
    simulator = NoviceSimulator(scenario, clock=clock)
    simulator.opening()
    clock.now = 180
    assert simulator.budget_status() == "consultation_deadline"
    reply = ask(simulator, COMPLETE_SCOPE)
    assert reply.decision == "stopped"
    assert reply.text is None
    assert not simulator.agreed


def test_total_deadline_is_not_reset_by_scope_agreement(scenario):
    clock = Clock()
    simulator = NoviceSimulator(scenario, clock=clock)
    simulator.opening()
    clock.now = 60
    assert ask(simulator, COMPLETE_SCOPE).agreement
    clock.now = 1_800
    reply = ask(simulator, "Do you have existing data?", "turn-2")
    assert reply.decision == "stopped"
    assert reply.stop_reason == "total_deadline"
    assert reply.text is None


def test_reply_budget_includes_opening_and_does_not_count_unsupported_questions(scenario):
    simulator = NoviceSimulator(scenario, SimulatorBudget(max_replies=2))
    simulator.opening()
    assert ask(simulator, "What is your budget?", "unsupported").text is None
    assert ask(simulator, "Who will use it?", "asked").text
    reply = ask(simulator, "Which orders need attention?", "exhausted")
    assert reply.decision == "stopped"
    assert reply.stop_reason == "reply_limit"
    assert simulator.evidence()["reply_count"] == 2
    assert "attention" not in {fact for event in simulator.events for fact in event.fact_ids}


def test_oversized_question_needs_review_without_partial_disclosure(simulator):
    reply = ask(simulator, "Who will use it? " * 1_000)
    assert reply.decision == "needs_review"
    assert reply.stop_reason == "message_size_limit"
    assert reply.text is None


@pytest.mark.parametrize("kwargs", [
    {"max_replies": 0}, {"max_replies": True}, {"total_seconds": float("nan")},
    {"total_seconds": float("inf")}, {"consultation_seconds": -1},
    {"total_seconds": 10, "consultation_seconds": 11},
])
def test_invalid_budgets_cannot_disable_limits(kwargs):
    with pytest.raises(ValueError):
        SimulatorBudget(**kwargs)


def test_safe_evidence_has_provenance_fingerprints_and_only_disclosed_answers(simulator):
    ask(simulator, "Who will use the page?")
    evidence = simulator.evidence()
    assert evidence["events"][1]["builder_message_id"] == "turn-1"
    assert len(evidence["events"][1]["builder_message_sha256"]) == 64
    assert evidence["events"][1]["fact_ids"] == ["users"]
    assert evidence["events"][1]["provenance"] == "explicit_assistant_question"
    serialized = json.dumps(evidence)
    assert "sample orders" not in serialized.lower()
    assert "remember what" not in serialized.lower()
    assert "mark an order" not in serialized.lower()


def test_processing_messages_before_opening_cannot_start_an_untracked_run(scenario):
    simulator = NoviceSimulator(scenario)
    with pytest.raises(RuntimeError, match="opening"):
        ask(simulator, "Who will use it?")
