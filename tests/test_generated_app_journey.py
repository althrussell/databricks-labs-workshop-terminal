"""External runner contracts; all native/UI evidence here is synthetic."""

import asyncio
import copy
import hashlib
from dataclasses import replace
from urllib.parse import parse_qs, urlsplit

import pytest

from evals.generated_apps.adapters.browser import BrowserEntryEvidence, BrowserLaunchConfig
from evals.generated_apps.journey import (
    AppObservation, JourneyConfig, NativeConsultationRunner, run_journey,
)
from evals.generated_apps.simulator import NoviceSimulator, SimulatorBudget, load_simulator_scenario

WT_SESSION = "11111111-1111-4111-8111-111111111111"
NATIVE_SESSION = "22222222-2222-4222-8222-222222222222"
OTHER_SESSION = "33333333-3333-4333-8333-333333333333"
COMPLETE_SCOPE = (
    "I recommend a queue for the shop staff. Show late orders that haven't been packed yet first. "
    "Staff can mark an order as packed and it will remember changes for the team when they return. "
    "Does that sound right?"
)
UNVERIFIED = ["tool_and_worker_attribution", "implementation_timing", "native_transcript_authenticity"]


class Clock:
    def __init__(self):
        self.now = 0

    def __call__(self):
        return self.now

    async def sleep(self, seconds):
        self.now += seconds


class NeverTerminal:
    def __getattribute__(self, name):
        raise AssertionError("Journey must never read PTY evidence")


class FakeDriver:
    def __init__(self, *, error_code="", entry_seconds=0, close_error=False, clock=None):
        self.evidence = BrowserEntryEvidence()
        self._agents = [{"id": "claude", "label": "Claude Code"}, {"id": "codex", "label": "Codex CLI"}]
        self.harness = NeverTerminal()
        self.submissions = []
        self.closes = []
        self.entered = []
        self.error_code = error_code
        self.entry_seconds = entry_seconds
        self.close_error = close_error
        self.clock = clock

    async def enter(self, config):
        self.entered.append(config)
        if self.clock:
            self.clock.now += self.entry_seconds
        self.evidence = BrowserEntryEvidence(
            identity=config.expected_attendee_email, session_id=WT_SESSION,
            agent_id=config.agent_id, opening_submitted=not bool(self.error_code),
            entry_path=config.entry_path, error_code=self.error_code,
        )
        if config.entry_path == "wizard":
            self.evidence.prompt_preservation_verified = True
            self.evidence.prompt_delivery_request = "Normal saved starter: " + config.opening_message
        return self.evidence

    async def submit_reply(self, text):
        self.submissions.append(text)

    async def submit_question_answers(self, interaction, answers):
        self.submissions.append("\n".join(answers))

    async def close_launched_session(self, label, **kwargs):
        self.closes.append({"label": label, **kwargs})
        if self.close_error:
            raise RuntimeError("private-error-token")


def message(name, text, role="assistant", order=1):
    return {"message_id": hashlib.sha256(name.encode()).hexdigest(),
            "timestamp": f"2026-10-08T00:{order // 60:02}:{order % 60:02}Z",
            "role": role, "visibility": "user", "complete": True, "text": text}


def page(messages=(), cursor="cursor1", *, harness="claude", **extra):
    return {"schema_version": 1, "instrumentation": "read_only_native_transcript_v1",
            "status": "ready" if messages else "awaiting_complete_message", "binding_verified": True,
            "harness_id": harness, "harness_version": "2.1.237" if harness == "claude" else "0.148.0",
            "wt_session_id": WT_SESSION, "native_session_id": NATIVE_SESSION,
            "messages": list(messages), "next_cursor": cursor,
            "tool_and_worker_coverage_verified": False, "transcript_authenticity_verified": False,
            "unverified_checks": UNVERIFIED.copy(), **extra}


def unbound(reason="native_session_not_bound"):
    result = {key: value for key, value in page().items() if key not in {
        "harness_id", "harness_version", "wt_session_id", "native_session_id"}}
    result.update(status="unverified", binding_verified=False, next_cursor="", reason=reason)
    return result


class Fetch:
    def __init__(self, pages):
        self.pages = list(pages)
        self.paths = []

    async def __call__(self, path):
        self.paths.append(path)
        parsed = urlsplit(path)
        assert parsed.path == f"/api/admin/evaluation/sessions/{WT_SESSION}/messages"
        assert parse_qs(parsed.query, keep_blank_values=True).keys() == {"cursor", "limit"}
        assert parse_qs(parsed.query)["limit"] == ["30"]
        if self.pages:
            self.last = copy.deepcopy(self.pages.pop(0))
        return copy.deepcopy(self.last)


