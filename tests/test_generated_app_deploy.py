"""Standalone Labs deployment boundaries, with SDK-shaped synthetic clients."""

import copy
import io
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from databricks.sdk.errors import NotFound, ResourceExhausted
from databricks.sdk.service.apps import (
    App, AppAccessControlRequest, AppAccessControlResponse, AppDeployment,
    AppPermission, AppPermissionLevel, AppPermissions,
)
from databricks.sdk.service.iam import Group
from databricks.sdk.service.catalog import CatalogInfo, GetPermissionsResponse, Privilege, PrivilegeAssignment

from evals.generated_apps.adapters.simulated_control_tower import plan_simulation
from scripts import deploy_generated_app_test as runner


ROOT = Path(__file__).parents[1]
NOW = datetime(2026, 10, 8, 5, 0, tzinfo=timezone.utc)


def plan(*, attendee_email="operator@example.com", attendee_mode="operator_bound"):
    return plan_simulation({
        "marker": "wt-eval-deploy-1", "disposable": True,
        "workspace_host": "https://labs.example.com", "profile": "labs",
        "attendee_email": attendee_email, "attendee_mode": attendee_mode,
        "ttl_seconds": 3600, "cost_budget_usd": 10,
        "release": {"source_kind": "runtime-snapshot", "source_digest": "a" * 64,
                    "parent_git_sha": "b" * 40, "instrumentation": True,
                    "prompt_policy_unchanged_asserted": True},
        "harnesses": ["claude", "codex", "omnigent"], "evaluation_observation": True,
    }, json.loads((ROOT / "assets/artifacts/manifest.json").read_text()), now=NOW)


def sources():
    return {
        "app.yaml": (ROOT / "app.yaml").read_bytes(),
        "requirements.txt": b"fastapi==0.1\n",
        "server/main.py": b"# app\n", "static/index.html": b"<html></html>",
        "content/default_pack.json": b"{}",
        "assets/artifacts/manifest.json": (ROOT / "assets/artifacts/manifest.json").read_bytes(),
    }


def write_tree(root, files):
    for name, content in files.items():
        destination = root / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(content)


class FakeClient:
    """All operations are in memory; no SDK transport or profile is created."""

    def __init__(self, deployment_plan, *, existing=None, fail=None, scopes=None, operator_email=None):
        self.plan = deployment_plan
        self.existing = existing
        self.fail = fail
        self.calls = []
        self.created = False
        self.uploads = {}
        self.config = SimpleNamespace(host=deployment_plan["workspace_host"])
        self.me = SimpleNamespace(id="456", user_name=operator_email or deployment_plan["attendee"]["email"])
        self.app = App(
            name=deployment_plan["names"]["app_name"], id="app-123",
            service_principal_id=123, service_principal_client_id="11111111-1111-1111-1111-111111111111",
            user_api_scopes=list(deployment_plan["app_resource"]["user_api_scopes"]) if scopes is None else scopes,
            url="https://fresh-wt.example.com",
        )
        self.app_permissions = AppPermissions(access_control_list=[
            AppAccessControlResponse(user_name=self.me.user_name, all_permissions=[
                AppPermission(permission_level=AppPermissionLevel.CAN_MANAGE, inherited=False),
            ]),
            AppAccessControlResponse(group_name="admins", all_permissions=[
                AppPermission(permission_level=AppPermissionLevel.CAN_MANAGE, inherited=True,
                              inherited_from_object=["/apps"]),
            ]),
        ], object_id="/apps/" + self.app.name, object_type="apps")
        self.current_user = SimpleNamespace(me=lambda: self.me)
        self.apps = SimpleNamespace(get=self.get_app, create=self.create_app, deploy=self.deploy_app,
                                    get_permissions=self.get_app_permissions,
                                    update_permissions=self.update_app_permissions)
        self.catalogs = SimpleNamespace(get=self.get_catalog)
        self.workspace = SimpleNamespace(get_status=self.get_source, mkdirs=self.mkdirs, upload=self.upload,
                                         download=self.download)
        self.groups = SimpleNamespace(list=self.list_groups, create=self.create_group)

    def get_app(self, name):
        self.calls.append(("app_get", name))
        if self.created:
            if self.fail == "identity_get":
                raise RuntimeError("Identity read failed after create")
            return copy.deepcopy(self.app)
        if self.existing == "app":
            return self.app
        raise NotFound("missing")

    def get_catalog(self, name):
        self.calls.append(("catalog_get", name))
        if self.existing == "catalog":
            return SimpleNamespace(name=name)
        raise NotFound("missing")

    def get_source(self, path):
        self.calls.append(("source_get", path))
        if self.existing == "source":
            return SimpleNamespace(path=path)
        raise NotFound("missing")

    def list_groups(self, **kwargs):
        self.calls.append(("group_list", kwargs))
        return [Group(id="old-group")] if self.existing == "group" else []

    def create_app(self, app, **kwargs):
        self.calls.append(("app_create", app.as_dict(), kwargs))
        self.created = True
        if self.fail == "create_response":
            raise RuntimeError("App may exist but creation response was lost")
        def cannot_wait(*_args, **_kwargs):
            raise AssertionError("create must not invoke an unbounded ACTIVE waiter")
        return SimpleNamespace(response=copy.deepcopy(self.app), result=cannot_wait)

    def create_group(self, **kwargs):
        self.calls.append(("group_create", kwargs))
        if self.fail == "group_response":
            raise RuntimeError("Group response lost")
        return Group(id="new-group", display_name=kwargs["display_name"], members=kwargs["members"])

    def get_app_permissions(self, app_name):
        self.calls.append(("app_get_permissions", app_name))
        assert app_name == self.app.name
        return copy.deepcopy(self.app_permissions)

    def update_app_permissions(self, app_name, *, access_control_list):
        self.calls.append(("app_update_permissions", app_name,
                           [entry.as_dict() for entry in access_control_list]))
        assert app_name == self.app.name
        assert all(isinstance(entry, AppAccessControlRequest) for entry in access_control_list)
        if self.fail == "acl_update":
            raise RuntimeError("App permission response lost")
        for entry in access_control_list:
            assert entry.user_name and not entry.group_name and not entry.service_principal_name
            self.app_permissions.access_control_list = [
                existing for existing in self.app_permissions.access_control_list
                if existing.user_name != entry.user_name
            ] + [AppAccessControlResponse(user_name=entry.user_name, all_permissions=[
                AppPermission(permission_level=entry.permission_level, inherited=False),
            ])]
        return copy.deepcopy(self.app_permissions)

    def mkdirs(self, path):
        self.calls.append(("mkdirs", path))
        if self.fail == "mkdirs":
            raise RuntimeError("Source directory creation response lost")

    def upload(self, path, stream, **kwargs):
        self.calls.append(("upload", path, kwargs))
        if self.fail == "upload":
            raise RuntimeError("Upload failed")
        self.uploads[path] = stream.read()

    def download(self, path):
        self.calls.append(("download", path))
        if path not in self.uploads:
            raise NotFound("missing")
        return io.BytesIO(self.uploads[path])

    def deploy_app(self, **kwargs):
        self.calls.append(("deploy", kwargs))
        if self.fail == "deployment":
            raise RuntimeError("Deployment response lost")
        return SimpleNamespace(response=AppDeployment(deployment_id="deployment-123", source_code_path=kwargs["app_deployment"].source_code_path))


