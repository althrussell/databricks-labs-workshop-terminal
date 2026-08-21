"""Contract tests for workshop insight capture (C6).

The producer is this repo, the consumer is Control Tower, and they ship on
separate branches — so the schema and the worked examples are the only thing
keeping them honest. These tests assert three things:

1. The documented examples satisfy the schema.
2. The schema actually rejects the shapes it claims to reject (a schema that
   accepts everything passes point 1 and is worthless).
3. The doc, the schema, and the code agree on the enumerations and versions
   they each restate.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from .schema_assert import assert_schema

ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = ROOT / "tests" / "fixtures" / "workshop-insight-events.schema.json"
EXAMPLES_DIR = ROOT / "docs" / "examples"
CONTRACT_DOC = ROOT / "docs" / "workshop-insight-contract.md"

EXAMPLE_NAMES = (
    "workshop-signal.json",
    "workshop-signal-explorer.json",
    "discovery-record.json",
    "discovery-record-partial.json",
    "discovery-record-fun.json",
    "discovery-record-withdrawn.json",
    "insight-summary.json",
    "insight-summary-extraction.json",
    "attendee-identity.json",
)

EVENT_TYPES = (
    "workshop.signal",
    "discovery.record",
    "insight.summary",
    "attendee.identity",
)


@pytest.fixture(scope="module")
def schema() -> dict:
    return json.loads(SCHEMA_PATH.read_text())


def _example(name: str) -> dict:
    return json.loads((EXAMPLES_DIR / name).read_text())


# --- The examples satisfy the schema ----------------------------------------


@pytest.mark.parametrize("name", EXAMPLE_NAMES)
def test_example_matches_schema(schema: dict, name: str) -> None:
    assert_schema(_example(name), schema)


def test_examples_cover_every_event_type() -> None:
    covered = {_example(name)["type"] for name in EXAMPLE_NAMES}
    assert covered == set(EVENT_TYPES)


def test_examples_cover_both_summary_generators() -> None:
    """An extraction summary is thin by design and must be represented.

    Only exercising the LLM path would let the fallback's shape drift until it
    fails in the one situation it exists for — a wrap that never happened.
    """
    generators = {
        _example(name)["payload"]["generator"]
        for name in EXAMPLE_NAMES
        if _example(name)["type"] == "insight.summary"
    }
    assert generators == {"llm", "extraction"}


def test_examples_cover_explorer_and_builder() -> None:
    """The explorer case is the one most likely to be dropped, so pin it."""
    engagements = {
        _example(name)["payload"]["signal"]["engagement"]
        for name in EXAMPLE_NAMES
        if _example(name)["type"] == "workshop.signal"
    }
    assert {"explorer", "builder"} <= engagements


# --- The schema rejects what it claims to reject -----------------------------


def test_rejects_unknown_event_type(schema: dict) -> None:
    event = _example("workshop-signal.json")
    event["type"] = "insight.exfiltrate"
    with pytest.raises(AssertionError):
        assert_schema(event, schema)


def test_rejects_missing_run_id(schema: dict) -> None:
    """Without a run_id nothing can be attributed to a customer."""
    event = _example("discovery-record.json")
    del event["run_id"]
    with pytest.raises(AssertionError):
        assert_schema(event, schema)


def test_rejects_non_uuid_run_id(schema: dict) -> None:
    event = _example("discovery-record.json")
    event["run_id"] = "acme-workshop"
    with pytest.raises(AssertionError):
        assert_schema(event, schema)


def test_rejects_missing_idempotency_key(schema: dict) -> None:
    """De-dupe is the only thing stopping a retried flush double-counting."""
    event = _example("workshop-signal.json")
    del event["idempotency_key"]
    with pytest.raises(AssertionError):
        assert_schema(event, schema)


@pytest.mark.parametrize(
    "name,prefix",
    [
        ("workshop-signal.json", "signal:"),
        ("discovery-record.json", "discovery:"),
        ("insight-summary.json", "summary:"),
        ("attendee-identity.json", "identity:"),
    ],
)
def test_idempotency_key_prefix_is_enforced(
    schema: dict, name: str, prefix: str
) -> None:
    """Key shape decides whether repeats accumulate or supersede."""
    event = _example(name)
    assert event["idempotency_key"].startswith(prefix)
    event["idempotency_key"] = "whatever"
    with pytest.raises(AssertionError):
        assert_schema(event, schema)


def test_rejects_unknown_signal_field(schema: dict) -> None:
    """Payloads are closed so a producer-side addition is a deliberate change."""
    event = _example("workshop-signal.json")
    event["payload"]["signal"]["revenue_estimate_usd"] = 1_000_000
    with pytest.raises(AssertionError):
        assert_schema(event, schema)


def test_rejects_unknown_engagement_value(schema: dict) -> None:
    event = _example("workshop-signal.json")
    event["payload"]["signal"]["engagement"] = "enthusiastic"
    with pytest.raises(AssertionError):
        assert_schema(event, schema)


def test_rejects_unknown_confidence_value(schema: dict) -> None:
    """Confidence separates a customer commitment from an agent's inference."""
    event = _example("discovery-record.json")
    event["payload"]["confidence"] = "certain"
    with pytest.raises(AssertionError):
        assert_schema(event, schema)