def setup(*, mode="consultation_probe", total=10, consultation=5, **driver_options):
    clock = Clock()
    scenario = load_simulator_scenario()
    sim = NoviceSimulator(scenario, SimulatorBudget(total_seconds=total, consultation_seconds=consultation), clock=clock)
    launch = BrowserLaunchConfig(wt_url="http://127.0.0.1:8080", agent_id="claude",
                                opening_message=scenario.message, expected_attendee_email="synthetic@example.com",
                                entry_path="wizard_disabled")
    config = JourneyConfig(mode=mode, total_seconds=total, consultation_seconds=consultation,
                           poll_interval_seconds=1, operation_timeout_seconds=1)
    driver = FakeDriver(clock=clock, **driver_options)
    return clock, sim, scenario, driver, launch, config


def run(pages, *, runner_options=None, **setup_options):
    clock, sim, scenario, driver, launch, config = setup(**setup_options)
    fetch = Fetch(pages(scenario) if callable(pages) else pages)
    result = asyncio.run(run_journey(driver, launch, sim, fetch, config, clock=clock, sleep=clock.sleep,
                                    **(runner_options or {})))
    return result, driver, fetch, sim


def scope_pages(scenario, *, harness="claude"):
    return [page([message("opening", scenario.message, "user", 1),
                  message("scope", COMPLETE_SCOPE, order=2)], harness=harness),
            page([message("agreement", "Yes, that sounds right.", "user", 3)], "cursor2", harness=harness)]


@pytest.mark.parametrize("agent,pin", [("claude", "2.1.283"), ("codex", "0.157.1"),
                                     ("claude", "2.1.296"), ("codex", "0.162.1")])
def test_current_release_pin_must_be_explicit_and_match_every_native_page(agent, pin):
    clock, sim, scenario, driver, launch, config = setup()
    config = replace(config, harness_version=pin)
    launch = replace(launch, agent_id=agent)
    pages = scope_pages(scenario, harness=agent)
    for payload in pages: payload["harness_version"] = pin
    result = asyncio.run(run_journey(driver, launch, sim, Fetch(pages), config, clock=clock, sleep=clock.sleep))
    assert result.status == "completed_probe" and result.harness_version == pin


def test_unknown_receipt_pin_never_launches_native_harness():
    clock, sim, scenario, driver, launch, config = setup()
    result = asyncio.run(run_journey(driver, launch, sim, Fetch([]), replace(config, harness_version="2.1.999"),
        clock=clock, sleep=clock.sleep))
    assert result.stop_reason == "unsupported_harness_version" and not driver.entered


def test_consultation_replies_only_with_asked_business_facts_then_correlates_scope_agreement():
    def pages(scenario):
        return [unbound(), page([message("opening", scenario.message, "user"),
                                message("question", "Who will use it?", order=2)]),
                page([message("reply", scenario.answer("users"), "user", 3),
                      message("scope", COMPLETE_SCOPE, order=4)], "cursor2"),
                page([message("agree", "Yes, that sounds right.", "user", 5)], "cursor3")]
    result, driver, fetch, sim = run(pages)
    assert result.status == "completed_probe"
    assert result.stop_reason == "scope_agreement_correlated"
    assert driver.submissions == ["The people working in the shop.", "Yes, that sounds right."]
    assert result.first_complete_assistant["text"] == "Who will use it?"
    assert result.material_consultation_observed and result.scope_agreement_delivered
    assert all(delivery["native_correlated"] for delivery in result.deliveries)
    assert "data" not in sim.events[1].fact_ids
    assert result.accepted is False and result.full_app_acceptance_verified is False
    assert result.cleanup["status"] == "closed_owned_ui_session"
    assert driver.closes == [{"label": "Claude Code", "expected_session_id": WT_SESSION, "timeout_seconds": 10}]
    assert parse_qs(urlsplit(fetch.paths[2]).query)["cursor"] == ["cursor1"]