def mock_catalog(monkeypatch, client, *, success=True):
    # Individual upload-pacing tests use an advancing deterministic clock.
    # General deployment tests must not spend real seconds on upload delays.
    monkeypatch.setattr(runner.time, "sleep", lambda _seconds: None)
    def provisioning(w, deployment_plan, app_resource, operator, receipt, receipt_path):
        assert w is client
        assert operator is client.me
        client.calls.append(("catalog_provision", deployment_plan["names"]["catalog"],
                             deployment_plan["attendee"]["email"], app_resource["service_principal_client_id"]))
        return success
    monkeypatch.setattr(runner, "provision_isolated_catalog", provisioning)


def resource_receipt(path, kind):
    receipt = json.loads(path.read_text())
    return next((item for item in receipt.get("created_resources", []) if item["kind"] == kind), None)


def test_source_bundle_excludes_private_eval_facts_and_runtime_caches(tmp_path):
    files = sources() | {
        "evals/generated_apps/private_facts.json": b"private-facts",
        "tests/private-user.json": b"private-test",
        "docs/evidence/captured.json": b"private-evidence",
        "scripts/example.py": b"script",
        "server/.env": b"private-token",
        "server/__pycache__/main.pyc": b"bytecode",
        ".git/config": b"credential",
    }
    write_tree(tmp_path, files)
    bundle = runner.runtime_sources(tmp_path)
    assert bundle == sources()
    assert b"private-facts" not in b"".join(bundle.values())


def test_runtime_source_symlink_is_refused(tmp_path):
    write_tree(tmp_path, sources())
    private = tmp_path / "private.json"
    private.write_bytes(b"secret")
    (tmp_path / "server/leak.json").symlink_to(private)
    with pytest.raises(ValueError, match="links"):
        runner.runtime_sources(tmp_path)


def test_runtime_source_hardlink_is_refused(tmp_path):
    write_tree(tmp_path, sources())
    (tmp_path / "server/hardlink.py").hardlink_to(tmp_path / "server/main.py")
    with pytest.raises(ValueError, match="links"):
        runner.runtime_sources(tmp_path)


def test_source_manifest_is_content_bound_and_independent_of_dictionary_order():
    a = {"server/a.py": b"a", "server/b.py": b"b"}
    assert runner.source_manifest(a) == runner.source_manifest(dict(reversed(list(a.items()))))
    assert runner.source_manifest(a)["digest"] != runner.source_manifest(a | {"server/a.py": b"changed"})["digest"]


def test_yaml_patch_blanks_ct_delivery_and_pat_without_copying_private_source():
    original = sources()["app.yaml"]
    patched = runner.app_yaml(original, plan()["environment"] | {
        "CONTROL_TOWER_URL": "https://event.example.com", "CONTROL_TOWER_INGEST_TOKEN": "unsafe",
    })
    env = {item["name"]: item.get("value") for item in yaml.safe_load(patched)["env"]}
    assert env["CONTROL_TOWER_URL"] == env["CONTROL_TOWER_INGEST_URL"] == env["CONTROL_TOWER_INGEST_TOKEN"] == ""
    assert env["WORKSHOP_PAT"] == ""
    assert env["ADMIN_GROUP"] == "wt-eval-deploy-1-operators"
    assert env["WORKSHOP_ATTENDEE_EMAIL"] == "operator@example.com"
    assert original == sources()["app.yaml"]


def test_yaml_patch_removes_valuefrom_when_setting_bound_runtime_value():
    original = yaml.safe_dump({"command": ["uvicorn"], "env": [
        {"name": "WORKSHOP_APP_SP_ID", "valueFrom": "old-resource"},
    ]}).encode()
    patched = yaml.safe_load(runner.app_yaml(original, {"WORKSHOP_APP_SP_ID": "123"}))
    entry = next(item for item in patched["env"] if item["name"] == "WORKSHOP_APP_SP_ID")
    assert entry == {"name": "WORKSHOP_APP_SP_ID", "value": "123"}


@pytest.mark.parametrize("existing", ["app", "catalog", "source", "group"])
def test_existing_resources_are_refused_before_any_mutation(tmp_path, monkeypatch, existing):
    value = plan()
    client = FakeClient(value, existing=existing)
    mock_catalog(monkeypatch, client)
    with pytest.raises(ValueError, match="existing|exists|fresh"):
        runner.deploy(value, sources(), str(tmp_path / "receipt.json"), client=client)
    assert not any(call[0] in {"app_create", "group_create", "catalog_provision", "mkdirs", "upload", "deploy"}
                   for call in client.calls)


def test_host_and_operator_identity_mismatches_fail_before_mutation(tmp_path):
    value = plan()
    client = FakeClient(value)
    client.config.host = "https://neighbor.example.com"
    with pytest.raises(ValueError, match="host"):
        runner.deploy(value, sources(), str(tmp_path / "receipt.json"), client=client)
    assert client.calls == []
    client.config.host = value["workspace_host"]
    client.me.user_name = "someone-else@example.com"
    with pytest.raises(ValueError, match="identity"):
        runner.deploy(value, sources(), str(tmp_path / "receipt.json"), client=client)
    assert client.calls == []


def test_fresh_create_starts_compute_and_uses_response_not_active_compute_waiter(tmp_path, monkeypatch):
    value = plan()
    client = FakeClient(value)
    mock_catalog(monkeypatch, client)
    receipt = runner.deploy(value, sources(), str(tmp_path / "receipt.json"), client=client)
    assert receipt["scope"] == "simulated_control_tower"
    assert receipt["control_tower_requests"] == 0
    assert receipt["status"] == "deployment_submitted"
    assert receipt["accepted"] is False
    app_call = next(call for call in client.calls if call[0] == "app_create")
    assert app_call[2] == {"no_compute": False}
    assert app_call[1]["user_api_scopes"] == value["app_resource"]["user_api_scopes"]
    group_call = next(call for call in client.calls if call[0] == "group_create")
    assert group_call[1]["display_name"] == value["names"]["admin_group"]
    assert {member.value for member in group_call[1]["members"]} == {"123", "456"}
    assert [call for call in client.calls if call[0] == "catalog_provision"][0][1:] == (
        value["names"]["catalog"], "operator@example.com", client.app.service_principal_client_id,
    )
    env = {item["name"]: item.get("value") for item in yaml.safe_load(
        client.uploads[value["names"]["source_path"] + "/app.yaml"]
    )["env"]}
    assert env["WORKSHOP_APP_SP_ID"] == "123"
    assert env["CONTROL_TOWER_URL"] == ""
    assert all(call[2]["overwrite"] is False for call in client.calls if call[0] == "upload")
    assert receipt["deployment"]["deployment_id"] == "deployment-123"


