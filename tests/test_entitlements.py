"""SP-driven entitlement reconciler: UC catalog grant + non-UC CAN_MANAGE sweep,
idempotency, disabled-flag skip, and fail-soft health on error."""

import random
from collections import Counter

import pytest
import requests


class _Resp:
    def __init__(self, status, payload=None, text="", headers=None):
        self.status_code = status
        self._payload = payload or {}
        self.text = text
        self.headers = headers or {}

    def json(self):
        return self._payload


class _StrictRequests:
    """Exact HTTP contract mock.

    Any unregistered URL, parameter set, identifier, or permission level raises
    immediately so a permissive fake cannot make a wrong adapter look correct.
    """

    RequestException = requests.RequestException

    def __init__(self):
        self.calls = []
        self.gets = {}
        self.patches = {}
        self.permission_effective = {}
        self.catalog_response = _Resp(200, {})

    @staticmethod
    def _params(params):
        return tuple(sorted((params or {}).items()))

    def register_get(self, url, params, payload, status=200, text="", headers=None):
        self.gets[(url, self._params(params))] = _Resp(
            status, payload, text, headers
        )

    def register_patch(self, url, levels, responses=None):
        self.patches[url] = {
            "levels": set(levels),
            "responses": list(responses or []),
        }
        self.permission_effective.setdefault(url, None)

    def get(self, url, headers=None, params=None, timeout=None):
        params = dict(params or {})
        self.calls.append(("GET", url, params))
        key = (url, self._params(params))
        if not params and url in self.permission_effective:
            level = self.permission_effective[url]
            return _Resp(200, {
                "access_control_list": [] if level is None else [{
                    "user_name": "alice@example.com",
                    "all_permissions": [{
                        "permission_level": level,
                        "inherited": False,
                    }],
                }]
            })
        assert key in self.gets, f"unregistered GET {url} params={params}"
        return self.gets[key]

    def patch(self, url, headers=None, json=None, timeout=None):
        self.calls.append(("PATCH", url, json))
        if "changes" in json:
            assert url.endswith("/api/2.1/unity-catalog/permissions/catalog/wsh_alice")
            return self.catalog_response
        if "owner" in json:
            # Optional catalog-ownership transfer, which succeeds at the API
            # without changing what a later metadata read reports here.
            assert url.endswith("/api/2.1/unity-catalog/catalogs/wsh_alice")
            return _Resp(200, {})
        assert url in self.patches, f"unregistered PATCH {url}"
        level = json["access_control_list"][0]["permission_level"]
        contract = self.patches[url]
        assert level in contract["levels"], f"unsupported {level} for {url}"
        if contract["responses"]:
            response = contract["responses"].pop(0)
        else:
            response = _Resp(200, {})
        if 200 <= response.status_code < 300:
            self.permission_effective[url] = level
        return response


def _strict_fake():
    fake = _StrictRequests()
    host = "https://test.cloud.databricks.com"
    fake.register_get(
        f"{host}/api/2.0/preview/scim/v2/Me",
        {"attributes": "id,userName,applicationId"},
        {
            "id": "sp-numeric-id",
            "userName": "app-sp",
            "applicationId": "app-client-id",
        },
    )
    fake.register_get(
        f"{host}/api/2.1/unity-catalog/catalogs/wsh_alice",
        {},
        {"name": "wsh_alice", "owner": "alice@example.com"},
    )
    fake.register_get(
        f"{host}/api/2.1/unity-catalog/permissions/catalog/wsh_alice",
        {},
        {
            "privilege_assignments": [{
                "principal": "alice@example.com",
                "privileges": ["ALL_PRIVILEGES"],
            }]
        },
    )
    for path, params, key in (
        ("/api/2.2/jobs/list", {"limit": 100}, "jobs"),
        ("/api/2.0/pipelines", {"max_results": 100}, "statuses"),
        ("/api/2.0/serving-endpoints", {}, "endpoints"),
        ("/api/2.0/apps", {"page_size": 100}, "apps"),
        ("/api/2.0/database/instances", {"page_size": 100}, "database_instances"),
        ("/api/2.0/postgres/projects", {"page_size": 100}, "projects"),
        ("/api/2.0/sql/warehouses", {"page_size": 100}, "warehouses"),
        ("/api/2.0/lakeview/dashboards", {"page_size": 100}, "dashboards"),
    ):
        fake.register_get(f"{host}{path}", params, {key: []})
    return fake


def _fake_with_resources():
    fake = _strict_fake()
    host = "https://test.cloud.databricks.com"
    fake.register_get(
        f"{host}/api/2.2/jobs/list",
        {"limit": 100},
        {"jobs": [{
            "job_id": 11,
            "creator_user_name": "app-sp",
            "created_time": 1_100_000,
        }]},
    )
    fake.register_get(
        f"{host}/api/2.0/serving-endpoints",
        {},
        {"endpoints": [{
            "id": "e1",
            "creator": "app-sp",
            "creation_timestamp": 1_100_000,
        }]},
    )
    fake.register_get(
        f"{host}/api/2.0/apps",
        {"page_size": 100},
        {"apps": [{
            "name": "app1",
            "creator": "app-sp",
            "create_time": "1970-01-01T00:18:20Z",
        }]},
    )
    fake.register_get(
        f"{host}/api/2.0/database/instances",
        {"page_size": 100},
        {"database_instances": [{
            "name": "db1",
            "creator": "app-sp",
            "creation_time": "1970-01-01T00:18:20Z",
        }]},
    )
    fake.register_patch(
        f"{host}/api/2.0/permissions/jobs/11",
        {"IS_OWNER", "CAN_MANAGE"},
    )
    fake.register_patch(
        f"{host}/api/2.0/permissions/serving-endpoints/e1",
        {"CAN_MANAGE"},
    )
    fake.register_patch(
        f"{host}/api/2.0/permissions/apps/app1",
        {"CAN_MANAGE"},
    )
    fake.register_patch(
        f"{host}/api/2.0/permissions/database-instances/db1",
        {"CAN_MANAGE"},
    )
    return fake


@pytest.fixture()
def enabled(monkeypatch):
    monkeypatch.setenv("ENABLE_ENTITLEMENTS", "true")
    monkeypatch.setenv("WORKSHOP_CATALOG", "wsh_alice")


