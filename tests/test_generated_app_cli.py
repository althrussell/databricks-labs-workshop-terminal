"""CLI boundary regressions using synthetic CT/browser transports only."""

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from scripts import evaluate_generated_apps as cli
from evals.generated_apps.adapters import browser as browser_module
from evals.generated_apps.adapters.ct_client import ControlTowerClient


BASE = "https://ct.example.com"
RUN = "eval-run-1"
UNIT = "unit-1"
MARKER = "wt-eval-cli-1"


def write(path, value):
    path.write_text(json.dumps(value))
    return str(path)


def ct_fixture(monkeypatch, tmp_path, *, marker=MARKER, seats=1, preflight=None):
    now = datetime.now(timezone.utc)
    expires = (now + timedelta(hours=2)).isoformat()
    ownership = {"marker": marker, "run_id": RUN, "ttl_seconds": 7200,
                 "cost_budget_usd": 10, "termination_at": expires}
    detail = {"run": {"run_id": RUN, "lab_name": marker, "requested_user_count": seats,
        "created_at": now.isoformat(), "termination_at": expires, "cost_budget_usd": 10, "status": "ready"},
        "units": [{"unit_id": UNIT, "run_id": RUN, "lab_user_email": "synthetic@example.com",
            "workspace_url": "https://child.example.com", "workspace_id": "123",
            "catalog_name": "eval_catalog", "schema_name": "default", "status": "ready",
            "apps": [{"app_kind": "workshop_terminal", "app_name": "wt", "app_url": "https://wt.example.com",
                "release_digest": "a" * 64, "resource_state": {"deployment": {
                    "mode": "package", "release_digest": "a" * 64, "deployment_id": "dep-1"}}}]}], "apps": []}
    calls = []
    def transport(method, url, **kwargs):
        path = url.removeprefix(BASE)
        calls.append((method, path))
        if path == "/api/labs":
            return 200, {"run_id": RUN, "status": "draft"}
        if path == f"/api/labs/{RUN}":
            return 200, detail
        if path.endswith("/preflight"):
            return 200, preflight if preflight is not None else {
                "run_id": RUN, "status": "ready_to_provision", "blocking_failures": 0, "warnings": 0,
                "checks": [{"name": "model_budget", "status": "pass"}],
            }
        if path.endswith("/provision"):
            return 200, {"run_id": RUN, "status": "provisioning"}
        raise AssertionError("unexpected synthetic request")
    client = ControlTowerClient(BASE, "labs", transport)
    monkeypatch.setattr(cli, "ControlTowerClient", lambda *args, **kwargs: client)
    payload = {"lab_name": marker, "workspace_prefix": "eval", "catalog_prefix": "eval",
        "requested_user_count": 1, "target_region": "ap-southeast-2", "termination_at": expires, "cost_budget_usd": 10}
    return SimpleNamespace(client=client, calls=calls, ownership=ownership, detail=detail,
        ownership_file=write(tmp_path / "ownership.json", ownership),
        payload_file=write(tmp_path / "create.json", payload), output=tmp_path / "operation.json")


def prepare_args(fixture):
    return ["prepare", "--profile", "labs", "--ct-url", BASE, "--ownership", fixture.ownership_file,
            "--create-spec", fixture.payload_file, "--output", str(fixture.output), "--execute", "--provision"]


@pytest.mark.parametrize("failed_stage", ["preflight", "provision"])
def test_created_run_ownership_survives_later_prepare_failure(monkeypatch, tmp_path, failed_stage):
    fixture = ct_fixture(monkeypatch, tmp_path)
    def fail(*args, **kwargs):
        raise RuntimeError("Bearer private-value")
    monkeypatch.setattr(fixture.client, failed_stage, fail)
    assert cli.main(prepare_args(fixture)) == 2
    report = json.loads(fixture.output.read_text())
    owned = report["artifacts"]["operation_state"]["ownership"]
    assert owned["run_id"] == RUN
    receipt = json.loads(fixture.output.with_suffix(".ownership.json").read_text())
    assert receipt["run_id"] == RUN
    assert receipt["termination_at"] == fixture.ownership["termination_at"]
    assert "private-value" not in fixture.output.read_text()


def test_prepare_proves_receipt_destination_writable_before_create_post(monkeypatch, tmp_path):
    fixture = ct_fixture(monkeypatch, tmp_path)
    parent = tmp_path / "not-a-directory"
    parent.write_text("original")
    fixture.output = parent / "operation.json"
    try:
        result = cli.main(prepare_args(fixture))
    except OSError:
        result = 2
    assert result != 0
    assert not any(method == "POST" for method, _ in fixture.calls)


@pytest.mark.parametrize("marker,seats", [("Ai-Demo-01", 1), (MARKER, 2)])
def test_poll_does_not_turn_matching_unowned_or_multiseat_run_disposable(monkeypatch, tmp_path, marker, seats):
    fixture = ct_fixture(monkeypatch, tmp_path, marker=marker, seats=seats)
    inventory = tmp_path / "inventory.json"
    result = cli.main(["poll", "--profile", "labs", "--ct-url", BASE,
        "--ownership", fixture.ownership_file, "--output", str(fixture.output),
        "--unit-id", UNIT, "--export-inventory", str(inventory)])
    assert result != 0
    assert not inventory.exists()
    assert all(method == "GET" for method, _ in fixture.calls)


