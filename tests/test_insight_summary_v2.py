"""Insight contract v2 for the edge summary (docs/workshop-insight-contract.md, section 1).

What this file pins down:

1. **Asks are chosen by index and copied, never written.** The model returns a
   number; the text is a verbatim (redacted) slice of what the attendee typed.
2. **Every cap and enum holds.** A model that over-produces is clamped, and an
   invalid item is dropped rather than failing the whole summary.
3. **Fallback is thin on purpose.** No model means at most five unclassified asks
   and no framing, approach, pivots or systems.
4. **Flag off is byte-for-byte v1.** Control Tower rejects unknown fields in sealed
   closeout, so a payload from an instance without the flag must not gain a key.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from server import artifacts, insight_summary
from server.artifacts import Artifact, Harvest
from server.event_emitter import EventEmitter

from .schema_assert import assert_schema

RUN_ID = "11111111-2222-3333-4444-555555555555"
SCHEMA = json.loads((Path(__file__).parent / "fixtures" / "workshop-insight-events.schema.json").read_text())

V1_KEYS = {
    "summary_id", "revision", "generated_at", "generator", "model", "phase", "headline",
    "what_they_built", "session_intent", "use_cases", "blockers", "products", "artifacts",
    "prompt_count", "redactions",
}
V2_KEYS = {"summary_schema_version", "asks", "attendee_background"}
V2_LLM_ONLY = {"framing", "approach", "pivots", "systems_mentioned"}


class _User:
    def __init__(self, home: str = "/nonexistent", email: str = "labuser001@x.com"):
        self.home = home
        self.email = email


@pytest.fixture(autouse=True)
def _capture_on(monkeypatch):
    monkeypatch.setenv("WORKSHOP_INSIGHT_CAPTURE", "true")
    monkeypatch.delenv("WORKSHOP_INSIGHT_V2", raising=False)
    insight_summary.stamps.clear()
    # The attendee's help thread and wizard brief are process state; keep them empty
    # unless a test supplies them.
    monkeypatch.setattr("server.help.thread_snapshot", lambda: {"messages": []})
    monkeypatch.setattr("server.wizard.read_brief", lambda user, **kw: SimpleNamespace(what_building=""))
    monkeypatch.setattr(insight_summary.attendee_profile, "read",
                        lambda user: SimpleNamespace(background=""))
    yield
    insight_summary.stamps.clear()


@pytest.fixture
def v2(monkeypatch):
    monkeypatch.setenv("WORKSHOP_INSIGHT_V2", "true")


@pytest.fixture
def emitter():
    return EventEmitter(run_id=RUN_ID, workspace_id="ws1", ingest_url="", ingest_token="")


def _harvest(prompts: list[str] | None = None, **overrides) -> Harvest:
    base = Harvest(
        prompts=prompts if prompts is not None else [
            "How do I stream our Confluent orders topic into Delta?",
            "Make the churn dashboard show results by region",
        ],
        plans=["Create a bronze table from the Kafka topic"],
        tool_targets=["Write /home/u/projects/etl/bronze.py"],
        errors=["PERMISSION_DENIED: no CREATE TABLE on main.sales"],
        artifacts=[Artifact(kind="promote_doc", title="architecture.md", bytes=2048)],
        documents=[("architecture.md", "Kafka to Delta, replacing a Snowflake job.")],
        prompt_count=14,
        redactions=0,
    )
    for key, value in overrides.items():
        setattr(base, key, value)
    return base


def _stub_harvest(monkeypatch, harvest: Harvest):
    monkeypatch.setattr(artifacts, "harvest_user", lambda user, **kw: harvest)


def _stub_model(monkeypatch, answer: dict):
    monkeypatch.setattr(insight_summary, "_ask_model", lambda harvest, signal: (answer, "m1"))


def _no_model(monkeypatch):
    def fail(harvest, signal):
        raise insight_summary.ModelUnavailable("endpoint down")

    monkeypatch.setattr(insight_summary, "_ask_model", fail)


def _headline(**extra) -> dict:
    return {"headline": "Streaming orders into Delta.", "what_they_built": "A bronze ingest.", **extra}


def _emitted(emitter: EventEmitter) -> list[dict]:
    sent: list[dict] = []
    emitter.drain(lambda ev: sent.append(ev) or True)
    return sent


def _summarise(emitter) -> dict:
    payload = insight_summary.summarise_user(_User(), phase="build", signal={"engagement": "builder"},
                                             emitter=emitter)
    assert payload is not None
    return payload


# --- asks: by index, verbatim, classified ---------------------------------------


def test_asks_are_chosen_by_index_and_copied_verbatim(monkeypatch, emitter, v2):
    prompts = [
        "How do I stream our Confluent orders topic into Delta?",
        "Make the churn dashboard show results by region",
    ]
    _stub_harvest(monkeypatch, _harvest(prompts))
    _stub_model(monkeypatch, _headline(asks=[{"index": 1, "kind": "business"},
                                              {"index": 0, "kind": "data"}]))

    payload = _summarise(emitter)

    assert payload["summary_schema_version"] == 2
    assert payload["asks"] == [
        {"text": prompts[1], "kind": "business", "source": "prompt", "at": None},
        {"text": prompts[0], "kind": "data", "source": "prompt", "at": None},
    ]


def test_out_of_range_bool_and_unknown_kind_items_are_dropped(monkeypatch, emitter, v2):
    _stub_harvest(monkeypatch, _harvest())
    _stub_model(monkeypatch, _headline(asks=[
        {"index": 9, "kind": "data"},          # out of range
        {"index": True, "kind": "data"},       # bool is not an index
        {"index": "0", "kind": "data"},        # string is not an index
        {"index": 0, "kind": "unclassified"},  # the fallback kind is not an llm kind
        {"index": 0, "kind": "opinion"},       # not in the enum
        "not an object",
        {"index": 1, "kind": "blocker"},       # the only valid one
    ]))

    payload = _summarise(emitter)

    assert [a["kind"] for a in payload["asks"]] == ["blocker"]


def test_asks_are_capped_at_twelve_and_deduplicated(monkeypatch, emitter, v2):
    prompts = [f"Question number {n} about the lakehouse" for n in range(15)]
    _stub_harvest(monkeypatch, _harvest(prompts))
    answer = [{"index": n, "kind": "data"} for n in range(15)] + [{"index": 0, "kind": "data"}]
    _stub_model(monkeypatch, _headline(asks=answer))

    payload = _summarise(emitter)

    assert len(payload["asks"]) == 12
    assert len({a["text"] for a in payload["asks"]}) == 12


def test_a_long_ask_is_clipped_to_280_at_a_word_boundary_and_stays_a_prefix(monkeypatch, emitter, v2):
    long = " ".join(f"word{n}" for n in range(200))  # well over 280 characters
    _stub_harvest(monkeypatch, _harvest([long]))
    _stub_model(monkeypatch, _headline(asks=[{"index": 0, "kind": "ambition"}]))

    [ask] = _summarise(emitter)["asks"]

    assert len(ask["text"]) <= 280
    assert long.startswith(ask["text"])
    assert long[len(ask["text"])] == " ", "the cut must fall between words"


def test_help_and_wizard_text_become_candidates_with_their_source(monkeypatch, emitter, v2):
    _stub_harvest(monkeypatch, _harvest(["Build a fraud dashboard"]))
    monkeypatch.setattr("server.help.thread_snapshot", lambda: {"messages": [
        {"sender_role": "operator", "body": "Sure, I can help with that."},
        {"sender_role": "attendee", "body": "How do I connect Lakebase from my app?",
         "created_at": "2026-11-20T01:02:03Z"},
    ]})
    monkeypatch.setattr("server.wizard.read_brief",
                        lambda user, **kw: SimpleNamespace(what_building="Fraud scoring for card payments"))
    _stub_model(monkeypatch, _headline(asks=[
        {"index": 1, "kind": "how_to"},   # help message
        {"index": 2, "kind": "ambition"},  # wizard answer
    ]))

    asks = _summarise(emitter)["asks"]

    assert asks == [
        {"text": "How do I connect Lakebase from my app?", "kind": "how_to", "source": "help",
         "at": "2026-11-20T01:02:03Z"},
        {"text": "Fraud scoring for card payments", "kind": "ambition", "source": "wizard", "at": None},
    ]


def test_personal_data_in_asks_is_redacted_before_it_leaves_the_seat(monkeypatch, emitter, v2):
    _stub_harvest(monkeypatch, _harvest(["Email ada@example.com about the 10.0.0.12 cluster"]))
    _stub_model(monkeypatch, _headline(asks=[{"index": 0, "kind": "blocker"}]))

    [ask] = _summarise(emitter)["asks"]

    assert "ada@example.com" not in ask["text"]
    assert "10.0.0.12" not in ask["text"]
    assert "[redacted-email]" in ask["text"] and "[redacted-ip]" in ask["text"]


def test_a_help_message_with_a_token_is_scrubbed_before_it_is_an_ask(monkeypatch, emitter, v2):
    _stub_harvest(monkeypatch, _harvest(["unrelated prompt"]))
    monkeypatch.setattr("server.help.thread_snapshot", lambda: {"messages": [
        {"sender_role": "attendee", "body": "my key is api_key=hunter2hunter2 please check"},
    ]})
    _stub_model(monkeypatch, _headline(asks=[{"index": 1, "kind": "blocker"}]))

    [ask] = _summarise(emitter)["asks"]

    assert "hunter2hunter2" not in ask["text"]


# --- framing, pivots, systems ----------------------------------------------------


def test_framing_pivots_and_systems_are_clamped_and_validated(monkeypatch, emitter, v2):
    _stub_harvest(monkeypatch, _harvest())
    _stub_model(monkeypatch, _headline(
        framing="F" * 2000,
        approach="A short approach.",
        pivots=[f"switched from X{n} to Y{n}" for n in range(8)],
        systems_mentioned=[{"name": f"System {n}", "kind": "incumbent_tool"} for n in range(12)]
        + [{"name": "Bad", "kind": "vendor"}],
    ))

    payload = _summarise(emitter)

    assert len(payload["framing"]) <= 600
    assert payload["approach"] == "A short approach."
    assert len(payload["pivots"]) == 5
    assert len(payload["systems_mentioned"]) == 10
    assert all(s["kind"] == "incumbent_tool" for s in payload["systems_mentioned"])


def test_framing_redacts_personal_identifiers(monkeypatch, emitter, v2):
    _stub_harvest(monkeypatch, _harvest())
    _stub_model(monkeypatch, _headline(
        framing="Their lead, ada@example.com, wants the workspace at adb-123.azuredatabricks.net",
        pivots=["moved from SQL to Python"],
        systems_mentioned=[{"name": "Snowflake", "kind": "incumbent_tool"}],
    ))

    payload = _summarise(emitter)

    assert "ada@example.com" not in payload["framing"]
    assert "azuredatabricks.net" not in payload["framing"]
    assert payload["systems_mentioned"] == [{"name": "Snowflake", "kind": "incumbent_tool"}]


# --- background ---------------------------------------------------------------


def test_background_comes_from_the_profile_enum_and_is_never_a_title(monkeypatch, emitter, v2):
    _stub_harvest(monkeypatch, _harvest())
    _stub_model(monkeypatch, _headline())
    monkeypatch.setattr(insight_summary.attendee_profile, "read",
                        lambda user: SimpleNamespace(background="developer"))

    assert _summarise(emitter)["attendee_background"] == "developer"


def test_a_background_outside_the_enum_is_not_sent(monkeypatch, emitter, v2):
    _stub_harvest(monkeypatch, _harvest())
    _stub_model(monkeypatch, _headline())
    monkeypatch.setattr(insight_summary.attendee_profile, "read",
                        lambda user: SimpleNamespace(background="Head of Data"))

    assert _summarise(emitter)["attendee_background"] == ""


# --- fallback --------------------------------------------------------------------


def test_fallback_has_at_most_five_unclassified_asks_and_no_framing(monkeypatch, emitter, v2):
    prompts = [f"prompt {n} " + "x" * n for n in range(9)]
    _stub_harvest(monkeypatch, _harvest(prompts))
    monkeypatch.setattr("server.help.thread_snapshot", lambda: {"messages": [
        {"sender_role": "attendee", "body": "please help with the pipeline"},
    ]})
    _no_model(monkeypatch)

    payload = _summarise(emitter)

    assert payload["generator"] == "extraction"
    assert payload["summary_schema_version"] == 2
    assert len(payload["asks"]) == 5
    assert all(a["kind"] == "unclassified" for a in payload["asks"])
    assert payload["asks"][0]["source"] == "help", "explicit words come before prompts"
    assert not V2_LLM_ONLY & set(payload)


def test_a_model_answer_cannot_supply_framing_on_a_fallback_payload(monkeypatch, emitter, v2):
    harvest = _harvest()
    raw = insight_summary._extraction(harvest, None)
    raw["framing"] = "Invented framing"
    harvest.ask_candidates = insight_summary.ask_candidates(harvest, _User())

    payload = insight_summary._payload(raw, harvest, run_id=RUN_ID, email="labuser001@x.com",
                                       phase="wrap", generator="extraction", model=None, background="")

    assert "framing" not in payload


# --- schema -------------------------------------------------------------------


def test_a_v2_summary_validates_against_the_contract_schema(monkeypatch, emitter, v2):
    _stub_harvest(monkeypatch, _harvest())
    _stub_model(monkeypatch, _headline(
        asks=[{"index": 0, "kind": "business"}],
        framing="Replacing a Snowflake job with streaming ingest.",
        pivots=["moved from batch to streaming"],
        systems_mentioned=[{"name": "Confluent", "kind": "source_system"}],
    ))
    monkeypatch.setattr(insight_summary.attendee_profile, "read",
                        lambda user: SimpleNamespace(background="data"))

    payload = _summarise(emitter)
    [event] = _emitted(emitter)

    assert event["payload"] == payload
    assert_schema(event, SCHEMA)


# --- flag off: byte-for-byte v1 --------------------------------------------------


def test_with_the_flag_off_a_summary_has_exactly_the_v1_keys(monkeypatch, emitter):
    _stub_harvest(monkeypatch, _harvest())
    _stub_model(monkeypatch, _headline(
        asks=[{"index": 0, "kind": "business"}], framing="ignored", pivots=["ignored"],
    ))
    monkeypatch.setattr("server.help.thread_snapshot", lambda: {"messages": [
        {"sender_role": "attendee", "body": "help"},
    ]})

    payload = _summarise(emitter)

    assert set(payload) <= V1_KEYS
    assert not (V2_KEYS | V2_LLM_ONLY) & set(payload)


def test_flag_off_payload_matches_the_v1_golden(monkeypatch):
    """A fixed input must serialise to the exact v1 shape, so the golden is the contract."""
    monkeypatch.setattr(insight_summary, "_now", lambda: "2026-11-20T00:00:00+00:00")
    harvest = _harvest()
    raw = {
        "headline": "Streaming orders into Delta.",
        "what_they_built": "A bronze ingest.",
        "session_intent": "business_problem",
        "use_cases": [{"title": "Real-time orders", "summary": "Streaming ingest.",
                       "products": ["Delta Lake"], "evidence": "prompt 1"}],
        "blockers": ["No CREATE TABLE grant on main.sales"],
        "products": ["Delta Lake", "Unity Catalog"],
    }

    payload = insight_summary._payload(raw, harvest, run_id=RUN_ID, email="labuser001@x.com",
                                       phase="wrap", generator="llm", model="m1", revision=2)

    assert payload == {
        "summary_id": insight_summary._summary_id(RUN_ID, "labuser001@x.com"),
        "generated_at": "2026-11-20T00:00:00+00:00",
        "generator": "llm",
        "model": "m1",
        "phase": "wrap",
        "revision": 2,
        "headline": "Streaming orders into Delta.",
        "prompt_count": 14,
        "redactions": 0,
        "what_they_built": "A bronze ingest.",
        "session_intent": "business_problem",
        "use_cases": [{"title": "Real-time orders", "summary": "Streaming ingest.",
                       "products": ["Delta Lake"], "evidence": "prompt 1"}],
        "blockers": ["No CREATE TABLE grant on main.sales"],
        "products": ["Delta Lake", "Unity Catalog"],
        "artifacts": [{"kind": "promote_doc", "title": "architecture.md", "bytes": 2048}],
    }


def test_flag_off_does_not_look_up_help_or_the_wizard(monkeypatch, emitter):
    def boom(*args, **kwargs):
        raise AssertionError("v2 candidates must not be gathered with the flag off")

    _stub_harvest(monkeypatch, _harvest())
    _stub_model(monkeypatch, _headline())
    monkeypatch.setattr(insight_summary, "ask_candidates", boom)

    assert _summarise(emitter)["generator"] == "llm"
