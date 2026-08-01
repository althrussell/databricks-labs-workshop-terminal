"""Edge summarisation at wrap, with a model-free backstop (contract C6, tier 3).

Three things decide whether this feature is trustworthy rather than merely
present, and they get the weight here:

1. **A summary never invents.** The material is bounded and redacted, the model
   is told to leave fields empty, and the payload is re-derived from the model's
   answer rather than trusted — so a model that ignores the schema produces a
   thin summary instead of a rejected event.
2. **The model failing costs nothing.** Every model path degrades to keyword
   extraction, and the resulting summary declares itself as such, because an
   extraction summary read as a finding is worse than no summary.
3. **Exactly once per attendee**, with the one permitted exception: an extraction
   summary may be upgraded by a model one, never the reverse.
"""

from __future__ import annotations

import json

import pytest

from server import artifacts, insight_summary
from server.artifacts import Artifact, Harvest
from server.event_emitter import EventEmitter

RUN_ID = "11111111-2222-3333-4444-555555555555"


class _User:
    def __init__(self, home: str = "/nonexistent", email: str = "labuser001@x.com"):
        self.home = home
        self.email = email


@pytest.fixture(autouse=True)
def _capture_on(monkeypatch):
    monkeypatch.setenv("WORKSHOP_INSIGHT_CAPTURE", "true")
    insight_summary.stamps.clear()
    yield
    insight_summary.stamps.clear()


@pytest.fixture
def emitter():
    return EventEmitter(
        run_id=RUN_ID, workspace_id="ws1",
        ingest_url="https://ct.example.com", ingest_token="tok",
    )


def _harvest(**overrides) -> Harvest:
    base = Harvest(
        prompts=["How do I stream our Confluent orders topic into Delta?"],
        plans=["Create a bronze table from the Kafka topic"],
        tool_targets=["Write /home/u/projects/etl/bronze.py"],
        errors=["PERMISSION_DENIED: no CREATE TABLE on main.sales"],
        artifacts=[Artifact(kind="promote_doc", title="architecture.md", bytes=2048)],
        documents=[("architecture.md", "Kafka to Delta, replacing a Snowflake job.")],
        prompt_count=14,
        redactions=1,
    )
    for key, value in overrides.items():
        setattr(base, key, value)
    return base


def _stub_harvest(monkeypatch, harvest: Harvest):
    monkeypatch.setattr(artifacts, "harvest_user", lambda user, **kw: harvest)


def _stub_model(monkeypatch, answer: dict | str, model: str = "databricks-claude-haiku-4-5"):
    def fake(harvest, signal):
        if isinstance(answer, str):
            return insight_summary._extract_json(answer), model
        return answer, model

    monkeypatch.setattr(insight_summary, "_ask_model", fake)


def _no_model(monkeypatch, reason: str = "endpoint down"):
    def fail(harvest, signal):
        raise insight_summary.ModelUnavailable(reason)

    monkeypatch.setattr(insight_summary, "_ask_model", fail)


def _emitted(emitter: EventEmitter) -> list[dict]:
    sent: list[dict] = []
    emitter.drain(lambda ev: sent.append(ev) or True)
    return sent


# --- the model path -----------------------------------------------------------


def test_a_model_summary_is_emitted_as_an_insight_summary_event(monkeypatch, emitter):
    _stub_harvest(monkeypatch, _harvest())
    _stub_model(monkeypatch, {
        "headline": "Streaming Confluent orders into Delta, blocked on UC grants.",
        "what_they_built": "A bronze ingest from a Kafka topic.",
        "use_cases": [{
            "title": "Real-time order ingestion",
            "summary": "Replace a nightly Snowflake job with streaming ingest.",
            "products": ["Structured Streaming", "Delta Lake"],
            "evidence": "attendee prompt about the Confluent orders topic",
        }],
        "blockers": ["No CREATE TABLE grant on main.sales"],
        "products": ["Delta Lake", "Unity Catalog"],
    })

    payload = insight_summary.summarise_user(
        _User(), phase="wrap", signal={"engagement": "builder"}, emitter=emitter
    )

    assert payload["generator"] == "llm"
    assert payload["model"] == "databricks-claude-haiku-4-5"
    assert payload["use_cases"][0]["title"] == "Real-time order ingestion"
    [event] = _emitted(emitter)
    assert event["type"] == "insight.summary"
    assert event["attendee"] == "labuser001@x.com"
    assert event["idempotency_key"] == f"summary:{RUN_ID}:labuser001@x.com:llm"