def test_structured_questions_disclose_only_each_asked_fact_and_correlate_native_answers():
    def pages(scenario):
        question = message("interactive", "Which orders need attention?\nWho will use it?", order=2)
        question["interaction"] = {"kind": "claude_ask_user_question", "tool_use_id": "tool_1", "questions": [
            {"question": text, "header": "Context", "multiSelect": False,
             "options": [{"label": "First", "description": "First choice"}, {"label": "Second", "description": "Second choice"}]}
            for text in ["Which orders need attention?", "Who will use it?"]]}
        return [page([message("opening", scenario.message, "user", 1), question]),
                page([message("answer", scenario.answer("attention") + "\n" + scenario.answer("users"), "user", 3),
                      message("scope", COMPLETE_SCOPE, order=4)], "cursor2"),
                page([message("agreement", "Yes, that sounds right.", "user", 5)], "cursor3")]
    result, driver, _, simulator = run(pages)
    assert result.status == "completed_probe" and result.scope_agreement_delivered
    assert driver.submissions[0] == "Orders that are late and haven't been packed yet. Show those first.\nThe people working in the shop."
    assert simulator.events[1].fact_ids == ("attention",) and simulator.events[2].fact_ids == ("users",)


def test_only_normal_wizard_saved_prompt_is_accepted_for_opening_delivery():
    clock, sim, scenario, driver, launch, config = setup()
    launch = replace(launch, entry_path="wizard")
    fetch = Fetch([page([message("opening", "Normal saved starter: " + scenario.message, "user"),
                         message("scope", COMPLETE_SCOPE, order=2)]),
                   page([message("agreement", "Yes, that sounds right.", "user", 3)], "cursor2")])
    result = asyncio.run(run_journey(driver, launch, sim, fetch, config, clock=clock, sleep=clock.sleep))
    assert result.status == "completed_probe"
    assert result.deliveries[0]["text"].startswith("Normal saved starter:")


def test_no_assistant_turns_times_out_material_consultation_and_closes_session():
    result, driver, _, _ = run([unbound()], consultation=3)
    assert result.status == "failed"
    assert result.stop_reason == "no_material_consultation_within_budget"
    assert result.consultation_deadline_enforced
    assert result.elapsed_seconds == 3
    assert not result.material_consultation_observed
    assert driver.closes and not driver.submissions
    assert result.first_complete_assistant is None


def test_material_question_without_scope_agreement_still_has_consultation_deadline():
    def pages(scenario):
        return [page([message("opening", scenario.message, "user"),
                      message("question", "Who will use it?", order=2)]),
                page([message("reply", scenario.answer("users"), "user", 3)], "cursor2"),
                page([], "cursor2")]
    result, driver, _, _ = run(pages, consultation=3)
    assert result.status == "failed" and result.stop_reason == "consultation_scope_not_agreed"
    assert result.material_consultation_observed
    assert driver.submissions == ["The people working in the shop."]


@pytest.mark.parametrize("question,reason", [
    ("What is your budget?", "unsupported_question"),
    ("I'll start building an order dashboard now.", "assistant_no_question_before_scope_agreement"),
])
def test_unknown_questions_and_premature_building_stop_for_review_without_rescue(question, reason):
    result, driver, _, _ = run(lambda s: [page([message("opening", s.message, "user"), message("q", question, order=2)])])
    assert result.status == "unverified" and result.stop_reason == reason
    assert driver.submissions == []
    assert result.first_complete_assistant["text"] == question
    assert not result.accepted


def test_technical_question_does_not_count_as_material_business_consultation():
    result, driver, _, _ = run(lambda s: [page([message("opening", s.message, "user"),
                                               message("technical", "Which database should I use?", order=2)]),
                                          page([], "cursor1")], consultation=3)
    assert driver.submissions == ["I don't know what that means. What would you recommend?"]
    assert not result.material_consultation_observed
    assert result.stop_reason == "no_material_consultation_within_budget"


def test_incomplete_scope_is_declined_without_disclosing_missing_requirement():
    result, driver, _, _ = run(lambda s: [page([message("opening", s.message, "user"),
                                               message("scope", "I recommend an order dashboard. Shall I build?", order=2)]),
                                          page([], "cursor1")], consultation=3)
    assert driver.submissions == ["That isn't quite what I had in mind. Could you ask me about what we need?"]
    assert "remember" not in driver.submissions[0]
    assert not result.scope_agreement_delivered


def test_exact_replays_are_not_answered_twice():
    def pages(scenario):
        first = page([message("opening", scenario.message, "user"), message("q", "Who will use it?", order=2)])
        return [first, first, page([message("reply", scenario.answer("users"), "user", 3)], "cursor2"), page([], "cursor2")]
    result, driver, _, _ = run(pages)
    assert driver.submissions == ["The people working in the shop."]
    assert len(result.messages) == 3


