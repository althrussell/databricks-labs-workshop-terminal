"""Offline checks for the immutable CT package and isolated privilege contract."""
import copy
import hashlib
import io
import json
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from evals.generated_apps.ct_deployment import (
    verify_package, plan_package, package_sources, validate_policy, grant, sha256, PACKAGE_FILES, deploy_package,
    grant_demo_read_access,
)

ROOT = Path(__file__).parents[1]


def package():
    files = {"server/main.py": b"# unchanged builder", "assets/instructions/CLAUDE.md": b"unchanged prompt",
        "assets/artifacts/manifest.json": (ROOT / "assets/artifacts/manifest.json").read_bytes()}
    logical = hashlib.sha256()
    entries = []
    for name, value in sorted(files.items()):
        logical.update(name.encode() + b"\0" + b"644\0" + value + b"\0")
        entries.append({"path": name, "mode": "644", "size_bytes": len(value), "sha256": sha256(value)})
    content = {"wt_git_sha": "a" * 40, "logical_contents_sha256": logical.hexdigest(), "files": entries}
    target = io.BytesIO()
    with zipfile.ZipFile(target, "w") as archive:
        for name, value in files.items():
            archive.writestr(name, value)
        archive.writestr("runtime-content-manifest.json", json.dumps(content))
    artifact = target.getvalue()
    manifest = {"artifact_name": "workshop-terminal.pex", "artifact_type": "pex", "format_version": 1,
        "entry_point": "server.otel_bootstrap:main", "platform": "linux_x86_64", "python_abi": "cp311",
        "python_implementation": "cpython", "minimum_control_tower_contract_version": 1,
        "size_bytes": len(artifact), "sha256": sha256(artifact), "wt_git_sha": "a" * 40,
        "wt_release_tag": "v1.2.3", "logical_contents_file_count": len(entries),
        "logical_contents_sha256": logical.hexdigest()}
    raw = json.dumps(manifest).encode()
    return raw, artifact, sha256(raw)


def policy():
    return {"revision": 1, "pool": [{"service_name": "system.ai." + name, "enabled": True,
        "capabilities": [capability], "principal_classes": ["wt_sp"], "limit_profile": {}}
        for name, capability in [("claude-sonnet-5", "claude"), ("gpt-5-6-terra", "codex"), ("gpt-oss-120b", "chat")]],
        "denied_models": ["system.ai.claude-opus-4-8"], "restart_processes": False}


def spec(pin):
    return {"marker": "wt-eval-contract-1", "workspace_host": "https://labs.example.com", "workspace_id": "123",
        "profile": "labs", "attendee_email": "attendee@example.com", "attendee_mode": "synthetic_attendee",
        "disposable": True, "ttl_seconds": 14400, "cost_budget_usd": 10, "harnesses": ["claude", "codex"],
        "evaluation_observation": True, "package": {"manifest_sha256": pin}, "model_policy": policy()}


@pytest.mark.parametrize("change", ["manifest_bytes", "artifact_bytes", "artifact_size", "platform", "logical_digest"])
def test_corruption_or_incompatible_package_fails_before_workspace_access(change):
    raw, artifact, pin = package()
    if change == "manifest_bytes":
        raw += b" "
    elif change == "artifact_bytes":
        artifact += b" "
    else:
        manifest = json.loads(raw)
        if change == "artifact_size": manifest["size_bytes"] += 1
        elif change == "platform": manifest["platform"] = "macos_arm64"
        else: manifest["logical_contents_sha256"] = "0" * 64
        raw = json.dumps(manifest).encode(); pin = sha256(raw)
    with pytest.raises(ValueError): verify_package(raw, artifact, pin)