def test_artifacts_travel_as_metadata_and_excerpts_do_not(monkeypatch, emitter):
    _stub_harvest(monkeypatch, _harvest())
    _stub_model(monkeypatch, {"headline": "Built an ingest pipeline."})

    payload = insight_summary.summarise_user(_User(), phase="wrap", emitter=emitter)

    assert payload["artifacts"] == [
        {"kind": "promote_doc", "title": "architecture.md", "bytes": 2048}
    ]
    # The excerpt fed the summariser on this instance; it must not be on the wire.
    assert "Snowflake job" not in json.dumps(payload)


def test_the_evidence_pack_carries_prompts_and_errors_but_no_stdout(monkeypatch):
    material = insight_summary._material(
        _harvest(), {"engagement": "explorer", "products": ["Delta Lake"], "shipped": False}
    )

    assert "Confluent orders topic" in material
    assert "PERMISSION_DENIED" in material
    assert "explorer" in material
    # Nothing in the harvest holds tool output, so nothing in the pack can.
    assert "row1" not in material


def test_a_model_answer_wrapped_in_prose_is_still_read(monkeypatch, emitter):
    """Models fence or preface JSON often enough that strict parsing would push
    most sessions onto the keyword fallback for no reason."""
    _stub_harvest(monkeypatch, _harvest())
    _stub_model(monkeypatch, 'Here you go:\n```json\n{"headline": "Ingest work."}\n```')

    payload = insight_summary.summarise_user(_User(), phase="wrap", emitter=emitter)

    assert payload["generator"] == "llm"
    assert payload["headline"] == "Ingest work."


def test_unparseable_model_output_falls_back_rather_than_shipping_nothing(
    monkeypatch, emitter
):
    _stub_harvest(monkeypatch, _harvest())
    monkeypatch.setattr(
        insight_summary, "_ask_model",
        lambda h, s: (_ for _ in ()).throw(
            insight_summary.ModelUnavailable("model returned unparseable JSON")
        ),
    )

    payload = insight_summary.summarise_user(
        _User(), phase="wrap", signal={"engagement": "builder"}, emitter=emitter
    )

    assert payload["generator"] == "extraction"


def test_a_model_that_ignores_the_schema_yields_a_thin_summary_not_a_bad_event(
    monkeypatch, emitter
):
    """The event is the only copy that survives teardown, so a malformed field
    must be dropped rather than allowed to fail ingest validation."""
    _stub_harvest(monkeypatch, _harvest())
    _stub_model(monkeypatch, {
        "headline": "Fine.",
        "use_cases": ["a bare string, not an object", {"summary": "no title"}, 7],
        "blockers": "a string where a list belongs",
        "products": None,
    })

    payload = insight_summary.summarise_user(_User(), phase="wrap", emitter=emitter)

    assert payload["use_cases"] == [{"title": "a bare string, not an object"}]
    assert payload["blockers"] == ["a string where a list belongs"]
    assert "products" not in payload


def test_an_empty_headline_says_so_rather_than_rendering_blank(monkeypatch, emitter):
    _stub_harvest(monkeypatch, _harvest())
    _stub_model(monkeypatch, {"headline": "   "})

    payload = insight_summary.summarise_user(_User(), phase="wrap", emitter=emitter)

    assert payload["headline"] == "No summary could be derived from this session."