def _mgr(monkeypatch, fake):
    from server import entitlements

    monkeypatch.setattr(entitlements, "requests", fake)
    monkeypatch.setattr(entitlements, "_sp_bearer", lambda: "sp-bearer")
    manager = entitlements.EntitlementManager()
    manager._run_started_at = 1_000
    return manager


def _scoped_mgr(monkeypatch, fake, *, started_at=1_000.0):
    from server import entitlements

    monkeypatch.setenv("DATABRICKS_CLIENT_ID", "app-client-id")
    monkeypatch.setattr(entitlements, "requests", fake)
    monkeypatch.setattr(entitlements, "_sp_bearer", lambda: "sp-bearer")
    manager = entitlements.EntitlementManager()
    manager._run_started_at = started_at
    return manager


def test_disabled_reconcile_is_noop(monkeypatch):
    monkeypatch.setenv("ENABLE_ENTITLEMENTS", "false")  # default is now ON
    from server import entitlements

    assert entitlements.EntitlementManager().reconcile("a@example.com") == {"enabled": False}


def test_no_catalog_is_a_supported_setup_not_a_failure(monkeypatch):
    """An app with no Unity Catalog still hands off cleanly.

    Control Tower only injects WORKSHOP_CATALOG when the event is configured for
    entitlements, while the app defaults ENABLE_ENTITLEMENTS on — so "reconciler
    running, no catalog" is the ordinary case. Live, it made `workshop-grant-me`
    print "WORKSHOP_CATALOG is not configured" at an attendee whose app grant had
    just succeeded, on a game that never touched UC, and turned the entitlements
    health red for the run.
    """
    monkeypatch.setenv("ENABLE_ENTITLEMENTS", "true")
    monkeypatch.delenv("WORKSHOP_CATALOG", raising=False)
    fake = _fake_with_resources()
    mgr = _mgr(monkeypatch, fake)

    result = mgr.reconcile("alice@example.com")
    assert result["enabled"] is True
    assert result["errors"] == [], "an absent catalog is a configuration, not a fault"
    assert result["catalog"] is None
    assert mgr.status()["ok"] is True

    # The point of running at all: the non-UC handoff still happens, which is
    # what the attendee actually needs to open the app they just deployed.
    patches = [c for c in fake.calls if c[0] == "PATCH"]
    assert [c for c in patches if "/permissions/apps/app1" in c[1]]
    assert not [c for c in patches if "unity-catalog" in c[1]], (
        "and nothing is attempted against a catalog that was never named"
    )


def test_reconcile_grants_catalog_and_non_uc(monkeypatch, enabled):
    fake = _fake_with_resources()
    mgr = _mgr(monkeypatch, fake)

    result = mgr.reconcile("alice@example.com")
    assert result["enabled"] is True
    assert result["errors"] == []
    assert result["catalog"] == "wsh_alice"

    patches = [c for c in fake.calls if c[0] == "PATCH"]
    # UC catalog ALL_PRIVILEGES grant with the correct payload.
    cat = [c for c in patches if "unity-catalog/permissions/catalog/wsh_alice" in c[1]]
    assert cat and cat[0][2] == {
        "changes": [{"principal": "alice@example.com", "add": ["ALL_PRIVILEGES"]}]
    }
    # Only creator- and run-verified resources are handed off, using each API's
    # actual permission identifier/path.
    owner_hit = [c for c in patches if "/permissions/jobs/11" in c[1]]
    assert owner_hit
    assert owner_hit[0][2]["access_control_list"][0]["permission_level"] == "IS_OWNER"
    for permission_path in (
        "/permissions/serving-endpoints/e1",
        "/permissions/apps/app1",
        "/permissions/database-instances/db1",
    ):
        hit = [c for c in patches if permission_path in c[1]]
        assert hit, f"missing handoff for {permission_path}"
        assert hit[0][2]["access_control_list"][0]["permission_level"] == "CAN_MANAGE"
        assert hit[0][2]["access_control_list"][0]["user_name"] == "alice@example.com"

    status = mgr.status()
    assert status["ok"] is True
    assert status["verified_email"] == "alice@example.com"
    assert status["verified_catalog"] == "wsh_alice"
    assert status["last_verified_at"] is not None
    assert status["verification_source"] == "on_demand"


def test_background_reconcile_uses_the_bound_attendee_before_anyone_arrives(
    monkeypatch, enabled
):
    """Admission has to be answerable before the attendee's first click.

    Control Tower gates on /readyz, of which this reconciler's proof is one
    check. Keying the background pass on the seen-users roster made a freshly
    deployed instance report "no attendee available" until someone opened it —
    the gate reporting red at exactly the moment it is supposed to pass.
    """
    fake = _fake_with_resources()
    mgr = _mgr(monkeypatch, fake)
    from server.users import user_manager

    monkeypatch.setattr(user_manager, "all", lambda: [])
    monkeypatch.setenv("WORKSHOP_ATTENDEE_EMAIL", "alice@example.com")

    result = mgr.reconcile()

    assert result["errors"] == []
    assert result["emails"] == ["alice@example.com"]
    assert mgr.status()["verified_email"] == "alice@example.com"
    assert mgr.status()["verification_source"] == "background"


def test_reconcile_without_attendee_fails_closed(monkeypatch, enabled):
    fake = _strict_fake()
    mgr = _mgr(monkeypatch, fake)
    from server.users import user_manager
    monkeypatch.setattr(user_manager, "all", lambda: [])
    monkeypatch.delenv("WORKSHOP_ATTENDEE_EMAIL", raising=False)

    result = mgr.reconcile()

    assert result["errors"] == ["no attendee available for entitlement verification"]
    assert mgr.status()["ok"] is False
    assert mgr.status()["last_verified_at"] is None


def test_catalog_patch_without_readback_proof_fails_closed(monkeypatch, enabled):
    fake = _strict_fake()
    host = "https://test.cloud.databricks.com"
    fake.register_get(
        f"{host}/api/2.1/unity-catalog/permissions/catalog/wsh_alice",
        {},
        {"privilege_assignments": []},
    )
    mgr = _mgr(monkeypatch, fake)

    result = mgr.reconcile("alice@example.com")

    assert any("catalog verification" in error for error in result["errors"])
    status = mgr.status()
    assert status["ok"] is False
    assert status["last_verified_at"] is None


