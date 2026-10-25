"""Only the attendee's own words count as attendee prompts.

The 25 Oct live run quoted, as attendees' use cases, a Claude Code skill body
("Base directory for this skill: …"), Codex's AGENTS.md injection, and the task
prompts Omnigent's polly typed into its Claude Code and Codex workers ("/goal
IMPLEMENT and DEPLOY …", "REVIEW ONLY — …"). Each fixture below has the shape
the harness actually writes (Claude Code 2.1.31x, Codex 0.16x, Omnigent 0.19).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from server import artifacts, insight_summary

ATTENDEE = "Using the energy demo data, build me an app that ranks today's outages by customers off supply."
SKILL = ("Base directory for this skill: /app/python/source_code/data/users/labuser-4-403e8c47/.claude/skills/apx\n\n"
         "# apx Toolkit\n\napx bundles together a set of tools …")
POLLY_GOAL = "/goal IMPLEMENT and DEPLOY a life support outage call-list app. Done when ALL of the following are true …"
POLLY_SCOPE = ("The user sent a new message while you were working:\nSCOPE ADDITION from the attendee (add to the "
               "current goal; finish the call list first): let the team mark a customer called.")
AGENTS_MD = ("# AGENTS.md instructions for /app/python/source_code/data/users/labuser-5-f60b6d43/projects/der-connections"
             "\n\n<INSTRUCTIONS>\n# Codex Agent Instructions\n<!-- workshop-terminal-safety:v1 -->\n**Keep this terminal "
             "alive.**\n</INSTRUCTIONS>")
REVIEW = "REVIEW ONLY — do not edit any files. Read the saved diff at /tmp/polly-review/der-dashboard.diff …"


@pytest.fixture(autouse=True)
def no_real_omnigent_ledger(monkeypatch, tmp_path):
    bridges = tmp_path / "tmp"
    bridges.mkdir()
    monkeypatch.setattr(artifacts, "_omnigent_bridge_roots", lambda: {str(bridges)})
    return bridges


def _write(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(record) for record in records) + "\n")


def _claude(text, *, session="s1", entrypoint="cli", origin="human", source="typed", **flags) -> dict:
    entry = {"parentUuid": None, "isSidechain": False, "type": "user", "userType": "external",
             "entrypoint": entrypoint, "cwd": "/home/a/projects/outages", "sessionId": session,
             "version": "2.1.310", "message": {"role": "user", "content": text}, **flags}
    if origin is not None:
        entry["origin"] = {"kind": origin}
        entry["promptSource"] = source
    return entry


def _codex_meta(source="cli", originator="codex_cli_rs") -> dict:
    return {"type": "session_meta", "payload": {"id": "019e", "cwd": "/home/a/projects/der", "originator": originator,
                                                "cli_version": "0.164.0", "source": source}}


def _codex_injected(text: str) -> dict:
    return {"type": "response_item", "payload": {"type": "message", "role": "user",
                                                 "content": [{"type": "input_text", "text": text}]}}


def _codex_typed(text: str) -> list[dict]:
    return [_codex_injected(text), {"type": "event_msg", "payload": {"type": "user_message", "message": text}}]


def _harvest(home: Path) -> artifacts.Harvest:
    harvest = artifacts.Harvest()
    artifacts._scan_transcripts(str(home), harvest)
    return harvest


def test_a_skill_body_claude_code_loads_is_not_a_prompt(tmp_path):
    _write(tmp_path / ".claude/projects/p/s1.jsonl", [
        _claude(ATTENDEE),
        _claude([{"type": "text", "text": SKILL}], origin=None, isMeta=True, sourceToolUseID="toolu_1"),
    ])
    harvest = _harvest(tmp_path)
    assert harvest.prompts == [ATTENDEE] and harvest.prompt_count == 1


def test_a_skill_body_is_dropped_by_its_wording_when_the_flag_is_missing(tmp_path):
    _write(tmp_path / ".claude/projects/p/s1.jsonl", [_claude(SKILL, origin=None), _claude(ATTENDEE)])
    assert _harvest(tmp_path).prompts == [ATTENDEE]


def test_notifications_and_other_sessions_messages_are_not_the_attendee(tmp_path):
    _write(tmp_path / ".claude/projects/p/s1.jsonl", [
        _claude("<task-notification><task-id>b1</task-id></task-notification>", origin="task-notification",
                source="system"),
        _claude("Review finished: two findings.", origin="peer"),
        _claude("Please continue: finish the deploy.", origin="coordinator", isMeta=True),
        _claude(ATTENDEE),
    ])
    harvest = _harvest(tmp_path)
    assert harvest.prompts == [ATTENDEE] and harvest.prompt_count == 1


def test_a_sub_agents_task_is_its_parents_words(tmp_path):
    _write(tmp_path / ".claude/projects/p/s1.jsonl", [_claude(ATTENDEE)])
    _write(tmp_path / ".claude/projects/p/s1/subagents/agent-a04f.jsonl",
           [_claude("READ-ONLY. Open these two PNG screenshots with your image Read tool …", isSidechain=True)])
    _write(tmp_path / ".claude/projects/p/s2.jsonl",
           [_claude("Explore the outage tables and report their columns.", isSidechain=True)])
    assert _harvest(tmp_path).prompts == [ATTENDEE]


def test_a_message_sent_while_the_agent_worked_keeps_the_attendees_words(tmp_path):
    _write(tmp_path / ".claude/projects/p/s1.jsonl", [
        _claude(ATTENDEE),
        _claude("The user sent a new message while you were working:\nAlso show how long each outage has run.",
                source="queued"),
    ])
    assert _harvest(tmp_path).prompts == [ATTENDEE, "Also show how long each outage has run."]


def test_headless_claude_runs_are_not_the_attendee_but_omnigents_polly_is(tmp_path):
    _write(tmp_path / ".claude/projects/p/headless.jsonl",
           [_claude("Summarise this diff for a reviewer.", entrypoint="sdk-cli", origin=None)])
    # polly, Omnigent's orchestrator, runs on the Agent SDK; its turns are what the attendee typed to Omnigent.
    _write(tmp_path / ".claude/projects/p/polly.jsonl",
           [_claude(ATTENDEE, entrypoint="sdk-py", origin=None, source="sdk")])
    harvest = _harvest(tmp_path)
    assert harvest.prompts == [ATTENDEE] and harvest.prompt_count == 1


def test_omnigents_claude_code_workers_are_known_from_its_own_ledger(tmp_path, no_real_omnigent_ledger):
    worker = "4b6e1c2a-7f1d-4f43-9a0e-2f8d0c1b9e77"
    moved = "8d2f7b10-3c4e-4c1a-8f5e-0a9b6c7d8e9f"
    _write(tmp_path / ".claude/projects/-home-a-projects-outages/polly.jsonl",
           [_claude(ATTENDEE, entrypoint="sdk-py", origin=None, source="sdk")])
    # polly types the task into the worker's terminal, so the worker's file says "typed by a human".
    _write(tmp_path / f".claude/projects/-home-a-projects-outages-worktrees-lso/{worker}.jsonl",
           [_claude(POLLY_GOAL, session=worker), _claude(POLLY_SCOPE, session=worker, source="queued")])
    _write(tmp_path / f".claude/projects/-home-a-projects-outages-worktrees-verify/{moved}.jsonl",
           [_claude("Second cross-vendor review found 3 BLOCKING issues. Fix them in the same worktree.",
                    session=moved)])
    bridge = no_real_omnigent_ledger / "omnigent-1000" / "claude-native" / "b1"
    bridge.mkdir(parents=True)
    (bridge / "hooks.jsonl").write_text(json.dumps({"recorded_at": 1.0, "payload": {
        "hook_event_name": "SessionStart", "session_id": worker,
        "transcript_path": str(tmp_path / f".claude/projects/-home-a-projects-outages/{worker}.jsonl")}}) + "\n")
    other = no_real_omnigent_ledger / "omnigent-1000" / "claude-native" / "b2"
    other.mkdir(parents=True)
    (other / "state.json").write_text(json.dumps({"claude_session_id": moved, "last_hook_event_name": "Stop"}))

    harvest = _harvest(tmp_path)

    assert harvest.prompts == [ATTENDEE] and harvest.prompt_count == 1


def test_codex_injections_are_dropped_and_typed_prompts_kept(tmp_path):
    _write(tmp_path / ".codex/sessions/2026/10/25/rollout-1.jsonl", [
        _codex_meta(),
        _codex_injected(AGENTS_MD),
        _codex_injected("<environment_context>\n  <cwd>/home/a/projects/der</cwd>\n</environment_context>"),
        *_codex_typed(ATTENDEE),
    ])
    harvest = _harvest(tmp_path)
    assert harvest.prompts == [ATTENDEE] and harvest.prompt_count == 1


def test_an_older_codex_without_typed_events_still_drops_agents_md(tmp_path):
    _write(tmp_path / ".codex/sessions/2026/10/25/rollout-1.jsonl",
           [_codex_meta(), _codex_injected(AGENTS_MD), _codex_injected(ATTENDEE)])
    assert _harvest(tmp_path).prompts == [ATTENDEE]


def test_codex_sessions_another_program_started_are_not_the_attendee(tmp_path):
    root = tmp_path / ".omnigent/codex-native/dddf7b16/codex-home/sessions/2026/10/25"
    _write(root / "rollout-worker.jsonl", [
        _codex_meta(source="vscode", originator="omnigent"),
        *_codex_typed(REVIEW),
        {"type": "response_item", "payload": {"type": "function_call", "name": "exec_command",
                                              "arguments": json.dumps({"cmd": "git diff"})}},
    ])
    _write(tmp_path / ".codex/sessions/2026/10/25/rollout-sub.jsonl", [
        _codex_meta(source={"subagent": {"thread_spawn": {"depth": 1, "agent_path": "/root/review"}}}),
        *_codex_typed("Audit the flags table for missing indexes."),
    ])
    _write(tmp_path / ".codex/sessions/2026/10/25/rollout-exec.jsonl",
           [_codex_meta(source="exec"), *_codex_typed("Write release notes.")])
    _write(tmp_path / ".codex/sessions/2026/10/25/rollout-1.jsonl", [_codex_meta(), *_codex_typed(ATTENDEE)])

    harvest = _harvest(tmp_path)

    assert harvest.prompts == [ATTENDEE] and harvest.prompt_count == 1
    # The workers' work still counts toward the seat's activity.
    assert harvest.tool_calls.get("exec_command") == 1


def test_codex_plugin_fixtures_are_not_session_logs(tmp_path):
    _write(tmp_path / ".codex/.tmp/plugins/plugins/plugin-eval/fixtures/observed-usage/responses.jsonl",
           [{"type": "user", "message": {"role": "user", "content": "fixture prompt"}}])
    assert _harvest(tmp_path).prompts == []


def test_a_bare_exit_code_is_counted_but_is_not_a_blocker(tmp_path):
    def failed(text: str) -> dict:
        return {"type": "user", "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t", "is_error": True, "content": text}]}}

    _write(tmp_path / ".claude/projects/p/s1.jsonl", [
        failed("Exit code 1\n============ test session starts ============"),
        failed("Exit code 2"),
        failed("Error: permission denied for table outages"),
    ])
    harvest = _harvest(tmp_path)
    assert harvest.errors == ["Error: permission denied for table outages"]
    assert harvest.error_count == 3 and harvest.tool_errors["command_failed"] == 2


def test_the_extraction_summary_quotes_only_the_attendee(tmp_path):
    _write(tmp_path / ".claude/projects/p/s1.jsonl", [
        _claude(ATTENDEE),
        _claude([{"type": "text", "text": SKILL}], origin=None, isMeta=True),
    ])
    _write(tmp_path / ".codex/sessions/2026/10/25/rollout-1.jsonl",
           [_codex_meta(), _codex_injected(AGENTS_MD), *_codex_typed("Let engineers flag a feeder for upgrade.")])
    summary = insight_summary._extraction(_harvest(tmp_path), None)
    titles = [case["title"] for case in summary["use_cases"]]
    assert titles == [ATTENDEE, "Let engineers flag a feeder for upgrade."]
    assert summary["blockers"] == []
