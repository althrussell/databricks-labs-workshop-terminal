"""W6: paired journeys, honest pilot reporting, and the data collection fixes from test1025."""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from evals.generated_apps import paired, pilot
from server import artifacts, journey, stats


# ---------------------------------------------------------------------------
# Paired journeys (CI smoke) and the live pilot's reporting
# ---------------------------------------------------------------------------

def test_every_brief_holds_its_guarantees_on_every_harness_and_path():
    results = paired.run_smoke()
    assert len(results) == 6 * 3 * 2
    failed = [(r["scenario"], r["harness"], r["path"], [k for k, ok in r["checks"].items() if not ok])
              for r in results if not r["passed"]]
    assert failed == []
    assert {r["provider"] for r in results if r["harness"] == "omnigent"} == {"claude"}


def test_the_briefs_cover_the_review_matrix():
    kinds = {s["scenario_id"] for s in paired.scenarios()}
    assert kinds == {"vague-business-problem", "precise-build-request", "consequential-business-rule",
                     "existing-branded-app", "multi-step-workflow", "follow-up-and-resume"}
    assert {s["entry"] for s in paired.scenarios()} == {"wizard", "chat"}
    assert all(s["evaluated_path"] == "guided" for s in paired.scenarios() if s["scenario_id"] == "vague-business-problem")


def test_the_pilot_counts_incomplete_runs_and_ignores_unblinded_visual_scores():
    lines = [
        {"scenario": "precise-build-request", "harness": "codex", "path": "rapid", "run": 1, "complete": True,
         "consistency": {"a": True, "b": True}, "product": {"c": True, "d": False},
         "visual": {"reviewer_blind": True, "hierarchy": 4, "density": 4, "coherence": 4, "responsive": 4, "accessibility": 4}},
        {"scenario": "precise-build-request", "harness": "codex", "path": "rapid", "run": 2, "complete": False},
        {"scenario": "precise-build-request", "harness": "codex", "path": "rapid", "run": 3, "complete": True,
         "consistency": {"a": True}, "visual": {"reviewer_blind": False, "hierarchy": 5}},
    ]
    [group] = pilot.report(lines, expected=4)["groups"]
    assert group["runs"] == 3 and group["incomplete"] == 1
    assert group["functional_pass_rate"]["product"] == 0.5 and group["visual_mean"] == 4.0
    assert group["unblinded_visual_ignored"] == 1
    assert pilot.report(lines, expected=4)["missing"] == 1
    assert len(pilot.plan(2)) == 6 * 3 * 2 * 2


# ---------------------------------------------------------------------------
# Capture (Codex prompts, Omnigent via Claude), activity counts
# ---------------------------------------------------------------------------

def _write(path: Path, records: list[dict], *, mtime: float | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(record) for record in records) + "\n")
    if mtime is not None:
        os.utime(path, (mtime, mtime))


def _codex_turn(text: str, *, context: bool = False) -> list[dict]:
    body = (f"<environment_context>\n  <cwd>/home/a/projects/planner</cwd>\n</environment_context>\n{text}"
            if context else text)
    return [{"type": "response_item", "payload": {"type": "message", "role": "user",
                                                 "content": [{"type": "input_text", "text": body}]}},
            {"type": "event_msg", "payload": {"type": "user_message", "message": text}}]


def test_codex_prompts_are_counted_once_even_when_context_shares_their_text(tmp_path):
    rollout = tmp_path / ".codex/sessions/2026/10/25/rollout-1.jsonl"
    records = []
    for n, text in enumerate(["Build the planner", "Show solar uptake by postcode", "Which feeders are full?",
                              "Estimate the next six months", "Let engineers flag a feeder"]):
        records += _codex_turn(text, context=n >= 3)
    _write(rollout, records)
    harvest = artifacts.Harvest()
    artifacts._scan_transcripts(str(tmp_path), harvest)
    assert harvest.prompt_count == 5
    assert harvest.prompts[-1] == "Let engineers flag a feeder"


def test_a_codex_prompt_seen_only_as_its_user_message_event_is_captured(tmp_path):
    _write(tmp_path / ".codex/sessions/2026/10/25/rollout-2.jsonl",
           [{"type": "event_msg", "payload": {"type": "user_message", "message": "Package my code to take home"}}])
    harvest = artifacts.Harvest()
    artifacts._scan_transcripts(str(tmp_path), harvest)
    assert harvest.prompts == ["Package my code to take home"]


def test_each_harness_keeps_its_newest_session_when_another_writes_many_files(tmp_path, monkeypatch):
    monkeypatch.setattr(artifacts, "MAX_TRANSCRIPT_FILES", 5)
    now = time.time()
    _write(tmp_path / ".codex/sessions/rollout-main.jsonl", _codex_turn("Record our decisions"), mtime=now - 100)
    for n in range(10):
        _write(tmp_path / f".claude/projects/p/side-{n}.jsonl",
               [{"type": "user", "message": {"role": "user", "content": f"side task {n}"}}], mtime=now - n)
    harvest = artifacts.Harvest()
    artifacts._scan_transcripts(str(tmp_path), harvest)
    assert "Record our decisions" in harvest.prompts