def test_package_plan_retains_owner_and_exact_ct_gateway_and_immutable_pins():
    raw, artifact, pin = package()
    plan = plan_package(spec(pin), raw, artifact, now=datetime(2026, 10, 8, tzinfo=timezone.utc))
    contract = plan["ct_compatible_contract"]
    assert contract["attendee_app_permission"] == "CAN_MANAGE"
    assert plan["grants"]["attendee"]["owner"] is False
    assert plan["environment"]["DATABRICKS_GATEWAY_HOST"] == "https://labs.example.com/ai-gateway"
    assert plan["environment"]["WORKSHOP_MODEL_POLICY_REQUIRED"] == "true"
    assert contract["package_manifest"]["sha256"] == sha256(artifact)
    files = package_sources(plan, raw)
    assert set(files) == PACKAGE_FILES
    assert not any(name.startswith(("assets/", "content/", "server/")) for name in files)
    assert "control_tower_wt_callers" not in json.dumps(plan)
    assert artifact == package()[1]


def quality_spec(pin):
    value = spec(pin)
    value["test_baseline"] = "quality-20261009"
    value["model_policy"]["pool"][0]["service_name"] = "system.ai.claude-opus-5-5"
    value["model_policy"]["pool"][1]["service_name"] = "system.ai.gpt-6-1-sol"
    return value


def test_quality_baseline_pins_models_without_changing_historical_plans():
    raw, artifact, pin = package()
    old = plan_package(spec(pin), raw, artifact)
    current = plan_package(quality_spec(pin), raw, artifact)
    assert "ANTHROPIC_MODEL" not in old["environment"]
    assert "test_baseline" not in old["ct_compatible_contract"]
    assert current["environment"]["ANTHROPIC_MODEL"] == "system.ai.claude-opus-5-5"
    assert current["environment"]["CODEX_MODEL"] == "system.ai.gpt-6-1-sol"
    assert current["ct_compatible_contract"]["test_baseline"]["id"] == "quality-20261009"


@pytest.mark.parametrize("change", ["unknown", "older_model", "extra_fallback", "old_toolchain"])
def test_quality_baseline_rejects_silent_substitution(change, monkeypatch):
    from evals.generated_apps import ct_deployment
    raw, artifact, pin = package()
    value = quality_spec(pin)
    if change == "unknown":
        value["test_baseline"] = "latest"
    elif change == "older_model":
        value["model_policy"]["pool"][0]["service_name"] = "system.ai.claude-sonnet-5"
    elif change == "extra_fallback":
        value["model_policy"]["pool"].append(policy()["pool"][0])
    else:
        verified, artifacts = verify_package(raw, artifact, pin)
        artifacts["artifacts"]["claude_binary"]["version"] = "2.1.283"
        monkeypatch.setattr(ct_deployment, "verify_package", lambda *_: (verified, artifacts))
    with pytest.raises(ValueError):
        plan_package(value, raw, artifact)


def test_quality_canary_rejects_invocation_on_a_different_model():
    from evals.generated_apps.test_baseline import baseline_canary_matches
    baseline = {"models": {"codex": "system.ai.gpt-6-1-sol"}}
    assert baseline_canary_matches(baseline, "codex", {
        "model": "system.ai.gpt-6-1-sol", "invocation_verified": True})
    assert not baseline_canary_matches(baseline, "codex", {
        "model": "system.ai.gpt-5-6-terra", "invocation_verified": True})
    assert not baseline_canary_matches(baseline, "codex", {
        "model": "system.ai.gpt-6-1-sol", "invocation_verified": False})


def test_existing_demo_catalog_is_optional_and_reaches_packaged_environment():
    raw, artifact, pin = package()
    value = spec(pin) | {"demo_catalog": "workshop_demo"}
    plan = plan_package(value, raw, artifact)
    assert plan["environment"]["WORKSHOP_DEMO_CATALOG"] == "workshop_demo"
    assert plan["ct_compatible_contract"]["demo_catalog"] == "workshop_demo"
    assert b"WORKSHOP_DEMO_CATALOG" in package_sources(plan, raw)["app.yaml"]
    assert plan_package(spec(pin), raw, artifact)["environment"]["WORKSHOP_DEMO_CATALOG"] == ""


@pytest.mark.parametrize("name", ["system", "hive_metastore", "a.b", "", "workshop_demo;drop", 1])
def test_invalid_shared_demo_catalog_is_rejected_before_workspace_access(name):
    raw, artifact, pin = package()
    with pytest.raises(ValueError):
        plan_package(spec(pin) | {"demo_catalog": name}, raw, artifact)


