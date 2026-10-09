"""Bounded, deterministic novice replies to observed assistant messages.

This is an external evaluation helper, never attendee instruction content. Its
input file deliberately contains no seed data or evaluator rubric. The driver
must supply complete, role-separated, user-visible assistant turns; raw PTY
output, source, tool results, and evaluator feedback are not supported inputs.
Unsupported questions require review and produce no invented reply. This policy
is deliberately conservative: an unrecognized paraphrase may need review, and
must never silently acquire a passing requirements verdict.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Literal, Mapping


_FACT_IDS = (
    "users", "attention", "primary_action", "remember_changes", "data",
    "current_workflow", "scope_exclusions",
)
_REPLY_IDS = ("technical_choice_reply", "visual_choice_reply")
_DEFAULT_SCENARIO = Path(__file__).parent / "scenarios" / "novice-bakery-order-queue-v1.json"
_MAX_SCENARIO_BYTES = 32_768


@dataclass(frozen=True)
class SimulatorScenario:
    scenario_id: str
    message: str
    persona: str
    entry_path: str
    source_sha256: str
    # Avoid spilling private facts through a dataclass repr in a run manifest.
    facts: tuple[tuple[str, str], ...] = field(repr=False)

    def public_payload(self) -> dict[str, str]:
        """The only scenario input suitable for the wizard/building agent."""
        return {"message": self.message, "persona": self.persona, "entry_path": self.entry_path}

    def answer(self, fact_id: str) -> str:
        return dict(self.facts)[fact_id]


def load_simulator_scenario(path: str | Path = _DEFAULT_SCENARIO) -> SimulatorScenario:
    """Load a simulator-only file; reject full setup/evaluation blueprints.

    No generic ``asdict(scenario)`` belongs in a builder prompt. Use
    ``public_payload()`` or the explicitly disclosed reply instead.
    """
    raw = Path(path).read_bytes()
    if len(raw) > _MAX_SCENARIO_BYTES:
        raise ValueError("simulator scenario exceeds size limit")
    payload = json.loads(raw)
    if not isinstance(payload, dict) or set(payload) != {
        "schema_version", "scenario_id", "public_input", "simulator_private",
    }:
        raise ValueError("expected simulator-only scenario; setup and evaluator data are forbidden")
    if type(payload["schema_version"]) is not int or payload["schema_version"] != 1:
        raise ValueError("unsupported simulator scenario version")
    scenario_id = payload["scenario_id"]
    if not isinstance(scenario_id, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{2,100}", scenario_id):
        raise ValueError("invalid scenario_id")
    public = payload["public_input"]
    private = payload["simulator_private"]
    if not isinstance(public, dict) or set(public) != {"persona", "entry_path", "message"}:
        raise ValueError("invalid public_input")
    if not isinstance(private, dict) or set(private) != set(_FACT_IDS + _REPLY_IDS):
        raise ValueError("invalid simulator business facts")
    if any(not isinstance(value, str) or not value.strip() or len(value) > 2_000
           for value in (*public.values(), *private.values())):
        raise ValueError("scenario values must be nonempty bounded strings")
    # This first policy intentionally supports exactly the bakery scenario. A
    # new business scenario needs its own reviewed fact and agreement matcher.
    if scenario_id != "novice-bakery-order-queue-v1":
        raise ValueError("no deterministic policy registered for this scenario")
    if public["persona"] != "business":
        raise ValueError("novice policy requires business persona")
    return SimulatorScenario(
        scenario_id=scenario_id,
        message=public["message"], persona=public["persona"], entry_path=public["entry_path"],
        source_sha256=hashlib.sha256(raw).hexdigest(),
        facts=tuple((key, private[key]) for key in _FACT_IDS + _REPLY_IDS),
    )


@dataclass(frozen=True)
class SimulatorBudget:
    """Caps start at opening submission; repairs consume the original budget."""
    max_replies: int = 10  # Includes the opening sentence.
    total_seconds: float = 1_800
    consultation_seconds: float = 180
    max_message_chars: int = 12_000
    max_reply_chars: int = 1_500

    def __post_init__(self) -> None:
        for key in ("max_replies", "max_message_chars", "max_reply_chars"):
            value = getattr(self, key)
            if type(value) is not int or value <= 0:
                raise ValueError(f"{key} must be a positive integer")
        for key in ("total_seconds", "consultation_seconds"):
            value = getattr(self, key)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
                raise ValueError(f"{key} must be a finite positive duration")
        if self.consultation_seconds > self.total_seconds:
            raise ValueError("consultation deadline cannot exceed total deadline")


@dataclass(frozen=True)
class BuilderMessage:
    message_id: str
    text: str
    role: str = "assistant"
    visibility: str = "user"
    complete: bool = True


@dataclass(frozen=True)
class SimulatorReply:
    text: str | None
    decision: Literal["opening", "answer", "agree", "decline_scope", "needs_review", "ignored", "stopped"]
    disclosed_fact_ids: tuple[str, ...] = ()
    agreement: bool = False
    stop_reason: str | None = None
    in_reply_to: str | None = None


@dataclass(frozen=True)
class DisclosureEvent:
    """Evidence has only already-public facts/answers and message fingerprints."""
    sequence: int
    elapsed_seconds: float
    builder_message_id: str | None
    builder_message_sha256: str | None
    decision: str
    reply_text: str | None
    fact_ids: tuple[str, ...]
    provenance: str
    agreement: bool
    reason: str | None

    def to_dict(self) -> dict:
        return {
            "sequence": self.sequence, "elapsed_seconds": self.elapsed_seconds,
            "builder_message_id": self.builder_message_id,
            "builder_message_sha256": self.builder_message_sha256,
            "decision": self.decision, "reply_text": self.reply_text,
            "fact_ids": list(self.fact_ids), "provenance": self.provenance,
            "agreement": self.agreement, "reason": self.reason,
        }


def _normal(text: str) -> str:
    text = re.sub(r"```.*?```", "", text, flags=re.S)
    return re.sub(r"\s+", " ", text.replace("’", "'").replace("‘", "'").lower()).strip()


def _questions(text: str) -> list[str]:
    # Declarative descriptions do not authorize fact disclosure. Imperative
    # questions are accepted so an absent '?' does not force a technical rescue.
    text = re.sub(r"```.*?```", "", text, flags=re.S)
    segments = re.split(r"(?<=[.!?])\s+|\n+", text)
    questions = []
    for segment in segments:
        normal = _normal(segment)
        if not normal:
            continue
        if "?" in segment or re.match(
            r"^(?:[-*\d.) ]*)?(?:please )?(?:tell me|describe|explain|share|let me know|"
            r"who\b|what\b|which\b|how\b|where\b|do you\b|would you\b|could you\b|can you\b)", normal,
        ):
            questions.append(normal)
    return questions


def _requested_facts(question: str) -> tuple[str, ...]:
    requested: set[str] = set()
    if re.search(r"\bwho\b.{0,100}\b(?:use|using|uses|page|app|shop|staff|team)\b", question) or re.search(
        r"\b(?:intended users|who is it for|who's it for|which people)\b", question,
    ):
        requested.add("users")
    if re.search(r"\b(?:attention|urgent|priority|prioritize|prioritise|overdue|late)\b", question) or re.search(
        r"\b(?:orders|work)\b.{0,60}\b(?:first|sort|rank)\b", question,
    ):
        requested.add("attention")
    if re.search(r"\bwhat\b.{0,20}\b(?:staff|team|people|they|we|you)\b.{0,45}\b(?:do|actions?|updates?|change|handle)\b|"
        r"\b(?:staff|team|people|they|we|you)\b.{0,25}\b(?:be able to|want to|need to|can|should|could|will)\b.{0,35}\b(?:do|actions?|updates?|change|handle)\b", question) or re.search(
        r"\b(?:mark|marked|packing|complete orders|order status|actions?|read[- ]only)\b", question,
    ):
        # A question about late/unpacked orders alone does not ask what to do.
        requested.add("primary_action")
    if re.search(r"\b(?:remember|save|saved|persist|persistent|persistence|retain|reopen|refresh|reload)\b|"
                 r"\b(?:come back|return later|close.{0,20}open)\b", question):
        requested.add("remember_changes")
    if (re.search(r"\b(?:data|records)\b", question) and re.search(
        r"\b(?:have|available|existing|source|where|sample|bring|start)\b", question,
    )) or re.search(r"\bsample (?:orders|data|records)\b", question) or re.search(
        r"\b(?:have|available|use)\b.{0,40}\bspreadsheet\b", question,
    ) or re.search(r"\bwhere\b.{0,45}\borders\b.{0,30}\b(?:live|come from|stored|right now)\b", question):
        requested.add("data")
    if re.search(r"\b(?:workflow|current process|currently|at the moment)\b", question) or re.search(
        r"\b(?:how|where)\b.{0,30}\b(?:manage|track|handle|process|keep track)\b", question,
    ) or re.search(
        r"\b(?:already|currently)\b.{0,30}\b(?:track|manage|keep)\b|"
        r"\b(?:track|keep track of)\b.{0,30}\borders\b.{0,30}\b(?:today|now|somewhere)\b|"
        r"\b(?:keep|have)\b.{0,25}\b(?:list of orders|order list)\b", question,
    ):
        requested.add("current_workflow")
    if re.search(r"\b(?:out of scope|exclude|exclusions|payments?|customer.{0,30}(?:sign|login)|"
                 r"don't need|not need|must not|avoid)\b", question):
        requested.add("scope_exclusions")
    return tuple(key for key in _FACT_IDS if key in requested)


def _technical_question(question: str) -> bool:
    return bool(re.search(
        r"\b(?:appkit|apx|react|vue|angular|svelte|typescript|javascript|python|sql|lakebase|"
        r"postgres|postgresql|sqlite|duckdb|database|api|framework|package|component|backend|frontend|deployment|deploy|"
        r"token|credentials?|authentication|resource id|sdk|cli|commands?|cluster|warehouse|"
        r"css|tailwind|storage|persist|persistent|persistence)\b", question,
    ))


def _visual_question(question: str) -> bool:
    return bool(re.search(r"\b(?:colou?rs?|theme|style|layout|fonts?|design|visual|branding|brand)\b", question))


def _scope_request(question: str) -> bool:
    return bool(re.search(
        r"\b(?:does|would) (?:that|this|the plan|the scope) (?:sound|work)|"
        r"\b(?:shall|should|can|may) i (?:go ahead|proceed|start|build|make|create)|"
        r"\b(?:do you agree|are we agreed|ready to proceed|okay to proceed|ok to proceed)|"
        r"\b(?:is|does) (?:that|this) (?:right|correct|okay|ok|match)|"
        r"\b(?:please )?confirm (?:the |this |that )?(?:scope|plan|first version)|"
        r"\bwould you like (?:me to|a (?:page|queue|app))", question,
    ))


def _scope_flags(text: str) -> Mapping[str, bool]:
    normal = _normal(text)
    late = bool(re.search(r"\b(?:late|overdue|past due)\b", normal))
    unpacked = bool(re.search(r"\b(?:unpacked|(?:not|haven't|aren't|isn't)(?: been)?(?: yet)? packed)\b", normal))
    late_term = r"\b(?:late|overdue|past due)\b"
    unpacked_term = r"\b(?:unpacked|(?:not|haven't|aren't|isn't)(?: been)?(?: yet)? packed)\b"
    priority_term = r"\b(?:first|top|ahead|prioriti\w*)\b"
    priority = bool(re.search(
        late_term + r".{0,80}" + unpacked_term + r".{0,60}" + priority_term + "|" +
        priority_term + r".{0,60}" + late_term + r".{0,80}" + unpacked_term + "|" +
        unpacked_term + r".{0,80}" + late_term + r".{0,60}" + priority_term + "|" +
        r"\bqueue of\b.{0,30}" + late_term + r".{0,80}" + unpacked_term,
        normal,
    ))
    action = bool(re.search(r"\b(?:mark|set|flag|change)\b.{0,60}\bpacked\b", normal))
    memory = bool(re.search(
        r"\b(?:remembers?|saves?|saved|retains?|keeps?)\b.{0,80}\b(?:changes|changed|updates|packed|status)\b|"
        r"\b(?:changes|updates|packed status)\b.{0,60}\b(?:remembered|saved|retained)\b", normal,
    ))
    users = bool(re.search(r"\b(?:staff|team|people (?:working )?in the shop|shop workers|everyone)\b", normal))
    contradictory = bool(re.search(
        r"\b(?:read[- ]only|view[- ]only|no updates|no (?:saved|remembered) changes|localstorage|session only)|"
        r"\bonly (?:in )?(?:this|one|your) (?:browser|device)|"
        r"\b(?:won't|will not|cannot|can't|don't|do not|never)\b.{0,25}\b(?:mark\w*|remember\w*|save\w*|retain\w*)|"
        r"\b(?:changes|updates|status)\b.{0,25}\b(?:not|never)\b.{0,15}\b(?:saved|remembered|retained)|"
        r"\b(?:ignore|exclude|hide|don't show|do not show)\b.{0,25}\b(?:late|overdue)\b|"
        r"\b(?:future|upcoming|not yet due)\b.{0,40}\b(?:first|ahead|top)\b|"
        r"\bnot for (?:the )?(?:staff|team)\b", normal,
    ))
    return {
        "users": users, "attention": late and unpacked and priority,
        "primary_action": action, "remember_changes": memory,
        "contradictory": contradictory,
    }


class NoviceSimulator:
    def __init__(
        self, scenario: SimulatorScenario, budget: SimulatorBudget | None = None,
        *, clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._scenario = scenario
        self._budget = budget or SimulatorBudget()
        if len(scenario.message) > self._budget.max_reply_chars:
            raise ValueError("opening sentence exceeds reply size limit")
        self._clock = clock
        self._started_at: float | None = None
        self._reply_count = 0
        self._agreement = False
        self._pending_scope = ""
        self._events: list[DisclosureEvent] = []
        self._seen: dict[str, tuple[str, SimulatorReply]] = {}
        self._opening: SimulatorReply | None = None
        self._stop_reason: str | None = None

    @property
    def agreed(self) -> bool:
        return self._agreement

    @property
    def events(self) -> tuple[DisclosureEvent, ...]:
        return tuple(self._events)

    def evidence(self) -> dict:
        """Safe run artifact: never includes undisclosed business facts."""
        return {
            "schema_version": 1, "scenario_id": self._scenario.scenario_id,
            "scenario_sha256": self._scenario.source_sha256,
            "policy": "deterministic-bakery-v1", "scope_agreed": self.agreed,
            "reply_count": self._reply_count, "stop_reason": self._stop_reason,
            "events": [event.to_dict() for event in self._events],
        }

    def budget_status(self) -> str | None:
        """Let the driver enforce deadlines even when the builder is silent."""
        reason = self._budget_reason() if self._started_at is not None else None
        if reason:
            self._stop_reason = reason
        return reason

    def _record(self, reply: SimulatorReply, message: BuilderMessage | None, provenance: str) -> SimulatorReply:
        self._events.append(DisclosureEvent(
            sequence=len(self._events) + 1,
            elapsed_seconds=round(self._elapsed(), 6),
            builder_message_id=message.message_id if message else None,
            builder_message_sha256=hashlib.sha256(message.text.encode()).hexdigest() if message else None,
            decision=reply.decision, reply_text=reply.text, fact_ids=reply.disclosed_fact_ids,
            provenance=provenance, agreement=reply.agreement, reason=reply.stop_reason,
        ))
        if reply.text is not None:
            self._reply_count += 1
        return reply

    def _elapsed(self) -> float:
        return 0.0 if self._started_at is None else max(0.0, self._clock() - self._started_at)

    def _budget_reason(self) -> str | None:
        if self._stop_reason:
            return self._stop_reason
        if self._elapsed() >= self._budget.total_seconds:
            return "total_deadline"
        if not self.agreed and self._elapsed() >= self._budget.consultation_seconds:
            return "consultation_deadline"
        if self._reply_count >= self._budget.max_replies:
            return "reply_limit"
        return None

    def opening(self) -> SimulatorReply:
        if self._opening is None:
            self._started_at = self._clock()
            self._opening = self._record(SimulatorReply(self._scenario.message, "opening"), None, "public_opening")
        return self._opening

    def respond(self, message: BuilderMessage) -> SimulatorReply:
        if self._started_at is None:
            raise RuntimeError("submit opening before processing assistant turns")
        if not isinstance(message, BuilderMessage) or not isinstance(message.text, str) or not isinstance(message.message_id, str) or not message.message_id:
            raise ValueError("expected a message with a stable nonempty ID and text")
        if message.role != "assistant" or message.visibility != "user" or message.complete is not True:
            return self._record(SimulatorReply(None, "ignored", stop_reason="not_complete_user_visible_assistant", in_reply_to=message.message_id), message, "rejected_input")
        digest = hashlib.sha256(message.text.encode()).hexdigest()
        previous = self._seen.get(message.message_id)
        if previous:
            if previous[0] != digest:
                return self._record(SimulatorReply(None, "needs_review", stop_reason="message_id_content_changed", in_reply_to=message.message_id), message, "rejected_input")
            # A reconnect/replay must not submit the same novice answer twice.
            return SimulatorReply(None, "ignored", stop_reason="duplicate_message", in_reply_to=message.message_id)
        reason = self._budget_reason()
        if reason:
            self._stop_reason = reason
            reply = SimulatorReply(None, "stopped", stop_reason=reason, in_reply_to=message.message_id)
            self._seen[message.message_id] = (digest, reply)
            return self._record(reply, message, "budget")
        if len(message.text) > self._budget.max_message_chars:
            reply = SimulatorReply(None, "needs_review", stop_reason="message_size_limit", in_reply_to=message.message_id)
            self._seen[message.message_id] = (digest, reply)
            return self._record(reply, message, "unsupported_question")

        questions = _questions(message.text)
        flags = _scope_flags(message.text)
        scope_request = any(_scope_request(question) for question in questions)
        # A new concrete proposal replaces an older one, even when incomplete.
        normal = _normal(message.text)
        if re.search(r"\b(?:order|orders|page|queue|dashboard|app|read-only|read only)\b", normal) and (
            any(flags.values()) or scope_request or re.search(r"\b(?:recommend|suggest|plan|will|first version|let's|we can)\b", normal)
        ):
            self._pending_scope = message.text
        if scope_request:
            scope = _scope_flags(self._pending_scope)
            complete = all(scope[key] for key in ("users", "attention", "primary_action", "remember_changes"))
            if complete and not scope["contradictory"]:
                self._agreement = True
                reply = SimulatorReply(
                    "Yes, that sounds right.", "agree",
                    ("users", "attention", "primary_action", "remember_changes"),
                    agreement=True, in_reply_to=message.message_id,
                )
                provenance = "requested_scope_confirmation"
            else:
                # Never name omitted requirements to repair an agent's proposal.
                reply = SimulatorReply(
                    "That isn't quite what I had in mind. Could you ask me about what we need?",
                    "decline_scope", in_reply_to=message.message_id,
                )
                provenance = "requested_scope_confirmation"
        else:
            # Technical/style choices do not authorize unrelated business fact
            # disclosure merely because they also mention data or attention.
            business_questions = [question for question in questions if not _technical_question(question) and not _visual_question(question)]
            fact_ids = tuple(key for key in _FACT_IDS if any(key in _requested_facts(question) for question in business_questions))
            answer_ids = list(fact_ids)
            if any(_technical_question(question) for question in questions):
                answer_ids.append("technical_choice_reply")
            if any(_visual_question(question) for question in questions):
                answer_ids.append("visual_choice_reply")
            if answer_ids:
                reply_text = " ".join(self._scenario.answer(key) for key in answer_ids)
                if len(reply_text) > self._budget.max_reply_chars:
                    reply = SimulatorReply(None, "needs_review", stop_reason="reply_size_limit", in_reply_to=message.message_id)
                else:
                    reply = SimulatorReply(reply_text, "answer", fact_ids, in_reply_to=message.message_id)
                provenance = "explicit_assistant_question"
            elif questions:
                reply = SimulatorReply(None, "needs_review", stop_reason="unsupported_question", in_reply_to=message.message_id)
                provenance = "unsupported_question"
            else:
                reply = SimulatorReply(None, "ignored", stop_reason="no_question", in_reply_to=message.message_id)
                provenance = "assistant_observation"
        self._seen[message.message_id] = (digest, reply)
        return self._record(reply, message, provenance)