def test_rejects_unknown_session_intent_value(schema: dict) -> None:
    """Intent is the triage datum. A free-text value would let a summariser file
    "maybe commercial?" and turn a four-way sort into a reading exercise."""
    event = _example("discovery-record.json")
    event["payload"]["session_intent"] = "side_project"
    with pytest.raises(AssertionError):
        assert_schema(event, schema)


def test_rejects_unknown_session_intent_on_a_summary(schema: dict) -> None:
    event = _example("insight-summary.json")
    event["payload"]["session_intent"] = "side_project"
    with pytest.raises(AssertionError):
        assert_schema(event, schema)


def test_both_events_share_one_session_intent_vocabulary(schema: dict) -> None:
    """The two are read side by side in one brief. Divergent enums would surface
    as a contradiction rather than a difference of opinion."""
    from server.discovery import SESSION_INTENTS

    payloads = _payload_schemas(schema)
    discovery = payloads["discovery.record"]["properties"]["session_intent"]["enum"]
    summary = payloads["insight.summary"]["properties"]["session_intent"]["enum"]

    assert discovery == summary
    assert set(discovery) == set(SESSION_INTENTS), "the code is the source of truth"


@pytest.mark.parametrize("intent", ("business_problem", "evaluation", "learning", "fun"))
def test_every_session_intent_validates_on_both_events(schema: dict, intent: str) -> None:
    for name in ("discovery-record.json", "insight-summary.json"):
        event = _example(name)
        event["payload"]["session_intent"] = intent
        assert_schema(event, schema)


def test_session_intent_stays_optional(schema: dict) -> None:
    """Absent means unclassified — an older Terminal build, or material that did
    not say. Requiring it would push agents into guessing."""
    for name in ("discovery-record.json", "insight-summary.json"):
        event = _example(name)
        event["payload"].pop("session_intent", None)
        assert_schema(event, schema)


def test_rejects_unknown_generator_value(schema: dict) -> None:
    event = _example("insight-summary.json")
    event["payload"]["generator"] = "vibes"
    with pytest.raises(AssertionError):
        assert_schema(event, schema)


def test_rejects_stale_stats_schema_version(schema: dict) -> None:
    """CT accepts stats v2 and v3 only; v1 is the skew this work closed."""
    event = _example("workshop-signal.json")
    event["payload"]["stats_schema_version"] = 1
    with pytest.raises(AssertionError):
        assert_schema(event, schema)


def test_rejects_artifact_contents(schema: dict) -> None:
    """Artifacts travel as metadata. Contents would reintroduce transcript capture."""
    event = _example("insight-summary.json")
    event["payload"]["artifacts"][0]["content"] = "# Architecture\nOur stack is..."
    with pytest.raises(AssertionError):
        assert_schema(event, schema)