def test_reconcile_is_idempotent(monkeypatch, enabled):
    fake = _fake_with_resources()
    mgr = _mgr(monkeypatch, fake)
    first = mgr.reconcile("alice@example.com")
    permission_patches = len([
        call for call in fake.calls
        if call[0] == "PATCH" and "unity-catalog/permissions" not in call[1]
    ])
    second = mgr.reconcile("alice@example.com")
    repeated_permission_patches = len([
        call for call in fake.calls
        if call[0] == "PATCH" and "unity-catalog/permissions" not in call[1]
    ])
    # Later sweeps read back durable effective access but do not mutate it when
    # the required level remains present.
    assert first["errors"] == [] and second["errors"] == []
    assert repeated_permission_patches == permission_patches
    assert len([
        call for call in fake.calls
        if call[0] == "GET" and "/api/2.0/permissions/" in call[1]
    ]) == permission_patches * 2


def test_handoff_readback_failure_degrades_health(monkeypatch, enabled):
    fake = _fake_with_resources()
    host = "https://test.cloud.databricks.com"
    url = f"{host}/api/2.0/permissions/jobs/11"
    fake.permission_effective[url] = "CAN_VIEW"
    mgr = _mgr(monkeypatch, fake)

    original_get = fake.get

    def fail_job_readback(request_url, **kwargs):
        if request_url == url:
            return _Resp(500, {}, "readback unavailable")
        return original_get(request_url, **kwargs)

    fake.get = fail_job_readback
    result = mgr.reconcile("alice@example.com")

    assert any("permission verification" in error for error in result["errors"])
    assert mgr.status()["ok"] is False
    job = next(
        detail for detail in result["handoff"]["details"]
        if detail["resource_type"] == "jobs"
    )
    assert job["state"] == "failed"


def test_later_sweep_reapplies_handoff_after_acl_drift(monkeypatch, enabled):
    fake = _fake_with_resources()
    host = "https://test.cloud.databricks.com"
    url = f"{host}/api/2.0/permissions/jobs/11"
    mgr = _mgr(monkeypatch, fake)

    first = mgr.reconcile("alice@example.com")
    fake.permission_effective[url] = None
    second = mgr.reconcile("alice@example.com")

    levels = [
        call[2]["access_control_list"][0]["permission_level"]
        for call in fake.calls
        if call[0] == "PATCH" and call[1] == url
    ]
    assert first["errors"] == second["errors"] == []
    assert levels == ["IS_OWNER", "IS_OWNER"]


def test_failure_emits_health_and_never_raises(monkeypatch, enabled):
    fake = _strict_fake()
    fake.catalog_response = _Resp(500, {}, "boom")
    mgr = _mgr(monkeypatch, fake)

    emitted = []
    from server import event_emitter

    monkeypatch.setattr(
        event_emitter.event_emitter,
        "emit",
        lambda *a, **k: emitted.append((a, k)),
    )

    result = mgr.reconcile("alice@example.com")  # must not raise
    assert result["errors"]  # catalog grant failed
    assert mgr.status()["ok"] is False
    assert any(a and a[0] == "entitlements.health" for a, _ in emitted)


def test_start_skips_thread_when_disabled(monkeypatch):
    monkeypatch.setenv("ENABLE_ENTITLEMENTS", "false")  # default is now ON
    from server import entitlements

    mgr = entitlements.EntitlementManager()
    mgr.start()
    assert mgr._thread is None


def test_no_bearer_records_error(monkeypatch, enabled):
    from server import entitlements

    monkeypatch.setattr(entitlements, "_sp_bearer", lambda: None)
    mgr = entitlements.EntitlementManager()
    result = mgr.reconcile("alice@example.com")
    assert result["errors"]
    assert mgr.status()["ok"] is False


def test_paginated_discovery_filters_creator_and_run_boundary(monkeypatch, enabled):
    fake = _strict_fake()
    host = "https://test.cloud.databricks.com"
    jobs = f"{host}/api/2.2/jobs/list"
    fake.register_get(jobs, {"limit": 100}, {
        "jobs": [
            {"job_id": 11, "creator_user_name": "app-sp", "created_time": 1_100_000},
            {"job_id": 12, "creator_user_name": "someone-else", "created_time": 1_100_000},
        ],
        "next_page_token": "page-2",
    })
    fake.register_get(jobs, {"limit": 100, "page_token": "page-2"}, {
        "jobs": [
            {"job_id": 13, "creator_user_name": "app-sp", "created_time": 900_000},
        ],
    })
    fake.register_patch(
        f"{host}/api/2.0/permissions/jobs/11",
        {"IS_OWNER", "CAN_MANAGE"},
    )
    mgr = _scoped_mgr(monkeypatch, fake)

    result = mgr.reconcile("alice@example.com")

    assert ("GET", jobs, {"limit": 100, "page_token": "page-2"}) in fake.calls
    permission_patches = [c for c in fake.calls if c[0] == "PATCH" and "/permissions/jobs/" in c[1]]
    assert [c[1].rsplit("/", 1)[-1] for c in permission_patches] == ["11"]
    handed_off = [
        d for d in result["handoff"]["details"]
        if d["resource_type"] == "jobs" and d["resource_id"] == "11"
    ][0]
    assert handed_off["state"] == "handed_off"
    assert handed_off["states"] == ["discovered", "verified_creator", "handed_off"]


