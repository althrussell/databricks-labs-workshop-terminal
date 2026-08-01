"""Discovery records: redaction, validation, store semantics, durability (C6).

This module handles the only attendee-authored content the app sends anywhere, so
the tests lean hardest on the two things that would make shipping it a mistake:
that credentials can't ride along in the prose, and that nothing is captured at
all when the operator didn't opt in.
"""

import json
import os
import stat

import pytest

from server import discovery
from server.discovery import DiscoveryRecord, DiscoveryStore, build_record, redact
from server.event_emitter import EventEmitter

from . import synthetic_secrets as fake
from .schema_assert import assert_schema


@pytest.fixture(autouse=True)
def capture_on(monkeypatch):
    monkeypatch.setenv("WORKSHOP_INSIGHT_CAPTURE", "true")
    monkeypatch.delenv("DISCOVERY_ENABLED", raising=False)


@pytest.fixture
def store():
    return DiscoveryStore()


@pytest.fixture(autouse=True)
def isolated_process_store(monkeypatch):
    """Keep the module-level store out of the tests that don't target it."""
    monkeypatch.setattr(discovery, "discovery_store", DiscoveryStore())
    return discovery.discovery_store


# --- Redaction ---------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        f"our token is {fake.DAPI_TOKEN}",
        fake.DAPI_SUFFIXED,
        fake.BEARER,
        f"token {fake.GITHUB_PAT}",
        "password=hunter2000",
        "DATABRICKS_CLIENT_SECRET: dose-abc123def456",
        f'api_key = "{fake.OPENAI_KEY}"',
        fake.PG_URL,
        fake.JWT,
        "hash 5d41402abc4b2a76b9719d911017c592abcdef0123456789",
    ],
)
def test_credential_shapes_are_stripped(text):
    clean, count = redact(text)
    assert count >= 1
    assert "[redacted]" in clean


def test_pem_body_is_removed_not_just_the_header():
    """A rule that matched only the BEGIN line would leave the key material."""
    pem = (
        "-----BEGIN RSA PRIVATE KEY-----\n"
        "MIIEowIBAAKCAQEAxyz123\nabcDEF456\n"
        "-----END RSA PRIVATE KEY-----"
    )
    clean, count = redact(f"here it is: {pem} thanks")
    assert count == 1
    assert "MIIEowIBAAKCAQEAxyz123" not in clean
    assert "BEGIN RSA PRIVATE KEY" not in clean


@pytest.mark.parametrize(
    "text",
    [
        "We run a Kafka to Snowflake pipeline for 400 stores",
        "Our team is called platform-eng and we use dbt",
        "Migrating off Teradata by Q3, blocked on CDC",
        "We have 12 billion rows in one table",
        "contact me at alice@acme.com",
        # A long unbroken lowercase identifier. A length-only base64 rule would
        # redact this, and it's the kind of thing a stack description contains.
        "table customerinteractionaggregatedailysnapshot",
    ],
)
def test_ordinary_business_prose_survives(text):
    """Over-redaction is a real failure: shredded prose is worthless in a brief."""
    clean, count = redact(text)
    assert count == 0
    assert clean == text


def test_high_entropy_base64_is_still_caught():
    """The entropy guard must not become a bypass."""
    clean, count = redact("creds aB3dEfGh1jKlMn0pQrStUvWxYz012345aB3dEfGh==")
    assert count == 1
    assert "[redacted]" in clean


def test_redaction_counts_every_hit():
    clean, count = redact(f"password=abc12345 and {fake.DAPI_TOKEN}")
    assert count == 2
    assert "abc12345" not in clean


def test_empty_text_is_not_counted():
    assert redact("") == ("", 0)


# --- Building a record -------------------------------------------------------


def test_only_a_record_id_is_synthesised_when_absent():
    """The agent may omit an id; losing the record over it would be worse."""
    record = build_record("labuser007@x.com", {})
    assert record.record_id
    assert record.captured_at


def test_partial_record_is_accepted():
    """The normal case — an attendee who described blockers but no timeline."""
    record = build_record(
        "labuser007@x.com",
        {"use_case_title": "Fraud scoring", "blockers": ["no CDC from Oracle"]},
    )
    assert record.use_case_title == "Fraud scoring"
    assert record.timeline == ""
    assert record.blockers == ["no CDC from Oracle"]


