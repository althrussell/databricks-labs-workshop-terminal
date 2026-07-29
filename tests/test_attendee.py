"""Attendee identity binding on a single-attendee instance.

Regression cover for the outage where Control Tower's WORKSHOP_ATTENDEE_EMAIL
injection was dropped (it rode on the dedicated-Omnigent deploy path and
travelled as per-deployment env), leaving every attendee request 403 with no
way to recover.
"""

import os

import pytest


@pytest.fixture(autouse=True)
def _unbound_instance(monkeypatch):
    """Start every test from an unbound instance with no Control Tower hint.

    The binding lives on the session-scoped DATA_ROOT, so it must be cleared on
    both sides or it leaks into unrelated tests.
    """
    from server import attendee

    monkeypatch.delenv("WORKSHOP_ATTENDEE_EMAIL", raising=False)
    if os.path.exists(attendee.binding_path()):
        os.unlink(attendee.binding_path())
    yield
    if os.path.exists(attendee.binding_path()):
        os.unlink(attendee.binding_path())


@pytest.fixture()
def enforcing(monkeypatch):
    """Leave local-dev mode so the binding is actually enforced.

    Request this *after* ``client``: the app refuses to start outside local dev
    without a full OAuth environment.
    """
    monkeypatch.setenv("LOCAL_DEV", "0")


def test_first_attendee_binds_the_instance(client, enforcing, as_non_admin):
    """The regression: a missing injection must not lock the attendee out."""
    response = client.get(
        "/api/config", headers={"X-Forwarded-Email": "alice@example.com"}
    )

    assert response.status_code == 200

    from server import attendee

    assert attendee.resolved_email() == "alice@example.com"
    assert attendee.binding_source() == attendee.SOURCE_SELF_BOUND


def test_self_bound_instance_still_refuses_a_second_attendee(
    client, enforcing, as_non_admin
):
    assert (
        client.get(
            "/api/config", headers={"X-Forwarded-Email": "alice@example.com"}
        ).status_code
        == 200
    )

    response = client.get(
        "/api/config", headers={"X-Forwarded-Email": "bob@example.com"}
    )

    assert response.status_code == 403
    assert "assigned to alice@example.com" in response.json()["detail"]


def test_operator_cannot_claim_an_unbound_instance(client, enforcing, as_admin):
    response = client.get(
        "/api/config", headers={"X-Forwarded-Email": "op@example.com"}
    )

    assert response.status_code == 403
    assert "operator identity cannot claim it" in response.json()["detail"]

    from server import attendee

    assert attendee.resolved_email() == ""


def test_control_tower_hint_wins_over_a_persisted_binding(monkeypatch):
    from server import attendee

    attendee.bind("stale@example.com")
    monkeypatch.setenv("WORKSHOP_ATTENDEE_EMAIL", "alice@example.com")

    assert attendee.resolved_email() == "alice@example.com"
    assert attendee.binding_source() == attendee.SOURCE_CONTROL_TOWER


def test_binding_survives_a_restart(as_non_admin):
    """A new process reads the same volume, so the owner does not change."""
    from server import attendee

    assert attendee.bind("alice@example.com") == "alice@example.com"

    assert attendee.resolved_email() == "alice@example.com"
    assert attendee.bind("bob@example.com") == "alice@example.com"
    assert attendee.resolved_email() == "alice@example.com"


def test_bind_refuses_a_non_email_identity():
    from server import attendee

    with pytest.raises(ValueError, match="valid email address"):
        attendee.bind("service-principal")


def test_unbindable_caller_is_refused_without_binding_the_instance(
    client, enforcing, monkeypatch
):
    """A bearer service principal has no email identity, so it cannot own this."""
    from server import attendee, auth, config

    monkeypatch.setattr(auth, "get_groups", lambda principal: set())
    monkeypatch.setattr(
        auth,
        "_principal_from_headers",
        lambda headers: auth.Principal("service-principal", "sp-token"),
    )

    response = client.get("/api/config", headers={"Authorization": "Bearer sp-token"})

    assert response.status_code == 403
    assert "no attendee identity" in response.json()["detail"]
    assert attendee.resolved_email() == ""
    assert config.workshop_attendee_email() == ""


def test_shared_topology_needs_no_binding(client, enforcing, monkeypatch):
    from server import attendee

    monkeypatch.setenv("ALLOW_SHARED_TOPOLOGY", "true")

    assert (
        client.get(
            "/api/config", headers={"X-Forwarded-Email": "bob@example.com"}
        ).status_code
        == 200
    )
    assert attendee.resolved_email() == ""


def test_readiness_reports_the_binding_source(client, enforcing, as_non_admin):
    from server import attendee, readiness

    client.get("/api/config", headers={"X-Forwarded-Email": "alice@example.com"})

    report = readiness.evaluate_runtime()
    check = report["checks"]["attendee_identity"]

    assert check["ok"] is True
    assert check["source"] == attendee.SOURCE_SELF_BOUND


def test_readiness_flags_an_unbound_instance():
    from server import attendee, readiness

    report = readiness.evaluate_runtime()
    check = report["checks"]["attendee_identity"]

    assert check["ok"] is False
    assert check["source"] == attendee.SOURCE_UNBOUND
