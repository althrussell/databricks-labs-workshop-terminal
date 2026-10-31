"""The 31 Oct sweep's runtime changes (sweep contract §2, decisions 3 and 6).

- Codex carries the contract's reasoning lines verbatim.
- A new build's Claude session starts in plan mode; an approved build keeps the
  full-permission launch. The PostToolUse hook records the approval.
- Every build folder is pre-trusted for Claude (R-004).
- ``workshop-workflow plan`` and the gate that refuses scaffolding before the
  attendee's yes (§2), including the `phase preview` alias (N-B12).
- The Omni host gets span export under WT's resource attributes, and agent
  runners never do (R-007).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import tomllib

import pytest

from server import agents, build_plan, cli_config, config, projects
from tests.plan_approval import approve_build

ROOT = Path(__file__).resolve().parents[1]
INIT_PROJECT = ROOT / "assets/bin/workshop-init-project"
WORKFLOW = ROOT / "assets/bin/workshop-workflow"


@pytest.fixture
def user(monkeypatch, tmp_path):
    from server.users import User

    monkeypatch.setattr(config, "users_root", lambda: str(tmp_path / "users"))
    monkeypatch.setattr(cli_config, "gateway_host", lambda: "")
    monkeypatch.setattr(cli_config, "discover_model_services", lambda token: {})
    attendee = User("alice@example.com")
    attendee.bootstrap_home()
    return attendee


def _claude(user) -> dict:
    with open(os.path.join(user.home, ".claude", "settings.json")) as handle:
        return json.load(handle)


def _claude_json(user) -> dict:
    with open(os.path.join(user.home, ".claude.json")) as handle:
        return json.load(handle)


def _plan_groups(settings: dict) -> list:
    return [group for group in settings.get("hooks", {}).get("PostToolUse", [])
            if any(h.get("command") == build_plan.PLAN_APPROVAL_COMMAND for h in group.get("hooks", []))]


# -- Codex reasoning (decision 3) --------------------------------------------------

CONTRACT_LINES = (
    'model_reasoning_summary = "auto"\n'
    "hide_agent_reasoning = false\n"
    "show_raw_agent_reasoning = false\n"
    "model_supports_reasoning_summaries = true\n"
)


def test_codex_carries_the_contract_reasoning_lines_verbatim(user, monkeypatch):
    monkeypatch.setenv("CODEX_REASONING_EFFORT", "high")
    cli_config.configure_codex(user, "tok-1", available=set())
    text = open(os.path.join(user.home, ".codex", "config.toml")).read()
    assert CONTRACT_LINES in text
    parsed = tomllib.loads(text)
    assert parsed["model_reasoning_effort"] == "high"
    assert parsed["model_reasoning_summary"] == "auto"
    assert parsed["hide_agent_reasoning"] is False
    assert parsed["show_raw_agent_reasoning"] is False
    assert parsed["model_supports_reasoning_summaries"] is True


def test_codex_reasoning_summary_lines_stay_top_level(user, monkeypatch):
    """A line after a [table] header would belong to that table, not to Codex."""
    monkeypatch.setenv("CODEX_REASONING_EFFORT", "xhigh")
    cli_config.configure_codex(user, "tok-1", available=set())
    parsed = tomllib.loads(open(os.path.join(user.home, ".codex", "config.toml")).read())
    assert parsed["model_reasoning_effort"] == "xhigh"
    assert parsed["hide_agent_reasoning"] is False
    assert "model_supports_reasoning_summaries" not in parsed.get("features", {})


# -- Claude settings: plan approval hook, allow rules, MCP timeout ----------------

def test_claude_user_settings_carry_no_base_mode_even_from_an_earlier_release(user, monkeypatch):
    """F-003 (31 Oct): a base mode of bypass in user settings is what Claude announced
    on every plan launch. An earlier release wrote it, so it must be removed."""
    monkeypatch.setenv("WORKSHOP_AUTO_MODE", "true")
    os.makedirs(os.path.join(user.home, ".claude"), exist_ok=True)
    with open(os.path.join(user.home, ".claude", "settings.json"), "w") as handle:
        json.dump({"permissions": {"defaultMode": "bypassPermissions"}}, handle)
    cli_config.configure_claude(user, "tok-1", available=set())
    settings = _claude(user)
    assert "defaultMode" not in settings["permissions"]
    assert settings["skipDangerousModePermissionPrompt"] is True


def test_claude_settings_record_plan_approval_on_exit_plan_mode(user, monkeypatch):
    monkeypatch.setenv("WORKSHOP_AUTO_MODE", "true")
    cli_config.configure_claude(user, "tok-1", available=set())
    groups = _plan_groups(_claude(user))
    assert groups == [{
        "matcher": "ExitPlanMode",
        "hooks": [{"type": "command", "command": "workshop-workflow plan --approved --source claude-plan-mode"}],
    }]


def test_claude_plan_hook_is_not_duplicated_and_keeps_other_hooks(user, monkeypatch):
    cli_config.configure_claude(user, "tok-1", available=set())
    settings = _claude(user)
    settings["hooks"]["PostToolUse"].insert(0, {"matcher": "Edit", "hooks": [
        {"type": "command", "command": "/home/attendee/.local/bin/their-lint"}]})
    with open(os.path.join(user.home, ".claude", "settings.json"), "w") as handle:
        json.dump(settings, handle)
    cli_config.configure_claude(user, "tok-1", available=set())
    settings = _claude(user)
    assert len(_plan_groups(settings)) == 1
    assert {"matcher": "Edit", "hooks": [{"type": "command", "command": "/home/attendee/.local/bin/their-lint"}]} \
        in settings["hooks"]["PostToolUse"]


def test_claude_allows_the_planning_helpers_but_never_the_approval(user, monkeypatch):
    cli_config.configure_claude(user, "tok-1", available=set())
    cli_config.configure_claude(user, "tok-1", available=set())
    allowed = _claude(user)["permissions"]["allow"]
    for rule in ("Bash(workshop-workflow show *)", "Bash(workshop-workflow prefs *)",
                 "Bash(workshop-workflow phase *)", "Bash(workshop-workflow plan --put *)",
                 "Bash(workshop-workflow plan --check *)", "Bash(workshop-discovery *)"):
        assert allowed.count(rule) == 1, rule
    assert not [rule for rule in allowed if "plan --approved" in rule or rule == "Bash(workshop-workflow *)"]
    assert "mcp__workshop__deploy_databricks_app" in allowed


def test_a_broad_rule_from_an_earlier_release_is_removed(user):
    cli_config.configure_claude(user, "tok-1", available=set())
    settings = _claude(user)
    settings["permissions"]["allow"].append("Bash(workshop-workflow *)")
    with open(os.path.join(user.home, ".claude", "settings.json"), "w") as handle:
        json.dump(settings, handle)
    cli_config.configure_claude(user, "tok-1", available=set())
    assert "Bash(workshop-workflow *)" not in _claude(user)["permissions"]["allow"]


def test_claude_mcp_tool_timeout_covers_a_deploy(user):
    cli_config.configure_claude(user, "tok-1", available=set())
    assert _claude(user)["env"]["MCP_TOOL_TIMEOUT"] == "3600000"


# -- Claude folder trust (R-004) ---------------------------------------------------

def test_every_build_folder_is_pre_trusted_for_claude(user):
    (Path(user.home) / "projects" / "codex-made").mkdir(parents=True)
    cli_config.configure_claude(user, "tok-1", available=set())
    projects_map = _claude_json(user)["projects"]
    root = os.path.realpath(os.path.join(user.home, "projects"))
    build = os.path.realpath(os.path.join(user.home, "projects", "codex-made"))
    assert projects_map[root]["hasTrustDialogAccepted"] is True
    assert projects_map[build]["hasTrustDialogAccepted"] is True
    assert _claude_json(user)["hasCompletedOnboarding"] is True


def test_trusting_keeps_the_attendees_existing_project_settings(user):
    path = os.path.join(user.home, ".claude.json")
    with open(path, "w") as handle:
        json.dump({"projects": {"/somewhere": {"allowedTools": ["Bash"]}}, "numStartups": 4}, handle)
    (Path(user.home) / "projects" / "build-a").mkdir(parents=True)
    cli_config.configure_claude(user, "tok-1", available=set())
    data = _claude_json(user)
    assert data["numStartups"] == 4
    assert data["projects"]["/somewhere"] == {"allowedTools": ["Bash"]}


def test_a_new_build_is_trusted_before_claude_opens_in_it(user):
    cli_config.configure_claude(user, "tok-1", available=set())
    build = Path(user.home) / "projects" / "made-later"
    build.mkdir(parents=True)
    cli_config.ensure_claude_trusts(user, str(build))
    entry = _claude_json(user)["projects"][os.path.realpath(build)]
    assert entry["hasTrustDialogAccepted"] is True


def test_ensure_claude_trusts_does_nothing_before_claude_is_configured(user):
    build = Path(user.home) / "projects" / "early"
    build.mkdir(parents=True)
    cli_config.ensure_claude_trusts(user, str(build))
    assert not os.path.exists(os.path.join(user.home, ".claude.json"))


# -- Launch decision (§2) ----------------------------------------------------------

def test_a_plan_mode_launch_can_move_to_full_permission_on_approval():
    assert agents.claude_plan_flags(True) == ("--permission-mode", "plan", "--allow-dangerously-skip-permissions")
    assert agents.claude_plan_flags(False) == ("--permission-mode", "plan")


def test_approved_and_resumed_claude_launches_pass_full_auto_explicitly():
    # F-003: full auto is a launch flag, not the user settings' base mode.
    assert agents.claude_auto_flags(True) == ("--dangerously-skip-permissions",)
    assert agents.claude_auto_flags(False) == ()


def test_the_plan_launch_puts_the_flags_before_the_opener():
    catalog = {item["id"]: item for item in agents.load_catalog()}
    launched = agents.with_first_message(
        agents.with_launch_flags(catalog["claude"], agents.claude_plan_flags(True)), agents.WORKSHOP_OPENER)
    assert launched["command"] == (
        "claude --permission-mode plan --allow-dangerously-skip-permissions "
        + agents.shlex.quote(agents.WORKSHOP_OPENER)
    )


def test_no_flags_keeps_the_catalogue_command_unchanged():
    catalog = {item["id"]: item for item in agents.load_catalog()}
    assert agents.with_launch_flags(catalog["claude"], ()) == catalog["claude"]


def test_no_active_build_is_not_yet_approved(user):
    assert build_plan.approved_for_active(user.home, "", Path(user.home) / "projects") is False


def test_an_approved_build_launches_as_today(user):
    approve_build(user.home, "repair-desk")
    assert build_plan.approved_for_active(user.home, "repair-desk", Path(user.home) / "projects") is True


def test_an_unapproved_build_launches_in_plan_mode(user):
    approve_build(user.home, "other-build")
    assert build_plan.approved_for_active(user.home, "repair-desk", Path(user.home) / "projects") is False


def test_a_scaffolded_build_counts_as_past_the_gate(user):
    build = Path(user.home) / "projects" / "old-build"
    (build / ".workshop").mkdir(parents=True)
    (build / ".workshop" / "scaffold.json").write_text("{}")
    assert build_plan.approved_for_active(user.home, "old-build", Path(user.home) / "projects") is True


def test_a_saved_goal_is_not_a_scaffold(user):
    """project.json is written when a goal is saved, before any scaffold (31 Oct review)."""
    build = Path(user.home) / "projects" / "new-build"
    (build / ".workshop").mkdir(parents=True)
    (build / ".workshop" / "project.json").write_text("{}")
    assert build_plan.approved_for_active(user.home, "new-build", Path(user.home) / "projects") is False


def test_a_new_build_resets_the_plan(user, monkeypatch):
    approve_build(user.home, "first")
    monkeypatch.setattr(config, "users_root", lambda: str(Path(user.home).parent))
    projects.select(user, "second", goal="A new thing", new=True)
    assert build_plan.read(user.home).get("build") == "second"
    assert build_plan.read(user.home).get("plan_approved_at") is None
    assert build_plan.is_approved(user.home, "second") is False


# -- The plan CLI and the gate (§2) -------------------------------------------------

def _env(home) -> dict:
    return {
        "HOME": str(home),
        "PATH": os.environ["PATH"],
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_AUTHOR_NAME": "Attendee", "GIT_AUTHOR_EMAIL": "attendee@example.com",
        "GIT_COMMITTER_NAME": "Attendee", "GIT_COMMITTER_EMAIL": "attendee@example.com",
        "WORKSHOP_USER_EMAIL": "attendee@example.com",
    }


def _select_build(home, name) -> None:
    state = Path(home) / ".config" / "workshop" / "projects.json"
    state.parent.mkdir(parents=True, exist_ok=True)
    state.write_text(json.dumps({"version": 1, "active": name, "goals": {name: ""}, "materialized": []}))


def _workflow(home, *args) -> subprocess.CompletedProcess:
    return subprocess.run(["python3", str(WORKFLOW), *args], env=_env(home),
                          capture_output=True, text=True, timeout=30)


def test_the_gate_refuses_scaffolding_before_approval_with_the_contract_guidance(tmp_path):
    result = subprocess.run(["bash", str(INIT_PROJECT), "garden-plan"], env=_env(tmp_path),
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 1
    assert "no approved plan for build garden-plan" in result.stderr
    assert ("Put the plan to the attendee and wait for their yes; then run "
            "`workshop-workflow plan --approved`.") in result.stderr
    assert not (tmp_path / "projects" / "garden-plan").exists()


def test_the_gate_refuses_a_plan_put_but_not_yet_approved(tmp_path):
    _select_build(tmp_path, "garden-plan")
    put = _workflow(tmp_path, "plan", "--put", "Garden planner with weekly tasks")
    assert put.returncode == 0, put.stderr
    result = subprocess.run(["bash", str(INIT_PROJECT), "garden-plan"], env=_env(tmp_path),
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 1
    assert not (tmp_path / "projects" / "garden-plan").exists()


def test_plan_put_then_approved_opens_the_gate(tmp_path):
    _select_build(tmp_path, "garden-plan")
    assert _workflow(tmp_path, "plan", "--put", "Garden planner").returncode == 0
    approved = _workflow(tmp_path, "plan", "--approved")
    assert approved.returncode == 0, approved.stderr
    result = subprocess.run(["bash", str(INIT_PROJECT), "garden-plan"], env=_env(tmp_path),
                            capture_output=True, text=True, timeout=60)
    assert "no approved plan" not in result.stderr
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "projects" / "garden-plan").is_dir()


def test_the_claude_hook_form_records_the_approval_as_plan_mode(tmp_path):
    _select_build(tmp_path, "garden-plan")
    result = _workflow(tmp_path, "plan", "--approved", "--source", "claude-plan-mode")
    assert result.returncode == 0, result.stderr
    record = build_plan.read(tmp_path)
    assert record["plan_approved_source"] == "claude-plan-mode"
    assert record["build"] == "garden-plan"


def test_show_reports_the_plan_put_and_approved(tmp_path):
    project = tmp_path / "projects" / "garden-plan"
    project.mkdir(parents=True)
    _select_build(tmp_path, "garden-plan")
    subprocess.run(["git", "init", "-q", str(project)], check=True)
    before = _workflow(tmp_path, "--project", str(project), "show")
    assert "Plan: not put yet" in before.stdout
    _workflow(tmp_path, "plan", "--put", "Garden planner with weekly tasks")
    put = _workflow(tmp_path, "--project", str(project), "show")
    assert "awaiting the attendee's yes" in put.stdout
    _workflow(tmp_path, "plan", "--approved")
    approved = _workflow(tmp_path, "--project", str(project), "show")
    assert "approved by the attendee" in approved.stdout


def test_a_plan_for_another_build_does_not_satisfy_this_one(tmp_path):
    _select_build(tmp_path, "garden-plan")
    _workflow(tmp_path, "plan", "--approved", "--build", "other-build")
    result = subprocess.run(["bash", str(INIT_PROJECT), "garden-plan"], env=_env(tmp_path),
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 1
    assert "for this build: workshop-workflow plan --approved --build garden-plan" in result.stderr


def test_phase_preview_is_read_as_the_build_stage(tmp_path):
    project = tmp_path / "projects" / "garden-plan"
    project.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(project)], check=True)
    result = _workflow(tmp_path, "--project", str(project), "phase", "preview", "--evidence", "preview=https://x")
    # Refused as a transition (understand -> build), not as an invalid choice.
    assert "invalid choice" not in result.stderr
    assert "not a workflow step" in result.stderr or "no path is chosen" in result.stderr


def test_an_unknown_stage_names_the_real_stages(tmp_path):
    result = _workflow(tmp_path, "phase", "bogus")
    assert result.returncode == 2
    assert "stages are understand, choose_path, shape, agree, build, inspect, improve, feedback" in result.stderr
    assert "phase build --evidence preview=<url>" in result.stderr


# -- Omni host spans (R-007, open) ---------------------------------------------------

def test_the_omni_host_env_carries_no_otel_settings(user, monkeypatch):
    """R-007 is open: the host env reaches native agent shells, so no OTEL_* there."""
    from server import omnigent_remote

    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "https://collector.example.com:4317")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_PROTOCOL", "grpc")
    _argv, env, _cwd = omnigent_remote.build_host_launch(user, "/usr/bin/true", "https://app.example.com")
    assert not [key for key in env if key.startswith("OTEL_")]
    passthrough = env[omnigent_remote.RUNNER_ENV_PASSTHROUGH].split(",")
    assert not [name for name in passthrough if name.startswith("OTEL_")]


# -- The first build (pending approval, sweep review) -----------------------------

def _pending_approval_from(home, *args) -> None:
    result = _workflow(home, *args)
    assert result.returncode == 0, result.stderr


def test_first_build_via_the_claude_hook_passes_the_gate(tmp_path):
    """No current build: the hook's approval is pending, and the first scaffold adopts it."""
    _pending_approval_from(tmp_path, "plan", "--approved", "--source", "claude-plan-mode")
    assert build_plan.read(tmp_path)["build"] == ""
    result = subprocess.run(["bash", str(INIT_PROJECT), "first-build"], env=_env(tmp_path),
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "projects" / "first-build").is_dir()
    assert build_plan.read(tmp_path)["build"] == "first-build"