def test_redaction_happens_on_the_way_in():
    """Redacting at emit time would leave the secret in memory and the journal."""
    record = build_record(
        "labuser007@x.com",
        {"use_case_summary": f"we connect with {fake.DAPI_TOKEN}"},
    )
    assert "dapi" not in record.use_case_summary
    assert record.redactions == 1


def test_redaction_count_spans_text_and_list_fields():
    record = build_record(
        "labuser007@x.com",
        {
            "goal": "password=abcdef12",
            "current_stack": [fake.PG_URL_SHORT, "kafka"],
        },
    )
    assert record.redactions == 2
    assert "kafka" in record.current_stack


def test_unknown_confidence_is_not_treated_as_high():
    """An agent's invented enum value must not inflate a claim's weight."""
    assert build_record("a@x.com", {"confidence": "certain"}).confidence == ""
    assert build_record("a@x.com", {"confidence": "HIGH"}).confidence == "high"


def test_unknown_session_intent_is_dropped_rather_than_passed_through():
    """Absent has to keep meaning "unclassified". If an improvised value survived,
    a reader could not tell a Terminal that declined to classify from one that
    classified badly, and the field stops being a triage sort."""
    assert build_record("a@x.com", {"session_intent": "side_project"}).session_intent == ""
    assert build_record("a@x.com", {"session_intent": "FUN"}).session_intent == "fun"
    assert build_record("a@x.com", {"session_intent": " learning "}).session_intent == (
        "learning"
    )
    assert build_record("a@x.com", {}).session_intent == ""


def test_a_fun_session_is_a_record_not_an_omission():
    """The whole point of the field: a game reaches Control Tower as an answer."""
    record = build_record(
        "a@x.com",
        {"use_case_title": "Space Invaders game", "session_intent": "fun"},
    )
    payload = record.payload()

    assert payload["session_intent"] == "fun"
    assert payload["use_case_title"] == "Space Invaders game"


def test_session_intent_is_omitted_from_the_payload_when_unset():
    """Empty optional fields are omitted rather than sent as "" — on CT's side
    "didn't classify" and "classified as nothing" must not look alike."""
    assert "session_intent" not in build_record("a@x.com", {"goal": "learn"}).payload()


def test_unknown_fields_are_ignored_not_fatal():
    """The agent writes this from an instruction file and will improvise."""
    record = build_record(
        "a@x.com", {"use_case_title": "Churn", "vibes": "immaculate"}
    )
    assert record.use_case_title == "Churn"
    assert not hasattr(record, "vibes")


def test_comma_separated_strings_are_accepted_as_lists():
    """Agents reach for a string as often as a list; format shouldn't cost data."""
    record = build_record("a@x.com", {"current_stack": "Kafka, Snowflake , dbt"})
    assert record.current_stack == ["Kafka", "Snowflake", "dbt"]


def test_list_items_are_deduped():
    record = build_record("a@x.com", {"blockers": ["cost", "cost", "latency"]})
    assert record.blockers == ["cost", "latency"]


def test_oversized_text_is_truncated():
    record = build_record("a@x.com", {"use_case_summary": "x" * 10_000})
    assert len(record.use_case_summary) == discovery.MAX_TEXT_CHARS


def test_oversized_lists_are_capped():
    record = build_record("a@x.com", {"blockers": [f"b{i}" for i in range(500)]})
    assert len(record.blockers) == discovery.MAX_LIST_ITEMS


def test_non_list_non_string_list_field_is_dropped():
    assert build_record("a@x.com", {"blockers": {"a": 1}}).blockers == []


# --- Payload shape -----------------------------------------------------------


def test_payload_matches_the_shared_schema(tmp_path):
    from pathlib import Path

    schema = json.loads(
        (Path(__file__).resolve().parents[1] / "tests" / "fixtures"
         / "workshop-insight-events.schema.json").read_text()
    )
    emitter = EventEmitter(
        run_id="3f1b8c2e-9a44-4d21-8f0e-7c5b1a2d6e90",
        workspace_id="42",
        ingest_url="https://ct.example.com",
        ingest_token="t",
    )
    discovery.record(
        "labuser007@example.com",
        {
            "record_id": "rec-1",
            "agent": "claude",
            "confidence": "high",
            "use_case_title": "Real-time fraud scoring",
            "current_stack": ["Kafka", "Oracle"],
            "blockers": ["no CDC"],
        },
        emitter,
    )
    delivered = []
    emitter.drain(lambda ev: delivered.append(ev) or True)
    assert len(delivered) == 1
    assert_schema(delivered[0], schema)


