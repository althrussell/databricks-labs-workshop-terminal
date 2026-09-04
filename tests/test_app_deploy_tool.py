"""Contract tests for the Claude/Codex App deploy-and-wait tool."""

from __future__ import annotations

import contextlib
import fcntl
import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import tomllib

import pytest


ROOT = Path(__file__).parents[1]
HELPER = ROOT / "assets" / "bin" / "workshop-app-deploy"


def _load_helper():
    name = "workshop_app_deploy_test_module"
    loader = importlib.machinery.SourceFileLoader(name, str(HELPER))
    spec = importlib.util.spec_from_loader(name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    loader.exec_module(module)
    return module


deploy_tool = _load_helper()


def _env(tmp_path: Path) -> tuple[dict[str, str], Path]:
    home = tmp_path / "home"
    project = home / "projects" / "demo"
    project.mkdir(parents=True)
    (project / "databricks.yml").write_text("bundle:\n  name: demo\n")
    env = {
        "HOME": str(home),
        "PATH": os.environ["PATH"],
        "WORKSHOP_PROJECTS_ROOT": str(home / "projects"),
        "WORKSHOP_APP_DEPLOY_STATE_DIR": str(home / ".state"),
    }
    return env, project


def _app(
    *,
    active: str = "old",
    pending: str = "",
    app_state: str = "RUNNING",
    compute_state: str = "ACTIVE",
) -> dict:
    return {
        "url": "https://demo.databricksapps.com",
        "active_deployment": {
            "deployment_id": active,
            "status": {"state": "SUCCEEDED" if active else ""},
        }
        if active
        else None,
        "pending_deployment": {
            "deployment_id": pending,
            "status": {"state": "IN_PROGRESS"},
        }
        if pending
        else None,
        "app_status": {"state": app_state},
        "compute_status": {"state": compute_state},
    }


class FakeBackend:
    def __init__(self, *, deployment_states=("SUCCEEDED",), app_states=None):
        self.submissions = 0
        self.submitted = False
        self.app_reads = 0
        self.deployment_reads = 0
        self.deployment_states = list(deployment_states)
        self.app_states = list(
            app_states
            or [
                _app(pending="new", app_state="DEPLOYING", compute_state="STARTING"),
                _app(active="new"),
            ]
        )
        self.submit_code = 0
        self.cancelled = False
        self.cleaned = 0

    def bundle_summary(self, _project, _target):
        return {
            "resources": {
                "apps": {
                    "demo": {
                        "id": "demo-app",
                        "url": "https://demo.databricksapps.com",
                    }
                }
            }
        }

    def bundle_validate(self, project, target):
        return self.bundle_summary(project, target)

    def get_app(self, _project, _name):
        if not self.submitted:
            return _app()
        value = self.app_states[min(self.app_reads, len(self.app_states) - 1)]
        self.app_reads += 1
        if isinstance(value, Exception):
            raise value
        return value

    def get_deployment(self, _project, _name, deployment_id):
        assert deployment_id.startswith("new")
        value = self.deployment_states[
            min(self.deployment_reads, len(self.deployment_states) - 1)
        ]
        self.deployment_reads += 1
        if isinstance(value, Exception):
            raise value
        return {"deployment_id": deployment_id, "status": {"state": value}}

    def start_submit(self, state):
        self.submissions += 1
        self.submitted = True
        state["submit_pid"] = 101
        state["submit_log_path"] = "/absent"
        state["submit_exit_path"] = "/absent"

    def submission_status(self, _state):
        return True, self.submit_code

    def progress_lines(self, _state):
        return ["validation complete"]

    def cancel_submission(self, _state):
        self.cancelled = True
        return True

    def cleanup_submission(self, _state):
        self.cleaned += 1


def _controller(env, backend, reports=None, **kwargs):
    reports = reports if reports is not None else []
    return deploy_tool.DeployController(
        env,
        backend=backend,
        reporter=lambda result, source: reports.append((dict(result), source)),
        poll_seconds=0,
        **kwargs,
    )


def test_progress_redacts_raw_tokens_email_and_is_bounded():
    jwt = "eyJheader.payload.signature"
    raw = f"dapi1234567890 {jwt} alice@example.com " + ("x" * 500)

    safe = deploy_tool._safe_line(raw)

    assert "dapi1234567890" not in safe
    assert jwt not in safe
    assert "alice@example.com" not in safe
    assert "[REDACTED_TOKEN]" in safe
    assert len(safe) == deploy_tool.MAX_PROGRESS_CHARS


def test_local_reporter_bounds_callback_values(tmp_path, monkeypatch):
    env, _project = _env(tmp_path)
    reporter = deploy_tool.LocalReporter(env)
    posted = []
    monkeypatch.setattr(
        reporter,
        "_post",
        lambda path, payload: posted.append((path, payload)) or {},
    )

    reporter(
        {
            "status": "timed_out",
            "reason": "timed_out",
            "duration_ms": 3_700_000,
            "attempts": 20_000,
            "resumed": True,
        },
        "mcp",
    )

    assert posted == [
        (
            "/api/tools/app-deploy/event",
            {
                "outcome": "timed_out",
                "reason": "timed_out",
                "duration_ms": 3_600_000,
                "attempts": 10_000,
                "resumed": True,
                "source": "mcp",
            },
        )
    ]


def test_concurrent_duplicate_returns_completed_operation_without_redeploy(
    tmp_path, monkeypatch
):
    env, project = _env(tmp_path)
    backend = FakeBackend()
    terminal = {
        "phase": "succeeded",
        "reason": "deployment_succeeded",
        "project_path": str(project),
        "target": "default",
        "app_name": "demo-app",
        "app_url": "https://demo.databricksapps.com",
        "deployment_id": "new",
        "deployment_state": "SUCCEEDED",
        "app_state": "RUNNING",
        "compute_state": "ACTIVE",
        "attempts": 3,
    }

    @contextlib.contextmanager
    def waited_lock(_self, **_kwargs):
        yield True

    monkeypatch.setattr(deploy_tool.StateStore, "lock", waited_lock)
    monkeypatch.setattr(deploy_tool.StateStore, "load", lambda _self: terminal)

    result = _controller(env, backend).deploy(project_path=str(project))

    assert result["status"] == "succeeded"
    assert result["resumed"] is True
    assert result["deployment_id"] == "new"
    assert backend.submissions == 0


def test_lock_wait_honours_cancellation_without_submitting(tmp_path):
    env, project = _env(tmp_path)
    backend = FakeBackend()
    store = deploy_tool.StateStore(project, "default", env)
    holder = os.open(store.lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(holder, fcntl.LOCK_EX)
    cancel = threading.Event()
    cancel.set()
    try:
        result = _controller(env, backend).deploy(
            project_path=str(project), cancel=cancel
        )
    finally:
        fcntl.flock(holder, fcntl.LOCK_UN)
        os.close(holder)

    assert result["status"] == "cancelled"
    assert result["reason"] == "lock_cancelled"
    assert result["remote_may_continue"] is True
    assert backend.submissions == 0


def test_lock_wait_honours_timeout_without_submitting(tmp_path):
    env, project = _env(tmp_path)
    backend = FakeBackend()
    store = deploy_tool.StateStore(project, "default", env)
    holder = os.open(store.lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(holder, fcntl.LOCK_EX)
    clock = SequenceClock([0, 31, 31])
    try:
        result = _controller(env, backend, monotonic=clock).deploy(
            project_path=str(project), timeout_seconds=30
        )
    finally:
        fcntl.flock(holder, fcntl.LOCK_UN)
        os.close(holder)

    assert result["status"] == "timed_out"
    assert result["reason"] == "lock_timed_out"
    assert result["remote_may_continue"] is True
    assert backend.submissions == 0


def test_success_waits_for_exact_deployment_and_live_compute(tmp_path):
    env, project = _env(tmp_path)
    backend = FakeBackend(
        deployment_states=("IN_PROGRESS", "SUCCEEDED"),
        app_states=[
            _app(pending="new", app_state="DEPLOYING", compute_state="STARTING"),
            _app(pending="new", app_state="DEPLOYING", compute_state="STARTING"),
            _app(active="new"),
        ],
    )
    progress = []
    reports = []

    result = _controller(env, backend, reports).deploy(
        project_path=str(project), progress=progress.append
    )

    assert result == {
        "status": "succeeded",
        "reason": "deployment_succeeded",
        "app_name": "demo-app",
        "app_url": "https://demo.databricksapps.com",
        "deployment_id": "new",
        "deployment_state": "SUCCEEDED",
        "app_state": "RUNNING",
        "compute_state": "ACTIVE",
        "duration_ms": result["duration_ms"],
        "attempts": 2,
        "resumed": False,
        "remote_may_continue": False,
        "message": "",
    }
    assert backend.submissions == 1
    assert backend.cleaned == 1
    assert any("deployment=IN_PROGRESS" in line for line in progress)
    assert reports[0][0]["reason"] == "deployment_succeeded"


def test_platform_failure_is_terminal_and_actionable(tmp_path):
    env, project = _env(tmp_path)
    backend = FakeBackend(
        deployment_states=("FAILED",),
        app_states=[_app(pending="new", app_state="DEPLOYING", compute_state="STARTING")],
    )

    result = _controller(env, backend).deploy(project_path=str(project))

    assert result["status"] == "failed"
    assert result["reason"] == "failed"
    assert result["deployment_state"] == "FAILED"


def test_stale_crashed_app_does_not_fail_an_in_progress_deployment(tmp_path):
    env, project = _env(tmp_path)
    backend = FakeBackend(
        deployment_states=("IN_PROGRESS", "SUCCEEDED"),
        app_states=[
            _app(pending="new", app_state="CRASHED", compute_state="ERROR"),
            _app(pending="new", app_state="CRASHED", compute_state="ERROR"),
            _app(active="new"),
        ],
    )

    result = _controller(env, backend).deploy(project_path=str(project))

    assert result["status"] == "succeeded"
    assert result["deployment_state"] == "SUCCEEDED"
    assert result["attempts"] == 2


def test_first_deployment_falls_back_to_bundle_validate(tmp_path):
    env, project = _env(tmp_path)

    class FirstDeployBackend(FakeBackend):
        def bundle_summary(self, _project, _target):
            raise deploy_tool.CliError("no deployed bundle state")

        def bundle_validate(self, _project, _target):
            return {
                "resources": {
                    "apps": {
                        "demo": {
                            "name": "demo-app",
                            "url": "https://demo.databricksapps.com",
                        }
                    }
                }
            }

    result = _controller(env, FirstDeployBackend()).deploy(project_path=str(project))

    assert result["status"] == "succeeded"
    assert result["app_name"] == "demo-app"


def test_cli_submission_failure_returns_bounded_progress_detail(tmp_path):
    env, project = _env(tmp_path)
    backend = FakeBackend(app_states=[_app()])
    backend.submit_code = 1

    result = _controller(env, backend).deploy(project_path=str(project))

    assert result["status"] == "failed"
    assert result["reason"] == "cli_failed"
    assert result["message"] == "validation complete"


def test_dead_submit_worker_without_status_fails_immediately(tmp_path):
    env, project = _env(tmp_path)
    backend = FakeBackend(app_states=[_app()])
    backend.submit_code = None

    result = _controller(env, backend).deploy(project_path=str(project))

    assert result["status"] == "failed"
    assert result["reason"] == "cli_failed"
    assert result["attempts"] == 1
    assert result["message"] == deploy_tool.MISSING_SUBMIT_STATUS_MESSAGE


def test_dead_submit_worker_still_follows_discovered_deployment(tmp_path):
    env, project = _env(tmp_path)
    backend = FakeBackend()
    backend.submit_code = None

    result = _controller(env, backend).deploy(project_path=str(project))

    assert result["status"] == "succeeded"
    assert result["deployment_id"] == "new"
    assert backend.deployment_reads == 1


def test_transient_poll_error_is_retried(tmp_path):
    env, project = _env(tmp_path)
    backend = FakeBackend(
        deployment_states=(deploy_tool.CliError("temporarily unavailable"), "SUCCEEDED"),
        app_states=[
            _app(pending="new", app_state="DEPLOYING", compute_state="STARTING"),
            _app(pending="new", app_state="DEPLOYING", compute_state="STARTING"),
            _app(active="new"),
        ],
    )
    progress = []

    result = _controller(env, backend).deploy(
        project_path=str(project), progress=progress.append
    )

    assert result["status"] == "succeeded"
    assert result["attempts"] == 2
    assert any("Transient status check failed" in line for line in progress)


def test_cancel_after_submission_keeps_remote_work_resumable(tmp_path):
    env, project = _env(tmp_path)
    backend = FakeBackend(
        deployment_states=("IN_PROGRESS",),
        app_states=[_app(pending="new", app_state="DEPLOYING", compute_state="STARTING")],
    )
    cancel = threading.Event()
    cancel.set()

    result = _controller(env, backend).deploy(
        project_path=str(project), cancel=cancel
    )

    assert result["status"] == "cancelled"
    assert result["remote_may_continue"] is True
    state = next(Path(env["WORKSHOP_APP_DEPLOY_STATE_DIR"]).glob("*.json"))
    assert json.loads(state.read_text())["phase"] == "polling"


class SequenceClock:
    def __init__(self, values):
        self.values = iter(values)
        self.last = 0.0

    def __call__(self):
        self.last = next(self.values, self.last)
        return self.last


def test_timeout_is_bounded_and_keeps_accepted_deployment(tmp_path):
    env, project = _env(tmp_path)
    backend = FakeBackend(
        deployment_states=("IN_PROGRESS",),
        app_states=[_app(pending="new", app_state="DEPLOYING", compute_state="STARTING")],
    )
    clock = SequenceClock([0, 31, 31, 31])

    result = _controller(env, backend, monotonic=clock).deploy(
        project_path=str(project), timeout_seconds=30
    )

    assert result["status"] == "timed_out"
    assert result["remote_may_continue"] is True
    assert result["deployment_id"] == "new"


def test_old_healthy_app_never_substitutes_for_the_new_deployment(tmp_path):
    env, project = _env(tmp_path)
    backend = FakeBackend(app_states=[_app()])
    clock = SequenceClock([0, 1, 1, 31, 31])

    result = _controller(env, backend, monotonic=clock).deploy(
        project_path=str(project), timeout_seconds=30
    )

    assert result["status"] == "timed_out"
    assert result["reason"] == "timed_out"
    assert result["deployment_id"] == ""
    assert result["remote_may_continue"] is True


def test_process_restart_resumes_without_a_duplicate_submission(tmp_path):
    env, project = _env(tmp_path)
    backend = FakeBackend(
        deployment_states=("IN_PROGRESS",),
        app_states=[_app(pending="new", app_state="DEPLOYING", compute_state="STARTING")],
    )
    cancel = threading.Event()
    cancel.set()
    first = _controller(env, backend).deploy(project_path=str(project), cancel=cancel)
    assert first["status"] == "cancelled"

    backend.app_states = [_app(active="new")]
    backend.app_reads = 0
    backend.deployment_states = ["SUCCEEDED"]
    backend.deployment_reads = 0
    second = _controller(env, backend).deploy(project_path=str(project))

    assert second["status"] == "succeeded"
    assert second["resumed"] is True
    assert backend.submissions == 1


def test_restart_between_durable_intent_and_worker_launch_starts_one_worker(tmp_path):
    env, project = _env(tmp_path)
    backend = FakeBackend()
    store = deploy_tool.StateStore(project, "default", env)
    store.save(
        {
            "schema_version": 1,
            "operation_id": "interrupted",
            "phase": "submitting",
            "project_path": str(project),
            "target": "default",
            "app_name": "demo-app",
            "app_url": "",
            "baseline_deployment_ids": ["old"],
            "deployment_id": "",
            "created_at": 1,
            "attempts": 0,
            "state_root": str(store.root),
        }
    )

    result = _controller(env, backend).deploy(project_path=str(project))

    assert result["status"] == "succeeded"
    assert result["resumed"] is True
    assert backend.submissions == 1


def test_multiple_deploys_have_no_per_session_invocation_budget(tmp_path):
    env, project = _env(tmp_path)
    backend = FakeBackend()
    controller = _controller(env, backend)

    for number in range(3):
        deployment = f"new-{number}"
        backend.submitted = False
        backend.app_reads = 0
        backend.deployment_reads = 0
        backend.app_states = [
            _app(pending=deployment, app_state="DEPLOYING", compute_state="STARTING"),
            _app(active=deployment),
        ]
        backend.deployment_states = ["SUCCEEDED"]
        result = controller.deploy(project_path=str(project))
        assert result["status"] == "succeeded"

    assert backend.submissions == 3


@pytest.mark.parametrize("target", ["--profile evil", "../../other", "bad target"])
def test_target_cannot_inject_cli_arguments(tmp_path, target):
    env, project = _env(tmp_path)
    with pytest.raises(deploy_tool.DeployError, match="target"):
        _controller(env, FakeBackend()).deploy(
            project_path=str(project), target=target
        )


def test_project_must_stay_inside_attendee_projects(tmp_path):
    env, _project = _env(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "databricks.yml").write_text("bundle: {}\n")
    with pytest.raises(deploy_tool.DeployError, match="inside"):
        _controller(env, FakeBackend()).deploy(project_path=str(outside))


def test_submit_worker_uses_supported_project_deploy_and_strips_ambient_secret(
    tmp_path,
):
    env, project = _env(tmp_path)
    shim = tmp_path / "shim"
    shim.mkdir()
    arguments = tmp_path / "arguments.json"
    executable = shim / "databricks"
    executable.write_text(
        "#!/bin/sh\n"
        "python3 - \"$@\" <<'PY'\n"
        "import json,os,sys\n"
        f"open({str(arguments)!r}, 'w').write(json.dumps({{'argv': sys.argv[1:], 'token': os.environ.get('DATABRICKS_TOKEN'), 'client_secret': os.environ.get('DATABRICKS_CLIENT_SECRET'), 'auth_type': os.environ.get('DATABRICKS_AUTH_TYPE'), 'profile': os.environ.get('DATABRICKS_CONFIG_PROFILE'), 'config_file': os.environ.get('DATABRICKS_CONFIG_FILE')}}))\n"
        "print('submitted')\n"
        "PY\n"
    )
    executable.chmod(0o755)
    log = tmp_path / "submit.log"
    exit_file = tmp_path / "submit.exit"
    command_env = {
        **env,
        "PATH": f"{shim}:{env['PATH']}",
        "DATABRICKS_TOKEN": "must-not-survive",
        "DATABRICKS_CLIENT_SECRET": "must-not-survive",
        "DATABRICKS_AUTH_TYPE": "oauth-m2m",
    }

    completed = subprocess.run(
        [
            sys.executable,
            str(HELPER),
            "--submit-worker",
            "--project",
            str(project),
            "--target",
            "default",
            "--log-path",
            str(log),
            "--exit-path",
            str(exit_file),
            "--worker-token",
            "test-worker",
        ],
        env=command_env,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert completed.returncode == 0, completed.stderr
    invocation = json.loads(arguments.read_text())
    assert invocation["argv"] == [
        "apps",
        "deploy",
        "--target",
        "default",
        "--auto-approve",
        "--no-wait",
        "--output",
        "json",
    ]
    assert invocation["token"] is None
    assert invocation["client_secret"] is None
    assert invocation["auth_type"] is None
    assert invocation["profile"] == "DEFAULT"
    assert invocation["config_file"] == str(Path(env["HOME"]) / ".databrickscfg")
    assert json.loads(exit_file.read_text())["exit_code"] == 0


def test_mcp_server_exposes_exactly_the_governed_deploy_tool(tmp_path):
    env, _project = _env(tmp_path)
    payload = "\n".join(
        [
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {"protocolVersion": "2025-06-18"},
                }
            ),
            json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}),
            json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}),
            "",
        ]
    )

    completed = subprocess.run(
        [sys.executable, str(HELPER), "--mcp"],
        input=payload,
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert completed.returncode == 0, completed.stderr
    messages = [json.loads(line) for line in completed.stdout.splitlines()]
    tools = next(item["result"]["tools"] for item in messages if item.get("id") == 2)
    assert [tool["name"] for tool in tools] == ["deploy_databricks_app"]
    assert tools[0]["inputSchema"]["required"] == ["project_path"]
    assert tools[0]["inputSchema"]["additionalProperties"] is False
    project_schema = tools[0]["inputSchema"]["properties"]["project_path"]
    assert "default" not in project_schema
    assert "Absolute" in project_schema["description"]


@pytest.mark.parametrize(
    "arguments, message",
    [
        ({}, "project_path is required"),
        ({"project_path": ".", "profile": "admin"}, "Unknown tool argument"),
        ({"project_path": "."}, "must be an absolute path"),
        (
            {
                "project_path": "/home/user/projects/demo",
                "timeout_seconds": "120",
            },
            "must be an integer",
        ),
    ],
)
def test_mcp_server_enforces_input_schema_at_runtime(capsys, arguments, message):
    class RefuseUnexpectedCall:
        def deploy(self, **_kwargs):
            raise AssertionError("invalid arguments reached the deploy controller")

    server = deploy_tool.McpServer(RefuseUnexpectedCall())
    server._call_tool(
        {
            "jsonrpc": "2.0",
            "id": 7,
            "method": "tools/call",
            "params": {"name": deploy_tool.TOOL_NAME, "arguments": arguments},
        }
    )

    response = json.loads(capsys.readouterr().out)
    assert response["error"]["code"] == -32602
    assert message in response["error"]["message"]


def test_claude_and_codex_register_and_allow_the_same_tool(client, monkeypatch):
    from server import credentials, user_content
    import server.main as main
    from server.users import user_manager

    monkeypatch.setenv("WORKSHOP_PAT", "dapi-test-token")
    monkeypatch.setattr(credentials.credential_manager, "token", lambda: "dapi-test-token")
    monkeypatch.setattr(
        main.install,
        "ready",
        lambda: {"claude": True, "codex": True, "omnigent": True},
    )
    monkeypatch.setattr(main.agents, "launch_command", lambda _agent: ["/bin/bash"])
    user_content._provisioned.discard("alice@example.com")
    response = client.post(
        "/api/sessions",
        json={"agent_id": "claude"},
        headers={"X-Forwarded-Email": "alice@example.com"},
    )
    assert response.status_code == 200
    home = Path(user_manager.get("alice@example.com").home)

    helper = home / ".local" / "bin" / "workshop-app-deploy"
    assert helper.is_file() and os.access(helper, os.X_OK)
    claude = json.loads((home / ".claude.json").read_text())
    assert claude["mcpServers"]["workshop"] == {
        "type": "stdio",
        "command": str(helper),
        "args": ["--mcp"],
    }
    settings = json.loads((home / ".claude" / "settings.json").read_text())
    assert "mcp__workshop__deploy_databricks_app" in settings["permissions"]["allow"]

    codex = tomllib.loads((home / ".codex" / "config.toml").read_text())
    assert codex["mcp_servers"]["workshop"]["command"] == str(helper)
    assert codex["mcp_servers"]["workshop"]["enabled_tools"] == [
        "deploy_databricks_app"
    ]
    assert codex["mcp_servers"]["workshop"]["tool_timeout_sec"] == 3600


def test_app_deploy_callback_emits_only_bounded_operational_fields(
    client, monkeypatch
):
    from server import telemetry, user_content
    from server.users import user_manager

    user = user_manager.get("deploy@example.com")
    user.bootstrap_home()
    user_content._write_callback_capability(user)
    capability = Path(user_content.callback_capability_path(user)).read_text().strip()
    seen = []
    monkeypatch.setattr(telemetry, "app_deploy", lambda attendee, **data: seen.append((attendee, data)))

    response = client.post(
        "/api/tools/app-deploy/event",
        headers={"X-Workshop-Capability": capability},
        json={
            "email": user.email,
            "outcome": "succeeded",
            "reason": "deployment_succeeded",
            "duration_ms": 1234,
            "attempts": 4,
            "resumed": True,
            "source": "mcp",
        },
    )

    assert response.status_code == 200
    assert seen == [
        (
            user.email,
            {
                "outcome": "succeeded",
                "reason": "deployment_succeeded",
                "duration_ms": 1234.0,
                "attempts": 4,
                "resumed": True,
                "source": "mcp",
            },
        )
    ]
