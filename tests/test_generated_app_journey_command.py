"""Synthetic command boundaries plus the real installed SDK constructor offline."""

import asyncio
import copy
import hashlib
import io
import json
import os
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

import httpx
import pytest

from evals.generated_apps.adapters.simulated_control_tower import bind_created_app, plan_simulation
from evals.generated_apps.adapters.browser import BrowserEntryEvidence, BrowserLaunchConfig
from evals.generated_apps.journey import JourneyConfig, JourneyResult, NativeEvidenceError, run_journey
from evals.generated_apps.simulator import NoviceSimulator, load_simulator_scenario
from scripts import deploy_generated_app_test as deployer
from scripts import run_generated_app_journey as runner

ROOT = Path(__file__).parents[1]
APP_ID = "11111111-1111-4111-8111-111111111111"
SESSION_ID = "22222222-2222-4222-8222-222222222222"


def fixture():
    now = datetime.now(timezone.utc)
    plan = plan_simulation({
        "marker": "wt-eval-cli-1", "disposable": True, "workspace_host": "https://labs.example.com",
        "workspace_id": "123", "profile": "labs", "attendee_email": "operator@example.com",
        "attendee_mode": "operator_bound", "ttl_seconds": 3600, "cost_budget_usd": 10,
        "release": {"source_kind": "runtime-snapshot", "source_digest": "a" * 64,
                    "parent_git_sha": "b" * 40, "instrumentation": True, "prompt_policy_unchanged_asserted": True},
        "harnesses": ["claude", "codex"], "evaluation_observation": True,
    }, json.loads((ROOT / "assets/artifacts/manifest.json").read_text()), now=now - timedelta(minutes=2))
    app = {"kind": "app", "name": plan["names"]["app_name"], "state": "created", "id": APP_ID,
           "service_principal_id": 456, "service_principal_client_id": APP_ID,
           "url": "https://wt-eval-cli-1-wt-123.aws.databricksapps.com"}
    bound = bind_created_app(plan, app)
    files = {"app.yaml": deployer.app_yaml(b"command: [python]\nenv: []\n", bound["environment"]),
             "server/main.py": b"# app\n", "server/evaluation.py": b"# observation\n", "static/index.html": b"<html/>"}
    manifest = deployer.source_manifest(files)
    receipt = {"schema_version": 1, "scope": "simulated_control_tower", "control_tower_requests": 0,
               "accepted": False, "status": "deployment_submitted", "plan": plan, "bound_plan": bound,
               "created_resources": [app, {"kind": "group", "name": plan["names"]["admin_group"], "state": "created", "id": "group-1"},
                                     {"kind": "catalog", "name": plan["names"]["catalog"], "state": "created_and_grants_verified"},
                                     {"kind": "workspace_source", "path": plan["names"]["source_path"], "state": "created"}],
               "deployment": {"deployment_id": "deploy-1", "source_code_path": plan["names"]["source_path"],
                              "status": {"state": "IN_PROGRESS"}},
               "uploaded_source": manifest, "source_identity": {"uploaded_runtime_digest": manifest["digest"],
                   "planned_unpatched_digest": "a" * 64, "runtime_environment_patched": True, "instrumented_release": True}}
    return receipt, files, now


class Client:
    """No mutation methods exist; unexpected paths/calls fail immediately."""
    def __init__(self, receipt, files, now):
        self.receipt, self.files, self.calls = receipt, dict(files), []
        expected = receipt["created_resources"][0]
        self.config = SimpleNamespace(host=receipt["plan"]["workspace_host"], authenticate=lambda: {"Authorization": "Bearer private-operator-token"})
        self.api_client = SimpleNamespace(do=self.api)
        self.me = SimpleNamespace(user_name=receipt["plan"]["attendee"]["email"])
        self.current_user = SimpleNamespace(me=lambda: self.me)
        self.deployment = SimpleNamespace(deployment_id="deploy-1", source_code_path=receipt["plan"]["names"]["source_path"],
                                         create_time=(now - timedelta(seconds=100)).isoformat(), status=SimpleNamespace(state="SUCCEEDED"),
                                         deployment_artifacts=SimpleNamespace(source_code_path=f"/Workspace/Users/{APP_ID}/src/deploy-1"))
        self.app = SimpleNamespace(**{key: value for key, value in expected.items() if key not in {"kind", "state"}},
                                   create_time=(now - timedelta(seconds=110)).isoformat(), creator=self.me.user_name,
                                   active_deployment=self.deployment, app_status=SimpleNamespace(state="RUNNING"),
                                   compute_status=SimpleNamespace(state="ACTIVE"))
        self.app_by_name = {self.app.name: self.app}
        self.inventory = [self.app]
        self.apps = SimpleNamespace(get=self.get, get_deployment=self.get_deployment, list=lambda **_: iter(self.inventory))
        self.workspace = SimpleNamespace(list=self.list_source, download=self.download)

    def api(self, method, path, **kwargs):
        assert method == "GET" and path == "/api/2.0/preview/scim/v2/Me"
        self.calls.append((method, path))
        return {"X-Databricks-Org-Id": "123"}

    def get(self, name):
        self.calls.append(("app_get", name))
        return self.app_by_name[name]

    def get_deployment(self, name, deployment_id):
        self.calls.append(("deployment_get", name, deployment_id))
        deployment = self.app_by_name[name].active_deployment
        assert deployment.deployment_id == deployment_id
        return deployment

    def list_source(self, root, **kwargs):
        assert kwargs == {"recursive": True}
        self.calls.append(("source_list", root))
        return iter([SimpleNamespace(path=root + "/" + path, object_type="FILE") for path in self.files])

    def download(self, path):
        self.calls.append(("source_download", path))
        relative = path.removeprefix(self.deployment.deployment_artifacts.source_code_path + "/")
        return io.BytesIO(self.files[relative])


def test_receipt_is_exact_single_seat_and_live_gate_hashes_copied_snapshot_not_old_upload():
    receipt, files, now = fixture()
    binding = runner.validate_receipt(receipt, now=now)
    client = Client(receipt, files, now)
    observed = runner.verify_deployment(binding, client, now=now)
    assert observed["verified"] and observed["runtime_snapshot"]["all_file_hashes_verified"]
    assert observed["runtime_snapshot"]["file_count"] == 4
    assert all(call[1].startswith(f"/Workspace/Users/{APP_ID}/src/deploy-1/") for call in client.calls if call[0] == "source_download")
    assert observed["control_tower_requests"] == 0
    assert observed["obo_consent_verified"] is False
    assert observed["event_attendee_equivalence_verified"] is False