def test_resource_adapters_use_correct_ids_and_safe_permission_fallback(monkeypatch, enabled):
    fake = _strict_fake()
    host = "https://test.cloud.databricks.com"
    mgr = _scoped_mgr(monkeypatch, fake)

    # Establish an empty at-start baseline for APIs without a reliable creation
    # timestamp (pipelines and warehouses).
    mgr.capture_baseline()
    for url, params, payload in (
        (f"{host}/api/2.2/jobs/list", {"limit": 100}, {
            "jobs": [{"job_id": 21, "creator_user_name": "app-sp", "created_time": 1_100_000}],
        }),
        (f"{host}/api/2.0/pipelines", {"max_results": 100}, {
            "statuses": [{"pipeline_id": "pipe-1", "creator_user_name": "app-sp"}],
        }),
        (f"{host}/api/2.0/serving-endpoints", {}, {
            "endpoints": [{
                "id": "serve-id", "name": "serve-name", "creator": "app-sp",
                "creation_timestamp": 1_100_000,
            }],
        }),
        (f"{host}/api/2.0/apps", {"page_size": 100}, {
            "apps": [{
                "id": "app-id", "name": "app-name", "creator": "app-sp",
                "create_time": "1970-01-01T00:18:20Z",
            }],
        }),
        (f"{host}/api/2.0/database/instances", {"page_size": 100}, {
            "database_instances": [{
                "uid": "database-uid", "name": "database-name", "creator": "app-sp",
                "creation_time": "1970-01-01T00:18:20Z",
            }],
        }),
        (f"{host}/api/2.0/postgres/projects", {"page_size": 100}, {
            "projects": [{
                "project_id": "project-id",
                "create_time": "1970-01-01T00:18:20Z",
                "status": {"owner": "app-sp"},
            }],
        }),
        (f"{host}/api/2.0/sql/warehouses", {"page_size": 100}, {
            "warehouses": [{"id": "warehouse-id", "creator_name": "app-sp"}],
        }),
        (f"{host}/api/2.0/lakeview/dashboards", {"page_size": 100}, {
            "dashboards": [{
                "dashboard_id": "dashboard-id",
                "lifecycle_state": "ACTIVE",
                "create_time": "1970-01-01T00:18:20Z",
            }],
        }),
        # Lakeview list responses omit the path, so the creator check reads the
        # dashboard itself; an SP's workspace home is /Users/<application-id>.
        (f"{host}/api/2.0/lakeview/dashboards/dashboard-id", {}, {
            "dashboard_id": "dashboard-id",
            "parent_path": "/Users/app-client-id",
            "create_time": "1970-01-01T00:18:20Z",
        }),
    ):
        fake.register_get(url, params, payload)
    for resource_type, resource_id in (("jobs", "21"), ("pipelines", "pipe-1")):
        fake.register_patch(
            f"{host}/api/2.0/permissions/{resource_type}/{resource_id}",
            {"IS_OWNER", "CAN_MANAGE"},
            [_Resp(403, {}, "owner transfer requires admin"), _Resp(200, {})],
        )
    for permission_type, resource_id in (
        ("serving-endpoints", "serve-id"),
        ("apps", "app-name"),
        ("database-instances", "database-name"),
        ("database-projects", "project-id"),
        ("dashboards", "dashboard-id"),
    ):
        fake.register_patch(
            f"{host}/api/2.0/permissions/{permission_type}/{resource_id}",
            {"CAN_MANAGE"},
        )
    warehouse_url = f"{host}/api/2.0/permissions/warehouses/warehouse-id"
    fake.register_patch(
        warehouse_url,
        {"IS_OWNER", "CAN_MANAGE"},
        [_Resp(403, {}, "owner requires unrestricted cluster creation"), _Resp(200, {})],
    )

    result = mgr.reconcile("alice@example.com")

    patches = [c for c in fake.calls if c[0] == "PATCH"]
    expected_urls = {
        f"{host}/api/2.0/permissions/jobs/21",
        f"{host}/api/2.0/permissions/pipelines/pipe-1",
        f"{host}/api/2.0/permissions/serving-endpoints/serve-id",
        f"{host}/api/2.0/permissions/apps/app-name",
        f"{host}/api/2.0/permissions/database-instances/database-name",
        f"{host}/api/2.0/permissions/database-projects/project-id",
        f"{host}/api/2.0/permissions/warehouses/warehouse-id",
        f"{host}/api/2.0/permissions/dashboards/dashboard-id",
    }
    assert expected_urls <= {c[1] for c in patches}
    for resource_type, resource_id in (
        ("jobs", "21"),
        ("pipelines", "pipe-1"),
        ("warehouses", "warehouse-id"),
    ):
        url = f"{host}/api/2.0/permissions/{resource_type}/{resource_id}"
        assert [c[2]["access_control_list"][0]["permission_level"] for c in patches if c[1] == url] == [
            "IS_OWNER", "CAN_MANAGE",
        ]
    warehouse = [
        detail for detail in result["handoff"]["details"]
        if detail["resource_type"] == "warehouses"
    ][0]
    assert warehouse["permission_level"] == "CAN_MANAGE"
    assert result["handoff"]["summary"]["handed_off"] == 8
    dashboard = [
        detail for detail in result["handoff"]["details"]
        if detail["resource_type"] == "dashboards"
    ][0]
    assert dashboard["state"] == "handed_off"
    assert dashboard["permission_level"] == "CAN_MANAGE"
    assert not any("dashboards" in error for error in result["errors"])
    assert mgr.status()["ok"] is True


def test_warehouse_owner_success_is_recorded_in_ledger(monkeypatch, enabled):
    fake = _strict_fake()
    host = "https://test.cloud.databricks.com"
    manager = _scoped_mgr(monkeypatch, fake)
    manager.capture_baseline()
    fake.register_get(
        f"{host}/api/2.0/sql/warehouses",
        {"page_size": 100},
        {"warehouses": [{"id": "warehouse-owner", "creator_name": "app-sp"}]},
    )
    permission_url = f"{host}/api/2.0/permissions/warehouses/warehouse-owner"
    fake.register_patch(permission_url, {"IS_OWNER", "CAN_MANAGE"})

    result = manager.reconcile("alice@example.com")

    levels = [
        call[2]["access_control_list"][0]["permission_level"]
        for call in fake.calls
        if call[0] == "PATCH" and call[1] == permission_url
    ]
    assert levels == ["IS_OWNER"]
    warehouse = [
        detail for detail in result["handoff"]["details"]
        if detail["resource_type"] == "warehouses"
    ][0]
    assert warehouse["state"] == "handed_off"
    assert warehouse["permission_level"] == "IS_OWNER"