def test_oversized_model_output_is_bounded(monkeypatch, emitter):
    _stub_harvest(monkeypatch, _harvest())
    _stub_model(monkeypatch, {
        "headline": "h" * 2000,
        "what_they_built": "w" * 9000,
        "use_cases": [{"title": f"case {i}"} for i in range(40)],
        "blockers": [f"blocker {i}" for i in range(50)],
    })

    payload = insight_summary.summarise_user(_User(), phase="wrap", emitter=emitter)

    assert len(payload["headline"]) == insight_summary.MAX_HEADLINE_CHARS
    assert len(payload["what_they_built"]) == insight_summary.MAX_TEXT_CHARS
    assert len(payload["use_cases"]) == insight_summary.MAX_USE_CASES
    assert len(payload["blockers"]) == insight_summary.MAX_LIST_ITEMS


# --- the extraction backstop --------------------------------------------------


def test_the_backstop_makes_no_model_call_at_all(monkeypatch, emitter):
    """Teardown may run when the app is cold; a model call there fails silently."""
    _stub_harvest(monkeypatch, _harvest())
    monkeypatch.setattr(
        insight_summary, "_ask_model",
        lambda h, s: pytest.fail("the teardown backstop must not call a model"),
    )

    payload = insight_summary.summarise_user(
        _User(), phase="wrap", allow_llm=False, emitter=emitter
    )

    assert payload["generator"] == "extraction"
    assert payload["model"] is None


def test_extraction_quotes_the_attendee_instead_of_paraphrasing(monkeypatch, emitter):
    """With no model behind it, the only honest use case is their own words."""
    _stub_harvest(monkeypatch, _harvest())

    payload = insight_summary.summarise_user(
        _User(), phase="wrap", allow_llm=False,
        signal={"engagement": "explorer", "products": ["Delta Lake"]}, emitter=emitter,
    )

    [case] = payload["use_cases"]
    assert case["title"].startswith("How do I stream our Confluent")
    assert "verbatim" in case["evidence"]
    # No paraphrase is offered at all, rather than an empty one — a blank summary
    # field would read downstream as a truncation bug.
    assert "summary" not in case


def test_extraction_reports_the_wall_rather_than_claiming_success(monkeypatch, emitter):
    _stub_harvest(monkeypatch, _harvest(artifacts=[]))
    _no_model(monkeypatch)

    payload = insight_summary.summarise_user(
        _User(), phase="wrap",
        signal={"engagement": "explorer", "shipped": False, "products": ["Delta Lake"]},
        emitter=emitter,
    )

    assert payload["blockers"] == ["PERMISSION_DENIED: no CREATE TABLE on main.sales"]
    assert "Explorer" in payload["headline"]
    assert "hit errors" in payload["headline"]


def test_extraction_declines_to_classify_the_session_intent(monkeypatch, emitter):
    """Intent is a judgement about *why* someone built something, and this pass
    makes no judgements. A keyword rule would read "game" in a prompt and file a
    fraud-detection session as `fun`, which is worse than leaving it empty —
    `generator: extraction` already explains the gap."""
    _stub_harvest(monkeypatch, _harvest())
    _no_model(monkeypatch)

    payload = insight_summary.summarise_user(_User(), phase="wrap", emitter=emitter)

    assert payload["generator"] == "extraction"
    assert "session_intent" not in payload


def test_the_model_classifies_the_session_intent(monkeypatch, emitter):
    _stub_harvest(monkeypatch, _harvest())
    monkeypatch.setattr(
        insight_summary, "_ask_model",
        lambda h, s: (
            {"headline": "Built a Space Invaders clone.", "session_intent": "fun"},
            "databricks-claude-haiku-4-5",
        ),
    )

    payload = insight_summary.summarise_user(_User(), phase="wrap", emitter=emitter)

    assert payload["generator"] == "llm"
    assert payload["session_intent"] == "fun"