def test_synthetic_attendee_gets_only_use_access_without_changing_operator_or_admin_grants(tmp_path, monkeypatch):
    value = plan(attendee_email="labuser+1@example.com", attendee_mode="synthetic_attendee")
    client = FakeClient(value, operator_email="operator@example.com")
    original_acl = copy.deepcopy(client.app_permissions.access_control_list)
    mock_catalog(monkeypatch, client)
    receipt = runner.deploy(value, sources(), str(tmp_path / "receipt.json"), client=client)
    updates = [call for call in client.calls if call[0] == "app_update_permissions"]
    assert updates == [("app_update_permissions", value["names"]["app_name"], [
        {"user_name": "labuser+1@example.com", "permission_level": "CAN_USE"},
    ])]
    assert client.app_permissions.access_control_list[:2] == original_acl
    granted = client.app_permissions.access_control_list[2]
    assert granted.user_name == value["attendee"]["email"]
    assert granted.all_permissions == [AppPermission(permission_level=AppPermissionLevel.CAN_USE, inherited=False)]
    assert receipt["attendee_app_access"] == {
        "app_name": value["names"]["app_name"], "app_id": "app-123",
        "attendee_email": "labuser+1@example.com", "minimum_permission": "CAN_USE",
        "update_required": True, "state": "independently_verified", "effective_permission_verified": True,
    }
    update_position = client.calls.index(updates[0])
    assert client.calls[update_position - 1][0] == "app_get"
    assert client.calls[update_position + 1][0] == "app_get"
    assert client.calls[update_position + 2][0] == "app_get_permissions"
    assert update_position < next(i for i, call in enumerate(client.calls) if call[0] == "group_create")
    group_call = next(call for call in client.calls if call[0] == "group_create")
    assert {member.value for member in group_call[1]["members"]} == {"123", "456"}


def test_operator_bound_attendee_retains_manage_access_with_independent_readback(tmp_path, monkeypatch):
    value = plan()
    client = FakeClient(value)
    before = copy.deepcopy(client.app_permissions)
    mock_catalog(monkeypatch, client)
    receipt = runner.deploy(value, sources(), str(tmp_path / "receipt.json"), client=client)
    assert not any(call[0] == "app_update_permissions" for call in client.calls)
    assert client.app_permissions == before
    assert len([call for call in client.calls if call[0] == "app_get_permissions"]) == 2
    assert receipt["attendee_app_access"]["state"] == "independently_verified"
    assert receipt["attendee_app_access"]["update_required"] is False


def test_grant_response_is_not_enough_without_independent_exact_attendee_readback(tmp_path, monkeypatch):
    value = plan(attendee_email="labuser+1@example.com", attendee_mode="synthetic_attendee")
    client = FakeClient(value, operator_email="operator@example.com")
    before = copy.deepcopy(client.app_permissions)
    update = client.apps.update_permissions
    def unpersisted_update(*args, **kwargs):
        response = update(*args, **kwargs)
        client.app_permissions = before
        return response
    client.apps.update_permissions = unpersisted_update
    mock_catalog(monkeypatch, client)
    clock = fake_identity_clock(monkeypatch)
    destination = tmp_path / "receipt.json"
    with pytest.raises(TimeoutError, match="CAN_USE"):
        runner.deploy(value, sources(), str(destination), client=client)
    receipt = json.loads(destination.read_text())
    assert receipt["attendee_app_access"]["state"] == "readback_pending"
    assert "effective_permission_verified" not in receipt["attendee_app_access"]
    assert receipt["created_resources"] == [resource_receipt(destination, "app")]
    assert sum(clock["sleeps"]) == 60
    assert not any(call[0] in {"group_create", "catalog_provision", "mkdirs", "upload", "deploy"}
                   for call in client.calls)


def test_attendee_acl_readback_can_converge_without_repeated_updates(tmp_path, monkeypatch):
    value = plan(attendee_email="labuser+1@example.com", attendee_mode="synthetic_attendee")
    client = FakeClient(value, operator_email="operator@example.com")
    before = copy.deepcopy(client.app_permissions)
    read = client.apps.get_permissions
    reads = 0
    def eventually_visible(app_name):
        nonlocal reads
        reads += 1
        result = read(app_name)
        return before if reads <= 2 else result
    client.apps.get_permissions = eventually_visible
    client.created = True
    receipt = identity_receipt(value)
    clock = fake_identity_clock(monkeypatch)
    runner.grant_attendee_app_access(client, value, receipt["created_resources"][0], receipt,
                                    str(tmp_path / "receipt.json"))
    assert receipt["attendee_app_access"]["state"] == "independently_verified"
    assert clock["sleeps"] == [2]
    assert len([call for call in client.calls if call[0] == "app_update_permissions"]) == 1


def test_app_identity_change_between_permission_read_and_patch_prevents_grant(tmp_path):
    value = plan(attendee_email="labuser+1@example.com", attendee_mode="synthetic_attendee")
    client = FakeClient(value, operator_email="operator@example.com")
    client.created = True
    receipt = identity_receipt(value)
    read = client.apps.get_permissions
    def replaced_after_read(app_name):
        permissions = read(app_name)
        client.app.id = "replacement-app"
        return permissions
    client.apps.get_permissions = replaced_after_read
    destination = tmp_path / "receipt.json"
    with pytest.raises(ValueError, match="exact creation receipt"):
        runner.grant_attendee_app_access(client, value, receipt["created_resources"][0], receipt, str(destination))
    assert not any(call[0] == "app_update_permissions" for call in client.calls)
    assert json.loads(destination.read_text())["attendee_app_access"]["state"] == "grant_requested"


def test_recreated_app_after_patch_cannot_be_reported_as_verified(tmp_path):
    value = plan(attendee_email="labuser+1@example.com", attendee_mode="synthetic_attendee")
    client = FakeClient(value, operator_email="operator@example.com")
    client.created = True
    receipt = identity_receipt(value)
    update = client.apps.update_permissions
    def replaced_after_patch(*args, **kwargs):
        response = update(*args, **kwargs)
        client.app.id = "replacement-app"
        return response
    client.apps.update_permissions = replaced_after_patch
    destination = tmp_path / "receipt.json"
    with pytest.raises(ValueError, match="exact creation receipt"):
        runner.grant_attendee_app_access(client, value, receipt["created_resources"][0], receipt, str(destination))
    assert json.loads(destination.read_text())["attendee_app_access"]["state"] == "readback_pending"


@pytest.mark.parametrize("timeout", [0, 61, True, float("inf")])
def test_attendee_permission_verification_deadline_cannot_be_unbounded(tmp_path, timeout):
    value = plan()
    client = FakeClient(value)
    receipt = identity_receipt(value)
    with pytest.raises(ValueError, match="bounded"):
        runner.grant_attendee_app_access(client, value, receipt["created_resources"][0], receipt,
                                        str(tmp_path / "receipt.json"), timeout_seconds=timeout)
    assert client.calls == []


def test_permission_update_failure_preserves_app_ownership_and_blocks_later_mutations(tmp_path, monkeypatch):
    value = plan(attendee_email="labuser+1@example.com", attendee_mode="synthetic_attendee")
    client = FakeClient(value, operator_email="operator@example.com", fail="acl_update")
    mock_catalog(monkeypatch, client)
    destination = tmp_path / "receipt.json"
    with pytest.raises(RuntimeError, match="permission response"):
        runner.deploy(value, sources(), str(destination), client=client)
    receipt = json.loads(destination.read_text())
    assert receipt["created_resources"] == [resource_receipt(destination, "app")]
    assert receipt["created_resources"][0]["id"] == "app-123"
    assert receipt["attendee_app_access"]["state"] == "grant_requested"
    assert not any(call[0] in {"group_create", "catalog_provision", "mkdirs", "upload", "deploy"}
                   for call in client.calls)