@pytest.mark.parametrize("mutation,reason", [
    ("app_id", "live_app_identity_mismatch"), ("sp", "live_app_identity_mismatch"),
    ("url", "live_app_identity_mismatch"), ("operator", "operator_attendee_identity_mismatch"),
    ("workspace_host", "workspace_host_mismatch"), ("workspace_id", "workspace_id_mismatch"),
    ("active_deployment", "active_deployment_mismatch"), ("not_running", "terminal_not_running"),
    ("old_app", "terminal_app_freshness_unverified"), ("old_deployment", "live_deployment_freshness_unverified"),
    ("source_path", "live_deployment_freshness_unverified"), ("snapshot_path", "owned_deployment_snapshot_unverified"),
    ("changed_file", "snapshot_file_digest_changed"), ("extra_file", "snapshot_file_set_changed"),
])
def test_live_identity_source_and_freshness_mismatch_fail_before_browser(mutation, reason):
    receipt, files, now = fixture()
    binding = runner.validate_receipt(receipt, now=now)
    client = Client(receipt, files, now)
    if mutation == "app_id": client.app.id = "replacement"
    elif mutation == "sp": client.app.service_principal_id = 999
    elif mutation == "url": client.app.url = "https://other.example.com"
    elif mutation == "operator": client.me.user_name = "other@example.com"
    elif mutation == "workspace_host": client.config.host = "https://other.example.com"
    elif mutation == "workspace_id": client.api_client.do = lambda *_a, **_k: {"X-Databricks-Org-Id": "999"}
    elif mutation == "active_deployment": client.app.active_deployment = SimpleNamespace(deployment_id="replacement")
    elif mutation == "not_running": client.app.app_status.state = "CRASHED"
    elif mutation == "old_app": client.app.create_time = (now - timedelta(days=1)).isoformat()
    elif mutation == "old_deployment": client.deployment.create_time = (now - timedelta(days=1)).isoformat()
    elif mutation == "source_path": client.deployment.source_code_path = "/Workspace/Shared/event/terminal"
    elif mutation == "snapshot_path": client.deployment.deployment_artifacts.source_code_path = "/Workspace/Users/other/src/deploy-1"
    elif mutation == "changed_file": client.files["server/main.py"] = b"changed"
    elif mutation == "extra_file": client.files["server/unrecorded.py"] = b"unexpected"
    with pytest.raises(runner.GateError, match=reason):
        runner.verify_deployment(binding, client, now=now)


def test_snapshot_environment_marker_is_verified_from_remote_bytes_even_with_self_consistent_manifest():
    receipt, files, now = fixture()
    files["app.yaml"] = files["app.yaml"].replace(b"value: wt-eval-cli-1\n", b"value: different-marker\n")
    manifest = deployer.source_manifest(files)
    receipt["uploaded_source"] = manifest
    receipt["source_identity"]["uploaded_runtime_digest"] = manifest["digest"]
    with pytest.raises(runner.GateError, match="runtime_marker_or_contract_mismatch"):
        runner.verify_deployment(runner.validate_receipt(receipt, now=now), Client(receipt, files, now), now=now)


@pytest.mark.parametrize("mutation", ["extra_resource", "missing_workspace_id", "expired", "changed_bound", "mixed_manifest", "not_standalone"])
def test_bad_receipts_are_not_independent_readiness_proof(mutation):
    receipt, _, now = fixture()
    if mutation == "extra_resource": receipt["created_resources"].append({"kind": "neighbor_app"})
    elif mutation == "missing_workspace_id": receipt["plan"]["environment"].pop("DATABRICKS_WORKSPACE_ID")
    elif mutation == "expired": receipt["plan"]["bounds"]["expires_at"] = (now - timedelta(seconds=1)).isoformat()
    elif mutation == "changed_bound": receipt["bound_plan"]["environment"]["WORKSHOP_APP_SP_ID"] = "999"
    elif mutation == "mixed_manifest": receipt["uploaded_source"]["files"][0]["sha256"] = "c" * 64
    elif mutation == "not_standalone": receipt["control_tower_requests"] = 1
    with pytest.raises((runner.GateError, ValueError)):
        runner.validate_receipt(receipt, now=now)


@pytest.mark.parametrize("client_id", ["", "operator@example.com", "not-a-uuid", "11111111-1111-4111-8111-11111111111G"])
def test_terminal_service_principal_client_id_cannot_alias_attendee_or_unknown_identity(client_id):
    receipt, _, now = fixture()
    receipt["created_resources"][0]["service_principal_client_id"] = client_id
    with pytest.raises(runner.GateError, match="app_identity_receipt_unverified"):
        runner.validate_receipt(receipt, now=now)


def inputs(tmp_path):
    receipt, _, _ = fixture()
    receipt_path, budget_path = tmp_path / "receipt.json", tmp_path / "budget.json"
    receipt_path.write_text(json.dumps(receipt))
    budget_path.write_text(json.dumps({"total_seconds": 1800, "consultation_seconds": 180,
                                     "token_ceiling": 100000, "spend_ceiling_usd": 5}))
    return receipt_path, budget_path


def test_real_installed_sdk_constructor_accepts_config_timeouts_offline(tmp_path, monkeypatch):
    """Exercise real Config/WorkspaceClient with synthetic auth and no HTTP I/O."""
    import databricks.sdk.config as sdk_config
    from databricks.sdk import WorkspaceClient
    for name in list(os.environ):
        if name.startswith("DATABRICKS_"):
            monkeypatch.delenv(name)
    config_file = tmp_path / "fake-databrickscfg"
    config_file.write_text("[offline]\nhost = https://sdk-offline.example.invalid\ntoken = synthetic-offline-token\nauth_type = pat\n")
    monkeypatch.setenv("DATABRICKS_CONFIG_FILE", str(config_file))
    monkeypatch.setattr(sdk_config, "get_host_metadata", lambda _host: SimpleNamespace(
        account_id=None, workspace_id=None, oidc_endpoint=None, cloud=None,
        token_federation_default_oidc_audiences=[]))
    def forbidden_http(*_args, **_kwargs):
        raise AssertionError("constructor regression must not contact a workspace")
    monkeypatch.setattr("requests.sessions.Session.request", forbidden_http)
    client = runner.workspace_client("offline")
    assert isinstance(client, WorkspaceClient)
    assert client.config.host == "https://sdk-offline.example.invalid" and client.config.profile == "offline"
    assert client.config.http_timeout_seconds == 10 and client.config.retry_timeout_seconds == 15
    assert client.config.authenticate() == {"Authorization": "Bearer synthetic-offline-token"}