def test_an_invented_session_intent_is_dropped(monkeypatch, emitter):
    """The producer re-bounds every field rather than trusting the model, so an
    off-schema value costs the field rather than the whole summary — the event is
    the only copy of the session that survives teardown."""
    _stub_harvest(monkeypatch, _harvest())
    monkeypatch.setattr(
        insight_summary, "_ask_model",
        lambda h, s: (
            {"headline": "Something happened.", "session_intent": "probably commercial"},
            "databricks-claude-haiku-4-5",
        ),
    )

    payload = insight_summary.summarise_user(_User(), phase="wrap", emitter=emitter)

    assert "session_intent" not in payload
    assert payload["headline"] == "Something happened."


def test_the_summariser_is_told_to_say_when_it_was_just_fun():
    """Without this the model reaches for a business framing, because that is
    what "summarise for an account team" sounds like it wants."""
    prompt = insight_summary._PROMPT

    assert "session_intent" in prompt
    for intent in ("business_problem", "evaluation", "learning", "fun"):
        assert intent in prompt
    assert "Say \"fun\" when it was fun" in prompt


def test_the_generator_is_always_declared(monkeypatch, emitter):
    """Downstream renders it: an extraction summary must not read as a finding."""
    _stub_harvest(monkeypatch, _harvest())
    _no_model(monkeypatch)

    payload = insight_summary.summarise_user(_User(), phase="wrap", emitter=emitter)

    assert payload["generator"] == "extraction"
    assert payload["model"] is None


# --- run-once semantics -------------------------------------------------------


def test_a_second_wrap_does_not_resummarise(monkeypatch, emitter):
    _stub_harvest(monkeypatch, _harvest())
    _stub_model(monkeypatch, {"headline": "First pass."})

    first = insight_summary.summarise_user(_User(), phase="wrap", emitter=emitter)
    second = insight_summary.summarise_user(_User(), phase="wrap", emitter=emitter)

    assert first is not None
    assert second is None
    assert len(_emitted(emitter)) == 1


def test_the_backstop_does_not_downgrade_a_model_summary(monkeypatch, emitter):
    """The whole point of the wrap trigger is that its output is the better one."""
    _stub_harvest(monkeypatch, _harvest())
    _stub_model(monkeypatch, {"headline": "Model pass."})
    insight_summary.summarise_user(_User(), phase="wrap", emitter=emitter)

    backstop = insight_summary.summarise_user(
        _User(), phase="wrap", allow_llm=False, emitter=emitter
    )

    assert backstop is None
    assert [e["payload"]["generator"] for e in _emitted(emitter)] == ["llm"]


def test_a_thin_summary_can_be_upgraded_when_the_model_recovers(monkeypatch, emitter):
    """Wrap with the endpoint down settles for extraction; a re-run should not be
    stuck with that, and the two carry different keys so CT can prefer the model."""
    _stub_harvest(monkeypatch, _harvest())
    _no_model(monkeypatch)
    insight_summary.summarise_user(_User(), phase="wrap", emitter=emitter)

    _stub_model(monkeypatch, {"headline": "Now with a model."})
    upgraded = insight_summary.summarise_user(_User(), phase="wrap", emitter=emitter)

    assert upgraded["generator"] == "llm"
    keys = [e["idempotency_key"] for e in _emitted(emitter)]
    assert keys == [
        f"summary:{RUN_ID}:labuser001@x.com:extraction",
        f"summary:{RUN_ID}:labuser001@x.com:llm",
    ]


def test_the_summary_id_is_stable_so_a_regeneration_supersedes(monkeypatch, emitter):
    _stub_harvest(monkeypatch, _harvest())
    _no_model(monkeypatch)
    first = insight_summary.summarise_user(_User(), phase="wrap", emitter=emitter)
    _stub_model(monkeypatch, {"headline": "Upgraded."})
    second = insight_summary.summarise_user(_User(), phase="wrap", emitter=emitter)

    assert first["summary_id"] == second["summary_id"]