def test_changed_obo_scopes_block_before_group_catalog_or_source_mutations(tmp_path, monkeypatch):
    value = plan()
    client = FakeClient(value, scopes=["sql"])
    mock_catalog(monkeypatch, client)
    fake_identity_clock(monkeypatch)
    with pytest.raises(TimeoutError, match="OBO scopes"):
        runner.deploy(value, sources(), str(tmp_path / "receipt.json"), client=client)
    assert resource_receipt(tmp_path / "receipt.json", "app") is not None
    assert not any(call[0] in {"group_create", "catalog_provision", "mkdirs"} for call in client.calls)


@pytest.mark.parametrize("fail,kind", [
    ("identity_get", "app"), ("create_response", "app"),
    ("group_response", "group"), ("mkdirs", "workspace_source"),
    ("upload", "workspace_source"), ("deployment", "workspace_source"),
])
def test_mutation_failure_preserves_exact_attempted_resource_for_cleanup(tmp_path, monkeypatch, fail, kind):
    value = plan()
    client = FakeClient(value, fail=fail)
    mock_catalog(monkeypatch, client)
    destination = tmp_path / "receipt.json"
    with pytest.raises(RuntimeError):
        runner.deploy(value, sources(), str(destination), client=client)
    resource = resource_receipt(destination, kind)
    assert resource is not None
    if kind == "workspace_source":
        assert resource["path"] == value["names"]["source_path"]
    else:
        assert resource["name"] == value["names"]["app_name" if kind == "app" else "admin_group"]


def test_catalog_grant_failure_does_not_lose_possibly_created_catalog_receipt(tmp_path, monkeypatch):
    value = plan()
    client = FakeClient(value)
    mock_catalog(monkeypatch, client, success=False)
    destination = tmp_path / "receipt.json"
    with pytest.raises(RuntimeError, match="catalog"):
        runner.deploy(value, sources(), str(destination), client=client)
    resource = resource_receipt(destination, "catalog")
    assert resource is not None
    assert resource["name"] == value["names"]["catalog"]


def catalog_fixture(tmp_path, monkeypatch, *, attendee_mode="synthetic_attendee", drop_operator=False,
                    drop_operator_after_transfer=False, existing=False):
    attendee = "labuser+1@example.com" if attendee_mode == "synthetic_attendee" else "operator@example.com"
    value = plan(attendee_email=attendee, attendee_mode=attendee_mode)
    client = FakeClient(value, operator_email="operator@example.com")
    client.created = True
    app_resource = {"kind": "app", "name": client.app.name, "id": client.app.id, "state": "created",
                    "service_principal_id": client.app.service_principal_id,
                    "service_principal_client_id": client.app.service_principal_client_id}
    receipt = {"scope": "simulated_control_tower", "control_tower_requests": 0,
               "created_resources": [app_resource, {"kind": "catalog", "name": value["names"]["catalog"],
                                                     "state": "creation_requested"}]}
    destination = tmp_path / "receipt.json"
    state = {"exists": existing, "owner": client.me.user_name, "transferred": False,
             "grants": {}, "sql": [], "grant_reads": []}
    catalog = value["names"]["catalog"]
    def get_catalog(name):
        assert name == catalog
        if not state["exists"]:
            raise NotFound("missing")
        return CatalogInfo(name=name, owner=state["owner"], created_by=client.me.user_name)
    def get_grants(*, securable_type, full_name):
        assert securable_type == "catalog" and full_name == catalog
        state["grant_reads"].append({"after_sql": len(state["sql"]), "transferred": state["transferred"]})
        grants = copy.deepcopy(state["grants"])
        if drop_operator or drop_operator_after_transfer and state["transferred"]:
            grants[client.me.user_name] = set()
        return GetPermissionsResponse(privilege_assignments=[
            PrivilegeAssignment(principal=principal, privileges=list(privileges))
            for principal, privileges in grants.items()
        ])
    def execute(w, warehouse_id, statement):
        assert w is client and warehouse_id == "warehouse-test"
        on_disk = json.loads(destination.read_text())
        assert on_disk["catalog_provisioning"]["steps"][-1]["state"] == "requested"
        state["sql"].append(statement)
        if statement.startswith("CREATE CATALOG "):
            assert "IF NOT EXISTS" not in statement and not state["exists"]
            state["exists"] = True
        elif statement.startswith("ALTER CATALOG "):
            assert on_disk["catalog_operator_access"]["verified_before_owner_transfer"] is True
            state.update(owner=attendee, transferred=True)
        else:
            target = statement.rsplit(" TO ", 1)[1]
            if statement.startswith("GRANT ALL PRIVILEGES "):
                privilege_set = {Privilege.ALL_PRIVILEGES}
            elif statement.startswith("GRANT MANAGE, USE CATALOG, CREATE SCHEMA "):
                privilege_set = {Privilege.MANAGE, Privilege.USE_CATALOG, Privilege.CREATE_SCHEMA}
            else:
                assert statement.startswith("GRANT MANAGE ")
                privilege_set = {Privilege.MANAGE}
            principals = (client.me.user_name, attendee, client.app.service_principal_client_id)
            matching = [principal for principal in principals if target == runner._quoted(principal)]
            assert matching
            state["grants"].setdefault(matching[0], set()).update(privilege_set)
        return SimpleNamespace(statement_id=f"statement-{len(state['sql'])}")
    client.catalogs.get = get_catalog
    client.grants = SimpleNamespace(get=get_grants)
    monkeypatch.setattr(runner, "_pick_warehouse", lambda w: "warehouse-test" if w is client else None)
    monkeypatch.setattr(runner, "_sql", execute)
    return value, client, app_resource, receipt, destination, state


@pytest.mark.parametrize("attendee_mode", ["synthetic_attendee", "operator_bound"])
def test_fresh_isolated_catalog_retains_verified_operator_grants_before_and_after_owner_transfer(tmp_path, monkeypatch, attendee_mode):
    value, client, app_resource, receipt, destination, state = catalog_fixture(
        tmp_path, monkeypatch, attendee_mode=attendee_mode)
    assert runner.provision_isolated_catalog(client, value, app_resource, client.me, receipt, str(destination))
    cat, principal = runner._quoted(value["names"]["catalog"]), runner._quoted(client.me.user_name)
    assert state["sql"] == [
        f"CREATE CATALOG {cat} COMMENT 'Disposable Workshop Terminal E2E catalog'",
        f"GRANT ALL PRIVILEGES ON CATALOG {cat} TO {principal}",
        f"GRANT MANAGE ON CATALOG {cat} TO {principal}",
        *runner._catalog_sql_plan(value["names"]["catalog"], value["attendee"]["email"],
                                  client.app.service_principal_client_id, create=False),
    ]
    assert state["grant_reads"][0] == {"after_sql": 3, "transferred": False}
    assert state["grant_reads"][-1] == {"after_sql": 7, "transferred": True}
    assert state["owner"] == value["attendee"]["email"]
    assert {Privilege.ALL_PRIVILEGES, Privilege.MANAGE} <= state["grants"][client.me.user_name]
    assert receipt["catalog_operator_access"] == {
        "catalog": value["names"]["catalog"], "principal": client.me.user_name, "identity_id": "456",
        "privileges": ["ALL_PRIVILEGES", "MANAGE"], "state": "independently_verified",
        "verified_before_owner_transfer": True, "verified_after_owner_transfer": True,
    }
    assert receipt["catalog_provisioning"]["status"] == "created_and_grants_verified"
    assert all(step["state"] == "succeeded" and step["statement_id"] for step in receipt["catalog_provisioning"]["steps"])
    assert all("METASTORE" not in statement and cat in statement for statement in state["sql"])