def test_default_cli_plan_is_executable_offline_and_cannot_claim_acceptance(tmp_path, monkeypatch):
    receipt, budget = inputs(tmp_path)
    async def forbidden_execute(*_args, **_kwargs):
        raise AssertionError("local planning must not create SDK or browser")
    monkeypatch.setattr(runner, "execute", forbidden_execute)
    output = tmp_path / "plan.json"
    assert runner.main(["--receipt", str(receipt), "--budget", str(budget), "--agent", "claude", "--output", str(output)]) == 0
    evidence = json.loads(output.read_text())
    assert evidence["status"] == "planned" and evidence["verdict"] == "unverified"
    assert evidence["accepted"] is False and evidence["control_tower_requests"] == 0
    assert "deployment_verification" not in evidence


def test_execute_without_genuine_browser_state_blocks_before_cloud_or_browser(tmp_path, monkeypatch):
    receipt, budget = inputs(tmp_path)
    async def forbidden_execute(*_args, **_kwargs):
        raise AssertionError("missing auth must stop before SDK")
    monkeypatch.setattr(runner, "execute", forbidden_execute)
    output = tmp_path / "blocked.json"
    assert runner.main(["--receipt", str(receipt), "--budget", str(budget), "--agent", "claude", "--execute", "--output", str(output)]) == 2
    assert json.loads(output.read_text())["reason"] == "genuine_attendee_browser_state_required"


def test_evidence_never_overwrites_input_auth_or_existing_receipt(tmp_path):
    receipt, budget = inputs(tmp_path)
    private = tmp_path / "auth.json"
    private.write_text('{"cookies": [{"value": "private-cookie"}]}')
    for destination in (receipt, budget, private):
        before = destination.read_bytes()
        assert runner.main(["--receipt", str(receipt), "--budget", str(budget), "--agent", "claude",
                            "--wt-browser-state", str(private), "--output", str(destination)]) == 2
        assert destination.read_bytes() == before
    output = tmp_path / "result.json"
    output.with_name(output.name + ".tmp").symlink_to(private)
    assert runner.main(["--receipt", str(receipt), "--budget", str(budget), "--agent", "claude", "--output", str(output)]) == 2
    assert "private-cookie" in private.read_text()


@pytest.mark.parametrize("status", [302, 401, 403, 404])
def test_operator_http_stays_separate_and_never_follows_redirect_or_leaks_errors(status):
    calls = []
    async def main():
        def handler(request):
            calls.append(request)
            return httpx.Response(status, headers={"Location": "https://other.example.com"}, content=b"private-operator-token")
        client = SimpleNamespace(config=SimpleNamespace(authenticate=lambda: {"Authorization": "Bearer private-operator-token"}))
        http = httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False)
        async with runner.OperatorFetcher(client, "https://terminal.example.com", http_client=http) as fetch:
            with pytest.raises(runner.GateError, match="operator_observation_http_unverified") as error:
                await fetch(f"/api/admin/evaluation/sessions/{SESSION_ID}/messages?cursor=&limit=30")
            assert "private" not in str(error.value)
            assert fetch.requests == [{"session_id": SESSION_ID, "http_status": status}]
    asyncio.run(main())
    assert len(calls) == 1 and calls[0].url.host == "terminal.example.com"
    assert calls[0].headers["Authorization"] == "Bearer private-operator-token"


@pytest.mark.parametrize("path", ["https://other.example.com/api/admin/evaluation/sessions/x/messages", "/api/config",
    f"/api/admin/evaluation/sessions/{SESSION_ID}/messages?cursor=&limit=30&attendee=other",
    f"/api/admin/evaluation/sessions/{SESSION_ID}/messages?cursor=../x&limit=30"])
def test_operator_fetch_rejects_any_other_path_before_authentication(path):
    async def main():
        def forbidden_auth():
            raise AssertionError("bad endpoint must not authenticate")
        fetch = runner.OperatorFetcher(SimpleNamespace(config=SimpleNamespace(authenticate=forbidden_auth)), "https://terminal.example.com")
        with pytest.raises(runner.GateError):
            await fetch(path)
    asyncio.run(main())


def new_app(client, *, creator, marker=True, name="orders", state="SUCCEEDED"):
    app = copy.deepcopy(client.app)
    app.name = "wt-eval-cli-1-" + name if marker else name
    app.id = "new-app-id"
    app.creator = creator
    app.create_time = datetime.now(timezone.utc).isoformat()
    app.url = "https://orders-123.aws.databricksapps.com"
    app.active_deployment.deployment_id = "new-deploy-id"
    app.active_deployment.create_time = app.create_time
    app.active_deployment.status.state = state
    app.active_deployment.source_code_path = "/Workspace/Users/operator/projects/orders"
    client.app_by_name[app.name] = app
    client.inventory.append(app)
    return app


@pytest.mark.parametrize("creator_kind,marker,expected", [("attendee", True, "deployment_observed"),
                                                          ("unique_sp", False, "deployment_observed"),
                                                          ("attendee", False, "needs_review"),
                                                          ("other", True, "awaiting")])
def test_inventory_diff_attributes_only_fresh_owned_apps_and_keeps_generic_attendee_ambiguous(creator_kind, marker, expected):
    receipt, files, now = fixture()
    client = Client(receipt, files, now)
    discovery = runner.AppDiscovery(client, runner.validate_receipt(receipt, now=now))
    discovery.start()
    creator = {"attendee": client.me.user_name, "unique_sp": APP_ID, "other": "someone@example.com"}[creator_kind]
    new_app(client, creator=creator, marker=marker)
    observation = discovery.observe()
    assert observation.status == expected
    if expected == "deployment_observed":
        assert "accepted" not in discovery.evidence
        assert discovery.evidence["candidates"][0]["namespace_marker_observed"] == marker
        assert observation.artifacts == ("workspace-app:new-app-id",)


def test_preexisting_and_in_progress_apps_cannot_be_observed_as_new_deployments():
    receipt, files, now = fixture()
    client = Client(receipt, files, now)
    new_app(client, creator=APP_ID, marker=False)
    discovery = runner.AppDiscovery(client, runner.validate_receipt(receipt, now=now))
    discovery.start()
    assert discovery.observe().status == "awaiting"
    second = new_app(client, creator=APP_ID, marker=True, name="second", state="IN_PROGRESS")
    second.id = "second-app-id"
    assert discovery.observe().status == "awaiting"


