"""Authenticated WT closeout, using real producers, persistence and final facts."""

import copy
import hashlib
import json
from pathlib import Path

import pytest

from server import closeout, discovery, help as help_module, insight_summary, stats
from server import event_emitter as emitter_module
from server.capture_journal import CaptureJournal, CaptureStorageError, LocalCaptureStorage
from server.event_emitter import EventEmitter
from server.users import user_manager

ADMIN = {"X-Forwarded-Email": "operator@example.com"}
OWNER = {"X-Forwarded-Email": "alice@example.com"}
BINDING = {"run_id": "api-run", "unit_id": "api-unit", "workspace_id": "123",
           "owner": "alice@example.com"}


def sha(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":"), allow_nan=False).encode()).hexdigest()


@pytest.fixture
def retained(client, monkeypatch, tmp_path):
    from server import attendee_names, identity, main

    monkeypatch.setenv("DATA_ROOT", str(tmp_path / "data"))
    monkeypatch.setenv("WORKSHOP_INSIGHT_CAPTURE", "true")
    storage = LocalCaptureStorage(tmp_path / "journal")
    emitter = EventEmitter(run_id=BINDING["run_id"], workspace_id=BINDING["workspace_id"],
                           ingest_url="", ingest_token="",
                           journal=CaptureJournal(storage, BINDING, {}))
    monkeypatch.setattr(emitter_module, "event_emitter", emitter)
    monkeypatch.setattr(main, "event_emitter", emitter)
    user_manager.configure_capture(emitter.journal, BINDING["owner"])
    discovery.discovery_store.configure_capture(emitter)
    stats.configure_capture(emitter.journal)
    help_module.configure_capture(emitter.journal)
    insight_summary.stamps.configure_capture(emitter.journal)
    attendee_names.configure_capture(emitter)
    identity.configure_capture(emitter)
    owner = user_manager.peek(BINDING["owner"])
    Path(owner.home, "projects").mkdir(parents=True)
    Path(owner.home, ".claude").mkdir()
    Path(owner.home, ".claude", "conversation.jsonl").write_text(
        json.dumps({"role": "user", "content": "Build customer analytics in Sydney"}) + "\n")
    counts = {"jobs": 0, "pipelines": 0, "apps": 0, "dashboards": 0}
    monkeypatch.setattr(stats, "_workspace_resources", lambda: stats._Census(counts, confirmed=counts))
    # Actual OS process draining is separately exercised with real processes;
    # this portable transport test injects verified no-writer evidence.
    monkeypatch.setattr(closeout, "_drain", lambda _timeout: {
        "complete": True, "issues": [], "outcomes": []})
    monkeypatch.setattr(insight_summary, "_ask_model", lambda *_a, **_k: pytest.fail("model called"))
    return emitter


def request(emitter):
    return {"schema_version": 1, "capture_id": "capture-api", "source": BINDING,
            "stream_id": emitter.stream_id, "drain_timeout_seconds": 5}


def seal(client, emitter):
    response = client.post("/api/admin/capture-closeout/begin", headers=ADMIN, json=request(emitter))
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["phase"] == "SEALED", result
    assert result["complete"] is True and result["ct_verification_required"] is True
    return result


def pages(client, kind):
    collected, after = [], 0
    while True:
        response = client.get("/api/admin/capture-closeout/entries", headers=ADMIN,
                              params={"capture_id": "capture-api", "kind": kind,
                                      "after": after, "limit": 1})
        assert response.status_code == 200, response.text
        page = response.json()
        collected.extend(page["entries"])
        after = page["high_water"]
        if not page["remaining"]:
            return collected


