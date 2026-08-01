"""The discovery API surface (contract C6, phase 2).

Two properties carry the security weight here. First, a PTY helper has no proxy
identity, so submission authenticates through the capability token — and that
token must bind the record to one attendee, or a shared instance would let one
attendee's agent file discovery against another. Second, withdrawal is
browser-only: if a capability token could withdraw, an agent could quietly erase
what it captured.
"""

import pytest

from server import config, discovery

from . import synthetic_secrets as fake
from .conftest import ALICE, BOB


@pytest.fixture(autouse=True)
def fresh_store(monkeypatch):
    monkeypatch.setattr(discovery, "discovery_store", discovery.DiscoveryStore())
    return discovery.discovery_store


@pytest.fixture
def capture_on(monkeypatch):
    monkeypatch.setenv("WORKSHOP_INSIGHT_CAPTURE", "true")
    monkeypatch.delenv("DISCOVERY_ENABLED", raising=False)


def _capability(email: str) -> str:
    from server import user_content
    from server.users import user_manager

    user = user_manager.get(email)
    user_content._write_callback_capability(user)
    with open(user_content.callback_capability_path(user)) as fh:
        return fh.read().strip()


def _helper_headers(email: str) -> dict:
    return {"X-Workshop-Capability": _capability(email)}


# --- Submission --------------------------------------------------------------