def test_demo_catalog_cannot_alias_the_owned_catalog_with_different_case():
    raw, artifact, pin = package()
    value = spec(pin)
    owned = plan_package(value, raw, artifact)["names"]["catalog"]
    with pytest.raises(ValueError, match="distinct"):
        plan_package(value | {"demo_catalog": owned.upper()}, raw, artifact)


def test_demo_provisioning_uses_only_read_grants_and_records_shared_identity(tmp_path, monkeypatch):
    from evals.generated_apps import ct_deployment as deployment
    catalog = SimpleNamespace(name="workshop_demo", created_at=123, metastore_id="meta", owner="owner")
    client = SimpleNamespace(catalogs=SimpleNamespace(get=lambda name: catalog))
    receipt = {"plan": {"ct_compatible_contract": {"demo_catalog": catalog.name}}}
    calls = []
    monkeypatch.setattr(deployment, "grant", lambda c,r,p,k,n,s,required: calls.append((k,n,s,required)))
    grant_demo_read_access(client, receipt, tmp_path / "receipt.json", "test-sp")
    assert calls == [("CATALOG", "workshop_demo", "test-sp", {"USE_CATALOG", "USE_SCHEMA", "SELECT", "READ_VOLUME"})]
    assert receipt["shared_demo_catalog"]["state"] == "independently_verified"
    assert receipt["shared_demo_catalog"]["read_only"] is True


@pytest.mark.parametrize("change", ["restart", "overlap", "missing_chat", "wrong_namespace", "duplicate"])
def test_incomplete_or_unsafe_model_policy_fails_closed(change):
    value = policy()
    if change == "restart": value["restart_processes"] = True
    elif change == "overlap": value["denied_models"] = [value["pool"][0]["service_name"]]
    elif change == "missing_chat": value["pool"].pop()
    elif change == "wrong_namespace": value["pool"][0]["service_name"] = "external.model"
    else: value["pool"].append(copy.deepcopy(value["pool"][0]))
    with pytest.raises(ValueError): validate_policy(value)


def test_shared_grants_preserve_others_and_record_delta_before_mutation(tmp_path):
    state = {"test-sp": {"USE_SCHEMA"}, "ct-production-group": {"ALL_PRIVILEGES"}}
    calls = []
    path = tmp_path / "receipt.json"
    receipt = {}
    def response(*args, **kwargs):
        calls.append("read")
        return SimpleNamespace(privilege_assignments=[SimpleNamespace(principal=k, privileges=list(v)) for k,v in state.items()])
    def update(kind, name, changes):
        recorded = json.loads(path.read_text())["permission_deltas"][-1]
        assert recorded["added"] == ["EXECUTE"] and recorded["state"] == "grant_requested"
        calls.append("patch")
        assert len(changes) == 1 and changes[0].principal == "test-sp"
        state["test-sp"].update(p.value for p in changes[0].add)
    client = SimpleNamespace(grants=SimpleNamespace(get=response, get_effective=response, update=update))
    grant(client, receipt, path, "MODEL_SERVICE", "system.ai.test", "test-sp", {"EXECUTE"})
    assert calls == ["read", "patch", "read", "read"]
    assert state["ct-production-group"] == {"ALL_PRIVILEGES"}
    assert receipt["permission_deltas"][0]["state"] == "independently_verified"


def test_grant_acknowledgement_does_not_pass_independent_readback(tmp_path):
    empty = SimpleNamespace(privilege_assignments=[])
    client = SimpleNamespace(grants=SimpleNamespace(get=lambda *a,**k: empty,
        get_effective=lambda *a,**k: empty, update=lambda *a,**k: None))
    receipt = {}
    with pytest.raises(RuntimeError): grant(client, receipt, tmp_path / "receipt.json", "SCHEMA", "system.ai", "test-sp", {"USE_SCHEMA"})
    assert receipt["permission_deltas"][0]["state"] == "grant_requested"


