"""Readiness must refuse an instance that cannot last the event, and the fleet
harness must find the instances that would strand an attendee.

The distinction this file defends: ``credentials`` asks whether the app
credential is healthy right now, and an instance can pass it at nine in the
morning and strand its attendee at noon. ``credential_durability`` asks the
question that has an answer before the room fills — can each plane be *kept*
alive for the rest of the event — so answering no is what should keep an
attendee off that instance.

Should, because the enforcement is Control Tower's item 9 and CT has not built
it: at ``databricks-labs-control-tower@e42228f`` provisioning admits on the Apps
deployment state and never calls ``/readyz``. What this file proves is that the
verdict is correct and machine-readable; a CT PR is what turns it into a gate.
"""

from __future__ import annotations

import importlib.util
import sys
import time
from pathlib import Path

import pytest

from server import readiness

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"


@pytest.fixture(scope="module")
def rehearse():
    spec = importlib.util.spec_from_file_location("rehearse", SCRIPTS / "rehearse.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["rehearse"] = module
    spec.loader.exec_module(module)
    return module


def event(hours: float) -> dict[str, str]:
    return {"WORKSHOP_EVENT_ENDS_AT": str(time.time() + hours * 3600)}


REMOTE = {"OMNIGENT_APP_URL": "https://omni.example.databricksapps.com"}


# --- the durability judgement --------------------------------------------


def test_a_rotating_app_credential_is_durable_however_short_its_token_is():
    """Rotation is the whole point: the token is short and always will be."""
    verdict = readiness.sustainability(
        {"state": "rotating", "token_expires_in": 300},
        {},
        8 * 3600,
        obo_renewing=True,
    )

    assert verdict["app_plane_durable"] is True
    assert verdict["sustainable"] is True


def test_a_static_credential_expiring_inside_the_event_is_not_durable():
    verdict = readiness.sustainability(
        {"state": "static", "token_expires_in": 3600}, {}, 8 * 3600, obo_renewing=True
    )

    assert verdict["app_plane_durable"] is False
    assert verdict["sustainable"] is False


def test_a_static_credential_outliving_the_event_is_accepted():
    verdict = readiness.sustainability(
        {"state": "static", "token_expires_in": 40000}, {}, 8 * 3600, obo_renewing=True
    )

    assert verdict["sustainable"] is True


def test_remote_omnigent_with_nothing_renewing_the_attendee_credential_fails():
    """The incident, expressed as a pre-flight check."""
    verdict = readiness.sustainability(
        {"state": "rotating"}, REMOTE, 8 * 3600, obo_renewing=False
    )

    assert verdict["attendee_plane_renewing"] is False
    assert verdict["sustainable"] is False


def test_a_local_deployment_is_not_judged_on_a_credential_it_does_not_use():
    verdict = readiness.sustainability(
        {"state": "rotating"}, {}, 8 * 3600, obo_renewing=False
    )

    assert verdict["sustainable"] is True


def test_no_declared_event_end_leaves_the_static_case_unjudged_rather_than_red():
    """An unanswerable question is not a failure — but the renewal loop is
    still checked, because that one has an answer either way."""
    unbounded = readiness.sustainability(
        {"state": "static", "token_expires_in": 60}, {}, None, obo_renewing=True
    )
    still_gated = readiness.sustainability(
        {"state": "static", "token_expires_in": 60}, REMOTE, None, obo_renewing=False
    )

    assert unbounded["sustainable"] is True
    assert still_gated["sustainable"] is False


# --- the gate -------------------------------------------------------------


def _evaluate(env: dict, credential: dict, *, obo_renewing: bool) -> dict:
    return readiness.evaluate(
        env=env,
        credential_status=credential,
        installer_status={},
        entitlement_status={},
        obo_status={},
        secret_protection_status={},
        obo_renewing=obo_renewing,
    )


def test_readyz_goes_red_when_the_credential_cannot_last_the_event():
    report = _evaluate(
        {**event(8), **REMOTE}, {"state": "rotating"}, obo_renewing=False
    )

    check = report["checks"]["credential_durability"]
    assert check["ok"] is False
    assert check.get("soft") is not True, "this must gate admission, not inform it"
    assert report["ready"] is False
    assert "renewing" in check["detail"]


def test_the_durability_check_is_green_on_a_correctly_wired_instance():
    report = _evaluate({**event(8), **REMOTE}, {"state": "rotating"}, obo_renewing=True)

    assert report["checks"]["credential_durability"]["ok"] is True


def test_durability_is_reported_next_to_the_verdict_for_the_operator():
    report = _evaluate({**event(8), **REMOTE}, {"state": "rotating"}, obo_renewing=True)

    assert set(report["durability"]) >= {
        "app_plane_durable",
        "attendee_plane_renewing",
        "sustainable",
    }


def test_a_live_instance_reports_the_new_check_over_http(client):
    body = client.get("/readyz").json()

    assert "credential_durability" in body["checks"]
    # The watcher runs under the app's lifespan, so a served instance has the
    # renewal loop the check asks about.
    assert body["durability"]["attendee_plane_renewing"] is True


def test_control_tower_acceptance_requires_the_durability_check():
    """CT admits on /readyz, and the two-instance gate enumerates what it needs.
    A check absent from that list is a check a fleet can ship without."""
    sys.path.insert(0, str(SCRIPTS))
    try:
        import ct_two_instance
    finally:
        sys.path.pop(0)

    assert "credential_durability" in ct_two_instance.REQUIRED_READY_CHECKS


# --- the fleet harness ----------------------------------------------------


def _instance(**overrides) -> dict:
    base = {
        "checks": {name: {"ok": True} for name in ("topology",)},
        "durability": {"app_plane_rotating": True, "attendee_plane_renewing": True},
        "ready": True,
        "release_manifest": {"skills": "v1"},
    }
    return {**base, **overrides}


def test_the_rehearsal_names_every_hard_check_a_build_fails_to_report(rehearse):
    """A build missing a check is worse than a build failing one: the fleet
    looks green because nobody asked."""
    problems = _run_inspect(rehearse, _instance())
    assert any("not reported" in problem for problem in problems)


def test_the_rehearsal_flags_a_collector_that_never_started(rehearse):
    problems = _run_inspect(
        rehearse,
        _instance(checks={name: {"ok": True} for name in rehearse.HARD_CHECKS}),
        collector={"running": False},
    )

    assert any("collector is not running" in problem for problem in problems)


def test_the_rehearsal_flags_a_static_credential_shorter_than_the_event(rehearse):
    problems = _run_inspect(
        rehearse,
        _instance(
            checks={name: {"ok": True} for name in rehearse.HARD_CHECKS},
            durability={
                "app_plane_rotating": False,
                "app_credential_expires_in": 3600,
                "attendee_plane_renewing": True,
            },
        ),
    )

    assert any("inside a 8h event" in problem for problem in problems)


def test_a_correctly_configured_instance_produces_no_findings(rehearse):
    problems = _run_inspect(
        rehearse, _instance(checks={name: {"ok": True} for name in rehearse.HARD_CHECKS})
    )

    assert problems == []


def test_release_drift_across_the_fleet_is_reported_even_when_every_instance_is_green(
    rehearse,
):
    results = [
        {"url": "https://a", "release_manifest": {"skills": "v1"}, "problems": []},
        {"url": "https://b", "release_manifest": {"skills": "v1"}, "problems": []},
        {"url": "https://c", "release_manifest": {"skills": "v2"}, "problems": []},
    ]

    drift = rehearse._fleet_drift(results)

    assert len(drift) == 1
    assert "https://c" in drift[0]


def _run_inspect(rehearse, ready: dict, collector: dict | None = None) -> list[str]:
    """Drive ``inspect`` against canned responses instead of a live app."""
    payloads = {
        "/readyz": ready,
        "/api/admin/diagnostics": {
            "errors": [],
            "collector": collector if collector is not None else {"running": True},
        },
    }
    original = rehearse._get
    rehearse._get = lambda base, path, token, **params: payloads[path]
    try:
        return rehearse.inspect("https://instance", "token", 8.0)["problems"]
    finally:
        rehearse._get = original