def test_operator_cleanup_grant_readback_failure_stops_before_any_attendee_handoff(tmp_path, monkeypatch):
    value, client, app_resource, receipt, destination, state = catalog_fixture(tmp_path, monkeypatch, drop_operator=True)
    with pytest.raises(RuntimeError, match="cleanup grants failed independent readback"):
        runner.provision_isolated_catalog(client, value, app_resource, client.me, receipt, str(destination))
    assert len(state["sql"]) == 3 and not state["transferred"]
    assert state["owner"] == client.me.user_name
    failed = json.loads(destination.read_text())
    assert failed["catalog_provisioning"]["status"] == "failed"
    assert failed["catalog_operator_access"]["verified_before_owner_transfer"] is False
    assert failed["created_resources"][1]["name"] == value["names"]["catalog"]


def test_retained_operator_grants_are_checked_independently_after_attendee_becomes_owner(tmp_path, monkeypatch):
    value, client, app_resource, receipt, destination, state = catalog_fixture(
        tmp_path, monkeypatch, drop_operator_after_transfer=True)
    with pytest.raises(RuntimeError, match="cleanup grants failed independent readback"):
        runner.provision_isolated_catalog(client, value, app_resource, client.me, receipt, str(destination))
    assert state["transferred"] and state["owner"] == value["attendee"]["email"]
    assert receipt["catalog_operator_access"]["verified_before_owner_transfer"] is True
    assert receipt["catalog_operator_access"]["verified_after_owner_transfer"] is False
    assert receipt["catalog_provisioning"]["status"] == "failed"


@pytest.mark.parametrize("mutation", ["operator_id", "operator_name", "app_id", "app_sp", "shared_catalog", "workspace_host", "ownership"])
def test_catalog_operator_grants_cannot_escape_current_identity_or_exact_isolated_resource(tmp_path, monkeypatch, mutation):
    value, client, app_resource, receipt, destination, state = catalog_fixture(tmp_path, monkeypatch)
    operator = copy.deepcopy(client.me)
    if mutation == "operator_id": operator.id = "999"
    elif mutation == "operator_name": operator.user_name = "unrelated@example.com"
    elif mutation == "app_id": client.app.id = "different-app"
    elif mutation == "app_sp": client.app.service_principal_client_id = "unrelated-principal"
    elif mutation == "shared_catalog":
        value["names"]["catalog"] = receipt["created_resources"][1]["name"] = "shared_catalog"
    elif mutation == "workspace_host": client.config.host = "https://other.example.com"
    else: receipt["created_resources"][1]["state"] = "created"
    with pytest.raises(ValueError):
        runner.provision_isolated_catalog(client, value, app_resource, operator, receipt, str(destination))
    assert not state["sql"] and "catalog_provisioning" not in receipt


def test_preexisting_catalog_is_never_granted_or_adopted_by_standalone_provisioner(tmp_path, monkeypatch):
    value, client, app_resource, receipt, destination, state = catalog_fixture(tmp_path, monkeypatch, existing=True)
    with pytest.raises(ValueError, match="fresh resource"):
        runner.provision_isolated_catalog(client, value, app_resource, client.me, receipt, str(destination))
    assert not state["sql"] and "catalog_operator_access" not in receipt


def test_direct_deploy_rechecks_upload_allowlist_before_cloud_mutation(tmp_path, monkeypatch):
    value = plan()
    client = FakeClient(value)
    mock_catalog(monkeypatch, client)
    files = sources() | {"evals/generated_apps/private.json": b"private-user-facts"}
    with pytest.raises(ValueError, match="source|allowlist|bundle|runtime"):
        runner.deploy(value, files, str(tmp_path / "receipt.json"), client=client)
    assert not any(call[0] in {"app_create", "group_create", "mkdirs"} for call in client.calls)


def test_receipt_destination_failure_prevents_resource_creation(tmp_path, monkeypatch):
    value = plan()
    client = FakeClient(value)
    mock_catalog(monkeypatch, client)
    def no_write(*args, **kwargs):
        raise PermissionError("Receipt cannot be written")
    monkeypatch.setattr(runner, "write_evidence", no_write)
    with pytest.raises(PermissionError):
        runner.deploy(value, sources(), str(tmp_path / "receipt.json"), client=client)
    assert not any(call[0] in {"app_create", "group_create", "mkdirs"} for call in client.calls)


def test_missing_reviewed_manifest_cannot_form_runtime_bundle(tmp_path):
    files = sources()
    del files["assets/artifacts/manifest.json"]
    write_tree(tmp_path, files)
    with pytest.raises(ValueError, match="incomplete"):
        runner.runtime_sources(tmp_path)


def cli_spec():
    value = plan()
    return {
        "marker": value["marker"], "disposable": True,
        "workspace_host": value["workspace_host"], "profile": "labs",
        "attendee_email": value["attendee"]["email"], "attendee_mode": "operator_bound",
        "ttl_seconds": 3600, "cost_budget_usd": 10,
        "release": copy.deepcopy(value["release"]),
        "harnesses": ["claude", "codex", "omnigent"], "evaluation_observation": True,
    }


def test_cli_will_not_overwrite_an_existing_cleanup_receipt(tmp_path, monkeypatch):
    input_path = tmp_path / "input.json"
    input_path.write_text(json.dumps(cli_spec()))
    output_path = tmp_path / "receipt.json"
    original = {"created_resources": [{"kind": "app", "name": "exact-previous-app"}], "status": "failed"}
    output_path.write_text(json.dumps(original))
    monkeypatch.setattr(runner, "runtime_sources", lambda root: sources())
    with pytest.raises((SystemExit, ValueError, FileExistsError)):
        runner.main(["--spec", str(input_path), "--output", str(output_path)])
    assert json.loads(output_path.read_text()) == original


def test_cli_input_and_output_cannot_alias(tmp_path):
    input_path = tmp_path / "input.json"
    original = json.dumps(cli_spec())
    input_path.write_text(original)
    with pytest.raises(SystemExit):
        runner.main(["--spec", str(input_path), "--output", str(input_path)])
    assert input_path.read_text() == original


def test_cli_exception_retains_deployments_exact_resource_receipt(tmp_path, monkeypatch):
    input_path = tmp_path / "input.json"
    input_path.write_text(json.dumps(cli_spec()))
    output_path = tmp_path / "receipt.json"
    monkeypatch.setattr(runner, "runtime_sources", lambda root: sources())
    monkeypatch.setattr(runner, "workspace_client", lambda _profile: object())
    def fail_after_creation(plan, files, receipt_path, **kwargs):
        runner.write_evidence(receipt_path, {
            "plan": plan, "scope": "simulated_control_tower", "control_tower_requests": 0,
            "created_resources": [{"kind": "app", "name": plan["names"]["app_name"], "id": "123"}],
        })
        raise RuntimeError("failed after creation")
    monkeypatch.setattr(runner, "deploy", fail_after_creation)
    assert runner.main(["--spec", str(input_path), "--output", str(output_path), "--execute"]) == 2
    result = json.loads(output_path.read_text())
    assert result["status"] == "failed"
    assert result["created_resources"] == [{"kind": "app", "name": "wt-eval-deploy-1-wt", "id": "123"}]
    assert result["control_tower_requests"] == 0