def test_payload_omits_fields_the_attendee_never_addressed():
    """"Didn't say" and "said nothing" are different facts in a brief."""
    payload = build_record("a@x.com", {"use_case_title": "Churn"}).payload()
    assert payload["use_case_title"] == "Churn"
    assert "timeline" not in payload
    assert "blockers" not in payload


def test_payload_always_carries_id_and_timestamp():
    payload = build_record("a@x.com", {}).payload()
    assert payload["record_id"] and payload["captured_at"]


def test_payload_does_not_leak_the_attendee_identity():
    """The envelope carries the attendee; duplicating it invites divergence."""
    assert "attendee" not in build_record("a@x.com", {}).payload()


# --- Store semantics ---------------------------------------------------------


def test_resubmitting_a_record_id_updates_it(store):
    """Understanding improves through a conversation; CT must not keep all three."""
    store.put(build_record("a@x.com", {"record_id": "r1", "goal": "explore"}))
    store.put(build_record("a@x.com", {"record_id": "r1", "goal": "migrate off Oracle"}))
    records = store.for_attendee("a@x.com")
    assert len(records) == 1
    assert records[0].goal == "migrate off Oracle"


def test_records_are_scoped_per_attendee(store):
    store.put(build_record("a@x.com", {"record_id": "r1"}))
    store.put(build_record("b@x.com", {"record_id": "r1"}))
    assert store.count_for("a@x.com") == 1
    assert store.count_for("b@x.com") == 1
    assert len(store.all_records()) == 2


def test_per_attendee_cap_drops_new_records(store):
    """A looping agent must not be able to grow this without bound."""
    for i in range(discovery.MAX_RECORDS_PER_ATTENDEE):
        assert store.put(build_record("a@x.com", {"record_id": f"r{i}"})) is not None
    assert store.put(build_record("a@x.com", {"record_id": "overflow"})) is None
    assert store.count_for("a@x.com") == discovery.MAX_RECORDS_PER_ATTENDEE


def test_cap_still_allows_updating_existing_records(store):
    """Otherwise an attendee at the cap can never correct what was captured."""
    for i in range(discovery.MAX_RECORDS_PER_ATTENDEE):
        store.put(build_record("a@x.com", {"record_id": f"r{i}"}))
    updated = store.put(build_record("a@x.com", {"record_id": "r0", "goal": "fixed"}))
    assert updated is not None
    assert store.for_attendee("a@x.com")[0].goal == "fixed"


def test_counts_reports_every_attendee(store):
    store.put(build_record("a@x.com", {"record_id": "r1"}))
    store.put(build_record("a@x.com", {"record_id": "r2"}))
    store.put(build_record("b@x.com", {"record_id": "r1"}))
    assert store.counts() == {"a@x.com": 2, "b@x.com": 1}


# --- Attendee withdrawal -----------------------------------------------------


def test_withdrawal_blanks_the_content(store):
    store.put(build_record("a@x.com", {"record_id": "r1", "goal": "secret plan"}))
    tombstone = store.redact_record("a@x.com", "r1")
    assert tombstone is not None
    assert tombstone.goal == ""
    assert store.for_attendee("a@x.com") == []
    assert store.count_for("a@x.com") == 0


def test_withdrawal_survives_a_resubmission(store):
    """A tombstone, not a delete: re-submitting the id must not resurrect it.

    An agent that re-elicits the same use case after the attendee withdrew it
    would otherwise silently undo the withdrawal.
    """
    store.put(build_record("a@x.com", {"record_id": "r1", "goal": "secret plan"}))
    store.redact_record("a@x.com", "r1")
    assert store.put(build_record("a@x.com", {"record_id": "r1", "goal": "again"})) is None
    assert store.for_attendee("a@x.com") == []