def test_first_build_via_codex_passes_the_gate(tmp_path):
    _pending_approval_from(tmp_path, "plan", "--put", "A pricing dashboard")
    _pending_approval_from(tmp_path, "plan", "--approved", "--source", "attendee")
    result = subprocess.run(["bash", str(INIT_PROJECT), "pricing"], env=_env(tmp_path),
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    assert build_plan.read(tmp_path)["plan_approved_source"] == "attendee"


def test_a_pending_approval_is_consumed_by_one_build_only(tmp_path):
    _pending_approval_from(tmp_path, "plan", "--approved")
    first = subprocess.run(["bash", str(INIT_PROJECT), "first"], env=_env(tmp_path),
                           capture_output=True, text=True, timeout=60)
    assert first.returncode == 0, first.stderr
    second = subprocess.run(["bash", str(INIT_PROJECT), "second"], env=_env(tmp_path),
                            capture_output=True, text=True, timeout=30)
    assert second.returncode == 1
    assert "no approved plan for build second" in second.stderr


def test_new_build_path_is_unchanged_and_needs_its_own_approval(tmp_path, monkeypatch):
    from server.users import User
    monkeypatch.setattr(config, "users_root", lambda: str(tmp_path / "users"))
    attendee = User("alice@example.com")
    attendee.bootstrap_home()
    _select_build(attendee.home, "earlier")
    approve_build(attendee.home, "earlier")
    projects.select(attendee, "later", goal="Next thing", new=True)
    assert build_plan.is_approved(attendee.home, "later") is False
    result = subprocess.run(["bash", str(INIT_PROJECT), "later"], env=_env(attendee.home),
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 1


def test_the_check_is_the_gate_init_calls(tmp_path):
    _pending_approval_from(tmp_path, "plan", "--approved")
    checked = subprocess.run(["python3", str(WORKFLOW), "plan", "--check", "gate-build"], env=_env(tmp_path),
                             capture_output=True, text=True, timeout=30)
    assert checked.returncode == 0, checked.stderr
    assert build_plan.read(tmp_path)["build"] == "gate-build"


def test_a_saved_goal_does_not_open_the_gate(tmp_path):
    """Codex review (31 Oct): attach-image or a saved goal leaves .workshop/project.json in a
    new build before any scaffold; the gate must still refuse an unapproved build."""
    context = tmp_path / "projects" / "goal-only" / ".workshop"
    context.mkdir(parents=True)
    (context / "project.json").write_text("{}")
    checked = subprocess.run(["python3", str(WORKFLOW), "plan", "--check", "goal-only"], env=_env(tmp_path),
                             capture_output=True, text=True, timeout=30)
    assert checked.returncode == 1
    assert "no approved plan for build goal-only" in checked.stderr


def test_a_scaffold_leaves_the_marker_the_gate_trusts(tmp_path):
    _pending_approval_from(tmp_path, "plan", "--approved")
    result = subprocess.run(["bash", str(INIT_PROJECT), "marked"], env=_env(tmp_path),
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "projects" / "marked" / ".workshop" / "scaffold.json").is_file()
    again = subprocess.run(["python3", str(WORKFLOW), "plan", "--check", "marked"], env=_env(tmp_path),
                           capture_output=True, text=True, timeout=30)
    assert again.returncode == 0, again.stderr


# -- ~/.claude.json safety (sweep review) ---------------------------------------------

def test_an_unparseable_claude_json_is_never_overwritten(user):
    path = os.path.join(user.home, ".claude.json")
    build = Path(user.home) / "projects" / "half"
    build.mkdir(parents=True)
    with open(path, "w") as handle:
        handle.write('{"projects": {"/keep": {"allowedTools": ["Bash"]')  # truncated write
    cli_config.ensure_claude_trusts(user, str(build))
    assert open(path).read() == '{"projects": {"/keep": {"allowedTools": ["Bash"]'


def test_an_already_trusted_folder_is_not_rewritten(user, monkeypatch):
    path = os.path.join(user.home, ".claude.json")
    build = Path(user.home) / "projects" / "done"
    build.mkdir(parents=True)
    trusted = {"projects": {os.path.realpath(build): {"hasTrustDialogAccepted": True}}}
    with open(path, "w") as handle:
        json.dump(trusted, handle)
    writes = []
    monkeypatch.setattr(cli_config, "_atomic_write_text", lambda *args: writes.append(args))
    cli_config.ensure_claude_trusts(user, str(build))
    assert writes == []


def test_a_concurrent_claude_save_is_reapplied_not_clobbered(user, monkeypatch):
    path = os.path.join(user.home, ".claude.json")
    build = Path(user.home) / "projects" / "race"
    build.mkdir(parents=True)
    with open(path, "w") as handle:
        json.dump({"numStartups": 1}, handle)
    real = cli_config._read_text_or_none
    calls = {"n": 0}

    def moved_once(target):
        calls["n"] += 1
        if calls["n"] == 2:  # Claude saved between our read and the replace
            with open(target, "w") as handle:
                json.dump({"numStartups": 9}, handle)
        return real(target)

    monkeypatch.setattr(cli_config, "_read_text_or_none", moved_once)
    cli_config.ensure_claude_trusts(user, str(build))
    data = _claude_json(user)
    assert data["numStartups"] == 9
    assert data["projects"][os.path.realpath(build)]["hasTrustDialogAccepted"] is True
