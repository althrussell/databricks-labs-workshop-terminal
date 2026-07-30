"""SP-driven entitlement reconciler: UC catalog grant + non-UC CAN_MANAGE sweep,
idempotency, disabled-flag skip, and fail-soft health on error."""

import pytest
import requests


class _Resp:
    def __init__(self, status, payload=None, text=""):
        self.status_code = status
        self._payload = payload or {}
        self.text = text

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

    def register_get(self, url, params, payload, status=200, text=""):
        self.gets[(url, self._params(params))] = _Resp(status, payload, text)

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


def test_reconcile_without_attendee_fails_closed(monkeypatch, enabled):
    fake = _strict_fake()
    mgr = _mgr(monkeypatch, fake)
    from server.users import user_manager
    monkeypatch.setattr(user_manager, "all", lambda: [])

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
                "create_time": "1970-01-01T00:18:20Z",
            }],
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
    assert result["handoff"]["summary"]["handed_off"] == 7
    dashboard = [
        detail for detail in result["handoff"]["details"]
        if detail["resource_type"] == "dashboards"
    ][0]
    assert dashboard["state"] == "unsupported"
    assert "unsupported" in dashboard["error"]
    assert result["handoff"]["summary"]["unsupported"] == 1
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