def test_helper_can_submit_with_a_capability(client, capture_on, fresh_store):
    email = "disc-alice@example.com"
    resp = client.post(
        "/api/discovery",
        json={
            "email": email,
            "record_id": "r1",
            "agent": "claude",
            "confidence": "high",
            "use_case_title": "Real-time fraud scoring",
            "current_stack": ["Kafka", "Oracle"],
            "blockers": ["no CDC from Oracle"],
        },
        headers=_helper_headers(email),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["captured"] is True
    assert body["record_id"] == "r1"
    assert body["records"] == 1

    stored = fresh_store.for_attendee(email)[0]
    assert stored.use_case_title == "Real-time fraud scoring"
    assert stored.blockers == ["no CDC from Oracle"]


def test_submission_without_authentication_is_denied(client, capture_on):
    resp = client.post("/api/discovery", json={"email": "x@example.com", "goal": "g"})
    assert resp.status_code == 403


def test_capability_binds_the_record_to_its_own_attendee(
    client, capture_on, fresh_store
):
    """The one-workspace-per-attendee model is enforced, not assumed.

    A helper token names its attendee; a body that claims another must not be
    able to file discovery under that identity.
    """
    resp = client.post(
        "/api/discovery",
        json={"email": "victim@example.com", "goal": "not mine"},
        headers=_helper_headers("holder@example.com"),
    )
    assert resp.status_code == 403
    assert fresh_store.all_records() == []


def test_browser_caller_cannot_submit_for_someone_else(client, capture_on, fresh_store):
    resp = client.post(
        "/api/discovery", json={"email": "bob@example.com", "goal": "x"}, headers=ALICE
    )
    assert resp.status_code == 403
    assert fresh_store.all_records() == []


def test_unknown_fields_do_not_cost_the_record(client, capture_on, fresh_store):
    """The agent writes this from an instruction file and will improvise."""
    email = "improv@example.com"
    resp = client.post(
        "/api/discovery",
        json={
            "email": email,
            "use_case_title": "Churn",
            "enthusiasm_level": 11,
            "nested": {"anything": True},
        },
        headers=_helper_headers(email),
    )
    assert resp.status_code == 200
    assert resp.json()["captured"] is True
    assert fresh_store.for_attendee(email)[0].use_case_title == "Churn"


def test_empty_submission_is_accepted(client, capture_on):
    """Only record_id is required by the contract, and the server can mint that."""
    email = "empty@example.com"
    resp = client.post(
        "/api/discovery", json={"email": email}, headers=_helper_headers(email)
    )
    assert resp.status_code == 200
    assert resp.json()["record_id"]


def test_secrets_are_stripped_and_counted(client, capture_on, fresh_store):
    email = "leaky@example.com"
    resp = client.post(
        "/api/discovery",
        json={
            "email": email,
            "use_case_summary": f"we auth with {fake.DAPI_TOKEN}",
        },
        headers=_helper_headers(email),
    )
    assert resp.json()["redactions"] == 1
    assert "dapi" not in fresh_store.for_attendee(email)[0].use_case_summary


def test_resubmission_updates_rather_than_accumulates(client, capture_on, fresh_store):
    email = "iterate@example.com"
    headers = _helper_headers(email)
    for goal in ("explore", "migrate off Oracle"):
        client.post(
            "/api/discovery",
            json={"email": email, "record_id": "r1", "goal": goal},
            headers=headers,
        )
    records = fresh_store.for_attendee(email)
    assert len(records) == 1
    assert records[0].goal == "migrate off Oracle"


# --- Disabled behaviour ------------------------------------------------------


def test_disabled_capture_reports_not_captured_rather_than_failing(
    client, monkeypatch, fresh_store
):
    """The agent must not retry, and must not tell the attendee something broke."""
    monkeypatch.delenv("WORKSHOP_INSIGHT_CAPTURE", raising=False)
    email = "off@example.com"
    resp = client.post(
        "/api/discovery",
        json={"email": email, "goal": "anything"},
        headers=_helper_headers(email),
    )
    assert resp.status_code == 200
    assert resp.json() == {"captured": False, "reason": "disabled"}
    assert fresh_store.all_records() == []


def test_disabled_capture_still_authenticates_first(client, monkeypatch):
    """An unauthenticated caller must not learn the flag state."""
    monkeypatch.delenv("WORKSHOP_INSIGHT_CAPTURE", raising=False)
    resp = client.post("/api/discovery", json={"email": "x@example.com"})
    assert resp.status_code == 403


def test_discovery_off_within_capture_is_also_refused(client, monkeypatch, fresh_store):
    monkeypatch.setenv("WORKSHOP_INSIGHT_CAPTURE", "true")
    monkeypatch.setenv("DISCOVERY_ENABLED", "false")
    email = "signalonly@example.com"
    resp = client.post(
        "/api/discovery",
        json={"email": email, "goal": "x"},
        headers=_helper_headers(email),
    )
    assert resp.json()["captured"] is False
    assert fresh_store.all_records() == []


# --- Attendee transparency ---------------------------------------------------


def test_attendee_reads_back_their_own_records(client, capture_on, fresh_store):
    fresh_store.put(
        discovery.build_record(
            "alice@example.com", {"record_id": "r1", "use_case_title": "Churn"}
        )
    )
    body = client.get("/api/discovery", headers=ALICE).json()
    assert body["enabled"] is True
    assert [r["use_case_title"] for r in body["records"]] == ["Churn"]


def test_readback_is_scoped_to_the_caller(client, capture_on, fresh_store):
    fresh_store.put(
        discovery.build_record("alice@example.com", {"record_id": "r1", "goal": "a"})
    )
    assert client.get("/api/discovery", headers=BOB).json()["records"] == []


def test_readback_reports_disabled_without_leaking_records(
    client, monkeypatch, fresh_store
):
    fresh_store.put(discovery.build_record("alice@example.com", {"record_id": "r1"}))
    monkeypatch.delenv("WORKSHOP_INSIGHT_CAPTURE", raising=False)
    body = client.get("/api/discovery", headers=ALICE).json()
    assert body == {"enabled": False, "records": []}


def test_attendee_can_withdraw_their_own_record(client, capture_on, fresh_store):
    fresh_store.put(
        discovery.build_record("alice@example.com", {"record_id": "r1", "goal": "x"})
    )
    resp = client.post(
        "/api/discovery/redact", json={"record_id": "r1"}, headers=ALICE
    )
    assert resp.status_code == 200
    assert fresh_store.for_attendee("alice@example.com") == []


def test_withdrawal_cannot_reach_another_attendee(client, capture_on, fresh_store):
    fresh_store.put(
        discovery.build_record("alice@example.com", {"record_id": "r1", "goal": "x"})
    )
    resp = client.post("/api/discovery/redact", json={"record_id": "r1"}, headers=BOB)
    assert resp.status_code == 404
    assert len(fresh_store.for_attendee("alice@example.com")) == 1


def test_a_capability_token_confers_nothing_on_withdrawal(
    client, capture_on, fresh_store
):
    """A PTY capability must not withdraw — an agent could erase its own capture.

    The route depends on browser identity, so the capability header is simply
    ignored; the caller is whoever the proxy headers say. Asserting the record
    survives (rather than a status code) keeps this honest under the LOCAL_DEV
    identity fallback, which resolves a headerless caller to a dev principal.
    """
    fresh_store.put(
        discovery.build_record("alice@example.com", {"record_id": "r1", "goal": "x"})
    )
    resp = client.post(
        "/api/discovery/redact",
        json={"record_id": "r1"},
        headers=_helper_headers("alice@example.com"),
    )
    assert resp.status_code != 200
    assert len(fresh_store.for_attendee("alice@example.com")) == 1


def test_a_capability_cannot_stand_in_for_the_wrong_browser_identity(
    client, capture_on, fresh_store
):
    """Holding alice's capability must not let bob's session withdraw her record."""
    fresh_store.put(
        discovery.build_record("alice@example.com", {"record_id": "r1", "goal": "x"})
    )
    resp = client.post(
        "/api/discovery/redact",
        json={"record_id": "r1"},
        headers={**BOB, **_helper_headers("alice@example.com")},
    )
    assert resp.status_code == 404
    assert len(fresh_store.for_attendee("alice@example.com")) == 1


def test_withdrawing_an_unknown_record_is_a_404(client, capture_on):
    resp = client.post(
        "/api/discovery/redact", json={"record_id": "nope"}, headers=ALICE
    )
    assert resp.status_code == 404


# --- Harvest reconciliation --------------------------------------------------


def test_harvest_reports_the_record_count(client, capture_on, as_admin, fresh_store):
    """CT needs to distinguish "said nothing" from "we lost what they said".

    The push path is bounded and fail-soft, so an outage longer than the buffer
    drops events; the polled count is how CT notices.
    """
    email = "counted@example.com"
    client.post(
        "/api/discovery",
        json={"email": email, "record_id": "r1", "goal": "x"},
        headers=_helper_headers(email),
    )
    payload = client.get("/api/admin/stats", headers=ALICE).json()
    row = next(r for r in payload["users"] if r["email"] == email)
    assert row["discovery_records"] == 1


def test_harvest_never_carries_the_record_contents(
    client, capture_on, as_admin, fresh_store
):
    """Discovery text goes to CT over ingest, not through an operator-readable poll."""
    email = "private@example.com"
    client.post(
        "/api/discovery",
        json={"email": email, "use_case_summary": "our secret migration plan"},
        headers=_helper_headers(email),
    )
    body = client.get("/api/admin/stats", headers=ALICE).text
    assert "secret migration plan" not in body


def test_harvest_count_is_zero_when_discovery_is_off(
    client, monkeypatch, as_admin, fresh_store
):
    monkeypatch.delenv("WORKSHOP_INSIGHT_CAPTURE", raising=False)
    fresh_store.put(discovery.build_record("alice@example.com", {"record_id": "r1"}))
    payload = client.get("/api/admin/stats", headers=ALICE).json()
    for row in payload["users"]:
        assert row["discovery_records"] == 0


def test_stats_route_still_requires_admin(client, capture_on, as_non_admin):
    assert client.get("/api/admin/stats", headers=ALICE).status_code == 403


def test_config_flag_helpers_agree_with_the_endpoint(monkeypatch):
    monkeypatch.setenv("WORKSHOP_INSIGHT_CAPTURE", "true")
    assert config.discovery_enabled() is True