@pytest.mark.parametrize("mutation,reason", [("source", "new_app_deployment_unverified"),
                                              ("active", "new_app_changed_during_observation")])
def test_app_discovery_rechecks_authoritative_source_and_current_active_identity(mutation, reason):
    receipt, files, now = fixture()
    client = Client(receipt, files, now)
    discovery = runner.AppDiscovery(client, runner.validate_receipt(receipt, now=now))
    discovery.start()
    app = new_app(client, creator=client.me.user_name, marker=False)
    app.active_deployment.source_code_path = "/Workspace/Shared/wt-eval-cli-1/orders"
    original_get = client.get_deployment
    def changed(name, deployment_id):
        deployed = copy.deepcopy(original_get(name, deployment_id))
        if mutation == "source":
            deployed.source_code_path = "/Workspace/Shared/unrelated/orders"
        else:
            app.active_deployment = SimpleNamespace(deployment_id="replacement")
        return deployed
    client.apps.get_deployment = changed
    with pytest.raises(runner.GateError, match=reason):
        discovery.observe()


@pytest.mark.parametrize("injected_client", [True, False])
def test_execution_wires_normal_browser_identity_and_keeps_completed_probe_unverified(tmp_path, monkeypatch, injected_client):
    receipt, files, now = fixture()
    binding = runner.validate_receipt(receipt, now=now)
    client = Client(receipt, files, now)
    auth = tmp_path / "private-auth.json"
    auth.write_text('{"cookies": [{"value": "private-cookie"}]}')
    browser_calls = []
    class Browser:
        def __init__(self, state, **kwargs):
            browser_calls.append((state, kwargs))
            self.page = object()
        async def __aenter__(self): return self
        async def __aexit__(self, *_args): pass
    class Fetch:
        requests = []
        def __init__(self, sdk, url):
            assert sdk is client and url == binding["app"]["url"]
        async def __aenter__(self): return self
        async def __aexit__(self, *_args): pass
    async def journey(driver, launch, simulator, fetch, config, **kwargs):
        assert launch.expected_attendee_email == "operator@example.com"
        assert launch.opening_message == simulator.opening().text
        assert config.mode == "consultation_probe" and kwargs["app_observer"] is None
        assert not hasattr(driver.page, "request")
        return JourneyResult(mode=config.mode, status="completed_probe", cleanup={"status": "closed_owned_ui_session"})
    monkeypatch.setattr(runner, "AuthenticatedBrowser", Browser)
    monkeypatch.setattr(runner, "OperatorFetcher", Fetch)
    monkeypatch.setattr(runner, "run_journey", journey)
    event_loop_thread = threading.get_ident()
    def make_client(profile):
        assert profile == binding["profile"]
        assert threading.get_ident() != event_loop_thread
        return client
    monkeypatch.setattr(runner, "workspace_client", make_client)
    args = SimpleNamespace(mode="consultation_probe", wt_browser_state=str(auth), show_browser=False, agent="claude",
                           entry_path="wizard_disabled", industry=None, allow_industry_step=False, output=str(tmp_path / "report.json"))
    report = {}
    budget = {"total_seconds": 120, "consultation_seconds": 60, "token_ceiling": 100000, "spend_ceiling_usd": 5}
    asyncio.run(runner.execute(args, binding, budget, report, client=client if injected_client else None))
    assert report["verdict"] == "unverified" and report["accepted"] is False
    assert report["journey"]["accepted"] is False
    assert browser_calls == [(str(auth), {"headless": True})]
    assert "private-cookie" not in json.dumps(report) and str(auth) not in json.dumps(report)


@pytest.mark.parametrize("identity,url,reason", [("other@example.com", "https://terminal.example.com", "browser_identity_unverified"),
    ("operator@example.com", "https://login.example.com/?secret=password", "terminal_origin_unverified")])
def test_terminal_capture_requires_observed_identity_and_exact_origin(tmp_path, identity, url, reason):
    result = JourneyResult(mode="consultation_probe", entry={"identity": identity})
    page = SimpleNamespace(url=url)
    binding = {"attendee": {"email": "operator@example.com"}, "app": {"url": "https://terminal.example.com"}}
    evidence = asyncio.run(runner.capture_wt_page(page, result, binding, tmp_path / "result.json"))
    assert evidence == {"status": "skipped", "reason": reason}
    assert not runner.wt_artifact_dir(tmp_path / "result.json").exists()


@pytest.mark.parametrize("aria_failure", [False, True])
def test_identified_terminal_capture_keeps_rendered_evidence_and_partial_failure(tmp_path, aria_failure):
    class Page:
        url = "https://terminal.example.com/"
        async def screenshot(self, *, path, **_kwargs):
            Path(path).write_bytes(b"synthetic screenshot")
        def locator(self, name):
            assert name == "body"
            return self
        async def aria_snapshot(self, **_kwargs):
            if aria_failure:
                raise RuntimeError("private error body must not enter evidence")
            return "- dialog: Tell us what you want to build\n- button: Retail"
    binding = {"attendee": {"email": "operator@example.com"}, "app": {"url": "https://terminal.example.com"}}
    result = JourneyResult(mode="consultation_probe", status="failed", stop_reason="industry_required",
                           entry={"identity": "operator@example.com"})
    output = tmp_path / "result.json"
    evidence = asyncio.run(runner.capture_wt_page(Page(), result, binding, output))
    assert evidence["status"] == ("capture_failed" if aria_failure else "captured")
    assert Path(evidence["artifacts"]["screenshot"]).read_bytes() == b"synthetic screenshot"
    if not aria_failure:
        assert "Retail" in Path(evidence["artifacts"]["accessibility_snapshot"]).read_text()
    assert "private error body" not in json.dumps(evidence)
    assert result.status == "failed" and result.stop_reason == "industry_required"