def test_poll_rejects_changed_budget_before_inventory_export(monkeypatch, tmp_path):
    fixture = ct_fixture(monkeypatch, tmp_path)
    fixture.detail["run"]["cost_budget_usd"] = 100
    inventory = tmp_path / "inventory.json"
    assert cli.main(["poll", "--profile", "labs", "--ct-url", BASE,
        "--ownership", fixture.ownership_file, "--output", str(fixture.output),
        "--export-inventory", str(inventory)]) != 0
    assert not inventory.exists()


@pytest.mark.parametrize("preflight", [
    {"run_id": RUN, "status": "failed", "blocking_failures": False, "checks": []},
    {"run_id": RUN, "status": "failed", "blocking_failures": 0, "checks": [{"name": "model_budget", "status": "fail"}]},
])
def test_prepare_never_provisions_a_malformed_or_failed_preflight(monkeypatch, tmp_path, preflight):
    fixture = ct_fixture(monkeypatch, tmp_path, preflight=preflight)
    assert cli.main(prepare_args(fixture)) != 0
    assert not any(path.endswith("/provision") for _, path in fixture.calls)


def browser_fixture(monkeypatch, tmp_path, *, screenshot_failure=False):
    target = {"name": "wt", "url": "https://wt.example.com", "workspace_host": "https://child.example.com",
        "ct_run_id": RUN, "ct_unit_id": UNIT, "attendee_email": "synthetic@example.com", "disposable": True,
        "resources": {"catalog": "eval_catalog"}}
    paths = SimpleNamespace(
        inventory=write(tmp_path / "inventory.json", {"apps": [target]}),
        observations=write(tmp_path / "observations.json", {}),
        budget=write(tmp_path / "budget.json", {"total_seconds": 30, "consultation_seconds": 20,
            "token_ceiling": 50000, "spend_ceiling_usd": 10}),
        state=write(tmp_path / "browser-private.json", {"cookies": [], "origins": []}),
        output=tmp_path / "entry.json", closed=[], configs=[],
    )
    monkeypatch.setattr(cli, "assess_target", lambda *args, **kwargs: {"execution_ready": True})
    class Page:
        async def screenshot(self, **kwargs):
            if screenshot_failure:
                raise RuntimeError("synthetic screenshot failure")
            return None
    class Browser:
        def __init__(self, *args, **kwargs):
            self.page = Page()
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            return None
    class Driver:
        def __init__(self, page):
            self.harness = SimpleNamespace(evidence=lambda: {"structured_events_supported": False})
            self._agents = [{"id": "claude", "label": "Claude"}]
            self.evidence = browser_module.BrowserEntryEvidence(session_id="session-created-by-eval", opening_submitted=True)
        async def enter(self, config):
            paths.configs.append(config)
            return self.evidence
        async def close_launched_session(self, label):
            paths.closed.append(label)
    monkeypatch.setattr(browser_module, "AuthenticatedBrowser", Browser)
    monkeypatch.setattr(browser_module, "WorkshopBrowserDriver", Driver)
    return paths


def entry_args(fixture):
    return ["entry", "--inventory", fixture.inventory, "--instance", "wt",
        "--observations", fixture.observations, "--wt-browser-state", fixture.state,
        "--agent", "claude", "--budget", fixture.budget, "--output", str(fixture.output)]


def test_entry_budget_reaches_browser_run_deadline_and_never_implies_acceptance(monkeypatch, tmp_path):
    fixture = browser_fixture(monkeypatch, tmp_path)
    assert cli.main(entry_args(fixture)) != 0
    assert fixture.configs[0].run_deadline_seconds == 30
    assert fixture.closed == ["Claude"]
    result = json.loads(fixture.output.read_text())
    assert result["accepted"] is False
    assert "real_deployment" in result["missing_acceptance_checks"]
    assert result["artifacts"]["entry"]["session_id"] == "session-created-by-eval"


def test_screenshot_failure_preserves_launched_session_and_always_attempts_cleanup(monkeypatch, tmp_path):
    fixture = browser_fixture(monkeypatch, tmp_path, screenshot_failure=True)
    assert cli.main(entry_args(fixture)) != 0
    result = json.loads(fixture.output.read_text())
    assert fixture.closed == ["Claude"]
    assert result["artifacts"]["entry"]["session_id"] == "session-created-by-eval"
    assert result["accepted"] is False


def test_output_alias_cannot_destroy_private_browser_state(monkeypatch, tmp_path):
    fixture = browser_fixture(monkeypatch, tmp_path)
    original = json.loads(open(fixture.state).read())
    fixture.output = tmp_path / "browser-private.json"
    assert cli.main(entry_args(fixture)) != 0
    assert json.loads(open(fixture.state).read()) == original
    assert fixture.configs == []