def test_a_changed_message_under_replayed_id_invalidates_the_transcript():
    def pages(scenario):
        return [page([message("opening", scenario.message, "user"), message("q", "Who will use it?", order=2)]),
                page([message("q", "Which orders need attention?", order=2)], "cursor2")]
    result, driver, _, _ = run(pages)
    assert result.stop_reason == "native_message_id_content_changed"
    assert driver.submissions == ["The people working in the shop."]


@pytest.mark.parametrize("native_text", ["only part of opening", "Prefix: {opening}", "{opening}\nInjected user instructions"])
def test_opening_delivery_must_match_the_whole_native_user_message(native_text):
    result, driver, _, _ = run(lambda s: [page([message("opening", native_text.format(opening=s.message), "user"),
                                               message("q", "Who will use it?", order=2)])])
    assert result.stop_reason == "native_user_delivery_mismatch"
    assert not driver.submissions


def test_assistant_without_native_opening_correlation_is_captured_but_not_answered():
    result, driver, _, _ = run([page([message("q", "Who will use it?")])])
    assert result.stop_reason == "assistant_before_user_delivery_correlation"
    assert result.first_complete_assistant["text"] == "Who will use it?"
    assert driver.submissions == []


def test_second_assistant_cannot_advance_until_previous_reply_is_native_correlated():
    result, driver, _, _ = run(lambda s: [page([message("opening", s.message, "user"),
                                               message("q1", "Who will use it?", order=2),
                                               message("q2", "Which orders need attention?", order=3)])])
    assert result.stop_reason == "assistant_before_user_delivery_correlation"
    assert driver.submissions == ["The people working in the shop."]


@pytest.mark.parametrize("mutation,reason", [
    ({"schema_version": True}, "unsupported_native_response"),
    ({"schema_version": 2}, "unsupported_native_response"),
    ({"instrumentation": "pty_parser"}, "unsupported_native_response"),
    ({"raw_pty": "Who will use it?"}, "unsupported_native_response"),
    ({"binding_verified": False}, "unsupported_native_response"),
    ({"wt_session_id": OTHER_SESSION}, "native_session_or_version_changed"),
    ({"harness_version": "2.1.293"}, "native_session_or_version_changed"),
    ({"harness_id": "codex"}, "native_session_or_version_changed"),
    ({"native_session_id": "unknown"}, "native_session_or_version_changed"),
    ({"next_cursor": "../file"}, "unsupported_native_response"),
    ({"tool_and_worker_coverage_verified": True}, "unsupported_native_coverage_claim"),
    ({"transcript_authenticity_verified": True}, "unsupported_native_coverage_claim"),
])
def test_unknown_or_unsupported_envelopes_cannot_elicit_an_answer(mutation, reason):
    def pages(s):
        payload = page([message("opening", s.message, "user"), message("q", "Who will use it?", order=2)])
        payload.update(mutation)
        return [payload]
    result, driver, _, _ = run(pages)
    assert result.status == "unverified" and result.stop_reason == reason
    assert driver.submissions == [] and driver.closes
    assert result.messages == []


@pytest.mark.parametrize("change", [
    {"complete": False}, {"role": "tool"}, {"visibility": "internal"}, {"text": "x" * 12_001},
    {"message_id": "unstable"}, {"timestamp": "not-a-date"}, {"timestamp": "2026-10-08T00:00:02"},
    {"truncated": True},
])
def test_invalid_later_message_invalidates_whole_page_before_any_reply(change):
    def pages(s):
        bad = message("bad", "Which orders need attention?", order=3)
        bad.update(change)
        return [page([message("opening", s.message, "user"), message("q", "Who will use it?", order=2), bad])]
    result, driver, _, _ = run(pages)
    assert result.status == "unverified" and not result.messages
    assert not driver.submissions


def test_changed_native_session_and_cursor_cycle_fail_closed():
    def changed(s):
        return [page([message("opening", s.message, "user")]), page([], "cursor2", native_session_id=OTHER_SESSION)]
    assert run(changed)[0].stop_reason == "native_session_or_version_changed"
    def cycle(s):
        return [page([message("opening", s.message, "user")]), page([], "cursor2"), page([], "cursor1")]
    assert run(cycle)[0].stop_reason == "native_cursor_replayed"


def test_new_native_message_without_cursor_advance_is_rejected():
    result, driver, _, _ = run(lambda s: [page([message("opening", s.message, "user")]),
                                          page([message("q", "Who will use it?", order=2)])])
    assert result.stop_reason == "native_cursor_did_not_advance"
    assert not driver.submissions


