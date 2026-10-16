"""Portable WT-02 producer corpus for the later CT-02 consumer.

The rejection assertions are a reference contract, not CT commit verification.
CT-02 must use these same bytes while testing its actual Lakebase consumer.
"""

from __future__ import annotations

from collections import defaultdict
from copy import deepcopy
from datetime import datetime
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
import time
import uuid

import pytest

from .schema_assert import assert_schema
from server import artifacts, closeout, discovery, help as help_module, insight_summary, stats
from server import event_emitter as emitter_module
from server.artifacts import Harvest
from server.capture_journal import CaptureJournal, MemoryCaptureStorage
from server.closeout import CloseoutManager
from server.event_emitter import EventEmitter
from server.users import user_manager

FIXTURE = Path(__file__).parent / "fixtures" / "capture-closeout" / "v1-sealed.json"
INSIGHT_SCHEMA = Path(__file__).parent / "fixtures" / "workshop-insight-events.schema.json"
BINDING = {"run_id": "00000000-0000-4000-8000-000000000017",
           "unit_id": "00000000-0000-4000-8000-000000000018",
           "workspace_id": "7474659000000001", "owner": "labuser+199@example.com"}
STREAM = "golden-closeout-epoch-v1"
GENERATION = "00000000000040008000000000000019"
CAPTURE_ID = "golden-closeout-v1"
OBSERVED = "2026-11-17T06:30:00+00:00"
TIMESTAMP = datetime.fromisoformat(OBSERVED).timestamp()
REQUIRED = {"workshop.signal", "insight.summary", "discovery.record",
            "attendee.identity", "identity.resolved", "help"}


def sha256(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
        ensure_ascii=False, allow_nan=False).encode("utf-8")).hexdigest()


def load_fixture():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def drained(_timeout):
    return {"complete": True, "issues": [], "outcomes": []}


def new_emitter(storage):
    return EventEmitter(run_id=BINDING["run_id"], workspace_id=BINDING["workspace_id"],
        ingest_url="", ingest_token="", journal=CaptureJournal(storage, BINDING, {}))


