"""Attendee App enrolment into the seat's account group (Control Tower option 2)."""

from __future__ import annotations

import importlib.machinery
import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import requests

from server import attendee_app_group as group

ROOT = Path(__file__).parents[1]
HELPER = ROOT / "assets" / "bin" / "workshop-app-deploy"
GROUP_ID = "1234567890"
HOST = "https://seat.cloud.databricks.com"


@pytest.fixture(autouse=True)
def _fresh_state():
    group.reset_for_tests()
    yield
    group.reset_for_tests()


class Response:
    def __init__(self, status_code: int):
        self.status_code = status_code


def _patcher(*statuses):
    calls = []
    queue = list(statuses)

    def patch(url, **kwargs):
        calls.append((url, kwargs))
        value = queue.pop(0)
        if isinstance(value, Exception):
            raise value
        return value if isinstance(value, Response) else Response(value)

    patch.calls = calls
    return patch


def test_unset_group_is_a_noop(monkeypatch):
    monkeypatch.delenv(group.GROUP_ENV, raising=False)
    assert group.group_id_from_env() == ""
    result = group.enrol(host=HOST, bearer="t", group_id="", member_scim_id="9", patch=_patcher())
    assert result.skipped == "unset" and result.attempts == 0


def test_enrol_patches_the_account_scim_group_with_the_numeric_member_id():
    patch = _patcher(200)
    result = group.enrol(host=HOST, bearer="wt-token", group_id=GROUP_ID,
                         member_scim_id="42", patch=patch, sleep=lambda _s: None)
    assert result.ok and result.attempts == 1
    url, kwargs = patch.calls[0]
    assert url == f"{HOST}/api/2.0/account/scim/v2/Groups/{GROUP_ID}"
    assert kwargs["headers"] == {"Authorization": "Bearer wt-token"}
    assert kwargs["json"] == {
        "schemas": ["urn:ietf:params:scim:api:messages:2.0:PatchOp"],
        "Operations": [{"op": "add", "path": "members", "value": [{"value": "42"}]}],
    }


def test_retry_after_on_throttling_is_honoured_and_capped():
    class Throttled(Response):
        def __init__(self, retry_after):
            super().__init__(429)
            self.headers = {"Retry-After": retry_after}

    sleeps = []
    patch = _patcher(Throttled("20"), Throttled("999"), 200)
    result = group.enrol(host=HOST, bearer="t", group_id=GROUP_ID, member_scim_id="42",
                         patch=patch, sleep=sleeps.append)
    assert result.ok and result.attempts == 3
    assert sleeps == [20.0, group.RETRY_AFTER_CAP_SECONDS]


def test_already_a_member_is_success():
    result = group.enrol(host=HOST, bearer="t", group_id=GROUP_ID, member_scim_id="42",
                         patch=_patcher(409), sleep=lambda _s: None)
    assert result.ok


def test_throttling_and_server_errors_retry_with_backoff_then_succeed():
    sleeps = []
    patch = _patcher(503, 429, 200)
    result = group.enrol(host=HOST, bearer="t", group_id=GROUP_ID, member_scim_id="42",
                         patch=patch, sleep=sleeps.append)
    assert result.ok and result.attempts == 3
    assert sleeps == [1.0, 2.0]


def test_transport_errors_are_retried_and_bounded():
    sleeps = []
    patch = _patcher(*[requests.ConnectionError("reset")] * 4)
    result = group.enrol(host=HOST, bearer="t", group_id=GROUP_ID, member_scim_id="42",
                         patch=patch, sleep=sleeps.append)
    assert not result.ok and result.attempts == 4
    assert sleeps == [1.0, 2.0, 4.0]
    assert "reset" in result.error


def test_client_errors_are_not_retried():
    patch = _patcher(403)
    result = group.enrol(host=HOST, bearer="t", group_id=GROUP_ID, member_scim_id="42",
                         patch=patch, sleep=lambda _s: None)
    assert not result.ok and result.attempts == 1 and result.status == 403
    assert len(patch.calls) == 1