@pytest.mark.parametrize("reason", ["mixed_native_session_or_version", "native_message_size_budget", "ambiguous_native_session"])
def test_bridge_reports_unverified_without_pty_fallback(reason):
    result, driver, _, _ = run([unbound(reason)])
    assert result.status == "unverified" and result.stop_reason == reason
    assert driver.submissions == []


def test_build_can_run_after_consultation_until_real_external_deployment_observation():
    observations = []
    async def observer(snapshot):
        observations.append(snapshot)
        if snapshot["scope_agreement_delivered"] and snapshot["elapsed_seconds"] >= 4:
            return AppObservation("deployment_observed", "https://new-app.example", ("artifact-ref",), "fresh independent inventory")
        return AppObservation(source="fresh independent inventory")
    async def continue_build(snapshot):
        assert snapshot["scope_agreement_delivered"]
        return "continue"
    def pages(s):
        return [*scope_pages(s), page([message("progress", "I am building the agreed queue.", order=4)], "cursor3"), page([], "cursor3")]
    result, driver, _, _ = run(pages, mode="build", runner_options={"app_observer": observer, "build_continue": continue_build})
    assert result.status == "completed_build_observation" and result.stop_reason == "deployment_observed"
    assert result.elapsed_seconds == 4
    assert driver.submissions == ["Yes, that sounds right."]
    assert result.app_observations[-1]["app_url"] == "https://new-app.example"
    assert result.full_app_acceptance_verified is False and result.accepted is False
    assert "simulator_private" not in str(observations)


def test_build_final_declarative_message_can_be_observed_without_continuation_callback():
    async def observer(snapshot):
        if any(item["text"] == "The app is deployed." for item in snapshot["messages"]):
            return AppObservation("deployment_observed", "https://new-app.example", source="fresh independent inventory")
    def pages(s):
        return [*scope_pages(s), page([message("done", "The app is deployed.", order=4)], "cursor3")]
    result, _, _, _ = run(pages, mode="build", runner_options={"app_observer": observer})
    assert result.status == "completed_build_observation" and not result.accepted


def test_premature_build_is_observed_past_consultation_deadline_without_consent_or_private_facts():
    snapshots = []
    async def observer(snapshot):
        snapshots.append(snapshot)
        if snapshot["elapsed_seconds"] >= 6:
            return AppObservation("deployment_observed", "https://new-app.example", ("app:owned",), "inventory")
        return AppObservation(source="inventory")
    def pages(scenario):
        return [page([message("opening", scenario.message, "user"),
                      message("rush", "I'll start building an order dashboard now.", order=2)]),
                page([], "cursor1"), page([], "cursor1"), page([], "cursor1"),
                page([message("late-question", "Who will use it?", order=3)], "cursor2"),
                page([message("done", "The app is deployed.", order=4)], "cursor3")]
    result, driver, _, simulator = run(pages, mode="build", total=8, consultation=3,
                                     runner_options={"app_observer": observer})
    assert result.elapsed_seconds == 6
    assert result.status == "unverified" and result.stop_reason == "deployment_before_correlated_scope_agreement"
    assert result.consultation_status == "failed"
    assert result.consultation_stop_reason == "no_material_consultation_within_budget"
    assert result.consultation_closed_elapsed_seconds == 3 and result.consultation_deadline_enforced
    assert result.no_question_before_scope_agreement_observed
    assert not result.scope_agreement_delivered and not result.accepted
    assert driver.submissions == [] and simulator.evidence()["reply_count"] == 1
    assert all(not event.fact_ids and not event.agreement for event in simulator.events)
    assert any(item["text"] == "Who will use it?" for item in result.messages)
    assert result.app_observations[-1]["app_url"] == "https://new-app.example"
    assert result.app_observations[-1]["artifacts"] == ["app:owned"]
    assert snapshots[-1]["consultation_status"] == "failed" and driver.closes