class FakeWorkspace:
    """Use SDK responses; mutations are visible only to independent reads."""
    def __init__(self, plan, monkeypatch, *, lost_model_grant=False):
        from databricks.sdk.errors import NotFound
        from databricks.sdk.service.apps import App, AppPermissions, AppDeployment, ComputeStatus, ComputeState
        from databricks.sdk.service.catalog import CatalogInfo, SchemaInfo, VolumeInfo, GetPermissionsResponse
        from databricks.sdk.service.iam import User, Group, ObjectPermissions
        from databricks.sdk.service.workspace import ObjectInfo, ObjectType
        self.calls, self.files_state, self.workspace_state, self.grants_state, self.acls = [], {}, {}, {}, {}
        self.app, self.catalog, self.group = None, None, None
        self.app_acl = AppPermissions(access_control_list=[])
        self.config = SimpleNamespace(host=plan["workspace_host"])
        def lookup(value):
            if value is None: raise NotFound("absent")
            return value
        def app_create(value, **kwargs):
            self.calls.append("app:create")
            self.app = App(name=value.name, id="00000000-0000-4000-8000-000000000001", service_principal_id=42,
                service_principal_client_id="00000000-0000-4000-8000-000000000002", user_api_scopes=value.user_api_scopes,
                url=f"https://{value.name}-123.aws.databricksapps.com", compute_status=ComputeStatus(state=ComputeState.ACTIVE))
            return SimpleNamespace(response=self.app)
        def app_acl_patch(name, access_control_list):
            self.calls.append("app:attendee_manage")
            self.app_acl = AppPermissions.from_dict({"access_control_list": [{"user_name": row.user_name,
                "all_permissions": [{"permission_level": row.permission_level.value}]} for row in access_control_list]})
        def deploy(app_name, app_deployment):
            self.calls.append("app:deploy")
            assert any(call == "acl:directories" for call in self.calls)
            assert self.workspace_state[plan["names"]["source_path"] + "/app.yaml"]
            return SimpleNamespace(response=AppDeployment(deployment_id="new-deployment", source_code_path=app_deployment.source_code_path))
        self.apps = SimpleNamespace(get=lambda name: lookup(self.app), create=app_create,
            get_permissions=lambda name: self.app_acl, update_permissions=app_acl_patch, deploy=deploy)
        self.current_user = SimpleNamespace(me=lambda: User(id="123", user_name="operator@example.com"))
        self.users = SimpleNamespace(list=lambda **k: [User(id="456", user_name=plan["attendee"]["email"], active=True)])
        def group_create(**kwargs):
            self.calls.append("group:create"); self.group = Group(id="group-id", **kwargs); return self.group
        self.groups = SimpleNamespace(list=lambda **k: [], create=group_create, get=lambda name: lookup(self.group))
        def sql(client, warehouse, statement):
            assert statement.startswith("CREATE CATALOG ") and "IF NOT EXISTS" not in statement
            self.calls.append("catalog:create")
            self.catalog = CatalogInfo(name=plan["names"]["catalog"], owner="operator@example.com",
                created_by="operator@example.com", created_at=1, metastore_id="meta")
        import scripts.deploy_ct_sim as ct_sim
        monkeypatch.setattr(ct_sim, "_sql", sql)
        monkeypatch.setattr(ct_sim, "_pick_warehouse", lambda client: "warehouse-id")
        self.catalogs = SimpleNamespace(get=lambda name: lookup(self.catalog))
        self.schemas = SimpleNamespace(create=lambda name,catalog_name: SchemaInfo(name=name,
            full_name=catalog_name + "." + name, schema_id="schema-id"))
        def volume_create(catalog_name, schema_name, name, **kwargs):
            self.calls.append("volume:" + name)
            return VolumeInfo(name=name, full_name=f"{catalog_name}.{schema_name}.{name}", volume_id=name + "-id")
        self.volumes = SimpleNamespace(create=volume_create)
        def grants_get(kind, name, **kwargs):
            return GetPermissionsResponse.from_dict({"privilege_assignments": [
                {"principal": principal, "privileges": sorted(values)}
                for principal, values in self.grants_state.get((kind, name), {}).items()]})
        def grants_patch(kind, name, changes):
            self.calls.append("grant:" + kind + ":" + name)
            if kind == "MODEL_SERVICE" and lost_model_grant: return
            for change in changes:
                self.grants_state.setdefault((kind,name), {}).setdefault(change.principal, set()).update(p.value for p in change.add)
        self.grants = SimpleNamespace(get=grants_get, get_effective=grants_get, update=grants_patch)
        def acl_get(kind, name):
            return ObjectPermissions.from_dict({"access_control_list": self.acls.get((kind,name), [])})
        def acl_patch(kind, name, access_control_list):
            self.calls.append("acl:" + kind)
            self.acls[(kind,name)] = [{"service_principal_name": row.service_principal_name,
                "all_permissions": [{"permission_level": row.permission_level.value}]} for row in access_control_list]
        self.permissions = SimpleNamespace(get=acl_get, update=acl_patch)
        def files_download(path):
            return SimpleNamespace(contents=io.BytesIO(lookup(self.files_state.get(path))))
        def files_upload(path, content, *, overwrite):
            assert path.startswith(f"/Volumes/{plan['names']['catalog']}/") and overwrite is False
            self.calls.append("files:upload"); self.files_state[path] = content.read()
        self.files = SimpleNamespace(download=files_download, upload=files_upload, create_directory=lambda path: None)
        def mkdir(path): self.workspace_state[path] = None
        def status(path):
            if path not in self.workspace_state: raise NotFound("absent")
            return ObjectInfo(object_id=99, path=path, object_type=ObjectType.DIRECTORY)
        def upload(path, content, **kwargs):
            self.calls.append("source:upload"); self.workspace_state[path] = content.read()
        self.workspace = SimpleNamespace(mkdirs=mkdir, get_status=status, upload=upload,
            download=lambda path: io.BytesIO(lookup(self.workspace_state.get(path))),
            list=lambda root,**kwargs: [ObjectInfo(path=p, object_type=ObjectType.FILE) for p,v in self.workspace_state.items()
                if p.startswith(root + "/") and v is not None])