def test_uploaded_manifest_and_raw_source_identity_are_both_preserved(tmp_path, monkeypatch):
    value = plan()
    files = sources()
    value["release"]["source_digest"] = runner.source_manifest(files)["digest"]
    client = FakeClient(value)
    mock_catalog(monkeypatch, client)
    result = runner.deploy(value, files, str(tmp_path / "receipt.json"), client=client)
    assert result["source_identity"]["planned_unpatched_digest"] == runner.source_manifest(files)["digest"]
    assert result["source_identity"]["uploaded_runtime_digest"] == result["uploaded_source"]["digest"]
    assert result["source_identity"]["uploaded_runtime_digest"] != result["source_identity"]["planned_unpatched_digest"]
    uploaded_bytes = {path.removeprefix(value["names"]["source_path"] + "/"): content
                      for path, content in client.uploads.items()}
    assert runner.source_manifest(uploaded_bytes) == result["uploaded_source"]


def upload_fixture():
    value, files = plan(), sources()
    receipt = {"scope": "simulated_control_tower", "control_tower_requests": 0,
               "created_resources": [{"kind": "workspace_source", "path": value["names"]["source_path"],
                                      "state": "created"}], "uploaded_source": runner.source_manifest(files)}
    return value, files, receipt, FakeClient(value)


def test_source_uploads_are_paced_sequentially_and_persist_intent_before_each_request(tmp_path, monkeypatch):
    value, files, receipt, client = upload_fixture()
    clock = fake_identity_clock(monkeypatch)
    destination = tmp_path / "receipt.json"
    started = []
    upload = client.workspace.upload
    def inspect_intent(path, stream, **kwargs):
        on_disk = json.loads(destination.read_text())["source_upload_progress"]
        pending = on_disk["files"][-1]
        assert pending["state"] == "upload_requested" and pending["attempts"] == 1
        assert path == value["names"]["source_path"] + "/" + pending["path"]
        assert on_disk["uploaded_files"] == len(started)
        assert kwargs["overwrite"] is False
        started.append(clock["now"])
        upload(path, stream, **kwargs)
    client.workspace.upload = inspect_intent
    runner.upload_runtime_sources(client, value, files, receipt, str(destination))
    assert len(started) == len(files)
    assert [later - earlier for earlier, later in zip(started, started[1:])] == pytest.approx([1.1] * (len(files) - 1))
    progress = json.loads(destination.read_text())["source_upload_progress"]
    assert progress["status"] == "uploaded" and progress["uploaded_files"] == len(files)
    assert [record["path"] for record in progress["files"]] == sorted(files)
    assert all(record["state"] == "upload_acknowledged" and record["attempts"] == 1 for record in progress["files"])
    assert not any(call[0] == "download" for call in client.calls)


def test_throttled_upload_retries_only_after_independent_absence_readback(tmp_path, monkeypatch):
    value, files, receipt, client = upload_fixture()
    fake_identity_clock(monkeypatch)
    upload = client.workspace.upload
    attempts = []
    def throttled_once(path, stream, **kwargs):
        attempts.append(path)
        if len(attempts) == 1:
            raise ResourceExhausted("synthetic rate limit")
        assert any(call[0] == "download" and call[1] == attempts[0] for call in client.calls)
        upload(path, stream, **kwargs)
    client.workspace.upload = throttled_once
    runner.upload_runtime_sources(client, value, files, receipt, str(tmp_path / "receipt.json"))
    record = receipt["source_upload_progress"]["files"][0]
    assert record["attempts"] == 2 and record["readback_attempts"] == 1
    assert receipt["source_upload_progress"]["uploaded_files"] == len(files)
    assert attempts[0] == attempts[1] and len(attempts) == len(files) + 1
    assert all(call[2]["overwrite"] is False for call in client.calls if call[0] == "upload")


def test_lost_upload_response_is_accepted_only_after_exact_byte_readback(tmp_path, monkeypatch):
    from requests.exceptions import ConnectionError

    value, files, receipt, client = upload_fixture()
    fake_identity_clock(monkeypatch)
    upload = client.workspace.upload
    calls = []
    def lost_response_once(path, stream, **kwargs):
        calls.append(path)
        upload(path, stream, **kwargs)
        if len(calls) == 1:
            raise ConnectionError("private transport error body")
    client.workspace.upload = lost_response_once
    destination = tmp_path / "receipt.json"
    runner.upload_runtime_sources(client, value, files, receipt, str(destination))
    record = receipt["source_upload_progress"]["files"][0]
    assert record["state"] == "verified_after_ambiguous_response" and record["attempts"] == 1
    assert record["readback_attempts"] == 1 and len(calls) == len(files)
    assert "private transport error body" not in destination.read_text()


def test_ambiguous_upload_with_conflicting_bytes_never_overwrites_or_deploys(tmp_path, monkeypatch):
    value, files, receipt, client = upload_fixture()
    fake_identity_clock(monkeypatch)
    upload = client.workspace.upload
    def conflicting_response(path, stream, **kwargs):
        upload(path, stream, **kwargs)
        client.uploads[path] = b"conflicting bytes"
        raise TimeoutError("response lost")
    client.workspace.upload = conflicting_response
    destination = tmp_path / "receipt.json"
    with pytest.raises(ValueError, match="bytes differ"):
        runner.upload_runtime_sources(client, value, files, receipt, str(destination))
    progress = json.loads(destination.read_text())["source_upload_progress"]
    assert progress["status"] == "failed" and progress["uploaded_files"] == 0
    assert progress["files"][0]["state"] == "source_conflict"
    assert len([call for call in client.calls if call[0] == "upload"]) == 1
    assert next(iter(client.uploads.values())) == b"conflicting bytes"
    assert receipt["created_resources"][0]["path"] == value["names"]["source_path"]


def test_persistent_throttling_has_four_attempts_and_preserves_owned_progress(tmp_path, monkeypatch):
    value, files, receipt, client = upload_fixture()
    fake_identity_clock(monkeypatch)
    attempts = []
    def always_throttled(path, _stream, **kwargs):
        attempts.append(path)
        assert kwargs["overwrite"] is False
        raise ResourceExhausted("rate limited")
    client.workspace.upload = always_throttled
    destination = tmp_path / "receipt.json"
    with pytest.raises(ResourceExhausted):
        runner.upload_runtime_sources(client, value, files, receipt, str(destination))
    progress = json.loads(destination.read_text())["source_upload_progress"]
    assert len(attempts) == 4 and len(set(attempts)) == 1
    assert progress["status"] == "failed" and progress["files"][0]["attempts"] == 4
    assert progress["files"][0]["readback_attempts"] == 4 and not client.uploads