def test_withdrawal_clears_the_session_intent_too(store):
    """The classification is derived from what they said, so it goes with the
    words. A tombstone reading `session_intent: business_problem` would still
    tell an account team this attendee was a lead."""
    store.put(
        build_record(
            "a@x.com",
            {"record_id": "r1", "goal": "secret plan", "session_intent": "business_problem"},
        )
    )
    tombstone = store.redact_record("a@x.com", "r1")

    assert tombstone is not None
    assert tombstone.session_intent == ""
    assert "session_intent" not in tombstone.payload()


def test_withdrawing_an_unknown_record_is_not_an_error(store):
    assert store.redact_record("a@x.com", "nope") is None


# --- Journal durability ------------------------------------------------------


def test_journal_round_trips(tmp_path):
    path = str(tmp_path / "discovery.json")
    first = DiscoveryStore(path)
    first.put(
        build_record(
            "a@x.com",
            {
                "record_id": "r1",
                "use_case_title": "Fraud",
                "blockers": ["no CDC"],
                "confidence": "high",
                "session_intent": "business_problem",
            },
        )
    )
    second = DiscoveryStore(path)
    assert second.load() == 1
    restored = second.for_attendee("a@x.com")[0]
    assert restored.use_case_title == "Fraud"
    assert restored.blockers == ["no CDC"]
    assert restored.confidence == "high"
    # A restart mid-workshop must not silently downgrade a record to unclassified.
    assert restored.session_intent == "business_problem"


def test_journal_is_owner_only(tmp_path):
    """The attendee has a shell in this container; the journal holds their narrative."""
    path = str(tmp_path / "discovery.json")
    DiscoveryStore(path).put(build_record("a@x.com", {"record_id": "r1"}))
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600


def test_journal_preserves_a_withdrawal(tmp_path):
    """A restart must not resurrect what the attendee withdrew."""
    path = str(tmp_path / "discovery.json")
    first = DiscoveryStore(path)
    first.put(build_record("a@x.com", {"record_id": "r1", "goal": "secret"}))
    first.redact_record("a@x.com", "r1")

    second = DiscoveryStore(path)
    second.load()
    assert second.for_attendee("a@x.com") == []
    assert second.put(build_record("a@x.com", {"record_id": "r1"})) is None


def test_corrupt_journal_is_survivable(tmp_path):
    path = tmp_path / "discovery.json"
    path.write_text("{not json")
    store = DiscoveryStore(str(path))
    assert store.load() == 0
    # And still usable afterwards — a bad file must not disable capture.
    assert store.put(build_record("a@x.com", {"record_id": "r1"})) is not None


def test_missing_journal_is_not_an_error(tmp_path):
    assert DiscoveryStore(str(tmp_path / "absent.json")).load() == 0


def test_journal_skips_unusable_entries(tmp_path):
    """One bad row must not cost the whole journal."""
    path = tmp_path / "discovery.json"
    path.write_text(
        json.dumps(
            {
                "records": [
                    {"record_id": "r1", "attendee": "a@x.com"},
                    {"record_id": "", "attendee": "a@x.com"},
                    {"attendee": "a@x.com"},
                    "nonsense",
                ]
            }
        )
    )
    store = DiscoveryStore(str(path))
    assert store.load() == 1


def test_unwritable_journal_does_not_break_capture(tmp_path):
    """Durability is best-effort; the attendee path is not."""
    store = DiscoveryStore(str(tmp_path / "nope" / "x" / "discovery.json"))
    os.chmod(tmp_path, 0o500)
    try:
        assert store.put(build_record("a@x.com", {"record_id": "r1"})) is not None
        assert store.count_for("a@x.com") == 1
    finally:
        os.chmod(tmp_path, 0o700)


def test_store_without_a_journal_still_works():
    """Capture on, SESSION_STATE_PATH unset — records live in memory only."""
    store = DiscoveryStore()
    assert store.put(build_record("a@x.com", {"record_id": "r1"})) is not None
    assert store.load() == 0


def test_journal_path_is_disabled_without_a_session_state_path(monkeypatch):
    monkeypatch.delenv("SESSION_STATE_PATH", raising=False)
    assert discovery.journal_path() == ""


def test_journal_path_sits_beside_the_session_journal(monkeypatch):
    monkeypatch.setenv("SESSION_STATE_PATH", "/data/sessions.json")
    assert discovery.journal_path() == "/data/discovery.json"