def test_catalog_is_verified_by_privileges_when_ownership_stays_put(
    monkeypatch, enabled
):
    """Ownership transfer is opt-in, so requiring it made every unit read red.

    Real case: a Control Tower workspace where the catalog is owned by the
    deploying principal reported `catalog owner is ..., expected attendee`
    forever, even though the attendee held ALL_PRIVILEGES and could use it.
    """
    fake = _fake_with_resources()
    host = "https://test.cloud.databricks.com"
    fake.register_get(
        f"{host}/api/2.1/unity-catalog/catalogs/wsh_alice",
        {},
        {"name": "wsh_alice", "owner": "control-tower-sp"},
    )
    mgr = _mgr(monkeypatch, fake)

    result = mgr.reconcile("alice@example.com")

    assert result["errors"] == []
    assert mgr.status()["verified_catalog"] == "wsh_alice"


def test_catalog_ownership_is_still_enforced_when_transfer_is_requested(
    monkeypatch, enabled
):
    monkeypatch.setenv("ENTITLEMENT_TRANSFER_OWNERSHIP", "true")
    fake = _fake_with_resources()
    host = "https://test.cloud.databricks.com"
    fake.register_get(
        f"{host}/api/2.1/unity-catalog/catalogs/wsh_alice",
        {},
        {"name": "wsh_alice", "owner": "control-tower-sp"},
    )
    mgr = _mgr(monkeypatch, fake)

    result = mgr.reconcile("alice@example.com")

    assert any("catalog owner is control-tower-sp" in e for e in result["errors"])


def test_builtin_resources_without_an_id_are_skipped_not_reported(
    monkeypatch, enabled
):
    """Foundation-model endpoints carry no ``id`` and are nobody's to hand off.

    Reporting the absent field made `entitlements` red in every workspace,
    hiding real failures behind an error the attendee cannot act on.
    """
    fake = _fake_with_resources()
    host = "https://test.cloud.databricks.com"
    fake.register_get(
        f"{host}/api/2.0/serving-endpoints",
        {},
        {"endpoints": [
            {"name": "databricks-claude-sonnet-5"},
            {"id": "e1", "creator": "app-sp", "creation_timestamp": 1_100_000},
        ]},
    )
    mgr = _mgr(monkeypatch, fake)

    result = mgr.reconcile("alice@example.com")

    assert result["errors"] == []
    # The SP-created endpoint is still handed off, so the skip is narrow.
    assert result["non_uc"]["alice@example.com"]["serving-endpoints"] == 1


def test_an_owned_resource_missing_its_id_is_still_reported(monkeypatch, enabled):
    """The skip must not swallow a resource this app made and cannot address."""
    fake = _fake_with_resources()
    host = "https://test.cloud.databricks.com"
    fake.register_get(
        f"{host}/api/2.0/serving-endpoints",
        {},
        {"endpoints": [{"creator": "app-sp", "creation_timestamp": 1_100_000}]},
    )
    mgr = _mgr(monkeypatch, fake)

    result = mgr.reconcile("alice@example.com")

    assert any("missing id" in e for e in result["errors"])


def test_a_dashboard_in_the_attendees_own_folder_is_left_alone(monkeypatch, enabled):
    """Creator verification for dashboards is a path check, and it cuts both ways.

    A dashboard the agent wrote into the attendee's workspace home already
    inherits CAN_MANAGE from that directory. Patching it would be noise at best,
    and in a workspace with more than one attendee it would be a handoff of
    somebody else's work.
    """
    fake = _strict_fake()
    host = "https://test.cloud.databricks.com"
    fake.register_get(f"{host}/api/2.0/lakeview/dashboards", {"page_size": 100}, {
        "dashboards": [{
            "dashboard_id": "theirs",
            "lifecycle_state": "ACTIVE",
            "create_time": "1970-01-01T00:18:20Z",
        }],
    })
    fake.register_get(f"{host}/api/2.0/lakeview/dashboards/theirs", {}, {
        "dashboard_id": "theirs",
        "parent_path": "/Users/alice@example.com",
        "create_time": "1970-01-01T00:18:20Z",
    })
    mgr = _scoped_mgr(monkeypatch, fake)

    result = mgr.reconcile("alice@example.com")

    assert not [c for c in fake.calls if c[0] == "PATCH" and "theirs" in c[1]]
    assert result["errors"] == []
    assert result["non_uc"]["alice@example.com"]["dashboards"] == 0


def test_a_trashed_dashboard_is_not_handed_off(monkeypatch, enabled):
    """Deleted dashboards linger in list responses for a while after the delete."""
    fake = _strict_fake()
    host = "https://test.cloud.databricks.com"
    fake.register_get(f"{host}/api/2.0/lakeview/dashboards", {"page_size": 100}, {
        "dashboards": [{
            "dashboard_id": "gone",
            "lifecycle_state": "TRASHED",
            "create_time": "1970-01-01T00:18:20Z",
        }],
    })
    mgr = _scoped_mgr(monkeypatch, fake)

    result = mgr.reconcile("alice@example.com")

    # Skipped outright: no detail read, no patch, and nothing in the ledger for
    # the attendee to be pointed at.
    assert not [c for c in fake.calls if "gone" in str(c[1])]
    assert not [
        d for d in result["handoff"]["details"] if d["resource_id"] == "gone"
    ]
    assert result["errors"] == []


def test_unverifiable_warehouse_is_failed_closed_and_visible(monkeypatch, enabled):
    fake = _strict_fake()
    host = "https://test.cloud.databricks.com"
    fake.register_get(f"{host}/api/2.0/sql/warehouses", {"page_size": 100}, {
        "warehouses": [{"id": "preexisting", "creator_name": "app-sp"}],
    })
    mgr = _scoped_mgr(monkeypatch, fake)

    result = mgr.reconcile("alice@example.com")

    assert not any(c for c in fake.calls if c[0] == "PATCH" and c[1].endswith("/preexisting"))
    failed = [
        d for d in result["handoff"]["details"]
        if d["resource_type"] == "warehouses" and d["resource_id"] == "preexisting"
    ][0]
    assert failed["state"] == "failed"
    assert "baseline" in failed["error"]
    assert mgr.status()["handoff"]["summary"]["failed"] >= 1