def test_rejects_summary_without_headline(schema: dict) -> None:
    event = _example("insight-summary-extraction.json")
    del event["payload"]["headline"]
    with pytest.raises(AssertionError):
        assert_schema(event, schema)


def test_discovery_record_needs_only_id_and_timestamp(schema: dict) -> None:
    """A partial record is the normal case and must validate.

    Requiring completeness would push the agent into interrogating people, which
    is the failure mode this design is built to avoid.
    """
    event = _example("discovery-record.json")
    event["payload"] = {
        "record_id": event["payload"]["record_id"],
        "captured_at": event["payload"]["captured_at"],
        "revision": event["payload"]["revision"],
    }
    assert_schema(event, schema)


def test_discovery_record_requires_a_record_id(schema: dict) -> None:
    event = _example("discovery-record.json")
    del event["payload"]["record_id"]
    with pytest.raises(AssertionError):
        assert_schema(event, schema)


def test_type_specific_payloads_are_not_interchangeable(schema: dict) -> None:
    """A signal payload under a discovery type must not validate."""
    signal = _example("workshop-signal.json")
    event = copy.deepcopy(_example("discovery-record.json"))
    event["payload"] = signal["payload"]
    with pytest.raises(AssertionError):
        assert_schema(event, schema)


# --- The doc, the schema, and the code agree --------------------------------


def test_contract_doc_documents_every_event_type() -> None:
    doc = CONTRACT_DOC.read_text()
    for event_type in EVENT_TYPES:
        assert f"`{event_type}`" in doc, f"{event_type} is undocumented"


def test_contract_doc_documents_the_health_types() -> None:
    """These were silently rejected before this work; the fix must stay recorded."""
    doc = CONTRACT_DOC.read_text()
    for event_type in (
        "credential.health",
        "entitlements.health",
        "operational.health",
    ):
        assert f"`{event_type}`" in doc


def test_contract_doc_names_the_config_flags() -> None:
    doc = CONTRACT_DOC.read_text()
    assert "`WORKSHOP_INSIGHT_CAPTURE`" in doc
    assert "`DISCOVERY_ENABLED`" in doc
    # Default-off is the consent mechanism, so the doc must say so.
    assert "false" in doc


def test_contract_doc_records_the_transport_it_reuses() -> None:
    doc = CONTRACT_DOC.read_text()
    assert "/api/ingest/events" in doc
    assert "X-Ingest-Token" in doc
    assert "idempotency_key" in doc


def test_schema_and_doc_agree_on_stats_version(schema: dict) -> None:
    accepted = _signal_payload_schema(schema)["properties"]["stats_schema_version"]
    assert accepted["enum"] == [2, 3]
    assert "`3`" in CONTRACT_DOC.read_text()


def test_schema_stats_version_matches_the_code() -> None:
    """The schema's newest accepted version must be what the app actually emits."""
    from server.stats import STATS_SCHEMA_VERSION

    schema = json.loads(SCHEMA_PATH.read_text())
    accepted = _signal_payload_schema(schema)["properties"]["stats_schema_version"]
    assert STATS_SCHEMA_VERSION in accepted["enum"]
    assert max(accepted["enum"]) == STATS_SCHEMA_VERSION


def test_what_the_code_actually_emits_satisfies_the_schema(schema: dict) -> None:
    """The producer, not just the hand-written examples.

    Examples drift: they are edited by whoever last read the doc, while the wire
    payload is built by ``DiscoveryRecord.payload()``. Validating the real thing is
    the only check Control Tower can rely on.
    """
    from server.discovery import build_record

    record = build_record(
        "labuser007@example.com",
        {
            "record_id": "r1",
            "use_case_title": "Streaming feature store",
            "confidence": "medium",
            "current_stack": ["Kafka"],
        },
    )
    event = _example("discovery-record.json")
    event["payload"] = record.payload()
    assert_schema(event, schema)


