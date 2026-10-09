"""SDK-shaped lifecycle failures must never qualify or mutate another run."""
import io
import json
from types import SimpleNamespace as NS

import pytest
from databricks.sdk.errors import NotFound
from databricks.sdk.service.apps import ApplicationState, AppDeploymentState
from databricks.sdk.service.catalog import GetPermissionsResponse, PrivilegeAssignment, Privilege
from databricks.sdk.service.iam import ObjectPermissions, AccessControlResponse, Permission, PermissionLevel

from evals.generated_apps import ct_lifecycle as lifecycle
from evals.generated_apps.ct_deployment import stream_digest


def test_stream_hash_is_chunked_and_bounded():
    class Stream(io.BytesIO):
        def read(self, size):
            assert 0 < size <= 1024 * 1024
            return super().read(size)
    payload = b"x" * (1024 * 1024 + 1)
    output = io.BytesIO()
    import hashlib
    assert stream_digest(Stream(payload), limit=len(payload), target=output) == (hashlib.sha256(payload).hexdigest(), len(payload))
    assert output.getvalue() == payload
    with pytest.raises(ValueError, match="size bound"):
        stream_digest(Stream(payload), limit=len(payload) - 1)


def lifecycle_client():
    marker, sp = "wt-eval-clean-1", "owned-sp"
    plan = {"marker": marker, "workspace_host": "https://labs.example.com", "disposable": True,
        "ct_compatible_contract": {"version": 2}, "names": {"app_name": marker + "-wt", "catalog": marker.replace("-", "_")}}
    app = NS(name=plan["names"]["app_name"], id="app-id", service_principal_id=42, service_principal_client_id=sp)
    catalog = NS(name=plan["names"]["catalog"], owner="operator@example.com", created_by="operator@example.com", created_at=1, metastore_id="meta")
    group = NS(id="group-id", display_name=marker + "-admins", members=[NS(value="123"), NS(value="42")])
    receipt = {"scope": "simulated_control_tower", "control_tower_requests": 0, "plan": plan,
        "operator": {"id": "123", "email": "operator@example.com"}, "created_resources": [
            {"kind": "app", "name": app.name, "id": app.id, "service_principal_id": 42, "service_principal_client_id": sp},
            {"kind": "catalog", "name": catalog.name, "created_at": 1, "metastore_id": "meta"},
            {"kind": "group", "id": group.id, "name": group.display_name}],
        "permission_deltas": [{"kind": "MODEL_SERVICE", "name": "system.ai.test", "principal": sp, "added": ["EXECUTE"]}],
        "work_sync": {"path": "/Workspace/Users/attendee/projects", "id": "home-id", "principal": sp, "added": "CAN_EDIT"}}
    state = {"app": app, "catalog": catalog, "group": group, "home_id": "home-id", "model": True, "acl": True, "reverse": False}
    calls = []
    def get(kind):
        if state[kind] is None:
            raise NotFound("absent")
        return state[kind]
    def delete(kind):
        calls.append("delete:" + kind); state[kind] = None
    def grants(*args, **kwargs):
        rows = [PrivilegeAssignment(principal="ct-production-group", privileges=[Privilege.USE_SCHEMA, Privilege.EXECUTE]),
                PrivilegeAssignment(principal="other", privileges=[Privilege.USE_SCHEMA])]
        if state["model"]:
            rows.append(PrivilegeAssignment(principal=sp, privileges=[Privilege.EXECUTE]))
        if state["reverse"]:
            rows.reverse()
            for row in rows:
                row.privileges.reverse()
        return GetPermissionsResponse(privilege_assignments=rows)
    def revoke(*args, changes, **kwargs):
        assert len(changes) == 1 and changes[0].principal == sp
        calls.append("revoke:model"); state.update(model=False, reverse=True)
    def acl(*args, **kwargs):
        rows = [AccessControlResponse(group_name="ct-production-group", all_permissions=[Permission(permission_level=PermissionLevel.CAN_MANAGE)]),
                AccessControlResponse(user_name="attendee@example.com", all_permissions=[Permission(permission_level=PermissionLevel.CAN_MANAGE)])]
        if state["acl"]:
            rows.append(AccessControlResponse(service_principal_name=sp, all_permissions=[Permission(permission_level=PermissionLevel.CAN_EDIT)]))
        if state["reverse"]: rows.reverse()
        return ObjectPermissions(access_control_list=rows)
    def revoke_acl(*args, access_control_list):
        assert len(access_control_list) == 2 and not any(row.service_principal_name == sp for row in access_control_list)
        assert any(row.group_name == "ct-production-group" for row in access_control_list)
        assert any(row.user_name == "attendee@example.com" for row in access_control_list)
        calls.append("revoke:home"); state["acl"] = False
    client = NS(config=NS(host=plan["workspace_host"]),
        apps=NS(get=lambda name: get("app"), delete=lambda name: delete("app")),
        catalogs=NS(get=lambda name: get("catalog"), delete=lambda name, force: delete("catalog")),
        groups=NS(get=lambda name: get("group"), delete=lambda name: delete("group")),
        workspace=NS(get_status=lambda path: NS(object_id=state["home_id"]), delete=lambda *a,**k: pytest.fail("home must remain")),
        grants=NS(get=grants, update=revoke), permissions=NS(get=acl, set=revoke_acl))
    return client, receipt, state, calls