def _produce(monkeypatch):
    """Use actual journal, discovery and help producers; freeze only observations."""
    monkeypatch.setenv("WORKSHOP_INSIGHT_CAPTURE", "true")
    monkeypatch.delenv("CONTROL_TOWER_URL", raising=False)
    observed = datetime.fromisoformat(OBSERVED)
    monkeypatch.setattr(emitter_module, "datetime", SimpleNamespace(now=lambda _timezone=None: observed))
    monkeypatch.setattr(emitter_module, "uuid", SimpleNamespace(uuid4=lambda: SimpleNamespace(hex=STREAM)))
    monkeypatch.setattr(closeout, "uuid", SimpleNamespace(uuid4=lambda: SimpleNamespace(hex=GENERATION)))
    storage = MemoryCaptureStorage()
    source = new_emitter(storage)
    monkeypatch.setattr(discovery, "discovery_store", discovery.DiscoveryStore())
    discovery.discovery_store.configure_capture(source)
    source.emit("attendee.identity", BINDING["owner"], {
        "attendee": BINDING["owner"], "binding_source": "control-tower", "observed_at": OBSERVED,
        "names": [{"name": "Zoë Example", "source": "wizard", "captured_at": OBSERVED}],
    }, idempotency_key=f"identity:{BINDING['run_id']}:{BINDING['owner']}:1")
    first = discovery.record(BINDING["owner"], {"record_id": "golden-discovery",
        "captured_at": OBSERVED, "goal": "Simplify customer reporting", "agent": "wizard"}, source)
    second = discovery.record(BINDING["owner"], {"record_id": "golden-discovery",
        "captured_at": OBSERVED, "goal": "Simplify weekly reporting", "agent": "claude"}, source)
    assert first.revision == 1 and second.revision == 2
    assert discovery.withdraw(BINDING["owner"], "golden-discovery", source)
    help_module.reset_for_tests()
    help_module.configure_capture(source.journal)
    ids = iter((uuid.UUID(int=1), uuid.UUID(int=2)))
    monkeypatch.setattr(help_module, "uuid4", lambda: next(ids))
    monkeypatch.setattr(help_module, "time", SimpleNamespace(time=lambda: TIMESTAMP,
        strftime=time.strftime, gmtime=lambda: time.gmtime(TIMESTAMP)))
    help_module.post_attendee_message("Where is the reporting guide?", sender=BINDING["owner"])
    help_module.post_attendee_message("Found it, thank you.", sender=BINDING["owner"])
    help_module.acknowledge_message("operator-golden-message")
    source.acknowledge(schema_version=2, stream_id=STREAM, through_seq=2)
    help_module.ack_outbox(1)
    signal = {"stats_schema_version": 3, "phase": "wrap", "minutes_building": 0,
        "agent_sessions": 0, "terminal_sessions": 0, "topics": [],
        "code": {"projects": 0, "commits": 0, "files": 0, "lines": 0},
        "resources": {"jobs": 0, "pipelines": 0, "apps": 0, "dashboards": 0},
        "errors": 0, "idle_seconds": None, "discovery_records": 0,
        "signal": {"engagement": "observer", "primary_topic": None, "topic_hits": {},
                   "products": [], "resource_kinds": [], "shipped": False},
        "final_capture_id": CAPTURE_ID}

    def finalizer(source, capture_id):
        key = f"signal:closeout:{capture_id}:{BINDING['owner']}"
        sequence = source.emit("workshop.signal", BINDING["owner"], signal, idempotency_key=key)
        return {"complete": True, "issues": [], "outputs": {
            "stats": {"stats_schema_version": 3, "users": [], "resources": signal["resources"],
                      "instance": {"phase": "wrap", "final": True, "final_capture_id": capture_id}},
            "summary_facts": {BINDING["owner"]: {"status": "no_material"}},
            "event_sequences": {key: sequence},
        }}

    now = [TIMESTAMP]
    manager = CloseoutManager(source, drainer=drained, finalizer=finalizer, clock=lambda: now[0])
    manager.begin(capture_id=CAPTURE_ID, stream_id=STREAM, source=BINDING, drain_timeout_seconds=120)
    now[0] += 1
    status = manager.advance(capture_id=CAPTURE_ID)
    pages, empty_pages = {}, {}
    for kind in ("insight", "help"):
        after, result = 0, []
        while True:
            page = manager.entries(capture_id=CAPTURE_ID, kind=kind, after=after, limit=2)
            result.append(page)
            after = page["high_water"]
            if page["remaining"] == 0:
                break
        pages[kind] = result
        empty_pages[kind] = manager.entries(capture_id=CAPTURE_ID, kind=kind, after=after, limit=2)
    return {"fixture_schema_version": 1, "description":
        "WT-02 producer golden bytes; CT-02 must independently persist and verify before authorizing teardown.",
        "expected_source": {**BINDING, "stream_id": STREAM},
        "pre_close_ack": {"insight": 2, "help": 1},
        "status": status, "pages": pages, "empty_pages": empty_pages,
        "negative_cases": NEGATIVE_CASES}, source, manager


NEGATIVE_CASES = [
    {"id": "manifest-digest-mismatch", "reject": "manifest digest", "mutations": [
        {"path": ["status", "manifest_sha256"], "value": "0" * 64}]},
    {"id": "missing-first-insight-record", "reject": "sequence gap", "mutations": [
        {"path": ["pages", "insight", 0, "entries", 0], "delete": True}]},
    {"id": "unsupported-manifest-schema", "reject": "manifest schema", "rehash_manifest": True, "mutations": [
        {"path": ["status", "manifest", "schema_version"], "value": 2}]},
    {"id": "source-workspace-mismatch", "reject": "source mismatch", "rehash_manifest": True, "mutations": [
        {"path": ["status", "manifest", "source", "workspace_id"], "value": "other-workspace"}]},
    {"id": "source-epoch-mismatch", "reject": "source mismatch", "rehash_manifest": True, "mutations": [
        {"path": ["status", "manifest", "source", "stream_id"], "value": "other-epoch"}]},
    {"id": "incomplete-manifest", "reject": "incomplete manifest", "rehash_manifest": True, "mutations": [
        {"path": ["status", "manifest", "complete"], "value": False},
        {"path": ["status", "manifest", "issues"], "value": ["final_outputs_incomplete"]}]},
    {"id": "incomplete-status", "reject": "incomplete status", "mutations": [
        {"path": ["status", "phase"], "value": "INCOMPLETE"}]},
    {"id": "unsupported-page-schema", "reject": "page schema", "mutations": [
        {"path": ["pages", "help", 0, "schema_version"], "value": 2}]},
    {"id": "wrong-page-generation", "reject": "generation mismatch", "mutations": [
        {"path": ["pages", "help", 0, "generation"], "value": "other-generation"}]},
    {"id": "changed-final-facts", "reject": "final output digest", "mutations": [
        {"path": ["status", "final_outputs", "summary_facts", BINDING["owner"], "status"], "value": "changed"}]},
]