def test_a_withdrawal_carries_no_content_the_attendee_revoked(schema: dict) -> None:
    from server.discovery import DiscoveryStore, build_record

    store = DiscoveryStore()
    store.put(build_record("labuser007@example.com", {"record_id": "r1", "goal": "x"}))
    tombstone = store.redact_record("labuser007@example.com", "r1")
    assert tombstone is not None

    payload = tombstone.payload()
    assert payload["redacted_by_attendee"] is True
    assert set(payload) == {"record_id", "captured_at", "revision", "redacted_by_attendee"}
    event = _example("discovery-record-withdrawn.json")
    event["payload"] = payload
    assert_schema(event, schema)


def test_the_discovery_idempotency_key_carries_the_revision(schema: dict) -> None:
    """Documented, exampled, and enforced — CT de-dupes on this string alone."""
    doc = CONTRACT_DOC.read_text()
    assert "discovery:{run_id}:{attendee}:{record_id}:{revision}" in doc
    for name in (
        "discovery-record.json",
        "discovery-record-partial.json",
        "discovery-record-fun.json",
        "discovery-record-withdrawn.json",
    ):
        event = _example(name)
        assert event["idempotency_key"].endswith(f":{event['payload']['revision']}")


def test_a_fun_build_is_recorded_rather_than_omitted() -> None:
    """The example exists because the alternative is a silent gap. A session that
    produces no record at all is indistinguishable from one nobody got to, so
    somebody chases it; `session_intent: fun` closes it in one glance."""
    payload = _example("discovery-record-fun.json")["payload"]

    assert payload["session_intent"] == "fun"
    assert payload["use_case_title"]
    # No blockers, no interest signals, no timeline — a fun build has none, and
    # inventing them to fill the record is the failure this guards against.
    assert "interest_signals" not in payload
    assert "timeline" not in payload


def test_what_the_summariser_actually_emits_satisfies_the_schema(schema: dict) -> None:
    """The real payload builder, not the hand-written example beside it.

    The summary is the only record of the session that outlives teardown, so a
    payload Control Tower rejects is unrecoverable — there is nothing left on the
    instance to re-derive it from.
    """
    from server.artifacts import Artifact, Harvest
    from server.insight_summary import _payload

    harvest = Harvest(
        prompts=["stream our orders topic into delta"],
        artifacts=[Artifact(kind="promote_doc", title="architecture.md", bytes=2048)],
        prompt_count=12,
        redactions=1,
    )
    for generator, model in (("llm", "system.ai.claude-haiku-4-5"), ("extraction", None)):
        payload = _payload(
            {
                "headline": "Streaming ingest prototype.",
                "what_they_built": "A bronze table off Kafka.",
                "use_cases": [{"title": "Real-time orders", "products": ["delta"]}],
                "blockers": ["PERMISSION_DENIED on main.sales"],
                "products": ["delta"],
            },
            harvest,
            run_id="3f1b8c2e-9a44-4d21-8f0e-7c5b1a2d6e90",
            email="labuser007@example.com",
            phase="build",
            generator=generator,
            model=model,
            revision=3,
        )
        event = _example("insight-summary.json")
        event["payload"] = payload
        event["idempotency_key"] = (
            f"summary:{event['run_id']}:{event['attendee']}:{generator}:3"
        )
        assert_schema(event, schema)


def test_the_summary_idempotency_key_carries_the_generator_and_revision() -> None:
    """Two things share this key, for two different reasons.

    The generator, because an extraction summary must be supersedable by a model
    one and a shared key would have CT drop the better of the pair as a duplicate.
    The revision, because the summary now rolls — without it, every regeneration
    after the first is silently discarded as a re-flush of the first.
    """
    doc = CONTRACT_DOC.read_text()
    assert "summary:{run_id}:{attendee}:{generator}:{revision}" in doc
    for name in ("insight-summary.json", "insight-summary-extraction.json"):
        event = _example(name)
        payload = event["payload"]
        assert event["idempotency_key"].endswith(
            f":{payload['generator']}:{payload['revision']}"
        )