def test_browser_startup_crossing_expiry_cannot_launch_journey_and_closes_partial_browser(tmp_path, monkeypatch):
    receipt, files, now = fixture()
    binding = runner.validate_receipt(receipt, now=now)
    client = Client(receipt, files, now)
    closes = []
    class SlowBrowser:
        page = object()
        def __init__(self, *_args, **_kwargs): pass
        async def __aenter__(self):
            binding["expires_at"] = datetime.now(timezone.utc).timestamp() - 1
            return self
        async def __aexit__(self, *_args): closes.append(True)
    async def forbidden_journey(*_args, **_kwargs):
        raise AssertionError("expired startup must not enter WT")
    monkeypatch.setattr(runner, "AuthenticatedBrowser", SlowBrowser)
    monkeypatch.setattr(runner, "run_journey", forbidden_journey)
    args = SimpleNamespace(mode="consultation_probe", wt_browser_state="private-auth.json", show_browser=False, agent="claude",
                           entry_path="skip_wizard", industry=None, allow_industry_step=False)
    budget = {"total_seconds": 120, "consultation_seconds": 60, "token_ceiling": 100000, "spend_ceiling_usd": 5}
    with pytest.raises(runner.GateError, match="evaluation_expired_before_browser_entry"):
        asyncio.run(runner.execute(args, binding, budget, {}, client=client))
    assert closes == [True]


def test_discovery_baseline_crossing_expiry_blocks_before_browser_startup(monkeypatch):
    receipt, files, now = fixture()
    binding = runner.validate_receipt(receipt, now=now)
    client = Client(receipt, files, now)
    def expired_start(_self):
        binding["expires_at"] = datetime.now(timezone.utc).timestamp() - 1
    def forbidden_browser(*_args, **_kwargs):
        raise AssertionError("expired baseline must not start browser")
    monkeypatch.setattr(runner.AppDiscovery, "start", expired_start)
    monkeypatch.setattr(runner, "AuthenticatedBrowser", forbidden_browser)
    args = SimpleNamespace(mode="build", wt_browser_state="private-auth.json", show_browser=False)
    budget = {"total_seconds": 120, "consultation_seconds": 60, "token_ceiling": 100000, "spend_ceiling_usd": 5}
    with pytest.raises(runner.GateError, match="evaluation_expired_before_browser_entry"):
        asyncio.run(runner.execute(args, binding, budget, {}, client=client))


class JourneyDriver:
    def __init__(self):
        self.evidence = BrowserEntryEvidence()
        self._agents = [{"id": "claude", "label": "Claude Code"}]
        self.closes = []
    async def enter(self, launch):
        self.evidence = BrowserEntryEvidence(identity=launch.expected_attendee_email, session_id=SESSION_ID,
                                             agent_id="claude", opening_submitted=True)
        return self.evidence
    async def close_launched_session(self, label, **kwargs):
        self.closes.append((label, kwargs))
    async def submit_reply(self, _text):
        pass


class StartupSocket:
    def __init__(self, url):
        self.url, self.handlers = url, {}
    def on(self, name, handler):
        self.handlers[name] = handler
    def output(self, text):
        self.handlers["framereceived"](json.dumps({"t": "output", "data": text}))


class StartupPage:
    def __init__(self, url):
        self.url, self.inputs, self.handlers, self.on_enter, self.on_focus, self.on_text = url, [], {}, None, None, None
        self.keyboard = self
    def on(self, name, handler):
        self.handlers[name] = handler
    def locator(self, _selector):
        return self
    async def wait_for(self, **_kwargs):
        pass
    async def focus(self, **_kwargs):
        if self.on_focus:
            self.on_focus()
    async def screenshot(self, path, **_kwargs):
        Path(path).write_bytes(b"synthetic screenshot fixture")
    async def insert_text(self, text):
        self.inputs.append(("text", text))
        if self.on_text:
            self.on_text()
    async def press(self, key):
        self.inputs.append(("key", key))
        if self.on_enter:
            self.on_enter()


def startup_driver(tmp_path, *, agent="claude"):
    pytest.importorskip("pyte", reason="Controlled startup evaluation requires the documented optional local renderer")
    receipt, _files, now = fixture()
    binding = runner.validate_receipt(receipt, now=now)
    page = StartupPage(binding["app"]["url"])
    driver = runner.StartupQualifiedDriver(page, binding=binding, output=tmp_path / "journey.json")
    driver.evidence = BrowserEntryEvidence(identity=binding["attendee"]["email"], session_id=SESSION_ID, agent_id=agent)
    driver.harness.session_ids.add(SESSION_ID)
    driver._deadline = driver._run_deadline = runner.time.monotonic() + 10
    socket = StartupSocket("wss://" + urlsplit(binding["app"]["url"]).netloc + "/ws/sessions/" + SESSION_ID)
    driver._startup_socket(socket)
    socket.handlers["framesent"](json.dumps({"t": "resize", "cols": 150, "rows": 40}))
    return driver, page, socket


def trust_screen(cwd, *, selected=True):
    return "\r\n".join(["Do you trust the files in this folder?", cwd,
                          ("❯ " if selected else "  ") + "1. Yes, I trust this folder",
                          "2. No, exit", "Enter to confirm · Esc to cancel"])


def native_screen(agent="claude", *, pin=None):
    artifacts = json.loads((ROOT / "assets/artifacts/manifest.json").read_text())["artifacts"]
    banner = ("Claude Code v" + (pin or artifacts["claude_binary"]["version"]) + "\r\n❯ " if agent == "claude"
              else "OpenAI Codex (v" + artifacts["codex_npm_launcher_package"]["version"] + ")\r\n› ")
    return "\x1b[2J\x1b[H" + banner


def test_claude_empty_prompt_placeholder_is_not_confused_with_typed_input(tmp_path):
    driver, _page, _socket = startup_driver(tmp_path)
    screen = native_screen().rstrip() + '\u00a0Try "how does <filepath> work?"'
    assert driver._native_prompt(screen)
    assert not driver._native_prompt(native_screen().rstrip() + " already typed request")
    assert not driver._native_prompt(screen + "\r\nRequire approval")


def test_current_wrapped_question_matches_complete_authored_words():
    screen = '│ Which orders need attention —\n│ and where do they come from?'
    assert runner.StartupQualifiedDriver._screen_text(screen) == 'Which orders need attention — and where do they come from?'


def current_trust_screen(cwd, *, selected_yes=False):
    return "\x1b[2J\x1b[H" + "\r\n".join(["Accessing workspace:", cwd,
        "Quick safety check: Is this a project you created or one you trust?",
        ("  " if selected_yes else "❯ ") + "No, exit",
        ("❯ " if selected_yes else "  ") + "Yes, I trust this folder",
        "Enter to confirm · Esc to cancel"])


