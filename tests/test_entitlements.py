"""SP-driven entitlement reconciler: UC catalog grant + non-UC CAN_MANAGE sweep,
idempotency, disabled-flag skip, and fail-soft health on error."""

from types import SimpleNamespace

import pytest
import requests


class _Resp:
    def __init__(self, status, payload=None, text=""):
        self.status_code = status
        self._payload = payload or {}
        self.text = text

    def json(self):
        return self._payload


class _FakeRequests:
    """Stand-in for the ``requests`` module: scripted list responses, records
    every call, and (optionally) fails one PATCH URL substring."""

    RequestException = requests.RequestException

    def __init__(self, fail_patch_substr=None):
        self.calls = []
        self._fail = fail_patch_substr
        self._lists = {
            "/api/2.1/jobs/list": {"jobs": [{"job_id": 11}]},
            "/api/2.0/pipelines": {"statuses": [{"pipeline_id": "p1"}]},
            "/api/2.0/serving-endpoints": {"endpoints": [{"id": "e1"}]},
            "/api/2.0/apps": {"apps": [{"name": "app1"}]},
            "/api/2.0/database/instances": {"database_instances": [{"name": "db1"}]},
        }

    def get(self, url, headers=None, timeout=None):
        self.calls.append(("GET", url))
        for suffix, payload in self._lists.items():
            if url.endswith(suffix):
                return _Resp(200, payload)
        return _Resp(404, {}, "not found")

    def patch(self, url, headers=None, json=None, timeout=None):
        self.calls.append(("PATCH", url, json))
        if self._fail and self._fail in url:
            return _Resp(500, {}, "boom")
        return _Resp(200, {})


@pytest.fixture()
def enabled(monkeypatch):
    monkeypatch.setenv("ENABLE_ENTITLEMENTS", "true")
    monkeypatch.setenv("WORKSHOP_CATALOG", "wsh_alice")


def _mgr(monkeypatch, fake):
    from server import entitlements

    monkeypatch.setattr(entitlements, "requests", fake)
    monkeypatch.setattr(entitlements, "_sp_bearer", lambda: "sp-bearer")
    return entitlements.EntitlementManager()


def test_disabled_reconcile_is_noop(monkeypatch):
    monkeypatch.delenv("ENABLE_ENTITLEMENTS", raising=False)
    from server import entitlements

    assert entitlements.EntitlementManager().reconcile("a@example.com") == {"enabled": False}


def test_reconcile_grants_catalog_and_non_uc(monkeypatch, enabled):
    fake = _FakeRequests()
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
    # One CAN_MANAGE grant per non-UC resource type.
    for perm_type in ("jobs", "pipelines", "serving-endpoints", "apps", "database-instances"):
        hit = [c for c in patches if f"/permissions/{perm_type}/" in c[1]]
        assert hit, f"missing CAN_MANAGE grant for {perm_type}"
        assert hit[0][2]["access_control_list"][0]["permission_level"] == "CAN_MANAGE"
        assert hit[0][2]["access_control_list"][0]["user_name"] == "alice@example.com"

    assert mgr.status()["ok"] is True


def test_reconcile_is_idempotent(monkeypatch, enabled):
    fake = _FakeRequests()
    mgr = _mgr(monkeypatch, fake)
    first = mgr.reconcile("alice@example.com")
    n = len(fake.calls)
    second = mgr.reconcile("alice@example.com")
    # Same call shape both runs (additive PATCH — re-running is a no-op server-side).
    assert first["errors"] == [] and second["errors"] == []
    assert len(fake.calls) == 2 * n


def test_failure_emits_health_and_never_raises(monkeypatch, enabled):
    fake = _FakeRequests(fail_patch_substr="unity-catalog/permissions/catalog")
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
    monkeypatch.delenv("ENABLE_ENTITLEMENTS", raising=False)
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