def assert_consumer_contract(corpus):
    """Independent reference assertions, reusable against these portable bytes."""
    status, manifest = corpus["status"], corpus["status"]["manifest"]
    assert status["phase"] == "SEALED" and status["complete"] is True, "incomplete status"
    assert manifest["complete"] is True and manifest["issues"] == [], "incomplete manifest"
    assert manifest["schema_version"] == 1, "manifest schema"
    assert status["schema_version"] == 1 and manifest["capability"] == "sealed-closeout-v1", "status schema"
    assert sha256(manifest) == status["manifest_sha256"], "manifest digest"
    assert manifest["source"] == corpus["expected_source"], "source mismatch"
    assert sha256(status["final_outputs"]) == manifest["final_outputs_sha256"], "final output digest"
    assert status["capture_id"] == manifest["capture_id"] and status["generation"] == manifest["generation"]
    assert status["ct_verification_required"] is True
    assert set(manifest["required_categories"]) == REQUIRED
    groups = defaultdict(list)
    schema = json.loads(INSIGHT_SCHEMA.read_text())
    for kind, pages in corpus["pages"].items():
        entries = []
        expected = manifest["streams"][kind]
        for page in pages + [corpus["empty_pages"][kind]]:
            assert page["schema_version"] == 1, "page schema"
            assert page["generation"] == manifest["generation"], "generation mismatch"
            assert page["capture_id"] == manifest["capture_id"] and page["manifest_sha256"] == status["manifest_sha256"]
            assert page["kind"] == kind
        for page in pages:
            entries.extend(page["entries"])
            assert page["high_water"] == (entries[-1]["seq"] if entries else 0)
            assert page["remaining"] == expected["count"] - len(entries), "sequence gap"
        assert [entry["seq"] for entry in entries] == list(range(1, expected["count"] + 1)), "sequence gap"
        assert expected["first_seq"] == (1 if entries else 0) and expected["last_seq"] == len(entries)
        assert expected["sha256"] == sha256(entries), "stream digest"
        assert expected["entries"] == [{"seq": entry["seq"], "sha256": sha256(entry)} for entry in entries], "entry digest"
        assert expected["stream_id"] == corpus["expected_source"]["stream_id"], "source mismatch"
        assert corpus["empty_pages"][kind]["entries"] == []
        for entry in entries:
            if kind == "insight":
                event = entry["event"]
                assert event["run_id"] == corpus["expected_source"]["run_id"], "source mismatch"
                assert event["workspace_id"] == corpus["expected_source"]["workspace_id"], "source mismatch"
                assert_schema(event, schema)
                groups[event["type"]].append(entry)
            else:
                groups["help"].append(entry)
    for category in manifest["categories"]:
        assert category["count"] == len(groups[category["type"]])
        assert category["sha256"] == sha256(groups[category["type"]])
        assert category["required"] is (category["type"] in REQUIRED)


def mutated(corpus, case):
    result = deepcopy(corpus)
    for mutation in case["mutations"]:
        parent = result
        for segment in mutation["path"][:-1]:
            parent = parent[segment]
        if mutation.get("delete"):
            del parent[mutation["path"][-1]]
        else:
            parent[mutation["path"][-1]] = deepcopy(mutation["value"])
    if case.get("rehash_manifest"):
        result["status"]["manifest_sha256"] = sha256(result["status"]["manifest"])
        for kind in result["pages"]:
            for page in result["pages"][kind] + [result["empty_pages"][kind]]:
                page["manifest_sha256"] = result["status"]["manifest_sha256"]
    return result