def test_later_business_question_is_answered_only_inside_consultation_budget():
    async def observer(snapshot):
        if snapshot["elapsed_seconds"] >= 5:
            return AppObservation("deployment_observed", "https://new-app.example", source="inventory")
        return AppObservation(source="inventory")
    def pages(scenario):
        return [page([message("opening", scenario.message, "user"),
                      message("progress", "Let me look at the available data.", order=2)]),
                page([message("question", "Who will use it?", order=3)], "cursor2"),
                page([message("reply", scenario.answer("users"), "user", order=4)], "cursor3"),
                page([], "cursor3"),
                page([message("too-late", "What should get attention first?", order=5)], "cursor4")]
    result, driver, _, simulator = run(pages, mode="build", total=8, consultation=3,
                                     runner_options={"app_observer": observer})
    assert driver.submissions == ["The people working in the shop."]
    assert result.consultation_status == "failed" and result.consultation_stop_reason == "consultation_scope_not_agreed"
    assert result.consultation_closed_elapsed_seconds == 3 and result.elapsed_seconds == 5
    assert [event.fact_ids for event in simulator.events if event.fact_ids] == [("users",)]
    assert not result.scope_agreement_delivered and result.app_observations[-1]["status"] == "deployment_observed"


def test_deployment_without_scope_before_deadline_records_separate_consultation_failure():
    async def observer(_snapshot):
        return AppObservation("deployment_observed", "https://new-app.example", source="inventory")
    result, driver, _, _ = run(lambda scenario: [page([
        message("opening", scenario.message, "user"), message("rush", "The app is deployed.", order=2),
    ])], mode="build", runner_options={"app_observer": observer})
    assert result.stop_reason == "deployment_before_correlated_scope_agreement"
    assert result.consultation_status == "failed"
    assert result.consultation_stop_reason == "deployment_before_correlated_scope_agreement"
    assert not result.consultation_deadline_enforced and result.elapsed_seconds == 0
    assert not driver.submissions and result.app_observations[-1]["status"] == "deployment_observed"


def test_build_without_correlated_native_opening_stops_at_consultation_budget():
    observed = []
    async def observer(snapshot):
        observed.append(snapshot["elapsed_seconds"])
        return AppObservation(source="inventory")
    def pages(scenario):
        return [page([], "cursor1")]
    result, driver, _, _ = run(pages, mode="build", total=20, consultation=3,
                              runner_options={"app_observer": observer})
    assert result.elapsed_seconds == 3 and result.polls == 3
    assert result.stop_reason == "no_material_consultation_within_budget"
    assert result.consultation_status == "unverified" and result.consultation_deadline_enforced
    assert result.first_complete_assistant is None and not driver.submissions
    assert observed == [0, 1, 2] and driver.closes


def test_correlated_opening_allows_bounded_build_collection_without_inventing_consultation_failure():
    observed = []
    async def observer(snapshot):
        observed.append(snapshot["elapsed_seconds"])
        if snapshot["elapsed_seconds"] >= 4:
            return AppObservation("deployment_observed", "https://new-app.example", source="inventory")
        return AppObservation(source="inventory")
    result, driver, _, _ = run(lambda scenario: [page([message("opening", scenario.message, "user")])],
        mode="build", total=8, consultation=3, runner_options={"app_observer": observer})
    assert result.elapsed_seconds == 4 and observed == [0, 1, 2, 3, 4]
    assert result.stop_reason == "deployment_before_correlated_scope_agreement"
    assert result.consultation_status == "unverified" and result.consultation_deadline_enforced
    assert result.first_complete_assistant is None and not driver.submissions and driver.closes


def test_passive_build_without_deployment_is_still_bounded_by_total_budget():
    async def observer(_snapshot):
        return AppObservation(source="inventory")
    result, driver, _, _ = run(lambda scenario: [page([
        message("opening", scenario.message, "user"), message("rush", "I'll build it now.", order=2),
    ])], mode="build", total=5, consultation=2, runner_options={"app_observer": observer})
    assert result.elapsed_seconds == 5 and result.total_deadline_enforced
    assert result.stop_reason == "total_deadline"
    assert result.consultation_status == "failed" and result.consultation_closed_elapsed_seconds == 2
    assert result.consultation_deadline_enforced and not driver.submissions and driver.closes


def test_late_native_agreement_correlation_cannot_erase_consultation_deadline_failure():
    async def observer(snapshot):
        if snapshot["elapsed_seconds"] >= 4:
            return AppObservation("deployment_observed", "https://new-app.example", source="inventory")
        return AppObservation(source="inventory")
    def pages(scenario):
        first, agreement = scope_pages(scenario)
        return [first, page([], "cursor1"), page([], "cursor1"), agreement]
    result, driver, _, _ = run(pages, mode="build", total=6, consultation=2,
                              runner_options={"app_observer": observer})
    assert driver.submissions == ["Yes, that sounds right."]
    assert result.scope_agreement_delivered
    assert result.consultation_status == "failed" and result.consultation_closed_elapsed_seconds == 2
    assert result.consultation_stop_reason == "consultation_scope_not_agreed"
    assert result.status == "completed_build_observation" and not result.accepted