def test_what_the_name_capture_actually_emits_satisfies_the_schema(schema: dict) -> None:
    from server import attendee_names

    observations = [
        attendee_names.NameObservation(
            name="Priya Raman", source="wizard", captured_at="2026-07-30T09:04:02+00:00"
        ),
        attendee_names.NameObservation(
            name="P. Raman",
            source="certificate",
            captured_at="2026-07-30T09:12:44+00:00",
        ),
    ]
    event = _example("attendee-identity.json")
    event["payload"] = attendee_names.payload("labuser017@example.com", observations)
    assert_schema(event, schema)


def test_the_identity_event_rejects_an_invented_name_source(schema: dict) -> None:
    """Source is what makes one observation weigh more than another. Accepting a
    free-text value would flatten a certificate name and an optional field into
    the same evidence."""
    event = _example("attendee-identity.json")
    event["payload"]["names"][0]["source"] = "vibes"
    with pytest.raises(AssertionError):
        assert_schema(event, schema)


def test_the_identity_event_carries_a_list_rather_than_a_full_name(schema: dict) -> None:
    """Two names is corroboration or two people, never a correction — a scalar
    field would render both as the second one winning."""
    payloads = _payload_schemas(schema)["attendee.identity"]
    assert payloads["properties"]["names"]["type"] == "array"
    assert "full_name" not in payloads["properties"]


def test_the_identity_idempotency_key_carries_the_observation_count() -> None:
    """Each new name is a new logical event; a retried flush of the same picture
    is a duplicate. Keying on the attendee alone would drop the second name."""
    doc = CONTRACT_DOC.read_text()
    assert "identity:{run_id}:{attendee}:{count}" in doc
    event = _example("attendee-identity.json")
    assert event["idempotency_key"].endswith(f":{len(event['payload']['names'])}")


def test_the_contract_says_a_typed_name_is_gated(schema: dict) -> None:
    """This is the first event carrying a real human name rather than a pooled
    lab identity, so the doc has to say the capture flag governs it."""
    doc = CONTRACT_DOC.read_text()
    identity = doc.split("`attendee.identity`", 1)[1][:4000]
    assert "WORKSHOP_INSIGHT_CAPTURE" in doc
    assert "name" in identity.lower()


def test_the_summary_id_does_not_vary_with_the_generator() -> None:
    """Both events describe one session; CT reconciles them on ``summary_id``."""
    from server.insight_summary import _summary_id

    run, email = "3f1b8c2e-9a44-4d21-8f0e-7c5b1a2d6e90", "labuser007@example.com"
    assert _summary_id(run, email) == _summary_id(run, email)
    assert _summary_id(run, email) != _summary_id(run, "labuser008@example.com")


def test_the_contract_tells_control_tower_to_prefer_the_model_summary() -> None:
    """Last-write-wins would let the teardown backstop overwrite the good copy."""
    doc = CONTRACT_DOC.read_text()
    assert "prefer `llm`" in doc


def test_envelope_version_matches_the_emitter() -> None:
    from server.event_emitter import SCHEMA_VERSION

    schema = json.loads(SCHEMA_PATH.read_text())
    assert schema["properties"]["schema_version"]["const"] == SCHEMA_VERSION


def test_every_example_declares_a_pooled_attendee() -> None:
    """The terminal only ever knows the pooled identity — the roster resolves it.

    An example that used a real-looking work email would imply the terminal can
    attribute insight itself, which is exactly the misunderstanding that made
    roster import look like a later phase.
    """
    for name in EXAMPLE_NAMES:
        attendee = _example(name)["attendee"]
        assert attendee.startswith("labuser"), f"{name} uses a non-pooled attendee"


def _payload_schemas(schema: dict) -> dict[str, dict]:
    """The ``then`` payload schema for each event type, keyed by type."""
    out: dict[str, dict] = {}
    for branch in schema["allOf"]:
        condition = branch.get("if", {}).get("properties", {}).get("type", {})
        event_type = condition.get("const")
        if event_type:
            out[event_type] = branch["then"]["properties"]["payload"]
    return out


def _signal_payload_schema(schema: dict) -> dict:
    """The ``then`` payload schema for the workshop.signal branch."""
    try:
        return _payload_schemas(schema)["workshop.signal"]
    except KeyError:
        raise AssertionError("no workshop.signal branch in the schema") from None