# --- Apps the attendee builds run as their own service principal ------------
#
# At servco every attendee-built app hit "does not have USE CATALOG privilege"
# and "does not have SELECT privilege" on data the attendee could read fine
# themselves, and it was cleared by hand mid-event. The principal is minted when
# the app is created, so nothing written at provision time names it.


class _CatalogAwareRequests(_StrictRequests):
    """Serves catalog permissions from state that a PATCH actually mutates.

    The base fake answers the permissions read from a fixed payload, which would
    let a grant that never landed read back as present -- the exact failure mode
    the readback exists to catch.
    """

    def __init__(self):
        super().__init__()
        self.catalog_grants = {"alice@example.com": {"ALL_PRIVILEGES"}}
        self.catalog_patch_response = None

    _CATALOG_PERMISSIONS = (
        "https://test.cloud.databricks.com"
        "/api/2.1/unity-catalog/permissions/catalog/wsh_alice"
    )

    def patch(self, url, headers=None, json=None, timeout=None):
        if "changes" in (json or {}) and url == self._CATALOG_PERMISSIONS:
            self.calls.append(("PATCH", url, json))
            if self.catalog_patch_response is not None:
                return self.catalog_patch_response
            for change in json["changes"]:
                self.catalog_grants.setdefault(change["principal"], set()).update(
                    change.get("add", [])
                )
            return _Resp(200, {})
        return super().patch(url, headers=headers, json=json, timeout=timeout)

    def get(self, url, headers=None, params=None, timeout=None):
        if not params and url == self._CATALOG_PERMISSIONS:
            self.calls.append(("GET", url, {}))
            return _Resp(200, {
                "privilege_assignments": [
                    {"principal": principal, "privileges": sorted(privileges)}
                    for principal, privileges in self.catalog_grants.items()
                ]
            })
        return super().get(url, headers=headers, params=params, timeout=timeout)


def _fake_with_attendee_app(client_id="app-sp-99"):
    fake = _CatalogAwareRequests()
    host = "https://test.cloud.databricks.com"
    for get in _strict_fake().gets.items():
        fake.gets.setdefault(*get)
    app = {
        "name": "showroom",
        "creator": "app-sp",
        "create_time": "1970-01-01T00:18:20Z",
    }
    if client_id:
        app["service_principal_client_id"] = client_id
    fake.register_get(f"{host}/api/2.0/apps", {"page_size": 100}, {"apps": [app]})
    fake.register_patch(f"{host}/api/2.0/permissions/apps/showroom", {"CAN_MANAGE"})
    return fake


def _catalog_grant_calls(fake, principal):
    return [
        call
        for call in fake.calls
        if call[0] == "PATCH"
        and "unity-catalog/permissions/catalog" in call[1]
        and call[2]["changes"][0]["principal"] == principal
    ]


def test_attendee_app_service_principal_is_granted_on_the_catalog(
    monkeypatch, enabled
):
    fake = _fake_with_attendee_app()
    mgr = _scoped_mgr(monkeypatch, fake)

    result = mgr.reconcile("alice@example.com")

    assert result["errors"] == []
    granted = _catalog_grant_calls(fake, "app-sp-99")
    assert granted, "the app's own principal was never granted on the catalog"
    added = set(granted[0][2]["changes"][0]["add"])
    # Read is what the room was blocked on; write is here so an app that
    # persists something is not the next incident. MANAGE deliberately is not.
    assert {"USE_CATALOG", "SELECT"} <= added
    assert "MANAGE" not in added
    assert fake.catalog_grants["app-sp-99"] >= {"USE_CATALOG", "SELECT"}

    entry = [
        d
        for d in result["handoff"]["details"]
        if d["resource_type"] == "app-service-principals"
    ][0]
    assert entry["state"] == "handed_off"
    assert entry["resource_id"] == "showroom"


def test_app_service_principal_grant_is_not_reissued_every_sweep(
    monkeypatch, enabled
):
    """A grant that is already proven is not re-sent.

    The reconciler sweeps every few minutes, and at a hundred instances a
    redundant control-plane write per app per sweep is the shape of load that
    produced the 429s in the servco handoff ledgers.
    """
    fake = _fake_with_attendee_app()
    mgr = _scoped_mgr(monkeypatch, fake)

    mgr.reconcile("alice@example.com")
    after_first = len(_catalog_grant_calls(fake, "app-sp-99"))
    mgr.reconcile("alice@example.com")

    assert after_first == 1
    assert len(_catalog_grant_calls(fake, "app-sp-99")) == 1


def test_app_service_principal_grant_failure_is_visible_and_retried(
    monkeypatch, enabled
):
    fake = _fake_with_attendee_app()
    fake.catalog_patch_response = _Resp(403, {}, "PERMISSION_DENIED")
    mgr = _scoped_mgr(monkeypatch, fake)

    result = mgr.reconcile("alice@example.com")

    assert any("app-sp-99" in e for e in result["errors"])
    assert mgr.status()["ok"] is False
    entry = [
        d
        for d in result["handoff"]["details"]
        if d["resource_type"] == "app-service-principals"
    ][0]
    assert entry["state"] == "failed"
    assert "403" in entry["error"]

    # Not memoized, so the next sweep tries again -- otherwise a single 429
    # would leave the app permanently unable to read.
    fake.catalog_patch_response = None
    second = mgr.reconcile("alice@example.com")
    assert not any("app-sp-99" in e for e in second["errors"])
    assert fake.catalog_grants["app-sp-99"] >= {"USE_CATALOG", "SELECT"}


def test_grant_that_does_not_land_is_caught_by_the_readback(monkeypatch, enabled):
    """A 200 from the permissions PATCH is not proof the privilege is held."""
    fake = _fake_with_attendee_app()

    def _swallow(url, headers=None, json=None, timeout=None):
        fake.calls.append(("PATCH", url, json))
        return _Resp(200, {})

    original = fake.patch

    def patch(url, headers=None, json=None, timeout=None):
        if "changes" in (json or {}) and json["changes"][0]["principal"] == "app-sp-99":
            return _swallow(url, headers, json, timeout)
        return original(url, headers=headers, json=json, timeout=timeout)

    fake.patch = patch
    mgr = _scoped_mgr(monkeypatch, fake)

    result = mgr.reconcile("alice@example.com")

    assert any("missing" in e and "app-sp-99" in e for e in result["errors"])
    assert mgr.status()["ok"] is False