def test_cleanup_only_removes_recorded_sp_deltas_and_tolerates_reordering(tmp_path):
    client, receipt, state, calls = lifecycle_client()
    source = tmp_path / "receipt.json"; source.write_text(json.dumps(receipt))
    result = lifecycle.cleanup(client, source, tmp_path / "cleanup.json")
    assert result["status"] == "cleanup_verified"
    assert calls == ["revoke:model", "revoke:home", "delete:app", "delete:catalog", "delete:group"]
    assert state["home_id"] == "home-id"


@pytest.mark.parametrize("principal_absent", [True, False])
def test_cleanup_retry_accepts_only_platform_removed_app_membership(tmp_path, principal_absent):
    client, receipt, state, calls = lifecycle_client()
    state["app"] = None
    state["group"].members = [NS(value="123")]
    def principal(_):
        if principal_absent: raise NotFound("removed by Apps")
        return NS(id="42", application_id="owned-sp")
    client.service_principals = NS(get=principal)
    source = tmp_path / "receipt.json"; source.write_text(json.dumps(receipt))
    if principal_absent:
        assert lifecycle.cleanup(client, source, tmp_path / "cleanup.json")["status"] == "cleanup_verified"
        assert "delete:app" not in calls
    else:
        with pytest.raises(ValueError, match="principal still exists"):
            lifecycle.cleanup(client, source, tmp_path / "cleanup.json")
        assert calls == []


def test_demo_cleanup_revokes_exact_read_delta_without_deleting_shared_catalog(tmp_path):
    client, receipt, state, calls = lifecycle_client()
    demo = NS(name="workshop_demo", created_at=2, metastore_id="meta")
    receipt["plan"]["ct_compatible_contract"]["demo_catalog"] = demo.name
    receipt["shared_demo_catalog"] = {"name":demo.name,"created_at":2,"metastore_id":"meta",
        "principal":"owned-sp","read_only":True}
    receipt["permission_deltas"] = [{"kind":"CATALOG","name":demo.name,"principal":"owned-sp",
        "added":["SELECT","USE_CATALOG","USE_SCHEMA","READ_VOLUME"]}]
    owned_get = client.catalogs.get
    client.catalogs.get = lambda name: demo if name == demo.name else owned_get(name)
    read_privileges = [Privilege.SELECT, Privilege.USE_CATALOG, Privilege.USE_SCHEMA, Privilege.READ_VOLUME]
    def demo_grants(kind, name):
        assert (kind, name) == ("CATALOG", demo.name)
        rows = [PrivilegeAssignment(principal="ct-production-group", privileges=read_privileges)]
        if state["model"]: rows.append(PrivilegeAssignment(principal="owned-sp", privileges=read_privileges))
        return GetPermissionsResponse(privilege_assignments=rows)
    def revoke_demo(kind, name, *, changes):
        assert (kind, name) == ("CATALOG", demo.name)
        assert len(changes) == 1 and changes[0].principal == "owned-sp"
        assert set(changes[0].remove) == set(read_privileges) and not changes[0].add
        calls.append("revoke:demo"); state["model"] = False
    client.grants = NS(get=demo_grants, update=revoke_demo)
    source = tmp_path / "receipt.json"; source.write_text(json.dumps(receipt))
    assert lifecycle.cleanup(client, source, tmp_path / "cleanup.json")["status"] == "cleanup_verified"
    assert state["catalog"] is None and client.catalogs.get(demo.name) is demo
    assert calls[0] == "revoke:demo"


@pytest.mark.parametrize("change", ["app", "owner", "group", "home", "shared_principal"])
def test_cleanup_preflights_changed_identity_before_any_mutation(tmp_path, change):
    client, receipt, state, calls = lifecycle_client()
    if change == "app": state["app"].id = "replacement"
    elif change == "owner": state["catalog"].owner = "new-owner"
    elif change == "group": state["group"].members.append(NS(value="unrelated"))
    elif change == "home": state["home_id"] = "replacement"
    else: receipt["work_sync"]["principal"] = "ct-production-sp"
    source = tmp_path / "receipt.json"; source.write_text(json.dumps(receipt))
    with pytest.raises(ValueError): lifecycle.cleanup(client, source, tmp_path / "cleanup.json")
    assert calls == []


def test_wait_ignores_old_active_deployment_and_fails_recorded_failure(monkeypatch):
    client, receipt, state, calls = lifecycle_client()
    receipt["deployment"] = {"deployment_id": "new"}
    state["app"].active_deployment = NS(deployment_id="old")
    state["app"].app_status = NS(state=ApplicationState.RUNNING)
    client.apps.get_deployment = lambda *a: NS(status=NS(state=AppDeploymentState.IN_PROGRESS))
    monkeypatch.setattr(lifecycle, "ensure_live", lambda plan: None)
    monkeypatch.setattr(lifecycle.time, "sleep", lambda *a: setattr(state["app"], "active_deployment", NS(deployment_id="new")))
    with pytest.raises(TimeoutError): lifecycle.wait_for_deployment(client, receipt, deadline=0)
    client.apps.get_deployment = lambda *a: NS(status=NS(state=AppDeploymentState.FAILED))
    with pytest.raises(RuntimeError, match="deployment failed"):
        lifecycle.wait_for_deployment(client, receipt, deadline=0)