# --- Gating ------------------------------------------------------------------


def test_record_is_a_noop_when_capture_is_off(monkeypatch, isolated_process_store):
    """A deployment that never opted in must hold no discovery data at all."""
    monkeypatch.delenv("WORKSHOP_INSIGHT_CAPTURE", raising=False)
    emitter = EventEmitter(
        run_id="r", workspace_id="w", ingest_url="https://x", ingest_token="t"
    )
    assert discovery.record("a@x.com", {"goal": "anything"}, emitter) is None
    assert isolated_process_store.all_records() == []
    assert emitter.pending() == 0


def test_record_is_a_noop_when_only_discovery_is_off(
    monkeypatch, isolated_process_store
):
    """Capture on, conversational tier off — the operator's middle setting."""
    monkeypatch.setenv("DISCOVERY_ENABLED", "false")
    emitter = EventEmitter(
        run_id="r", workspace_id="w", ingest_url="https://x", ingest_token="t"
    )
    assert discovery.record("a@x.com", {"goal": "anything"}, emitter) is None
    assert isolated_process_store.all_records() == []


def test_record_stores_even_when_ingest_is_unconfigured(isolated_process_store):
    """Held locally so the wrap summary can still use it if CT arrives later."""
    emitter = EventEmitter(
        run_id="", workspace_id="", ingest_url="", ingest_token=""
    )
    assert discovery.record("a@x.com", {"goal": "explore"}, emitter) is not None
    assert isolated_process_store.count_for("a@x.com") == 1


def test_a_refinement_is_a_distinct_event_from_a_retried_flush(isolated_process_store):
    """Control Tower de-dupes on the key, so a revision has to be in it.

    With only the record_id in the key, an agent's improved second statement of
    the same use case looks exactly like the transport resending the first, and CT
    would keep the worse version forever.
    """
    emitter = EventEmitter(
        run_id="run-1", workspace_id="w", ingest_url="https://x", ingest_token="t"
    )
    for goal in ("first", "second"):
        discovery.record("a@x.com", {"record_id": "r1", "goal": goal}, emitter)
    delivered = []
    emitter.drain(lambda ev: delivered.append(ev) or True)

    assert [e["idempotency_key"] for e in delivered] == [
        "discovery:run-1:a@x.com:r1:1",
        "discovery:run-1:a@x.com:r1:2",
    ]
    assert [e["payload"]["revision"] for e in delivered] == [1, 2]
    assert delivered[-1]["payload"]["goal"] == "second"


def test_withdrawal_is_pushed_so_control_tower_can_honour_it(isolated_process_store):
    """The original is already in Lakebase by the time an attendee removes it."""
    emitter = EventEmitter(
        run_id="run-1", workspace_id="w", ingest_url="https://x", ingest_token="t"
    )
    discovery.record("a@x.com", {"record_id": "r1", "goal": "secret plan"}, emitter)
    assert discovery.withdraw("a@x.com", "r1", emitter) is True
    delivered = []
    emitter.drain(lambda ev: delivered.append(ev) or True)

    tombstone = delivered[-1]
    assert tombstone["idempotency_key"] == "discovery:run-1:a@x.com:r1:2"
    assert tombstone["payload"]["redacted_by_attendee"] is True
    # Nothing the attendee said may ride along with the revocation.
    assert "goal" not in tombstone["payload"]


def test_withdrawing_an_unknown_record_emits_nothing(isolated_process_store):
    emitter = EventEmitter(
        run_id="run-1", workspace_id="w", ingest_url="https://x", ingest_token="t"
    )
    assert discovery.withdraw("a@x.com", "nope", emitter) is False
    delivered = []
    emitter.drain(lambda ev: delivered.append(ev) or True)
    assert delivered == []


def test_json_round_trip_of_a_record():
    record = build_record(
        "a@x.com",
        {"record_id": "r1", "use_case_title": "Churn", "current_stack": ["dbt"]},
    )
    restored = DiscoveryRecord.from_json(record.to_json())
    assert restored is not None
    assert restored.to_json() == record.to_json()


def test_from_json_rejects_a_record_without_an_attendee():
    """An unattributable record can't be resolved to a company by CT."""
    assert DiscoveryRecord.from_json({"record_id": "r1"}) is None