def test_app_without_a_principal_yet_is_not_reported(monkeypatch, enabled):
    """A just-created app has no principal for a moment. That is not a fault."""
    fake = _fake_with_attendee_app(client_id=None)
    mgr = _scoped_mgr(monkeypatch, fake)

    result = mgr.reconcile("alice@example.com")

    assert result["errors"] == []
    assert not _catalog_grant_calls(fake, "app-sp-99")
    assert not [
        d
        for d in result["handoff"]["details"]
        if d["resource_type"] == "app-service-principals"
    ]


def test_foreign_app_principal_is_left_alone(monkeypatch, enabled):
    """An app this SP did not create is not the attendee's build.

    In practice that is the Workshop Terminal itself, whose grants belong to
    Control Tower. Granting on an unverified creator is how a reconciler widens
    access to something it does not own.
    """
    fake = _fake_with_attendee_app()
    host = "https://test.cloud.databricks.com"
    fake.register_get(f"{host}/api/2.0/apps", {"page_size": 100}, {
        "apps": [{
            "name": "someone-elses",
            "creator": "other-sp",
            "create_time": "1970-01-01T00:18:20Z",
            "service_principal_client_id": "app-sp-99",
        }]
    })
    mgr = _scoped_mgr(monkeypatch, fake)

    result = mgr.reconcile("alice@example.com")

    assert result["errors"] == []
    assert not _catalog_grant_calls(fake, "app-sp-99")


# --- Fleet-safe reconciliation scheduling -----------------------------------


class _Clock:
    def __init__(self, value=10_000.0):
        self.value = float(value)

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += seconds


def _scheduled_mgr(monkeypatch, fake, clock, *, jitter=lambda: 0.5):
    from server import entitlements

    monkeypatch.setenv("DATABRICKS_CLIENT_ID", "app-client-id")
    monkeypatch.setattr(entitlements, "requests", fake)
    monkeypatch.setattr(entitlements, "_sp_bearer", lambda: "sp-bearer")
    return entitlements.EntitlementManager(
        run_started_at=1_000,
        clock=clock,
        monotonic=clock,
        jitter=jitter,
    )


def test_resource_exhausted_enters_full_jitter_backoff_and_keeps_last_good(
    monkeypatch, enabled
):
    fake = _fake_with_resources()
    clock = _Clock()
    mgr = _scheduled_mgr(monkeypatch, fake, clock, jitter=lambda: 0.5)
    assert mgr.reconcile("alice@example.com")["errors"] == []
    last_good = mgr.status()["handoff"]

    host = "https://test.cloud.databricks.com"
    fake.register_get(
        f"{host}/api/2.2/jobs/list",
        {"limit": 100},
        {"error_code": "RESOURCE_EXHAUSTED"},
        status=429,
        text="RESOURCE_EXHAUSTED",
        headers={"Retry-After": "4"},
    )
    calls_before_limit = len(fake.calls)
    limited = mgr.reconcile("alice@example.com")

    status = mgr.status()
    assert limited["state"] == "degraded_backoff"
    assert limited["reason"] == "resource_exhausted"
    assert limited["requests"]["rate_limited"] == 1
    assert limited["requests"]["http_429"] == 1
    assert limited["requests"]["count"] == 5
    assert status["state"] == "degraded_backoff"
    assert status["ok"] is True
    assert status["handoff"] == last_good
    assert status["backoff_attempt"] == 1
    assert status["backoff_seconds"] == 4
    assert status["next_attempt_at"] == clock() + 4

    # Session/admin pressure cannot turn a Retry-After into a tight loop.
    calls_after_limit = len(fake.calls)
    mgr.wake("session_start")
    deferred = mgr.reconcile("alice@example.com")
    assert deferred["deferred"] is True
    assert len(fake.calls) == calls_after_limit
    assert calls_after_limit > calls_before_limit

    clock.advance(4)
    fake.register_get(
        f"{host}/api/2.2/jobs/list",
        {"limit": 100},
        {
            "jobs": [
                {
                    "job_id": 11,
                    "creator_user_name": "app-sp",
                    "created_time": 1_100_000,
                }
            ]
        },
    )
    recovered = mgr.reconcile("alice@example.com", source="background")
    assert recovered["errors"] == []
    assert mgr.status()["state"] == "healthy"
    assert mgr.status()["backoff_attempt"] == 0


def test_plain_429_and_resource_exhausted_have_distinct_reasons(monkeypatch, enabled):
    from server import entitlements

    plain = entitlements._api_error(_Resp(429, {}, "too many requests"))
    exhausted = entitlements._api_error(
        _Resp(503, {"error_code": "RESOURCE_EXHAUSTED"}, "capacity")
    )
    assert plain.rate_limit_reason == "http_429"
    assert exhausted.rate_limit_reason == "resource_exhausted"


def test_exponential_backoff_is_capped(monkeypatch):
    from server import entitlements

    monkeypatch.setenv("ENTITLEMENT_BACKOFF_BASE", "5")
    monkeypatch.setenv("ENTITLEMENT_BACKOFF_CAP", "300")
    clock = _Clock()
    manager = entitlements.EntitlementManager(clock=clock, jitter=lambda: 1.0)
    stats = entitlements._RequestStats(
        rate_limits=1,
        http_429s=1,
        rate_limit_reason="http_429",
    )
    delays = []
    for _ in range(10):
        delay = manager._defer_backoff(stats, {})
        delays.append(delay)
        clock.advance(delay)

    assert delays[:7] == [5, 10, 20, 40, 80, 160, 300]
    assert delays[7:] == [300, 300, 300]