@pytest.mark.parametrize("pin", ["2.1.283", "2.1.295"])
def test_current_claude_default_no_is_selected_then_independently_verified_before_confirmation(tmp_path, pin):
    async def run():
        driver, page, socket = startup_driver(tmp_path)
        driver._startup_binding["environment"]["CLAUDE_CODE_VERSION"] = pin
        socket.output(current_trust_screen(driver._expected_cwd))
        def update():
            if page.inputs[-1] == ("key", "ArrowDown"):
                socket.output(current_trust_screen(driver._expected_cwd, selected_yes=True))
            else:
                socket.output(native_screen(pin=pin))
        page.on_enter = update
        await driver.submit_reply("Original simple novice opening")
        assert page.inputs == [("key", "ArrowDown"), ("key", "Enter"),
                               ("text", "Original simple novice opening"), ("key", "Enter")]
        assert driver.startup_evidence["native_prompt_verified"]
        assert [action["kind"] for action in driver.startup_evidence["actions"]] == [
            "select_owned_folder_trust", "selected_owned_folder_trust"]
    asyncio.run(run())


@pytest.mark.parametrize("changed", ["different_folder", "different_pin", "unknown_selection"])
def test_current_trust_navigation_rejects_unowned_or_unqualified_choices(tmp_path, changed):
    driver, _page, _socket = startup_driver(tmp_path)
    screen = current_trust_screen(driver._expected_cwd)
    if changed == "different_folder":screen = current_trust_screen("/app/another-user/projects")
    elif changed == "different_pin":driver._startup_binding["environment"]["CLAUDE_CODE_VERSION"] = "2.1.999"
    else:screen = screen.replace("❯ No, exit", "❯ Allow all tools")
    assert not driver._current_owned_trust(screen, selected_yes=False)


def test_confirmed_ui_entry_failure_cannot_be_a_builder_quality_verdict():
    result = JourneyResult(mode="build", status="failed", stop_reason="startup_native_prompt_unverified",
        entry={"opening_submitted": False}, cleanup={"status": "closed_owned_ui_session"})
    assessment = runner.assess_journey_outcome(result)
    assert assessment["verdict"] == "unverified" and assessment["reason"] == "browser_entry_unverified"


@pytest.mark.parametrize("multiple", [False, True])
def test_native_question_adapter_reviews_exact_free_text_before_submitting(tmp_path, multiple):
    async def run():
        driver, page, socket = startup_driver(tmp_path)
        driver._startup_complete = True
        question = {"question": "Which orders need attention?", "header": "Attention", "multiSelect": multiple,
            "options": [{"label": "Late", "description": "Late orders"}, {"label": "Unpaid", "description": "Unpaid orders"}]}
        interaction = {"kind": "claude_ask_user_question", "tool_use_id": "tool_1", "questions": [question]}
        answer = "Late orders that haven't been packed yet."
        state = {"selected": 1, "answer": "", "review": False, "submitted": False, "returns": 0}
        def render():
            if state["submitted"]:text = native_screen()
            elif state["review"]:
                text = "\r\n".join(["Review your answers", question["question"], state["answer"],
                    "Ready to submit your answers?", "❯ 1. Submit answers", "2. Cancel"])
            else:
                lines = [question["question"], "Attention"]
                for index, label in enumerate(["Late", "Unpaid", state["answer"] or "Type something."]):
                    lines.append(("❯ " if state["selected"] == index + 1 else "  ") + str(index + 1) + ". " + label)
                lines.extend(["Submit", "Enter to select · ↑/↓ to navigate"])
                text = "\r\n".join(lines)
            socket.output("\x1b[2J\x1b[H" + text)
        def key():
            value = page.inputs[-1][1]
            if value == "ArrowDown":state["selected"] += 1
            elif value == "Enter" and not state["answer"]:
                pytest.fail("Enter on the empty native inline editor would cancel the question")
            elif value == "Enter" and state["review"]:state["submitted"] = True
            elif value == "Enter" and state["answer"]:
                state["returns"] += 1
                if not multiple or state["selected"] == 4:state["review"] = True
            render()
        page.on_enter = key
        page.on_text = lambda: (state.update(answer=page.inputs[-1][1]), render())
        render()
        await driver.submit_question_answers(interaction, [answer])
        assert state["submitted"] and [item for item in page.inputs if item[0] == "text"] == [("text", answer)]
        assert driver.startup_evidence["question_answers"][0]["review_verified"]
        assert driver.startup_evidence["captures"][-1]["label"] == "native-question-answer-review"
    asyncio.run(run())


def test_qualified_startup_accepts_only_owned_selected_trust_then_original_opening(tmp_path):
    async def run():
        driver, page, socket = startup_driver(tmp_path)
        socket.output(trust_screen(driver._expected_cwd))
        page.on_enter = lambda: socket.output(native_screen())
        await driver.submit_reply("Original simple novice opening")
        assert page.inputs == [("key", "Enter"), ("text", "Original simple novice opening"), ("key", "Enter")]
        assert driver.evidence.harness_readiness_verified and driver.startup_evidence["native_prompt_verified"]
        assert driver.startup_evidence["actions"] == [{"kind": "selected_owned_folder_trust", "key": "Enter", "session_id": SESSION_ID, "cwd": driver._expected_cwd}]
        assert len(driver.startup_evidence["captures"]) == 2
        assert driver.startup_evidence["user_scope_consent_inferred"] is False and driver.startup_evidence["assistant_turns_inferred"] is False
        for capture in driver.startup_evidence["captures"]:
            assert hashlib.sha256(Path(capture["screenshot"]).read_bytes()).hexdigest() == capture["screenshot_sha256"]
            assert Path(capture["screen_text"]).stat().st_mode & 0o077 == 0
    asyncio.run(run())


@pytest.mark.parametrize("agent", ["claude", "codex"])
def test_qualified_startup_waits_for_pinned_native_prompt_without_extra_keys(tmp_path, agent):
    async def run():
        driver, page, socket = startup_driver(tmp_path, agent=agent)
        socket.output(native_screen(agent))
        await driver.submit_reply("Original simple novice opening")
        assert page.inputs == [("text", "Original simple novice opening"), ("key", "Enter")]
        assert driver.startup_evidence["status"] == "qualified" and driver.startup_evidence["actions"] == []
    asyncio.run(run())


@pytest.mark.parametrize("kind", ["other_folder", "selection_unverified", "unknown_permission", "unsupported_pin"])
def test_qualified_startup_never_types_into_unrecognized_or_unowned_startup(tmp_path, kind):
    async def run():
        driver, page, socket = startup_driver(tmp_path)
        if kind == "other_folder":
            screen = trust_screen("/app/another-user/projects")
        elif kind == "selection_unverified":
            screen = trust_screen(driver._expected_cwd, selected=False)
        elif kind == "unknown_permission":
            screen = "Claude Code v2.1.237\r\nRequire approval\r\nEnter to confirm\r\n❯ "
        else:
            screen = "Claude Code v2.1.999\r\n❯ "
        socket.output(screen)
        driver._deadline = runner.time.monotonic() + .02
        with pytest.raises(runner.BrowserJourneyError, match="recognized pinned native prompt"):
            await driver.submit_reply("Original simple novice opening")
        assert page.inputs == [] and not driver.evidence.harness_readiness_verified
        assert driver.startup_evidence["status"] == "unverified"
    asyncio.run(run())