def test_full_package_provisioning_orders_permissions_mirror_and_source_before_deploy(tmp_path, monkeypatch):
    raw, artifact, pin = package()
    value = spec(pin); value["toolchain_mirror"] = {"source_volume": "/Volumes/main/wt_central/toolchain", "strict": False}
    plan = plan_package(value, raw, artifact)
    client = FakeWorkspace(plan, monkeypatch)
    receipt = deploy_package(client, plan, raw, artifact, tmp_path / "receipt.json")
    assert receipt["status"] == "deployment_submitted"
    assert receipt["attendee_app_access"]["minimum_permission"] == "CAN_MANAGE"
    assert receipt["toolchain_staging"]["state"] == "partial_with_declared_network_fallback"
    assert client.calls.index("app:attendee_manage") < client.calls.index("catalog:create")
    assert client.calls.index("volume:toolchain") < client.calls.index("grant:SCHEMA:system.ai")
    assert client.calls[-1] == "app:deploy"
    assert all(row["state"] == "independently_verified" for row in receipt["permission_deltas"])
    assert receipt["plan"]["bounds"] == plan["bounds"]
    assert receipt["staged_package"]["sha256"] == sha256(artifact)


def test_failed_model_grant_records_recoverable_ownership_and_never_deploys(tmp_path, monkeypatch):
    raw, artifact, pin = package(); plan = plan_package(spec(pin), raw, artifact)
    client = FakeWorkspace(plan, monkeypatch, lost_model_grant=True)
    path = tmp_path / "receipt.json"
    with pytest.raises(RuntimeError, match="readback"):
        deploy_package(client, plan, raw, artifact, path)
    receipt = json.loads(path.read_text())
    assert {r["kind"] for r in receipt["created_resources"]} == {"app", "group", "catalog"}
    assert receipt["permission_deltas"][-1]["state"] == "grant_requested"
    assert "app:deploy" not in client.calls and "source:upload" not in client.calls