def test_two_attendees_get_two_summaries(monkeypatch, emitter):
    _stub_harvest(monkeypatch, _harvest())
    _stub_model(monkeypatch, {"headline": "Built something."})

    insight_summary.summarise_user(_User(email="a@x.com"), phase="wrap", emitter=emitter)
    insight_summary.summarise_user(_User(email="b@x.com"), phase="wrap", emitter=emitter)

    assert {e["attendee"] for e in _emitted(emitter)} == {"a@x.com", "b@x.com"}


# --- when not to summarise ----------------------------------------------------


def test_nothing_is_summarised_when_capture_is_off(monkeypatch, emitter):
    monkeypatch.setenv("WORKSHOP_INSIGHT_CAPTURE", "false")
    _stub_harvest(monkeypatch, _harvest())
    _stub_model(monkeypatch, {"headline": "should never happen"})

    assert insight_summary.summarise_user(_User(), phase="wrap", emitter=emitter) is None
    assert emitter.pending() == 0


def test_an_empty_session_produces_no_summary(monkeypatch, emitter):
    """A summary over nothing is fabrication; the gap is the honest answer."""
    _stub_harvest(monkeypatch, Harvest())
    _stub_model(monkeypatch, {"headline": "should never happen"})

    assert insight_summary.summarise_user(_User(), phase="wrap", emitter=emitter) is None
    assert emitter.pending() == 0


def test_the_summary_is_generated_on_an_instance_with_no_push_sink(monkeypatch):
    """Which is every instance Control Tower deploys.

    This once refused to spend a model call without an ingest endpoint, on the
    reasoning that teardown would delete the result. That reasoning was sound and
    the premise was wrong: CT collects the buffer on its harvest, so the wrap
    summary does leave — and skipping it meant the one artefact the account team
    reads was never generated in production.
    """
    pull_only = EventEmitter(
        run_id="", workspace_id="", ingest_url="", ingest_token=""
    )
    _stub_harvest(monkeypatch, _harvest())
    _stub_model(monkeypatch, {"headline": "Streaming Kafka into Delta"})

    payload = insight_summary.summarise_user(
        _User(), phase="wrap", emitter=pull_only
    )

    assert payload is not None
    assert pull_only.pending() == 1


def test_a_harvest_failure_is_not_a_summary_failure(monkeypatch, emitter):
    monkeypatch.setattr(
        artifacts, "harvest_user",
        lambda user, **kw: (_ for _ in ()).throw(RuntimeError("disk gone")),
    )

    assert insight_summary.summarise_user(_User(), phase="wrap", emitter=emitter) is None


def test_an_unexpected_model_error_still_ships_a_summary(monkeypatch, emitter):
    """A serving library raising something we didn't anticipate must not cost the
    only record of the session."""
    _stub_harvest(monkeypatch, _harvest())
    monkeypatch.setattr(
        insight_summary, "_ask_model",
        lambda h, s: (_ for _ in ()).throw(TypeError("surprise")),
    )

    payload = insight_summary.summarise_user(_User(), phase="wrap", emitter=emitter)

    assert payload["generator"] == "extraction"


# --- model selection ----------------------------------------------------------


def test_the_cheap_tier_is_preferred_for_a_summarisation_job(monkeypatch):
    monkeypatch.delenv("INSIGHT_SUMMARY_MODEL", raising=False)
    monkeypatch.setattr(
        insight_summary, "_ready_endpoints",
        lambda token: {"databricks-claude-sonnet-5", "databricks-claude-haiku-4-5"},
    )
    assert insight_summary._pick_model("tok") == "databricks-claude-haiku-4-5"


def test_a_pinned_model_wins_without_discovery(monkeypatch):
    monkeypatch.setenv("INSIGHT_SUMMARY_MODEL", "databricks-claude-opus-4-8")
    monkeypatch.setattr(
        insight_summary, "_ready_endpoints",
        lambda token: pytest.fail("a pinned endpoint needs no discovery"),
    )
    assert insight_summary._pick_model("tok") == "databricks-claude-opus-4-8"