def test_failed_add_cools_down_before_the_next_attempt(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(group.time, "monotonic", lambda: clock[0])
    failing = _patcher(403)
    group.enrol_once(host=HOST, bearer="t", group_id=GROUP_ID, member_scim_id="77",
                     app_name="notes", patch=failing, sleep=lambda _s: None)
    during = group.enrol_once(host=HOST, bearer="t", group_id=GROUP_ID, member_scim_id="77",
                              patch=failing, sleep=lambda _s: None)
    assert during.skipped == "cooling_down" and len(failing.calls) == 1
    clock[0] += group.FAILED_COOLDOWN_SECONDS + 1
    retry = _patcher(200)
    assert group.enrol_once(host=HOST, bearer="t", group_id=GROUP_ID, member_scim_id="77",
                            patch=retry, sleep=lambda _s: None).ok


def test_success_memo_expires_so_a_removed_member_is_restored(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(group.time, "monotonic", lambda: clock[0])
    group.enrol_once(host=HOST, bearer="t", group_id=GROUP_ID, member_scim_id="42",
                     patch=_patcher(200), sleep=lambda _s: None)
    clock[0] += group.ENROLLED_TTL_SECONDS + 1
    again = _patcher(200)
    group.enrol_once(host=HOST, bearer="t", group_id=GROUP_ID, member_scim_id="42",
                     patch=again, sleep=lambda _s: None)
    assert len(again.calls) == 1


def test_enrol_once_memoises_success_and_records_failures():
    patch = _patcher(200)
    group.enrol_once(host=HOST, bearer="t", group_id=GROUP_ID, member_scim_id="42",
                     patch=patch, sleep=lambda _s: None)
    again = group.enrol_once(host=HOST, bearer="t", group_id=GROUP_ID, member_scim_id="42",
                             patch=patch, sleep=lambda _s: None)
    assert again.skipped == "already_enrolled"
    assert len(patch.calls) == 1
    assert group.status()["enrolled"] == 1

    failing = _patcher(403)
    group.enrol_once(host=HOST, bearer="t", group_id=GROUP_ID, member_scim_id="77",
                     app_name="notes", patch=failing, sleep=lambda _s: None)
    assert group.status()["failed"] == 1
    assert "notes" in group.status()["last_error"]


def test_control_tower_managed_apps_are_excluded():
    assert group.is_control_tower_managed(
        {"service_principal_client_id": "wt-sp"}, own_client_id="wt-sp")
    assert group.is_control_tower_managed(
        {"name": "wt-1"}, own_app_name="wt-1")
    assert group.is_control_tower_managed(
        {"url": "https://omnigent-1.databricksapps.com/x"},
        omnigent_url="https://omnigent-1.databricksapps.com")
    assert not group.is_control_tower_managed(
        {"name": "notes", "url": "https://notes-1.databricksapps.com",
         "service_principal_client_id": "attendee-sp"},
        own_client_id="wt-sp", own_app_name="wt-1",
        omnigent_url="https://omnigent-1.databricksapps.com")


def _reconciler_method():
    from server.entitlements import EntitlementManager

    return EntitlementManager._enrol_attendee_app


def test_reconciler_enrols_attendee_apps_and_skips_ct_apps(monkeypatch):
    monkeypatch.setenv(group.GROUP_ENV, GROUP_ID)
    monkeypatch.setenv("DATABRICKS_CLIENT_ID", "wt-sp")
    monkeypatch.setenv("DATABRICKS_APP_NAME", "wt-1")
    seen = []
    monkeypatch.setattr(group, "enrol_once", lambda **kw: seen.append(kw))
    method = _reconciler_method()
    manager = SimpleNamespace()

    method(manager, HOST, "t", {"name": "wt-1", "service_principal_id": "1",
                                "service_principal_client_id": "wt-sp"})
    method(manager, HOST, "t", {"name": "notes", "service_principal_id": "77",
                                "service_principal_client_id": "attendee-sp"})
    method(manager, HOST, "t", {"name": "no-sp-yet"})

    assert [(call["member_scim_id"], call["app_name"]) for call in seen] == [("77", "notes")]


def test_reconciler_hook_is_a_noop_without_the_group(monkeypatch):
    monkeypatch.delenv(group.GROUP_ENV, raising=False)
    seen = []
    monkeypatch.setattr(group, "enrol_once", lambda **kw: seen.append(kw))
    _reconciler_method()(SimpleNamespace(), HOST, "t", {"name": "notes", "service_principal_id": "77"})
    assert seen == []


def test_reconciler_hook_never_raises(monkeypatch):
    monkeypatch.setenv(group.GROUP_ENV, GROUP_ID)

    def boom(**_kw):
        raise RuntimeError("scim exploded")

    monkeypatch.setattr(group, "enrol_once", boom)
    _reconciler_method()(SimpleNamespace(), HOST, "t", {"name": "notes", "service_principal_id": "77"})


# --- deploy helper -------------------------------------------------------------

def _load_helper():
    name = "workshop_app_deploy_group_test_module"
    loader = importlib.machinery.SourceFileLoader(name, str(HELPER))
    spec = importlib.util.spec_from_loader(name, loader)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    loader.exec_module(module)
    return module


deploy_tool = _load_helper()


class EnrolBackend:
    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    def enrol_app_group(self, project, group_id, member):
        self.calls.append((group_id, member))
        outcome = self.outcomes.pop(0) if self.outcomes else None
        if isinstance(outcome, Exception):
            raise outcome
        return {}


def _helper_controller(env, backend):
    return deploy_tool.DeployController(env, backend=backend, reporter=lambda *_a: None,
                                        poll_seconds=0)


APP_PAYLOAD = {"service_principal_id": "555", "service_principal_client_id": "attendee-sp"}


def test_helper_enrols_new_app_sp_once(monkeypatch):
    env = {"HOME": "/tmp", "WORKSHOP_ATTENDEE_APP_GROUP_ID": GROUP_ID, "DATABRICKS_CLIENT_ID": "wt-sp"}
    backend = EnrolBackend(None)
    state = {}
    controller = _helper_controller(env, backend)

    controller._enrol_attendee_group(state, Path("/tmp"), APP_PAYLOAD)
    controller._enrol_attendee_group(state, Path("/tmp"), APP_PAYLOAD)

    assert backend.calls == [(GROUP_ID, "555")]
    assert state["attendee_app_group"] == {"ok": True, "attempts": 1, "error": ""}


def test_helper_never_blocks_and_never_raises(monkeypatch):
    monkeypatch.setattr(deploy_tool.time, "sleep", lambda _s: pytest.fail("the poll loop must not sleep"))
    env = {"HOME": "/tmp", "WORKSHOP_ATTENDEE_APP_GROUP_ID": GROUP_ID, "DATABRICKS_CLIENT_ID": "wt-sp"}
    backend = EnrolBackend(*[RuntimeError("cli down")] * 40)
    state = {}
    controller = _helper_controller(env, backend)

    for _ in range(20):  # one attempt per poll, stopping at the cap
        controller._enrol_attendee_group(state, Path("/tmp"), APP_PAYLOAD)

    assert len(backend.calls) == 12
    assert state["attendee_app_group"]["ok"] is False
    assert "cli down" in state["attendee_app_group"]["error"]


def test_helper_skips_wt_itself_and_unset_env(monkeypatch):
    backend = EnrolBackend()
    own = {"HOME": "/tmp", "WORKSHOP_ATTENDEE_APP_GROUP_ID": GROUP_ID, "DATABRICKS_CLIENT_ID": "wt-sp"}
    _helper_controller(own, backend)._enrol_attendee_group(
        {}, Path("/tmp"), {"service_principal_id": "9", "service_principal_client_id": "wt-sp"})
    unset = {"HOME": "/tmp", "DATABRICKS_CLIENT_ID": "wt-sp"}
    _helper_controller(unset, backend)._enrol_attendee_group({}, Path("/tmp"), APP_PAYLOAD)
    assert backend.calls == []


def test_cli_already_member_conflict_counts_as_enrolled(monkeypatch):
    backend = deploy_tool.CliBackend({"HOME": "/tmp"})

    def conflict(*_args, **_kwargs):
        raise deploy_tool.CliError("HTTP 409 Conflict: member already exists")

    monkeypatch.setattr(backend, "_json", conflict)
    assert backend.enrol_app_group(Path("/tmp"), GROUP_ID, "555") == {"already_member": True}


def test_cli_backend_enrol_app_group_accepts_null_and_empty_scim_204_output(monkeypatch):
    """Databricks SCIM PATCH returns 204 No Content, which `databricks api patch --output json`
    prints as `null\\n`. That must succeed rather than raising CliError."""
    backend = deploy_tool.CliBackend({"HOME": "/tmp"})
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        stdout = "null\n" if len(calls) == 1 else ""
        return SimpleNamespace(returncode=0, stdout=stdout, stderr="")

    monkeypatch.setattr(deploy_tool.subprocess, "run", fake_run)
    assert backend.enrol_app_group(Path("/tmp"), GROUP_ID, "555") == {}
    assert backend.enrol_app_group(Path("/tmp"), GROUP_ID, "555") == {}
    assert len(calls) == 2
    assert calls[0][0][:4] == [
        "databricks",
        "api",
        "patch",
        f"/api/2.0/account/scim/v2/Groups/{GROUP_ID}",
    ]


def test_schemeless_urls_do_not_falsely_match_empty_netloc_as_ct_managed():
    assert not group.is_control_tower_managed(
        {"name": "notes", "url": "notes-without-scheme"},
        omnigent_url="omnigent-without-scheme",
    )
    assert not deploy_tool._control_tower_managed(
        {"name": "notes", "url": "notes-without-scheme"},
        {"OMNIGENT_APP_URL": "omnigent-without-scheme"},
    )


def test_reconciler_excludes_own_sp_from_sp_identities_when_client_id_env_unset(monkeypatch):
    monkeypatch.setenv(group.GROUP_ENV, GROUP_ID)
    monkeypatch.delenv("DATABRICKS_CLIENT_ID", raising=False)
    monkeypatch.delenv("DATABRICKS_APP_NAME", raising=False)
    seen = []
    monkeypatch.setattr(group, "enrol_once", lambda **kw: seen.append(kw))
    method = _reconciler_method()
    manager = SimpleNamespace()

    method(
        manager,
        HOST,
        "t",
        {"name": "wt-custom", "service_principal_id": "101", "service_principal_client_id": "wt-sp-uuid"},
        {"101", "wt-sp-uuid"},
    )
    method(
        manager,
        HOST,
        "t",
        {"name": "attendee-app", "service_principal_id": "202", "service_principal_client_id": "att-sp-uuid"},
        {"101", "wt-sp-uuid"},
    )
    assert [(call["member_scim_id"], call["app_name"]) for call in seen] == [("202", "attendee-app")]


def test_enrol_once_uses_post_attempt_monotonic_time_for_ttl_and_cooldown(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(group.time, "monotonic", lambda: clock[0])

    def slow_sleep(seconds: float) -> None:
        clock[0] += seconds

    # 3 failed attempts + 1 success takes 1.0 + 2.0 = 3.0s of sleep
    patch = _patcher(503, 503, 200)
    res = group.enrol_once(
        host=HOST,
        bearer="t",
        group_id=GROUP_ID,
        member_scim_id="42",
        patch=patch,
        sleep=slow_sleep,
    )
    assert res.ok
    # At 1000 + ENROLLED_TTL_SECONDS + 1 (1901.0), 3s after 1003.0 has NOT elapsed ENROLLED_TTL_SECONDS yet!
    clock[0] = 1000.0 + group.ENROLLED_TTL_SECONDS + 1.0
    still_cached = group.enrol_once(
        host=HOST,
        bearer="t",
        group_id=GROUP_ID,
        member_scim_id="42",
        patch=_patcher(200),
        sleep=slow_sleep,
    )
    assert still_cached.skipped == "already_enrolled"


def test_helper_excludes_the_omnigent_and_own_app_by_name_and_url(monkeypatch):
    backend = EnrolBackend()
    env = {"HOME": "/tmp", "WORKSHOP_ATTENDEE_APP_GROUP_ID": GROUP_ID, "DATABRICKS_CLIENT_ID": "wt-sp",
           "DATABRICKS_APP_NAME": "wt-1", "OMNIGENT_APP_URL": "https://omni-1.databricksapps.com"}
    controller = _helper_controller(env, backend)
    controller._enrol_attendee_group({}, Path("/tmp"), {"name": "wt-1", "service_principal_id": "1"})
    controller._enrol_attendee_group({}, Path("/tmp"), {"name": "omni-1", "service_principal_id": "2",
                                                       "url": "https://omni-1.databricksapps.com/"})
    assert backend.calls == []