def test_qualified_startup_is_bound_to_exact_owned_session(tmp_path):
    async def run():
        driver, page, socket = startup_driver(tmp_path)
        socket.output(trust_screen(driver._expected_cwd))
        driver.harness.session_ids.add("33333333-3333-4333-8333-333333333333")
        with pytest.raises(runner.BrowserJourneyError, match="sole receipt-bound attendee session"):
            await driver.submit_reply("Original simple novice opening")
        assert page.inputs == []
    asyncio.run(run())


def test_qualified_startup_uses_current_rendered_screen_instead_of_old_trust_scrollback(tmp_path):
    driver, _page, socket = startup_driver(tmp_path)
    socket.output(trust_screen(driver._expected_cwd))
    assert driver._selected_owned_trust(driver._startup_screen())
    socket.output(native_screen())
    assert not driver._selected_owned_trust(driver._startup_screen()) and driver._native_prompt(driver._startup_screen())


def test_qualified_startup_rejects_wizard_before_touching_browser(tmp_path):
    async def run():
        driver, page, _socket = startup_driver(tmp_path)
        launch = BrowserLaunchConfig(wt_url=page.url, agent_id="claude", opening_message="A simple opening",
                                    expected_attendee_email=driver.evidence.identity, entry_path="wizard")
        with pytest.raises(runner.GateError, match="startup_qualification_requires_skip_wizard"):
            await driver.enter(launch)
        assert page.inputs == []
    asyncio.run(run())


def test_qualified_startup_missing_optional_renderer_remains_unverified(tmp_path, monkeypatch):
    monkeypatch.setitem(runner.sys.modules, "pyte", None)
    receipt, _files, now = fixture()
    binding = runner.validate_receipt(receipt, now=now)
    page = StartupPage(binding["app"]["url"])
    with pytest.raises(runner.GateError, match="startup_screen_renderer_unavailable"):
        runner.StartupQualifiedDriver(page, binding=binding, output=tmp_path / "journey.json")
    assert page.inputs == []


@pytest.mark.parametrize("stage", ["trust", "native"])
def test_qualified_startup_rechecks_screen_after_focus_before_any_input(tmp_path, stage):
    async def run():
        driver, page, socket = startup_driver(tmp_path)
        socket.output(trust_screen(driver._expected_cwd) if stage == "trust" else native_screen())
        page.on_focus = lambda: socket.output("\x1b[2J\x1b[HClaude Code v2.1.237\r\nRequire approval\r\nEnter to confirm\r\n❯ ")
        with pytest.raises(runner.BrowserJourneyError, match="changed before"):
            await driver.submit_reply("Original simple novice opening")
        assert page.inputs == [] and not driver.evidence.harness_readiness_verified
        assert driver.startup_evidence["status"] == "unverified" and not driver.startup_evidence["native_prompt_verified"]
        assert driver._startup_complete is False
    asyncio.run(run())


def test_qualified_startup_keeps_observing_until_enter_and_preserves_partial_input(tmp_path):
    async def run():
        driver, page, socket = startup_driver(tmp_path)
        socket.output(native_screen())
        page.on_text = lambda: socket.output("\x1b[2J\x1b[HClaude Code v2.1.237\r\nRequire approval\r\nEnter to confirm\r\n❯ ")
        with pytest.raises(runner.BrowserJourneyError, match="confirmation appeared"):
            await driver.submit_reply("Original simple novice opening")
        assert page.inputs == [("text", "Original simple novice opening")]
        assert not driver.evidence.harness_readiness_verified and not driver._startup_complete
        assert driver.startup_evidence["reason"] == "startup_confirmation_before_submit"
    asyncio.run(run())


def test_operator_403_is_an_unverified_observation_with_specific_code_and_owned_cleanup():
    async def run():
        client = SimpleNamespace(config=SimpleNamespace(authenticate=lambda: {"Authorization": "Bearer private-token"}))
        http = httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(403)))
        scenario = load_simulator_scenario()
        simulator = NoviceSimulator(scenario)
        driver = JourneyDriver()
        launch = BrowserLaunchConfig(wt_url="https://terminal.example.com", agent_id="claude", entry_path="wizard_disabled",
                                    opening_message=scenario.message, expected_attendee_email="operator@example.com")
        async with runner.OperatorFetcher(client, launch.wt_url, http_client=http) as fetch:
            result = await run_journey(driver, launch, simulator, fetch, JourneyConfig(total_seconds=2, consultation_seconds=1))
        assert result.status == "unverified" and result.stop_reason == "operator_observation_http_unverified"
        assert driver.closes and "journey_operation_GateError" not in str(result.to_dict())
    asyncio.run(run())


def test_discovery_sdk_failure_is_not_a_failed_builder_consultation():
    async def run():
        receipt, files, now = fixture()
        client = Client(receipt, files, now)
        def denied(**_kwargs):
            raise RuntimeError("Bearer private-remote-error-token")
        client.apps.list = denied
        discovery = runner.AppDiscovery(client, runner.validate_receipt(receipt, now=now))
        scenario = load_simulator_scenario()
        simulator = NoviceSimulator(scenario)
        async def fetch(_path):
            return {"schema_version": 1, "instrumentation": "read_only_native_transcript_v1", "status": "ready",
                    "binding_verified": True, "harness_id": "claude", "harness_version": "2.1.237", "wt_session_id": SESSION_ID,
                    "native_session_id": APP_ID, "messages": [{"message_id": hashlib.sha256(b"opening").hexdigest(),
                        "timestamp": now.isoformat(), "role": "user", "visibility": "user", "complete": True, "text": scenario.message}],
                    "next_cursor": "cursor1", "tool_and_worker_coverage_verified": False, "transcript_authenticity_verified": False,
                    "unverified_checks": ["tool_and_worker_attribution", "implementation_timing", "native_transcript_authenticity"]}
        driver = JourneyDriver()
        launch = BrowserLaunchConfig(wt_url="https://terminal.example.com", agent_id="claude", entry_path="wizard_disabled",
                                    opening_message=scenario.message, expected_attendee_email="operator@example.com")
        result = await run_journey(driver, launch, simulator, fetch, JourneyConfig(mode="build", total_seconds=2, consultation_seconds=1),
                                   app_observer=discovery)
        assert result.status == "unverified" and result.stop_reason == "app_discovery_collection_unverified"
        assert discovery.evidence["collection_error_type"] == "RuntimeError"
        assert "private-remote-error-token" not in json.dumps(result.to_dict())
        assert driver.closes
    asyncio.run(run())