def test_actual_producer_matches_shared_sealed_closeout_corpus(monkeypatch):
    produced, source, manager = _produce(monkeypatch)
    assert produced == load_fixture()
    assert_consumer_contract(produced)
    before = deepcopy(source.journal.read())
    assert manager.status() == produced["status"]
    assert source.journal.read() == before
    source.acknowledge(schema_version=2, stream_id=STREAM, through_seq=5)
    help_module.ack_outbox(3)
    replacement = CloseoutManager(new_emitter(source.journal.storage), drainer=drained,
        finalizer=lambda *_args: pytest.fail("sealed corpus was regenerated"))
    assert replacement.advance() == produced["status"]
    for kind, pages in produced["pages"].items():
        after = 0
        for page in pages:
            assert replacement.entries(capture_id=CAPTURE_ID, kind=kind, after=after, limit=2) == page
            after = page["high_water"]
    assert source.pending() == 0 and help_module.outbox_snapshot() == []


def test_golden_corpus_retains_withdrawals_acknowledged_help_and_explicit_zero_categories():
    corpus = load_fixture()
    assert_consumer_contract(corpus)
    entries = [entry for page in corpus["pages"]["insight"] for entry in page["entries"]]
    discovery_entries = [entry for entry in entries if entry["event"]["type"] == "discovery.record"]
    assert [entry["event"]["payload"]["revision"] for entry in discovery_entries] == [1, 2, 3]
    assert discovery_entries[-1]["event"]["payload"]["redacted_by_attendee"] is True
    assert "goal" not in discovery_entries[-1]["event"]["payload"]
    assert corpus["pages"]["help"][0]["entries"][0]["seq"] <= corpus["pre_close_ack"]["help"]
    assert corpus["status"]["final_outputs"]["summary_facts"][BINDING["owner"]] == {"status": "no_material"}
    categories = {row["type"]: row for row in corpus["status"]["manifest"]["categories"]}
    assert categories["insight.summary"]["count"] == 0
    assert categories["identity.resolved"]["count"] == 0
    assert "Zoë" in FIXTURE.read_text(encoding="utf-8")


@pytest.mark.parametrize("case", NEGATIVE_CASES, ids=lambda case: case["id"])
def test_shared_negative_cases_fail_reference_consumer_contract(case):
    corpus = load_fixture()
    assert case in corpus["negative_cases"]
    with pytest.raises(AssertionError, match=case["reject"]):
        assert_consumer_contract(mutated(corpus, case))


def test_actual_finalizer_events_validate_shared_insight_schema(monkeypatch, tmp_path):
    monkeypatch.setenv("WORKSHOP_INSIGHT_CAPTURE", "true")
    monkeypatch.setenv("DATA_ROOT", str(tmp_path))
    source = new_emitter(MemoryCaptureStorage())
    monkeypatch.setattr(emitter_module, "event_emitter", source)
    user_manager.configure_capture(source.journal, BINDING["owner"])
    discovery.discovery_store.configure_capture(source)
    stats.configure_capture(source.journal)
    insight_summary.stamps.configure_capture(source.journal)
    owner = user_manager.peek(BINDING["owner"])
    Path(owner.home, "projects").mkdir(parents=True)
    resources = {"jobs": 0, "pipelines": 0, "apps": 0, "dashboards": 0}
    monkeypatch.setattr(stats, "_workspace_resources", lambda: stats._Census(resources, confirmed=resources))
    monkeypatch.setattr(artifacts, "harvest_user", lambda *_args, **_kwargs: Harvest(prompts=["Build weekly reporting"], prompt_count=1))
    monkeypatch.setattr(insight_summary, "_ask_model", lambda *_args: (
        {"headline": "Earlier model narrative"}, "system.ai.claude-haiku-4-5"))
    insight_summary.summarise_user(owner, phase="build", emitter=source)
    manager = CloseoutManager(source, drainer=drained)
    manager.begin(capture_id=CAPTURE_ID, stream_id=source.stream_id, source=BINDING)
    status = manager.advance()
    assert status["complete"] is True
    schema = json.loads(INSIGHT_SCHEMA.read_text())
    events = list(source.journal.read()["insight"]["events"].values())
    for event in events:
        assert_schema(event, schema)
    summaries = [event for event in events if event["type"] == "insight.summary"]
    assert summaries[-1]["payload"]["generator"] == "extraction"
    assert summaries[-1]["payload"]["prior_narrative"]["generator"] == "llm"
    assert summaries[-1]["idempotency_key"].startswith("summary:closeout:")