def test_readback_throttling_retries_reads_without_repeating_the_ambiguous_mutation(tmp_path, monkeypatch):
    from requests.exceptions import ConnectionError

    value, files, receipt, client = upload_fixture()
    fake_identity_clock(monkeypatch)
    upload, download = client.workspace.upload, client.workspace.download
    upload_calls, read_calls = [], []
    def lost_response(path, stream, **kwargs):
        upload_calls.append(path)
        upload(path, stream, **kwargs)
        if len(upload_calls) == 1:
            raise ConnectionError("response lost")
    def throttled_read(path):
        read_calls.append(path)
        if len(read_calls) == 1:
            raise ResourceExhausted("read rate limit")
        return download(path)
    client.workspace.upload, client.workspace.download = lost_response, throttled_read
    runner.upload_runtime_sources(client, value, files, receipt, str(tmp_path / "receipt.json"))
    assert len(upload_calls) == len(files) and len(read_calls) == 2
    assert upload_calls.count(read_calls[0]) == 1
    assert receipt["source_upload_progress"]["files"][0]["readback_attempts"] == 2


def test_upload_deadline_stops_before_later_file_mutation_and_preserves_partial_progress(tmp_path, monkeypatch):
    value, files, receipt, client = upload_fixture()
    clock = fake_identity_clock(monkeypatch)
    destination = tmp_path / "receipt.json"
    with pytest.raises(TimeoutError, match="phase deadline"):
        runner.upload_runtime_sources(client, value, files, receipt, str(destination), timeout_seconds=.5)
    progress = json.loads(destination.read_text())["source_upload_progress"]
    assert clock["now"] == .5 and len(client.uploads) == 1
    assert progress["status"] == "failed" and progress["uploaded_files"] == 1
    assert progress["files"][0]["state"] == "upload_acknowledged"
    assert progress["files"][1]["state"] == "pending"


@pytest.mark.parametrize("mutation", ["ownership_path", "uncreated_source", "manifest", "progress_already_present"])
def test_upload_never_reuses_unowned_or_previously_progressed_sources(tmp_path, mutation):
    value, files, receipt, client = upload_fixture()
    if mutation == "ownership_path":
        receipt["created_resources"][0]["path"] = "/Workspace/Shared/another-test"
    elif mutation == "uncreated_source":
        receipt["created_resources"][0]["state"] = "creation_requested"
    elif mutation == "manifest":
        receipt["uploaded_source"]["digest"] = "0" * 64
    else:
        receipt["source_upload_progress"] = {"uploaded_files": 1}
    with pytest.raises(ValueError, match="fresh owned source"):
        runner.upload_runtime_sources(client, value, files, receipt, str(tmp_path / "receipt.json"))
    assert not client.calls


@pytest.mark.parametrize("policy", [{"timeout_seconds": 0}, {"timeout_seconds": 901}, {"timeout_seconds": True},
                                    {"pace_seconds": 0}, {"pace_seconds": float("inf")},
                                    {"max_attempts": 5}, {"max_attempts": True}])
def test_upload_attempts_pacing_and_deadline_are_bounded_before_io(tmp_path, policy):
    value, files, receipt, client = upload_fixture()
    with pytest.raises(ValueError, match="bounded"):
        runner.upload_runtime_sources(client, value, files, receipt, str(tmp_path / "receipt.json"), **policy)
    assert not client.calls and "source_upload_progress" not in receipt


def test_real_installed_sdk_constructor_bounds_request_and_retry_timeouts_without_network(tmp_path, monkeypatch):
    import databricks.sdk.config as sdk_config
    from databricks.sdk import WorkspaceClient

    for name in list(os.environ):
        if name.startswith("DATABRICKS_"):
            monkeypatch.delenv(name)
    config_file = tmp_path / "synthetic-databrickscfg"
    config_file.write_text("[offline]\nhost = https://sdk-offline.example.invalid\ntoken = synthetic-offline-token\nauth_type = pat\n")
    monkeypatch.setenv("DATABRICKS_CONFIG_FILE", str(config_file))
    monkeypatch.setattr(sdk_config, "get_host_metadata", lambda _host: SimpleNamespace(
        account_id=None, workspace_id=None, oidc_endpoint=None, cloud=None,
        token_federation_default_oidc_audiences=[]))
    def forbidden_http(*_args, **_kwargs):
        raise AssertionError("Deployment constructor test must never contact a workspace")
    monkeypatch.setattr("requests.sessions.Session.request", forbidden_http)
    client = runner.workspace_client("offline")
    assert isinstance(client, WorkspaceClient)
    assert client.config.http_timeout_seconds == 10 and client.config.retry_timeout_seconds == 15
    assert client.config.authenticate() == {"Authorization": "Bearer synthetic-offline-token"}


def identity_receipt(value=None):
    value = value or plan()
    return {
        "schema_version": 1, "scope": "simulated_control_tower", "control_tower_requests": 0,
        "plan": copy.deepcopy(value), "accepted": False, "status": "failed",
        "created_resources": [{"kind": "app", "name": value["names"]["app_name"],
                               "id": "app-123", "state": "created", "service_principal_id": None,
                               "service_principal_client_id": None, "url": "Unavailable"}],
    }


def fake_identity_clock(monkeypatch):
    clock = {"now": 0, "sleeps": []}
    def advance(seconds):
        clock["sleeps"].append(seconds)
        clock["now"] += seconds
    # Scope the clock to the runner. Patching the shared stdlib time module
    # lets unrelated background threads advance this test's phase deadline.
    monkeypatch.setattr(runner, "time", SimpleNamespace(monotonic=lambda: clock["now"], sleep=advance))
    return clock


def test_identity_poll_waits_for_assigned_sp_and_url_before_binding(tmp_path, monkeypatch):
    value = plan()
    client = FakeClient(value)
    pending = copy.deepcopy(client.app)
    pending.service_principal_id = pending.service_principal_client_id = None
    pending.url = "Unavailable"
    values = iter([pending, client.app])
    client.apps.get = lambda name: next(values)
    receipt = identity_receipt(value)
    clock = fake_identity_clock(monkeypatch)
    app, bound = runner.await_app_identity(client, value, receipt["created_resources"][0], receipt,
                                          str(tmp_path / "receipt.json"), timeout_seconds=4)
    assert app.service_principal_id == 123
    assert bound["environment"]["WORKSHOP_APP_SP_ID"] == "123"
    assert clock["sleeps"] == [2]
    assert receipt["created_resources"][0]["state"] == "created"


def test_identity_poll_waits_for_scopes_that_arrive_after_the_sp(tmp_path, monkeypatch):
    value = plan()
    client = FakeClient(value)
    pending = copy.deepcopy(client.app)
    pending.user_api_scopes = []
    values = iter([pending, client.app])
    client.apps.get = lambda name: next(values)
    receipt = identity_receipt(value)
    clock = fake_identity_clock(monkeypatch)
    app, _bound = runner.await_app_identity(client, value, receipt["created_resources"][0], receipt,
                                            str(tmp_path / "receipt.json"), timeout_seconds=4)
    assert app.user_api_scopes == value["app_resource"]["user_api_scopes"]
    assert clock["sleeps"] == [2]
    assert not any(call[0] in {"group_create", "catalog_provision", "mkdirs"} for call in client.calls)