def test_omnigent_sessions_are_captured_through_claudes_transcripts(tmp_path):
    _write(tmp_path / ".omnigent/sessions/claude/session.jsonl",
           [{"type": "user", "message": {"role": "user", "content": [{"type": "text", "text": "Log an outage"}]}}])
    harvest = artifacts.Harvest()
    artifacts._scan_transcripts(str(tmp_path), harvest)
    assert harvest.prompts == ["Log an outage"]


def test_activity_counts_tools_and_classifies_errors_without_content(tmp_path):
    _write(tmp_path / ".claude/projects/p/s.jsonl", [
        {"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "tool_use", "name": "Bash", "input": {"command": "databricks apps deploy"}},
            {"type": "tool_use", "name": "mcp__databricks__query", "input": {"query": "select 1"}}]}},
        {"type": "user", "message": {"role": "user", "content": [
            {"type": "tool_result", "is_error": True, "content": "Error: HTTP 403 Forbidden for secret token=abc"},
            {"type": "tool_result", "is_error": True, "content": "Command failed with exit code 1"}]}},
    ])
    activity = artifacts.activity(str(tmp_path))
    assert activity["tool_calls"] == {"Bash": 1, "mcp:databricks": 1}
    assert activity["tool_errors"] == {"command_failed": 1, "permission_denied": 1}
    assert activity["errors"] == 2
    assert "abc" not in json.dumps(activity)


# ---------------------------------------------------------------------------
# Stats: errors, minutes per seat, lines of code
# ---------------------------------------------------------------------------

def test_minutes_building_is_each_seats_own_activity_span():
    from server.users import User

    early, late = User("a@example.com"), User("b@example.com")
    early.first_seen, early.last_seen = 1000.0, 1000.0 + 30 * 60
    late.first_seen, late.last_seen = 1000.0, 1000.0 + 75 * 60
    spans = [int(max(0.0, u.last_seen - u.first_seen) / 60) for u in (early, late)]
    assert spans == [30, 75]
    source = Path(stats.__file__).read_text()
    assert "(user.last_seen or user.first_seen) - user.first_seen" in source


def test_errors_include_the_agents_own_failed_tool_results(tmp_path, monkeypatch):
    from server.users import User

    user = User("errors@example.com")
    user.errors = 1
    monkeypatch.setattr(stats, "agent_activity", lambda u, fresh=True: {
        "prompts": 3, "tool_calls": {"Bash": 4}, "tool_errors": {"timeout": 2}, "errors": 2})
    monkeypatch.setattr(stats, "_code_observation", lambda u, fresh, saved: ({"projects": 0, "commits": 0, "files": 0, "lines": 0}, {}))
    monkeypatch.setattr(stats, "_discovery_count", lambda email: 0)
    row = stats.gather_user(user)
    assert row["errors"] == 3
    assert row["agent_activity"] == {"prompts": 3, "tool_calls": {"Bash": 4}, "tool_errors": {"timeout": 2}}


@pytest.mark.parametrize("path, counted", [
    ("src/app.py", True), ("ui/src/routeTree.gen.ts", False), ("ui/dist/app.min.js", False),
    ("ui/src/main.tsx.map", False), (".next/server/page.js", False), ("coverage/lcov.js", False),
    (".agents/skills/apx/SKILL.py", False), ("bun.lock", False),
])
def test_lines_of_code_count_only_what_the_attendee_wrote(path, counted):
    assert stats.counted(path) is counted


# ---------------------------------------------------------------------------
# Events: phase durations, the attendee's outcome, agent activity
# ---------------------------------------------------------------------------

def test_phase_changes_emit_coarse_durations_and_activity_is_emitted_on_change(monkeypatch):
    from server import telemetry

    sent: list[tuple[str, dict]] = []
    monkeypatch.setattr(telemetry, "emit", lambda kind, attendee, payload=None: sent.append((kind, payload or {})) or True)
    journey.reset_for_tests()
    journey.emit_if_changed("a@example.com", {"phase": "shape", "path": "guided", "status": "needs_decision"})
    journey.emit_if_changed("a@example.com", {"phase": "build", "path": "guided", "status": "working"})
    kinds = [kind for kind, _ in sent]
    assert kinds == ["journey.status", "journey.phase", "journey.status"]
    assert sent[1][1]["from"] == "shape" and sent[1][1]["to"] == "build" and sent[1][1]["seconds"] >= 0

    sent.clear()
    stats._activity_emitted.clear()
    payload = {"users": [{"email": "a@example.com", "errors": 1,
                          "agent_activity": {"prompts": 2, "tool_calls": {}, "tool_errors": {}}}]}
    assert stats.emit_activity(payload) == 1 and stats.emit_activity(payload) == 0
    assert sent == [("agent.activity", {"prompts": 2, "tool_calls": {}, "tool_errors": {}, "errors": 1})]


def test_new_event_types_are_accepted_by_closeout():
    from server.closeout import SUPPORTED_EVENTS

    assert {"agent.activity", "journey.phase", "journey.outcome"} <= SUPPORTED_EVENTS