def test_operator_authentication_exception_is_bounded_private_observation_failure():
    async def run():
        def denied():
            raise RuntimeError("Bearer private-profile-token")
        fetch = runner.OperatorFetcher(SimpleNamespace(config=SimpleNamespace(authenticate=denied)), "https://terminal.example.com")
        with pytest.raises(NativeEvidenceError, match="operator_observation_collection_unverified"):
            await fetch(f"/api/admin/evaluation/sessions/{SESSION_ID}/messages?cursor=&limit=30")
        assert fetch.collection_failure == {"reason": "operator_observation_collection_unverified", "error_type": "RuntimeError"}
        assert "private-profile-token" not in json.dumps(fetch.collection_failure)
    asyncio.run(run())


@pytest.mark.parametrize("reason", ["no_material_consultation_within_budget", "consultation_scope_not_agreed", "total_deadline", "operation_timeout"])
def test_missing_complete_native_assistant_coverage_cannot_become_builder_quality_failure(reason):
    result = JourneyResult(mode="consultation_probe", status="failed", stop_reason=reason,
                           cleanup={"status": "closed_owned_ui_session"})
    assessment = runner.assess_journey_outcome(result)
    assert assessment["verdict"] == "unverified"
    assert assessment["reason"] == ("operation_timeout_unverified" if reason == "operation_timeout" else "native_assistant_coverage_unverified")
    assert assessment["operational_status"] == "failed" and assessment["operational_stop_reason"] == reason
    assert assessment["quality_failure_inferred_from_silence"] is False


def test_observed_native_question_and_opening_correlation_keep_real_deadline_failure():
    result = JourneyResult(mode="consultation_probe", status="failed", stop_reason="no_material_consultation_within_budget",
                           first_complete_assistant={"text": "Which framework should I use?"},
                           deliveries=[{"decision": "opening", "native_correlated": True}], cleanup={"status": "closed_owned_ui_session"})
    assessment = runner.assess_journey_outcome(result)
    assert assessment["complete_assistant_text_available"] and assessment["verdict"] == "failed"
    assert assessment["native_format_qualification_verified"] is False


def test_passive_app_observation_keeps_failed_consultation_separate_from_app_acceptance():
    result = JourneyResult(
        mode="build", status="unverified", stop_reason="deployment_before_correlated_scope_agreement",
        first_complete_assistant={"text": "I'll build it now."},
        deliveries=[{"decision": "opening", "native_correlated": True}],
        consultation_status="failed", consultation_stop_reason="no_material_consultation_within_budget",
        consultation_closed_elapsed_seconds=180, consultation_deadline_enforced=True,
        no_question_before_scope_agreement_observed=True,
        app_observations=[{"status": "deployment_observed", "app_url": "https://new-app.example"}],
        cleanup={"status": "closed_owned_ui_session"},
    )
    assessment = runner.assess_journey_outcome(result)
    assert assessment["verdict"] == "unverified" and assessment["complete_assistant_text_available"]
    assert assessment["consultation"] == {
        "verdict": "failed", "reason": "no_material_consultation_within_budget",
        "closed_elapsed_seconds": 180, "deadline_enforced": True,
        "scope_agreement_delivered": False, "no_question_before_scope_agreement_observed": True,
    }
    assert result.app_observations[0]["app_url"] == "https://new-app.example" and not result.accepted


@pytest.mark.parametrize("stalled_collector", ["operator_fetch", "app_observer"])
def test_collector_stall_after_valid_native_assistant_remains_unverified(stalled_collector):
    async def run():
        scenario = load_simulator_scenario()
        driver = JourneyDriver()
        now = datetime.now(timezone.utc).isoformat()
        calls = []
        async def fetch(_path):
            calls.append(True)
            if len(calls) > 1:
                await asyncio.Event().wait()
            return {"schema_version": 1, "instrumentation": "read_only_native_transcript_v1", "status": "ready",
                    "binding_verified": True, "harness_id": "claude", "harness_version": "2.1.237", "wt_session_id": SESSION_ID,
                    "native_session_id": APP_ID, "messages": [
                        {"message_id": hashlib.sha256(b"opening").hexdigest(), "timestamp": now, "role": "user",
                         "visibility": "user", "complete": True, "text": scenario.message},
                        {"message_id": hashlib.sha256(b"question").hexdigest(), "timestamp": now, "role": "assistant",
                         "visibility": "user", "complete": True, "text": "Who will use this page?"}],
                    "next_cursor": "cursor1", "tool_and_worker_coverage_verified": False, "transcript_authenticity_verified": False,
                    "unverified_checks": ["tool_and_worker_attribution", "implementation_timing", "native_transcript_authenticity"]}
        async def observer(_snapshot):
            await asyncio.Event().wait()
        launch = BrowserLaunchConfig(wt_url="https://terminal.example.com", agent_id="claude", entry_path="wizard_disabled",
                                    opening_message=scenario.message, expected_attendee_email="operator@example.com")
        result = await run_journey(driver, launch, NoviceSimulator(scenario), fetch,
                JourneyConfig(mode="build", total_seconds=2, consultation_seconds=1,
                              poll_interval_seconds=.001, operation_timeout_seconds=.02),
                app_observer=observer if stalled_collector == "app_observer" else None)
        assessment = runner.assess_journey_outcome(result)
        assert result.status == "failed" and result.stop_reason == "operation_timeout"
        assert assessment["complete_assistant_text_available"] and assessment["verdict"] == "unverified"
        assert assessment["reason"] == "operation_timeout_unverified"
        assert assessment["operational_status"] == "failed" and assessment["operational_stop_reason"] == "operation_timeout"
        assert driver.closes
    asyncio.run(run())


def test_cleanup_failure_is_preserved_even_when_native_coverage_is_unknown():
    result = JourneyResult(mode="consultation_probe", status="failed", stop_reason="operation_timeout", cleanup={"status": "failed"})
    assert runner.assess_journey_outcome(result)["verdict"] == "failed"
