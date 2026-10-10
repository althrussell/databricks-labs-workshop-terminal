"""External normal-UI novice journey with read-only native conversation polling.

HTTP/auth and optional deployment discovery are injected by the external caller.
No cloud SDK, terminal parser, shell command or repair prompt belongs here. The
local observation bridge must first be qualified against the actual harness pin.
Readable, correlated messages still do not prove tool timing or log authenticity.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import re
import time
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime
from typing import Awaitable, Callable, Literal
from urllib.parse import urlencode
from uuid import UUID

from .adapters.browser import BrowserLaunchConfig, WorkshopBrowserDriver, validate_app_url
from .adapters.harness import redact_evidence
from .simulator import BuilderMessage, NoviceSimulator
from .interactions import validate_question

SUPPORTED_PINS = {"claude": "2.1.237", "codex": "0.148.0"}
REVIEWED_PINS = {"claude": frozenset({"2.1.237", "2.1.283", "2.1.295", "2.1.296"}),
                 "codex": frozenset({"0.148.0", "0.157.1", "0.162.0", "0.162.1"})}
_INSTRUMENTATION = "read_only_native_transcript_v1"
_UNVERIFIED = ["tool_and_worker_attribution", "implementation_timing", "native_transcript_authenticity"]
_BASE_FIELDS = {"schema_version", "instrumentation", "status", "binding_verified", "messages",
                "next_cursor", "tool_and_worker_coverage_verified", "transcript_authenticity_verified",
                "unverified_checks"}
_BOUND_FIELDS = {"harness_id", "harness_version", "wt_session_id", "native_session_id"}
_MESSAGE_FIELDS = {"message_id", "timestamp", "role", "visibility", "complete", "text"}


class NativeEvidenceError(ValueError):
    """Stable reason only; never includes an HTTP body or raw conversation."""


@dataclass(frozen=True)
class JourneyConfig:
    mode: Literal["consultation_probe", "build"] = "consultation_probe"
    total_seconds: float = 1_800
    consultation_seconds: float = 180
    poll_interval_seconds: float = 1
    operation_timeout_seconds: float = 15
    cleanup_seconds: float = 10
    max_polls: int = 2_000
    max_messages: int = 1_000
    max_evidence_chars: int = 2 * 1024 * 1024
    harness_version: str | None = None

    def __post_init__(self):
        if self.mode not in {"consultation_probe", "build"}:
            raise ValueError("Unknown journey mode")
        if self.harness_version is not None and not isinstance(self.harness_version, str):
            raise ValueError("Harness version must be an explicit reviewed pin")
        for key in ("total_seconds", "consultation_seconds", "poll_interval_seconds",
                    "operation_timeout_seconds", "cleanup_seconds"):
            value = getattr(self, key)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
                raise ValueError(f"{key} must be finite and positive")
        if self.consultation_seconds > min(180, self.total_seconds):
            raise ValueError("Consultation is bounded by 180 seconds and the total budget")
        if self.cleanup_seconds > 60 or self.operation_timeout_seconds > 60:
            raise ValueError("Individual operations and cleanup must be bounded by 60 seconds")
        for key in ("max_polls", "max_messages", "max_evidence_chars"):
            if type(getattr(self, key)) is not int or getattr(self, key) < 1:
                raise ValueError(f"{key} must be a positive integer")


@dataclass(frozen=True)
class AppObservation:
    """Read-only discovery result; never an app acceptance verdict."""
    status: Literal["awaiting", "deployment_observed", "needs_review"] = "awaiting"
    app_url: str = ""
    artifacts: tuple[str, ...] = ()
    source: str = ""

    def __post_init__(self):
        if self.status not in {"awaiting", "deployment_observed", "needs_review"}:
            raise ValueError("Unknown app observation status")
        if self.app_url:
            if not isinstance(self.app_url, str) or len(self.app_url) > 2_048:
                raise ValueError("App URL must be a bounded string")
            validate_app_url(self.app_url)
        if self.status == "deployment_observed" and not self.app_url:
            raise ValueError("Deployment observation requires an app URL")
        if self.status == "deployment_observed" and not self.source:
            raise ValueError("Deployment observation requires its external source")
        if (not isinstance(self.artifacts, tuple) or len(self.artifacts) > 100
                or any(not isinstance(value, str) or len(value) > 2_000 for value in self.artifacts)):
            raise ValueError("App artifact references must be bounded strings")
        if not isinstance(self.source, str) or len(self.source) > 2_000:
            raise ValueError("Observation source must be bounded")


@dataclass
class JourneyResult:
    mode: str
    status: str = "unverified"
    stop_reason: str = ""
    entry: dict = field(default_factory=dict)
    native_session_id: str = ""
    harness_id: str = ""
    harness_version: str = ""
    messages: list[dict] = field(default_factory=list)
    deliveries: list[dict] = field(default_factory=list)
    simulator: dict = field(default_factory=dict)
    first_complete_assistant: dict | None = None
    material_consultation_observed: bool = False
    scope_agreement_delivered: bool = False
    consultation_deadline_enforced: bool = False
    consultation_status: str = "pending"
    consultation_stop_reason: str = ""
    consultation_closed_elapsed_seconds: float | None = None
    no_question_before_scope_agreement_observed: bool = False
    total_deadline_enforced: bool = False
    polls: int = 0
    elapsed_seconds: float = 0
    budget_start: str = "before_normal_ui_entry"
    cleanup: dict = field(default_factory=lambda: {"status": "not_needed"})
    app_observations: list[dict] = field(default_factory=list)
    full_app_acceptance_verified: bool = False
    accepted: bool = False
    unverified_checks: list[str] = field(default_factory=lambda: [*_UNVERIFIED, "full_app_acceptance"])

    def to_dict(self) -> dict:
        return asdict(self)


OperatorFetch = Callable[[str], Awaitable[dict]]
AppObserver = Callable[[dict], Awaitable[AppObservation | None]]
BuildContinue = Callable[[dict], Awaitable[Literal["continue", "done", "needs_review"]]]


def _uuid(value: object) -> bool:
    try:
        return isinstance(value, str) and str(UUID(value)) == value
    except ValueError:
        return False


def _epoch(value: object) -> float:
    if not isinstance(value, str):
        raise NativeEvidenceError("native_message_timestamp_unverified")
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if not stamp.tzinfo:
            raise ValueError()
        return stamp.timestamp()
    except ValueError as exc:
        raise NativeEvidenceError("native_message_timestamp_unverified") from exc


def _normal_delivery(text: str) -> str:
    # CR/LF is transport presentation, not permission to accept partial text or
    # an arbitrary prompt which happens to contain the attendee's sentence.
    return text.replace("\r\n", "\n").replace("\r", "\n").strip()


class NativeConsultationRunner:
    """Single-use, bounded polling of one freshly UI-launched WT session.

    ``operator_fetch`` gets only an exact relative admin endpoint. It must use a
    separately authenticated operator and return the decoded response object.
    ``build_continue`` can return only a decision; it cannot supply attendee
    replies. Unsupported questions and unrecognized formats require review.
    """

    def __init__(self, driver: WorkshopBrowserDriver, simulator: NoviceSimulator,
                 operator_fetch: OperatorFetch, config: JourneyConfig | None = None,
                 *, app_observer: AppObserver | None = None,
                 build_continue: BuildContinue | None = None,
                 clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
                 secrets: tuple[str, ...] = ()):
        self.driver, self.simulator, self.operator_fetch = driver, simulator, operator_fetch
        self.config = config or JourneyConfig()
        if self.config.mode != "build" and (app_observer or build_continue):
            raise ValueError("App observation and build continuation belong to build mode")
        self.app_observer, self.build_continue = app_observer, build_continue
        self.clock, self.sleep, self.secrets = clock, sleep, secrets
        self.result = JourneyResult(self.config.mode)
        self._used = False
        self._started = 0.0
        self._cursor = ""
        self._cursors: set[str] = set()
        self._seen: dict[str, str] = {}
        self._pending: list[dict] = []
        self._last_timestamp = float("-inf")
        self._evidence_chars = 0
        self._session_id = ""
        self._consultation_seen_at: float | None = None
        self._entry_started = False

    def _elapsed(self) -> float:
        return max(0, self.clock() - self._started)

    def _consultation_text_available(self) -> bool:
        return self.result.first_complete_assistant is not None and any(
            delivery.get("decision") == "opening" and delivery.get("native_correlated") is True
            for delivery in self.result.deliveries
        )

    def _can_passively_observe(self) -> bool:
        # A model that rushed ahead can still produce an app worth measuring.
        # A tool-using turn can take longer than consultation without producing
        # an end_turn message. Continue only after the exact opening is bound to
        # the native session; silence still cannot establish consultation quality.
        return (self.config.mode == "build" and self.app_observer is not None
                and bool(self.result.native_session_id) and any(
                    delivery.get("decision") == "opening" and delivery.get("native_correlated") is True
                    for delivery in self.result.deliveries))

    def _close_consultation(self, status: str, reason: str):
        if self.result.consultation_status == "pending":
            self.result.consultation_status = status
            self.result.consultation_stop_reason = reason
            self.result.consultation_closed_elapsed_seconds = self._elapsed()

    def _consultation_failure_reason(self) -> str:
        return ("consultation_scope_not_agreed" if self.result.material_consultation_observed
                else "no_material_consultation_within_budget")

    def _remaining(self) -> float:
        remaining = self.config.total_seconds - self._elapsed()
        if not self.result.scope_agreement_delivered and not self._can_passively_observe():
            remaining = min(remaining, self.config.consultation_seconds - self._elapsed())
        return remaining

    async def _bounded(self, awaitable):
        remaining = self._remaining()
        if remaining <= 0:
            if hasattr(awaitable, "close"):
                awaitable.close()
            raise asyncio.TimeoutError()
        value = await asyncio.wait_for(awaitable, timeout=min(remaining, self.config.operation_timeout_seconds))
        if self._remaining() <= 0:
            raise asyncio.TimeoutError()
        return value

    def _finish(self, status: str, reason: str):
        self.result.status, self.result.stop_reason = status, reason
        self._close_consultation("unverified", reason)

    def _deadline(self) -> bool:
        elapsed = self._elapsed()
        if elapsed >= self.config.total_seconds:
            self.result.total_deadline_enforced = True
            if not self.result.scope_agreement_delivered:
                self._close_consultation("failed" if self._consultation_text_available() else "unverified",
                                        self._consultation_failure_reason())
            self._finish("failed", "total_deadline")
            return True
        if not self.result.scope_agreement_delivered and elapsed >= self.config.consultation_seconds:
            self.result.consultation_deadline_enforced = True
            self._close_consultation("failed" if self._consultation_text_available() else "unverified",
                                    self._consultation_failure_reason())
            # Freeze simulator replies at its own consultation boundary too.
            # Native/app collection has a separate total budget in build mode.
            self.simulator.budget_status()
            if not self._can_passively_observe():
                self._finish("failed", self._consultation_failure_reason())
                return True
        return False

    def _snapshot(self) -> dict:
        # Only already-observed transcript and already-disclosed facts. The
        # simulator's private scenario and evaluator rubric are never included.
        snapshot = self.result.to_dict()
        snapshot["simulator"] = self.simulator.evidence()
        snapshot["elapsed_seconds"] = self._elapsed()
        return json.loads(redact_evidence(json.dumps(snapshot), self.secrets))

    def _validate_page(self, payload: dict) -> tuple[list[dict], str]:
        if not isinstance(payload, dict):
            raise NativeEvidenceError("unsupported_native_response")
        try:
            response_bytes = len(json.dumps(payload, ensure_ascii=False, allow_nan=False).encode())
        except (ValueError, TypeError) as exc:
            raise NativeEvidenceError("unsupported_native_response") from exc
        if response_bytes > 300 * 1024:
            raise NativeEvidenceError("native_response_size_budget")
        if (type(payload.get("schema_version")) is not int or payload["schema_version"] != 1
                or payload.get("instrumentation") != _INSTRUMENTATION):
            raise NativeEvidenceError("unsupported_native_response")
        if (payload.get("tool_and_worker_coverage_verified") is not False
                or payload.get("transcript_authenticity_verified") is not False
                or payload.get("unverified_checks") != _UNVERIFIED):
            raise NativeEvidenceError("unsupported_native_coverage_claim")
        if payload.get("status") == "unverified":
            if (set(payload) != _BASE_FIELDS | {"reason"} or payload.get("binding_verified") is not False
                    or payload.get("messages") != [] or payload.get("next_cursor") != ""
                    or not isinstance(payload.get("reason"), str)):
                raise NativeEvidenceError("unsupported_native_response")
            # No native file immediately after UI launch is expected. Every
            # other unverified result fails closed, with no fallback to PTY.
            if payload["reason"] == "native_session_not_bound" and not self.result.native_session_id:
                return [], self._cursor
            raise NativeEvidenceError(payload["reason"] if re.fullmatch(r"[a-z_]{1,100}", payload["reason"])
                                      else "unsupported_native_response")
        if (set(payload) != _BASE_FIELDS | _BOUND_FIELDS or payload.get("binding_verified") is not True
                or payload.get("status") not in {"ready", "awaiting_complete_message"}):
            raise NativeEvidenceError("unsupported_native_response")
        if (payload.get("wt_session_id") != self._session_id
                or payload.get("harness_id") != self.result.harness_id
                or payload.get("harness_version") != self.result.harness_version
                or not _uuid(payload.get("native_session_id"))):
            raise NativeEvidenceError("native_session_or_version_changed")
        native_id = payload["native_session_id"]
        if self.result.native_session_id and native_id != self.result.native_session_id:
            raise NativeEvidenceError("native_session_or_version_changed")
        cursor, messages = payload.get("next_cursor"), payload.get("messages")
        if (not isinstance(cursor, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,1024}", cursor)
                or not isinstance(messages, list) or len(messages) > 30
                or bool(messages) != (payload["status"] == "ready")):
            raise NativeEvidenceError("unsupported_native_response")
        if cursor != self._cursor and cursor in self._cursors:
            raise NativeEvidenceError("native_cursor_replayed")
        validated = []
        batch_seen = dict(self._seen)
        timestamp = self._last_timestamp
        chars = self._evidence_chars
        for message in messages:
            if (not isinstance(message, dict) or set(message) not in (_MESSAGE_FIELDS, _MESSAGE_FIELDS | {"interaction"})
                    or message.get("role") not in {"user", "assistant"}
                    or message.get("visibility") != "user" or message.get("complete") is not True
                    or not isinstance(message.get("message_id"), str)
                    or not re.fullmatch(r"[a-f0-9]{64}", message["message_id"])
                    or not isinstance(message.get("text"), str) or not message["text"].strip()
                    or len(message["text"]) > 12_000):
                raise NativeEvidenceError("unsupported_or_truncated_native_message")
            if "interaction" in message:
                if self.result.harness_id != "claude" or message["role"] != "assistant":
                    raise NativeEvidenceError("unsupported_native_question")
                try:
                    validate_question(message["interaction"])
                except ValueError as error:
                    raise NativeEvidenceError("unsupported_native_question") from error
            stamp = _epoch(message["timestamp"])
            digest = hashlib.sha256(json.dumps(message, sort_keys=True).encode()).hexdigest()
            old = batch_seen.get(message["message_id"])
            if old is not None:
                if old != digest:
                    raise NativeEvidenceError("native_message_id_content_changed")
                continue
            if stamp < timestamp:
                raise NativeEvidenceError("native_messages_out_of_order")
            timestamp = stamp
            batch_seen[message["message_id"]] = digest
            chars += len(message["text"])
            if len(batch_seen) > self.config.max_messages or chars > self.config.max_evidence_chars:
                raise NativeEvidenceError("native_evidence_budget")
            validated.append(message)
        if cursor == self._cursor and validated:
            raise NativeEvidenceError("native_cursor_did_not_advance")
        self.result.native_session_id = native_id
        return validated, cursor

    async def _process_message(self, message: dict) -> bool:
        digest = hashlib.sha256(json.dumps(message, sort_keys=True).encode()).hexdigest()
        self._seen[message["message_id"]] = digest
        self._last_timestamp = _epoch(message["timestamp"])
        self._evidence_chars += len(message["text"])
        safe = json.loads(redact_evidence(json.dumps(message), self.secrets))
        safe["observed_elapsed_seconds"] = self._elapsed()
        self.result.messages.append(safe)
        if message["role"] == "user":
            if not self._pending or _normal_delivery(message["text"]) != _normal_delivery(self._pending[0]["text"]):
                raise NativeEvidenceError("native_user_delivery_mismatch")
            delivery = self._pending.pop(0)
            delivery["native_message_id"] = message["message_id"]
            delivery["native_correlated"] = True
            delivery["correlated_elapsed_seconds"] = self._elapsed()
            if delivery["decision"] == "agree":
                self.result.scope_agreement_delivered = True
                self._close_consultation("passed", "scope_agreement_correlated")
                if self.config.mode == "consultation_probe":
                    self._finish("completed_probe", "scope_agreement_correlated")
                    return True
            return False
        if self.result.first_complete_assistant is None:
            self.result.first_complete_assistant = dict(safe)
        if self._pending:
            raise NativeEvidenceError("assistant_before_user_delivery_correlation")
        if self.result.consultation_deadline_enforced and self._can_passively_observe():
            # Keep authored native evidence, but disclose nothing and provide no
            # new attendee reply after consultation timed out.
            return False
        interaction = message.get("interaction")
        answers = []
        if interaction:
            replies = [self.simulator.respond(BuilderMessage(message["message_id"] + ":" + str(i), question["question"]))
                       for i, question in enumerate(interaction["questions"])]
            unresolved = next((item for item in replies if item.text is None), None)
            if unresolved is not None:
                reply = unresolved
            else:
                answers = [item.text for item in replies]
                reply = replace(replies[0], text="\n".join(answers),
                    decision="agree" if all(item.decision == "agree" for item in replies) else "answer",
                    disclosed_fact_ids=tuple(dict.fromkeys(fact for item in replies for fact in item.disclosed_fact_ids)))
        else:
            reply = self.simulator.respond(BuilderMessage(message["message_id"], safe["text"]))
        if reply.decision == "needs_review":
            self._finish("unverified", reply.stop_reason or "simulator_needs_review")
            return True
        if reply.decision == "stopped":
            if reply.stop_reason == "consultation_deadline" and self._can_passively_observe():
                self.result.consultation_deadline_enforced = True
                self._close_consultation("failed", self._consultation_failure_reason())
                return False
            if not self._deadline():
                self._finish("failed", reply.stop_reason or "simulator_budget")
            return True
        if reply.text is not None:
            # Mark material consultation only for an explicitly asked business
            # topic. A framework/style choice alone cannot satisfy this check.
            if reply.decision in {"answer", "agree"} and reply.disclosed_fact_ids:
                self.result.material_consultation_observed = True
                if self._consultation_seen_at is None:
                    self._consultation_seen_at = self._elapsed()
            if interaction:
                await self._bounded(self.driver.submit_question_answers(interaction, answers))
            else:
                await self._bounded(self.driver.submit_reply(reply.text))
            delivery = {"text": reply.text, "decision": reply.decision,
                        "in_reply_to": reply.in_reply_to, "fact_ids": list(reply.disclosed_fact_ids),
                        "ui_submitted": True, "native_correlated": False,
                        "submitted_elapsed_seconds": self._elapsed()}
            self.result.deliveries.append(delivery)
            self._pending.append(delivery)
            return False
        if reply.stop_reason == "no_question":
            if not self.result.scope_agreement_delivered:
                self.result.no_question_before_scope_agreement_observed = True
                if self._can_passively_observe():
                    return False
            if self.config.mode == "build" and self.result.scope_agreement_delivered:
                decision = (await self._bounded(self.build_continue(self._snapshot())) if self.build_continue
                            else "continue" if self.app_observer else "done")
                if decision not in {"continue", "done", "needs_review"}:
                    raise NativeEvidenceError("unsupported_build_continuation")
                if decision == "continue":
                    return False
                self._finish("unverified", "builder_done_without_app_acceptance" if decision == "done"
                             else "build_continuation_needs_review")
            else:
                self._finish("unverified", "assistant_no_question_before_scope_agreement")
            return True
        # Simulator decisions unknown to this driver never become continuation
        # or an inferred attendee answer.
        self._finish("unverified", reply.stop_reason or "unsupported_simulator_decision")
        return True

    async def run(self, launch: BrowserLaunchConfig) -> JourneyResult:
        if self._used:
            raise RuntimeError("A journey runner can be used only once")
        self._used = True
        self._started = self.clock()
        label = ""
        try:
            if SUPPORTED_PINS.get(launch.agent_id) is None:
                raise NativeEvidenceError("unsupported_harness")
            if not launch.require_fresh_session:
                raise NativeEvidenceError("fresh_ui_session_required")
            if getattr(self.driver.evidence, "session_id", "") or getattr(self.driver, "_entry_started", False):
                raise NativeEvidenceError("fresh_browser_driver_required")
            opening = self.simulator.opening()
            if opening.text != launch.opening_message:
                raise NativeEvidenceError("opening_differs_from_simulator")
            self.result.harness_id = launch.agent_id
            self.result.harness_version = self.config.harness_version or SUPPORTED_PINS[launch.agent_id]
            if self.result.harness_version not in REVIEWED_PINS[launch.agent_id]:
                raise NativeEvidenceError("unsupported_harness_version")
            bounded_launch = replace(launch,
                                     deadline_seconds=min(launch.deadline_seconds, self.config.consultation_seconds),
                                     run_deadline_seconds=self.config.total_seconds)
            # Entry gets its explicit UI deadline, while consultation and total
            # budgets include the entire entry. Waiting never resets a budget.
            self._entry_started = True
            entry = await asyncio.wait_for(self.driver.enter(bounded_launch), timeout=self._remaining())
            self.result.entry = entry.to_dict()
            self._session_id = entry.session_id
            label = next((agent.get("label", "") for agent in self.driver._agents or []
                          if agent.get("id") == launch.agent_id), "")
            if entry.error_code:
                self._finish("failed", entry.error_code)
                return self.result
            if not entry.opening_submitted or not _uuid(self._session_id) or entry.agent_id != launch.agent_id:
                raise NativeEvidenceError("normal_ui_opening_unverified")
            delivered_text = (entry.prompt_delivery_request if launch.entry_path == "wizard" else opening.text)
            if launch.entry_path == "wizard" and not entry.prompt_preservation_verified:
                raise NativeEvidenceError("normal_ui_opening_unverified")
            delivery = {"text": delivered_text, "decision": "opening", "in_reply_to": None, "fact_ids": [],
                        "ui_submitted": True, "native_correlated": False,
                        "submitted_elapsed_seconds": self._elapsed()}
            self.result.deliveries.append(delivery)
            self._pending.append(delivery)
            while not self._deadline():
                if self.result.polls >= self.config.max_polls:
                    self._finish("failed", "poll_budget")
                    break
                path = f"/api/admin/evaluation/sessions/{self._session_id}/messages?" + urlencode(
                    {"cursor": self._cursor, "limit": 30})
                self.result.polls += 1
                payload = await self._bounded(self.operator_fetch(path))
                if self._deadline():
                    break
                messages, cursor = self._validate_page(payload)
                stopped = False
                for message in messages:
                    if self._deadline():
                        stopped = True
                        break
                    if await self._process_message(message):
                        stopped = True
                        break
                if stopped:
                    break
                self._cursor = cursor
                if cursor:
                    self._cursors.add(cursor)
                if self.app_observer:
                    observation = await self._bounded(self.app_observer(self._snapshot()))
                    if observation is not None:
                        if not isinstance(observation, AppObservation):
                            raise NativeEvidenceError("unsupported_app_observation")
                        item = json.loads(redact_evidence(json.dumps(asdict(observation)), self.secrets))
                        item["observed_elapsed_seconds"] = self._elapsed()
                        if (not self.result.app_observations
                                or {key: value for key, value in self.result.app_observations[-1].items()
                                    if key != "observed_elapsed_seconds"} != {
                                        key: value for key, value in item.items()
                                        if key != "observed_elapsed_seconds"}):
                            self._evidence_chars += len(json.dumps(item))
                            if self._evidence_chars > self.config.max_evidence_chars:
                                raise NativeEvidenceError("native_evidence_budget")
                            self.result.app_observations.append(item)
                        if observation.status == "needs_review":
                            self._finish("unverified", "app_observation_needs_review")
                            break
                        if observation.status == "deployment_observed":
                            if self.result.scope_agreement_delivered and not self._pending:
                                self._finish("completed_build_observation", "deployment_observed")
                            else:
                                self._close_consultation("failed" if self._consultation_text_available() else "unverified",
                                                        "deployment_before_correlated_scope_agreement")
                                self._finish("unverified", "deployment_before_correlated_scope_agreement")
                            break
                if self._deadline():
                    break
                await self._bounded(self.sleep(min(self.config.poll_interval_seconds, max(0, self._remaining()))))
        except NativeEvidenceError as exc:
            self._finish("unverified", str(exc))
        except asyncio.TimeoutError:
            if self._entry_started and not getattr(self.driver.evidence, "opening_submitted", False):
                self._finish("failed", "browser_entry_timeout")
            elif not self._deadline():
                self._finish("failed", "operation_timeout")
        except Exception as exc:
            self._finish("failed", "journey_operation_" + type(exc).__name__)
        finally:
            # An interrupted UI launch may already have created its own session.
            # Refuse to close if UI observation now names a different session.
            current = getattr(self.driver, "evidence", None)
            if self._entry_started and not self.result.entry and current is not None:
                self.result.entry = current.to_dict()
            owned_id = (self._session_id or getattr(current, "session_id", "")) if self._entry_started else ""
            if owned_id:
                if not label:
                    label = next((agent.get("label", "") for agent in self.driver._agents or []
                                  if agent.get("id") == launch.agent_id), "")
                try:
                    await asyncio.wait_for(self.driver.close_launched_session(
                        label, expected_session_id=owned_id, timeout_seconds=self.config.cleanup_seconds),
                        timeout=self.config.cleanup_seconds)
                    self.result.cleanup = {"status": "closed_owned_ui_session", "session_id": owned_id}
                except Exception as exc:
                    self.result.cleanup = {"status": "failed", "session_id": owned_id,
                                           "error_type": type(exc).__name__}
            self.result.simulator = self.simulator.evidence()
            self.result.elapsed_seconds = self._elapsed()
            # The result may contain observed user-visible prose. Never include
            # operator auth, browser storage, undisclosed facts, or PTY content.
            safe_result = json.loads(redact_evidence(json.dumps(self.result.to_dict()), self.secrets))
            for key, value in safe_result.items():
                setattr(self.result, key, value)
        return self.result


async def run_journey(driver: WorkshopBrowserDriver, launch: BrowserLaunchConfig,
                      simulator: NoviceSimulator, operator_fetch: OperatorFetch,
                      config: JourneyConfig | None = None, **kwargs) -> JourneyResult:
    """Convenience interface for an external CLI with injected operator HTTP."""
    return await NativeConsultationRunner(driver, simulator, operator_fetch, config, **kwargs).run(launch)