def test_baseline_429_stops_enumeration_and_is_not_overwritten_at_start(
    monkeypatch, enabled
):
    from server import entitlements

    fake = _strict_fake()
    host = "https://test.cloud.databricks.com"
    fake.register_get(
        f"{host}/api/2.0/pipelines",
        {"max_results": 100},
        {},
        status=429,
        text="rate limited",
    )
    clock = _Clock()
    mgr = _scheduled_mgr(monkeypatch, fake, clock, jitter=lambda: 0.5)

    mgr.start()
    try:
        status = mgr.status()
        assert status["state"] == "degraded_backoff"
        assert status["next_attempt_reason"] == "http_429"
        assert status["next_attempt_at"] == clock() + 3
        resource_gets = [
            call
            for call in fake.calls
            if call[0] == "GET"
            and any(
                call[1].endswith(spec.list_path)
                for spec in entitlements._RESOURCE_SPECS
            )
        ]
        assert len(resource_gets) == 1

        clock.advance(3)
        fake.register_get(
            f"{host}/api/2.0/pipelines",
            {"max_results": 100},
            {"statuses": []},
        )
        recovered = mgr.reconcile("alice@example.com", source="background")
        assert recovered["errors"] == []
        assert mgr.status()["baseline_ready"] == ["pipelines", "warehouses"]
        assert mgr.status()["state"] == "healthy"
    finally:
        mgr.stop()


def test_background_cache_refresh_is_targeted_and_session_start_wakes_idle(
    monkeypatch, enabled
):
    from server import entitlements

    fake = _fake_with_resources()
    clock = _Clock()
    mgr = _scheduled_mgr(monkeypatch, fake, clock)
    mgr.reconcile("alice@example.com")

    fake.calls.clear()
    unchanged = mgr.reconcile("alice@example.com", source="background")
    list_calls = [
        call
        for call in fake.calls
        if call[0] == "GET"
        and "/permissions/" not in call[1]
        and not call[1].endswith("/Me")
        and "/catalogs/" not in call[1]
    ]
    assert list_calls == []
    assert unchanged["requests"]["cache_hits"] == len(entitlements._RESOURCE_SPECS)
    assert mgr.status()["idle"] is True

    mgr.notify_resource_created("app")
    fake.calls.clear()
    targeted = mgr.reconcile("alice@example.com", source="background")
    resource_lists = [
        call[1]
        for call in fake.calls
        if call[0] == "GET"
        and any(
            call[1].endswith(spec.list_path) for spec in entitlements._RESOURCE_SPECS
        )
    ]
    assert resource_lists == ["https://test.cloud.databricks.com/api/2.0/apps"]
    assert targeted["requests"]["cache_misses"] == 1

    mgr.reconcile("alice@example.com", source="background")
    assert mgr.status()["idle"] is True
    mgr.wake("session_start")
    assert mgr.status()["idle"] is False
    assert mgr.status()["next_attempt_at"] == clock()
    assert mgr.status()["next_attempt_reason"] == "session_start"


def test_resource_wake_during_list_is_not_lost_or_recached(monkeypatch, enabled):
    from server import entitlements

    fake = _fake_with_resources()
    clock = _Clock()
    mgr = _scheduled_mgr(monkeypatch, fake, clock)
    assert mgr.reconcile("alice@example.com")["errors"] == []

    mgr.notify_resource_created("apps")
    original_enumerate = entitlements._enumerate
    notified = False

    def enumerate_with_concurrent_wake(host, bearer, spec):
        nonlocal notified
        items = original_enumerate(host, bearer, spec)
        if spec.label == "apps" and not notified:
            notified = True
            mgr.notify_resource_created("apps")
        return items

    monkeypatch.setattr(entitlements, "_enumerate", enumerate_with_concurrent_wake)
    first = mgr.reconcile("alice@example.com", source="background")

    status = mgr.status()
    assert first["errors"] == []
    assert status["idle"] is False
    assert status["next_attempt_at"] == clock()
    assert status["next_attempt_reason"] == "resource_created"
    assert status["cache"]["apps"]["cached"] is False

    fake.calls.clear()
    second = mgr.reconcile("alice@example.com", source="background")
    resource_lists = [
        call[1]
        for call in fake.calls
        if call[0] == "GET"
        and any(
            call[1].endswith(spec.list_path) for spec in entitlements._RESOURCE_SPECS
        )
    ]
    assert second["errors"] == []
    assert resource_lists == ["https://test.cloud.databricks.com/api/2.0/apps"]
    assert mgr.status()["cache"]["apps"]["cached"] is True


def test_hundred_instance_schedule_stays_under_request_budget(monkeypatch):
    """Default cadence remains inside the measured fleet planning envelope."""
    from server import config, entitlements

    monkeypatch.setenv("ENTITLEMENT_RECONCILE_INTERVAL", "300")
    monkeypatch.setenv("ENTITLEMENT_CACHE_TTL", "1800")
    monkeypatch.setenv("ENTITLEMENT_FLEET_REQUEST_BUDGET_PER_MINUTE", "400")
    rng = random.Random(20260825)
    scheduled: list[tuple[float, int]] = []
    baseline_specs = sum(
        spec.requires_baseline for spec in entitlements._RESOURCE_SPECS
    )
    base_requests = 4  # catalog grant/readback plus service-principal identity

    retry_delays = {
        entitlements.EntitlementManager(
            jitter=lambda fraction=fraction: fraction
        )._jittered_delay(1, 5)
        for fraction in (index / 99 for index in range(100))
    }
    assert len(retry_delays) == 100

    for _ in range(100):
        manager = entitlements.EntitlementManager(jitter=rng.random)
        # Baseline capture is eager and security-sensitive, but only touches the
        # two APIs that have no trustworthy creation timestamp.
        scheduled.append((0.0, baseline_specs))
        expiries = [manager._cache_lifetime() for _ in range(baseline_specs)]
        startup = manager._startup_delay()
        scheduled.append(
            (
                startup,
                base_requests + len(entitlements._RESOURCE_SPECS) - baseline_specs,
            )
        )
        expiries.extend(
            startup + manager._cache_lifetime()
            for _ in range(len(entitlements._RESOURCE_SPECS) - baseline_specs)
        )
        for expiry in expiries:
            while expiry < 3600:
                # One staggered cache miss plus catalog/identity verification.
                scheduled.append((expiry, base_requests + 1))
                expiry += manager._cache_lifetime()

    requests_by_minute = Counter({minute: 0 for minute in range(60)})
    for instant, request_total in scheduled:
        requests_by_minute[int(instant // 60)] += request_total

    assert max(requests_by_minute.values()) <= (
        config.entitlement_fleet_request_budget_per_minute()
    )