def test_default_build_done_behavior_is_reviewable_and_does_not_send_a_repair_prompt():
    def pages(s):
        return [*scope_pages(s), page([message("done", "The app is deployed.", order=4)], "cursor3")]
    result, driver, _, _ = run(pages, mode="build")
    assert result.status == "unverified" and result.stop_reason == "builder_done_without_app_acceptance"
    assert driver.submissions == ["Yes, that sounds right."]


def test_deployment_observed_before_scope_agreement_remains_unverified():
    async def observer(_snapshot):
        return AppObservation("deployment_observed", "https://existing-app.example", source="inventory")
    result, _, _, _ = run([unbound()], mode="build", runner_options={"app_observer": observer})
    assert result.status == "unverified" and result.stop_reason == "deployment_before_correlated_scope_agreement"
    assert not result.scope_agreement_delivered and not result.accepted


def test_build_total_budget_is_enforced_and_cleanup_is_still_attempted():
    async def observer(_snapshot):
        return AppObservation(source="inventory")
    def pages(s):
        return [*scope_pages(s), page([], "cursor2")]
    result, driver, _, _ = run(pages, mode="build", total=4, consultation=3, runner_options={"app_observer": observer})
    assert result.stop_reason == "total_deadline" and result.total_deadline_enforced
    assert result.elapsed_seconds == 4 and driver.closes


def test_observer_completing_at_total_deadline_cannot_override_budget_failure():
    clock, sim, scenario, driver, launch, config = setup(mode="build", total=4, consultation=3)
    async def observer(snapshot):
        if snapshot["scope_agreement_delivered"]:
            clock.now = 4
            return AppObservation("deployment_observed", "https://new-app.example", source="inventory")
    result = asyncio.run(run_journey(driver, launch, sim, Fetch(scope_pages(scenario)), config,
                                    app_observer=observer, clock=clock, sleep=clock.sleep))
    assert result.status == "failed" and result.stop_reason == "total_deadline"
    assert result.total_deadline_enforced and driver.closes
    assert not result.app_observations


def test_build_continuation_callback_cannot_inject_a_technical_rescue():
    async def continue_build(_snapshot):
        return "Run this SQL to fix the app"
    result, driver, _, _ = run(lambda s: [*scope_pages(s), page([message("done", "The app is deployed.", order=4)], "cursor3")],
                              mode="build", runner_options={"build_continue": continue_build})
    assert result.stop_reason == "unsupported_build_continuation"
    assert driver.submissions == ["Yes, that sounds right."]


def test_consultation_budget_includes_time_spent_entering_normal_ui():
    result, driver, _, _ = run([unbound()], consultation=3, entry_seconds=3)
    assert result.stop_reason == "no_material_consultation_within_budget"
    assert result.polls == 0 and result.elapsed_seconds == 3 and driver.closes
    assert result.budget_start == "before_normal_ui_entry"


def test_entry_failure_closes_only_the_created_ui_session_without_polling():
    result, driver, fetch, _ = run([unbound()], error_code="starter_prompt_truncated")
    assert result.stop_reason == "starter_prompt_truncated" and result.status == "failed"
    assert driver.closes and not fetch.paths


def test_entry_timeout_cannot_be_classified_as_failed_agent_consultation():
    async def run_timeout():
        clock, sim, _, driver, launch, config = setup()
        async def blocked_entry(_config):
            driver.evidence.identity = launch.expected_attendee_email
            clock.now = config.consultation_seconds
            raise asyncio.TimeoutError()
        driver.enter = blocked_entry
        fetch = Fetch([])
        result = await run_journey(driver, launch, sim, fetch, config, clock=clock, sleep=clock.sleep)
        assert result.stop_reason == "browser_entry_timeout"
        assert result.consultation_status == "unverified"
        assert result.consultation_stop_reason == "browser_entry_timeout"
        assert not result.entry["opening_submitted"] and not fetch.paths and not driver.closes
    asyncio.run(run_timeout())


def test_existing_driver_session_is_never_entered_or_closed():
    clock, sim, _, driver, launch, config = setup()
    driver.evidence.session_id = OTHER_SESSION
    result = asyncio.run(run_journey(driver, launch, sim, Fetch([unbound()]), config, clock=clock, sleep=clock.sleep))
    assert result.stop_reason == "fresh_browser_driver_required"
    assert driver.entered == [] and driver.closes == []
    assert result.cleanup["status"] == "not_needed"