def test_identity_poll_has_bounded_deadline_and_preserves_pending_app_receipt(tmp_path, monkeypatch):
    value = plan()
    client = FakeClient(value)
    client.created = True
    client.app.service_principal_id = client.app.service_principal_client_id = None
    client.app.url = "Unavailable"
    receipt = identity_receipt(value)
    clock = fake_identity_clock(monkeypatch)
    destination = tmp_path / "receipt.json"
    with pytest.raises(TimeoutError):
        runner.await_app_identity(client, value, receipt["created_resources"][0], receipt,
                                  str(destination), timeout_seconds=4)
    assert sum(clock["sleeps"]) == 4
    assert len(client.calls) == 3
    assert resource_receipt(destination, "app")["id"] == "app-123"
    assert resource_receipt(destination, "app")["state"] == "identity_pending"
    assert "WORKSHOP_APP_SP_ID" not in receipt["plan"]["environment"]


def test_identity_poll_rejects_app_id_change_during_warmup(tmp_path, monkeypatch):
    value = plan()
    client = FakeClient(value)
    pending = copy.deepcopy(client.app)
    pending.service_principal_id = None
    pending.url = "Unavailable"
    unexpected = copy.deepcopy(client.app)
    unexpected.id = "other-app-id"
    values = iter([pending, unexpected])
    client.apps.get = lambda name: next(values)
    receipt = identity_receipt(value)
    fake_identity_clock(monkeypatch)
    with pytest.raises(ValueError):
        runner.await_app_identity(client, value, receipt["created_resources"][0], receipt,
                                  str(tmp_path / "receipt.json"), timeout_seconds=4)
    assert receipt["created_resources"][0]["id"] == "app-123"


@pytest.mark.parametrize("timeout", [0, 61, True, float("inf")])
def test_identity_poll_refuses_unbounded_or_invalid_deadline(tmp_path, timeout):
    value = plan()
    client = FakeClient(value)
    receipt = identity_receipt(value)
    with pytest.raises(ValueError):
        runner.await_app_identity(client, value, receipt["created_resources"][0], receipt,
                                  str(tmp_path / "receipt.json"), timeout_seconds=timeout)
    assert client.calls == []


def test_resume_only_reads_exact_existing_app_and_keeps_other_resources_fresh(tmp_path, monkeypatch):
    value = plan()
    client = FakeClient(value)
    client.created = True
    previous = identity_receipt(value)
    original = copy.deepcopy(previous)
    mock_catalog(monkeypatch, client)
    result = runner.deploy(value, sources(), str(tmp_path / "receipt.json"), client=client,
                           resume_receipt=previous)
    assert not any(call[0] == "app_create" for call in client.calls)
    assert result["resume_provenance"]["previous_app_id"] == "app-123"
    assert result["resume_provenance"]["app_identity_independently_verified"] is True
    assert result["created_resources"][0]["id"] == "app-123"
    assert result["status"] == "deployment_submitted"
    assert previous == original


@pytest.mark.parametrize("mutation", [
    "marker", "profile", "workspace_host", "names", "attendee", "bounds", "environment",
    "extra_resource", "missing_app_id", "already_uploaded", "already_bound", "deployment",
    "ct_scope", "release_policy",
])
def test_resume_receipt_cannot_reuse_an_unrelated_or_progressed_deployment(tmp_path, mutation):
    value = plan()
    previous = identity_receipt(value)
    if mutation in {"marker", "profile", "workspace_host", "names", "attendee", "bounds", "environment"}:
        previous["plan"][mutation] = "different"
    elif mutation == "extra_resource":
        previous["created_resources"].append({"kind": "catalog", "name": value["names"]["catalog"]})
    elif mutation == "missing_app_id":
        previous["created_resources"][0]["id"] = None
    elif mutation == "already_uploaded":
        previous["uploaded_source"] = {}
    elif mutation == "already_bound":
        previous["bound_plan"] = {}
    elif mutation == "deployment":
        previous["deployment"] = {}
    elif mutation == "release_policy":
        previous["plan"]["release"]["instrumentation"] = False
    else:
        previous["scope"] = "control_tower"
    client = FakeClient(value)
    client.created = True
    with pytest.raises(ValueError):
        runner.deploy(value, sources(), str(tmp_path / "receipt.json"), client=client,
                      resume_receipt=previous)
    assert client.calls == []


def test_resume_cannot_reuse_a_different_app_even_when_name_matches(tmp_path):
    value = plan()
    previous = identity_receipt(value)
    client = FakeClient(value)
    client.created = True
    client.app.id = "neighbor-app"
    with pytest.raises(ValueError):
        runner.deploy(value, sources(), str(tmp_path / "receipt.json"), client=client,
                      resume_receipt=previous)
    assert not any(call[0] == "app_create" for call in client.calls)


@pytest.mark.parametrize("existing", ["catalog", "group", "source"])
def test_resume_still_refuses_every_other_existing_resource(tmp_path, existing):
    value = plan()
    previous = identity_receipt(value)
    client = FakeClient(value, existing=existing)
    client.created = True
    destination = tmp_path / "receipt.json"
    with pytest.raises(ValueError):
        runner.deploy(value, sources(), str(destination), client=client, resume_receipt=previous)
    assert resource_receipt(destination, "app")["id"] == "app-123"
    assert not any(call[0] in {"app_create", "group_create", "catalog_provision", "mkdirs"}
                   for call in client.calls)


def test_cli_resume_preserves_original_expiry_and_original_ownership_file(tmp_path, monkeypatch):
    raw = cli_spec()
    value = plan_simulation(raw, json.loads((ROOT / "assets/artifacts/manifest.json").read_text()),
                            now=datetime.now(timezone.utc))
    previous = identity_receipt(value)
    input_path, previous_path, output_path = (tmp_path / name for name in ("input.json", "previous.json", "new.json"))
    input_path.write_text(json.dumps(raw))
    previous_path.write_text(json.dumps(previous))
    original = previous_path.read_bytes()
    client = FakeClient(value)
    client.created = True
    monkeypatch.setattr(runner, "runtime_sources", lambda root: sources())
    monkeypatch.setattr(runner, "workspace_client", lambda _profile: client)
    mock_catalog(monkeypatch, client)
    assert runner.main(["--spec", str(input_path), "--resume-receipt", str(previous_path),
                        "--output", str(output_path), "--execute"]) == 0
    assert json.loads(output_path.read_text())["plan"]["bounds"] == previous["plan"]["bounds"]
    assert previous_path.read_bytes() == original
    assert not any(call[0] == "app_create" for call in client.calls)


def test_cli_resume_refuses_expired_original_bounds_before_mutation(tmp_path, monkeypatch):
    input_path, previous_path, output_path = (tmp_path / name for name in ("input.json", "previous.json", "new.json"))
    input_path.write_text(json.dumps(cli_spec()))
    previous = identity_receipt()
    previous["plan"]["bounds"]["created_at"] = "2000-01-01T00:00:00Z"
    previous["plan"]["bounds"]["expires_at"] = "2000-01-01T01:00:00Z"
    previous_path.write_text(json.dumps(previous))
    def no_files(root):
        raise AssertionError("Expired deployment must be rejected before reading sources")
    monkeypatch.setattr(runner, "runtime_sources", no_files)
    with pytest.raises(SystemExit):
        runner.main(["--spec", str(input_path), "--resume-receipt", str(previous_path), "--output", str(output_path)])
    assert not output_path.exists()