def test_no_ready_endpoint_is_reported_as_model_unavailable(monkeypatch):
    monkeypatch.delenv("INSIGHT_SUMMARY_MODEL", raising=False)
    monkeypatch.setattr(insight_summary, "_ready_endpoints", lambda token: set())
    with pytest.raises(insight_summary.ModelUnavailable):
        insight_summary._pick_model("tok")


# --- the fan-out --------------------------------------------------------------


def test_summarise_all_pairs_each_narrative_with_its_own_signal(monkeypatch, emitter):
    """Deriving engagement twice is how a brief comes to contradict its own
    metrics, so the summariser is handed the same signal the harvest reports."""
    from server import stats

    _stub_harvest(monkeypatch, _harvest())
    monkeypatch.setattr(stats, "_workspace_resources", lambda: {})
    monkeypatch.setattr(
        stats, "gather_user",
        lambda user, **kw: {"signal": {"engagement": f"band-{user.email[0]}"}},
    )
    seen: list[str] = []

    def fake_model(harvest, signal):
        seen.append(signal["engagement"])
        return {"headline": "ok"}, "m"

    monkeypatch.setattr(insight_summary, "_ask_model", fake_model)

    count = insight_summary.summarise_all(
        [_User(email="a@x.com"), _User(email="b@x.com")],
        phase="wrap", allow_llm=True, emitter=emitter,
    )

    assert count == 2
    assert sorted(seen) == ["band-a", "band-b"]


def test_a_shared_instance_does_not_trust_shared_tmp(monkeypatch, emitter):
    """/tmp/promote is shared by uid, so on a multi-attendee instance it cannot be
    attributed — and attributing it would put one company's doc in another's brief."""
    from server import stats

    monkeypatch.setattr(stats, "_workspace_resources", lambda: {})
    monkeypatch.setattr(stats, "gather_user", lambda user, **kw: {"signal": {}})
    seen: list[bool] = []

    def fake_harvest(user, *, single_attendee=True):
        seen.append(single_attendee)
        return _harvest()

    monkeypatch.setattr(artifacts, "harvest_user", fake_harvest)
    _stub_model(monkeypatch, {"headline": "ok"})

    insight_summary.summarise_all(
        [_User(email="a@x.com"), _User(email="b@x.com")],
        phase="wrap", allow_llm=True, emitter=emitter,
    )
    insight_summary.stamps.clear()
    insight_summary.summarise_all(
        [_User(email="solo@x.com")], phase="wrap", allow_llm=True, emitter=emitter
    )

    assert seen == [False, False, True]


def test_one_attendees_failure_does_not_stop_the_others(monkeypatch, emitter):
    from server import stats

    monkeypatch.setattr(stats, "_workspace_resources", lambda: {})
    monkeypatch.setattr(stats, "gather_user", lambda user, **kw: {"signal": {}})

    def flaky(user, **kw):
        if user.email == "a@x.com":
            raise RuntimeError("unreadable home")
        return _harvest()

    monkeypatch.setattr(artifacts, "harvest_user", flaky)
    _stub_model(monkeypatch, {"headline": "ok"})

    count = insight_summary.summarise_all(
        [_User(email="a@x.com"), _User(email="b@x.com")],
        phase="wrap", allow_llm=True, emitter=emitter,
    )

    assert count == 1
    assert [e["attendee"] for e in _emitted(emitter)] == ["b@x.com"]


def test_a_broken_signal_lookup_does_not_block_the_summary(monkeypatch, emitter):
    from server import stats

    monkeypatch.setattr(stats, "_workspace_resources", lambda: {})
    monkeypatch.setattr(
        stats, "gather_user",
        lambda user, **kw: (_ for _ in ()).throw(RuntimeError("git hung")),
    )
    _stub_harvest(monkeypatch, _harvest())
    _stub_model(monkeypatch, {"headline": "ok"})

    assert insight_summary.summarise_all(
        [_User()], phase="wrap", allow_llm=True, emitter=emitter
    ) == 1