def test_opening_mismatch_never_closes_or_launches_a_session():
    clock, sim, _, driver, launch, config = setup()
    result = asyncio.run(run_journey(driver, replace(launch, opening_message="Injected full blueprint"), sim,
                                    Fetch([unbound()]), config, clock=clock, sleep=clock.sleep))
    assert result.stop_reason == "opening_differs_from_simulator"
    assert not driver.entered and not driver.closes


def test_cleanup_failure_is_reported_without_disclosing_exception_details():
    result, driver, _, _ = run(scope_pages, close_error=True)
    assert result.cleanup["status"] == "failed" and result.cleanup["error_type"] == "RuntimeError"
    assert "private-error-token" not in str(result.to_dict())
    assert driver.closes


def test_callback_error_is_redacted_and_session_cleanup_runs():
    clock, sim, _, driver, launch, config = setup()
    async def bad_fetch(_path):
        raise ValueError("Bearer private-operator-token")
    result = asyncio.run(run_journey(driver, launch, sim, bad_fetch, config, clock=clock, sleep=clock.sleep))
    assert result.stop_reason == "journey_operation_ValueError"
    assert "private-operator-token" not in str(result.to_dict()) and driver.closes


def test_actual_async_operation_timeout_stops_and_closes():
    clock, sim, _, driver, launch, config = setup()
    config = replace(config, operation_timeout_seconds=.01)
    async def blocked_fetch(_path):
        await asyncio.sleep(60)
    result = asyncio.run(run_journey(driver, launch, sim, blocked_fetch, config, clock=clock, sleep=clock.sleep))
    assert result.stop_reason == "operation_timeout" and result.status == "failed"
    assert driver.closes


def test_poll_and_evidence_budgets_are_enforced():
    clock, sim, scenario, driver, launch, config = setup()
    config = replace(config, max_polls=1)
    result = asyncio.run(run_journey(driver, launch, sim, Fetch([unbound()]), config, clock=clock, sleep=clock.sleep))
    assert result.stop_reason == "poll_budget"
    clock, sim, scenario, driver, launch, config = setup()
    config = replace(config, max_messages=1)
    result = asyncio.run(run_journey(driver, launch, sim, Fetch(scope_pages(scenario)), config, clock=clock, sleep=clock.sleep))
    assert result.stop_reason == "native_evidence_budget" and not driver.submissions


def test_codex_pin_uses_same_role_separated_delivery_contract():
    clock, sim, scenario, driver, launch, config = setup()
    launch = replace(launch, agent_id="codex")
    result = asyncio.run(run_journey(driver, launch, sim, Fetch(scope_pages(scenario, harness="codex")), config,
                                    clock=clock, sleep=clock.sleep))
    assert result.status == "completed_probe" and result.harness_version == "0.148.0"
    assert driver.closes[0]["label"] == "Codex CLI"


def test_runner_cannot_be_reused_and_probe_cannot_receive_build_callbacks():
    clock, sim, scenario, driver, launch, config = setup()
    runner = NativeConsultationRunner(driver, sim, Fetch(scope_pages(scenario)), config, clock=clock, sleep=clock.sleep)
    asyncio.run(runner.run(launch))
    with pytest.raises(RuntimeError, match="only once"):
        asyncio.run(runner.run(launch))
    with pytest.raises(ValueError, match="build mode"):
        NativeConsultationRunner(driver, sim, Fetch([]), config, app_observer=lambda _: None)


@pytest.mark.parametrize("invalid", [{"consultation_seconds": 181}, {"total_seconds": 1, "consultation_seconds": 2},
                                     {"operation_timeout_seconds": 61}, {"cleanup_seconds": float("inf")},
                                     {"max_polls": True}, {"max_messages": 0}, {"mode": "repair"}])
def test_invalid_budgets_are_rejected(invalid):
    with pytest.raises(ValueError):
        JourneyConfig(**invalid)


@pytest.mark.parametrize("invalid", [
    {"status": "deployment_observed", "app_url": "https://app.example"},
    {"app_url": "https://user:secret@app.example"},
    {"app_url": "https://app.example?token=secret"},
    {"artifacts": ["wrong shape"]},
])
def test_app_observation_requires_bounded_credential_free_external_references(invalid):
    with pytest.raises(ValueError):
        AppObservation(**invalid)