def test_full_api_close_ack_replay_restart_and_frozen_final_facts(client, as_admin, retained, monkeypatch):
    from server import main

    for goal in ("Explore", "Customer analytics – final"):
        response = client.post("/api/discovery", headers=OWNER, json={
            "email": BINDING["owner"], "record_id": "answer-1", "goal": goal})
        assert response.status_code == 200, response.text
    assert client.post("/api/discovery/redact", headers=OWNER,
                       json={"record_id": "answer-1"}).status_code == 200
    assert client.post("/api/help/raise", headers=OWNER, json={"note": "Need help"}).status_code == 200
    assert client.post("/api/help/messages", headers=OWNER,
                       json={"body": "Please check my deployment"}).status_code == 200
    sealed = seal(client, retained)
    manifest = sealed["manifest"]
    assert sha(manifest) == sealed["manifest_sha256"]
    original = {}
    for kind in ("insight", "help"):
        entries = original[kind] = pages(client, kind)
        stream = manifest["streams"][kind]
        assert stream["count"] == len(entries)
        assert stream["sha256"] == sha(entries)
        assert stream["entries"] == [{"seq": e["seq"], "sha256": sha(e)} for e in entries]
    revisions = [e["event"]["payload"] for e in original["insight"]
                 if e["event"]["type"] == "discovery.record"]
    assert len(revisions) == 3  # initial, edit and withdrawal remain in history
    final_facts = sealed["final_outputs"]["summary_facts"][BINDING["owner"]]["payload"]
    assert final_facts["generator"] == "extraction" and final_facts["model"] is None
    assert client.post("/api/admin/insight-events/ack", headers=ADMIN, json={
        "schema_version": 2, "stream_id": retained.stream_id,
        "through_seq": manifest["streams"]["insight"]["last_seq"]}).status_code == 200
    assert client.post("/api/admin/help/ack", headers=ADMIN, json={
        "through_seq": manifest["streams"]["help"]["last_seq"]}).status_code == 200

    replacement = EventEmitter(run_id=BINDING["run_id"], workspace_id=BINDING["workspace_id"],
                               ingest_url="", ingest_token="", journal=CaptureJournal(
                                   retained.journal.storage, BINDING, {}))
    monkeypatch.setattr(emitter_module, "event_emitter", replacement)
    monkeypatch.setattr(main, "event_emitter", replacement)
    before = copy.deepcopy(replacement.journal.read())
    for kind in original:
        assert pages(client, kind) == original[kind]
    assert client.get("/api/admin/capture-closeout/status", headers=ADMIN).json() == sealed
    assert client.post("/api/admin/capture-closeout/begin", headers=ADMIN,
                       json=request(replacement)).json() == sealed
    for final in (False, True):
        response = client.get("/api/admin/stats", headers=ADMIN, params={"final": final})
        assert response.status_code == 200
        assert response.json()["users"] == sealed["final_outputs"]["stats"]["users"]
    assert client.post("/api/help/raise", headers=OWNER, json={"note": "late"}).status_code == 409
    assert client.post("/api/discovery", headers=OWNER,
                       json={"email": BINDING["owner"], "goal": "late"}).status_code == 409
    assert replacement.journal.read() == before


def test_closeout_transport_requires_admin(client, as_non_admin, retained):
    for path in ("status", "entries?capture_id=capture-api&kind=help"):
        assert client.get("/api/admin/capture-closeout/" + path, headers=ADMIN).status_code == 403
    assert client.post("/api/admin/capture-closeout/begin", headers=ADMIN,
                       json=request(retained)).status_code == 403
    assert closeout.manager().status()["phase"] == "OPEN"


@pytest.mark.parametrize("changes", [{"schema_version": True}, {"schema_version": 2},
    {"drain_timeout_seconds": "5"}, {"capture_id": "bad / id"}, {"extra": 1}])
def test_invalid_close_request_never_establishes_barrier(client, as_admin, retained, changes):
    response = client.post("/api/admin/capture-closeout/begin", headers=ADMIN,
                           json={**request(retained), **changes})
    assert response.status_code == 422
    assert closeout.manager().status()["phase"] == "OPEN"


def test_conflicting_source_and_capture_never_replace_seal(client, as_admin, retained):
    bad = {**request(retained), "source": {**BINDING, "owner": "other@example.com"}}
    assert client.post("/api/admin/capture-closeout/begin", headers=ADMIN, json=bad).status_code == 409
    sealed = seal(client, retained)
    assert client.post("/api/admin/capture-closeout/begin", headers=ADMIN,
                       json={**request(retained), "capture_id": "other"}).status_code == 409
    assert client.get("/api/admin/capture-closeout/status", headers=ADMIN).json() == sealed


def test_close_storage_failure_is_generic_and_does_not_claim_complete(client, as_admin, retained, monkeypatch):
    body = request(retained)
    def offline(*_args, **_kwargs):
        raise CaptureStorageError("sensitive URL and credentials")

    monkeypatch.setattr(retained.journal, "read", offline)
    for method, path, kwargs in [
        (client.post, "begin", {"json": body}),
        (client.get, "status", {}),
        (client.get, "entries?capture_id=capture-api&kind=help", {}),
    ]:
        response = method("/api/admin/capture-closeout/" + path, headers=ADMIN, **kwargs)
        assert response.status_code == 503
        assert "sensitive" not in response.text
        assert response.json()["detail"] == "Workshop capture is held; retry when persistence is available."
