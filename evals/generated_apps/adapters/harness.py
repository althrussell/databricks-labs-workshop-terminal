"""Observe the WT browser's PTY transport without inventing agent messages.

This adapter subscribes to Playwright's existing WebSocket observation surface.
It neither creates a session nor opens a second socket. A terminal replay is
diagnostic evidence, not a structured assistant turn or proof of consultation.
"""

from __future__ import annotations

import json
import hashlib
import re
import time
from dataclasses import asdict, dataclass
from typing import Iterable
from pathlib import Path
from urllib.parse import urlsplit


def redact_evidence(value: str, secrets: Iterable[str] = ()) -> str:
    """Redact known credentials and common token forms before evidence leaves memory."""
    for secret in sorted((s for s in secrets if s), key=len, reverse=True):
        value = value.replace(secret, "[REDACTED]")
    value = re.sub(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]+", r"\1[REDACTED]", value)
    value = re.sub(r"\bdapi[a-fA-F0-9]{16,}\b", "[REDACTED]", value)
    value = re.sub(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b", "[REDACTED]", value)
    value = re.sub(
        r"(?i)([\"']?(?:access_token|refresh_token|client_secret|api_key|authorization)[\"']?\s*[:=]\s*[\"']?)[^\s\"',}]+",
        r"\1[REDACTED]",
        value,
    )
    return value


@dataclass(frozen=True)
class TransportFrame:
    elapsed_seconds: float
    direction: str
    kind: str
    byte_count: int
    session_id: str = ""


class HarnessObserver:
    """Bounded diagnostic capture from the socket the attendee actually uses."""

    structured_events_supported = False

    def __init__(self, *, max_output_bytes: int = 2 * 1024 * 1024,
                 secrets: Iterable[str] = ()):
        if max_output_bytes < 1:
            raise ValueError("max_output_bytes must be positive")
        self.max_output_bytes = max_output_bytes
        self.secrets = tuple(secrets)
        self.started = time.monotonic()
        self.frames: list[TransportFrame] = []
        self._output = bytearray()
        self.output_bytes = 0
        self.omitted_bytes = 0
        self.inputs: list[dict] = []
        self._input = bytearray()
        self.session_ids: set[str] = set()
        self.closed_sessions: set[str] = set()
        self.socket_errors = 0

    @staticmethod
    def session_id(url: str) -> str | None:
        match = re.fullmatch(r"/ws/sessions/([^/]+)", urlsplit(url).path)
        return match.group(1) if match else None

    def attach(self, socket, *, wt_origin: str) -> bool:
        """Observe only the selected WT origin's session WebSockets."""
        parsed = urlsplit(socket.url)
        expected = urlsplit(wt_origin)
        sid = self.session_id(socket.url)
        if not sid or parsed.netloc != expected.netloc:
            return False
        self.session_ids.add(sid)
        socket.on("framereceived", lambda payload: self.feed("received", payload, sid))
        socket.on("framesent", lambda payload: self.feed("sent", payload, sid))
        socket.on("close", lambda: self.closed_sessions.add(sid))
        socket.on("socketerror", lambda _error: self._socket_error())
        return True

    def _socket_error(self) -> None:
        self.socket_errors += 1

    def feed(self, direction: str, payload: str | bytes, session_id: str = "") -> None:
        if direction not in {"sent", "received"}:
            raise ValueError("direction must be sent or received")
        try:
            message = json.loads(payload)
        except (json.JSONDecodeError, UnicodeDecodeError, TypeError):
            return
        if not isinstance(message, dict):
            return
        kind = message.get("t")
        if kind not in {"input", "output", "replay", "resize", "exit", "ping", "pong"}:
            return
        data = message.get("data")
        raw = data.encode("utf-8") if isinstance(data, str) else b""
        elapsed = time.monotonic() - self.started
        # Frame metadata deliberately excludes chunks: a credential may span
        # frames. Redaction runs on the composed terminal evidence instead.
        if len(self.frames) < 100_000:
            self.frames.append(TransportFrame(elapsed, direction, kind, len(raw), session_id))
        if session_id:
            self.session_ids.add(session_id)
        if direction == "received" and kind in {"output", "replay"} and raw:
            self.output_bytes += len(raw)
            self._output.extend(raw)
            excess = max(0, len(self._output) - self.max_output_bytes)
            if excess:
                del self._output[:excess]
                self.omitted_bytes += excess
        elif direction == "sent" and kind == "input" and isinstance(data, str):
            self.inputs.append({
                "elapsed_seconds": elapsed,
                "byte_count": len(raw),
            })
            self._input.extend(raw)
            if len(self._input) > self.max_output_bytes:
                del self._input[:-self.max_output_bytes]

    def evidence(self) -> dict:
        return {
            "source": "attendee_browser_existing_pty_websocket",
            "structured_events_supported": False,
            "assistant_turns": [],
            "tool_events": [],
            "unverified_checks": ["consultation", "recommendation", "scope_before_implementation",
                                  "assistant_turn_boundaries", "tool_and_worker_attribution"],
            "session_ids": sorted(self.session_ids),
            "frame_metadata": [asdict(frame) for frame in self.frames],
            "novice_input_transport": self.inputs,
            "composed_input_transport": redact_evidence(
                self._input.decode("utf-8", errors="replace"), self.secrets),
            "terminal_replay_and_output": redact_evidence(
                self._output.decode("utf-8", errors="replace"), self.secrets),
            "output_bytes_observed": self.output_bytes,
            "output_bytes_omitted": self.omitted_bytes,
            "socket_errors": self.socket_errors,
        }

    def clear(self) -> None:
        """Discard private in-memory terminal evidence once the report is written."""
        self._output.clear()
        self._input.clear()
        self.inputs.clear()
        self.frames.clear()


@dataclass(frozen=True)
class StructuredHarnessEvent:
    event_id: str
    timestamp: str
    kind: str
    text: str = ""
    role: str = ""
    complete: bool = False
    tool_name: str = ""
    tool_arguments: dict | str | None = None


@dataclass(frozen=True)
class TranscriptExport:
    """Explicit, externally exported synthetic transcript; no home-directory scan."""
    harness_id: str
    harness_version: str
    session_id: str
    source_sha256: str
    events: tuple[StructuredHarnessEvent, ...]
    ignored_records: int
    incomplete_assistant_messages: int

    def complete_assistant_turns(self):
        """Only completed user-visible authored text can drive the novice policy."""
        from ..simulator import BuilderMessage
        return [BuilderMessage(message_id=e.event_id, text=e.text, complete=True)
                for e in self.events if e.kind == "assistant_message" and e.complete and e.text]

    def evidence(self) -> dict:
        return {
            "source": "operator_exported_native_synthetic_transcript",
            "harness_id": self.harness_id,
            "harness_version": self.harness_version,
            "session_id": self.session_id,
            "source_sha256": self.source_sha256,
            "events": [asdict(event) for event in self.events],
            "ignored_records": self.ignored_records,
            "incomplete_assistant_messages": self.incomplete_assistant_messages,
            "structured_assistant_turns_available": bool(self.complete_assistant_turns()),
            # A transcript with some readable turns is not proof that every
            # mutation/subagent event was exported and attributable.
            "tool_and_worker_coverage_verified": False,
        }


def load_exported_transcript(path: str | Path, *, harness_id: str, harness_version: str,
                             session_id: str, secrets: Iterable[str] = (),
                             max_bytes: int = 24 * 1024 * 1024) -> TranscriptExport:
    """Read known native envelopes from an explicitly supplied evaluation export.

    Claude's message envelope and Codex's response_item envelope are already
    recognized by server/artifacts.py. This stricter adapter requires the native
    session/version markers and explicit completion markers. It supports the
    current repository pins only; the exported runtime format still needs live
    qualification. Other versions require an adapter audit.
    Missing markers yield no invented complete turns. Raw PTY is never accepted.
    """
    from ..journey import REVIEWED_PINS
    secrets = tuple(secrets)
    if harness_id not in REVIEWED_PINS or harness_version not in REVIEWED_PINS[harness_id]:
        raise ValueError("No transcript adapter is implemented for this harness/version pin")
    if not session_id:
        raise ValueError("The run's native harness session ID is required")
    file = Path(path)
    if file.stat().st_size > max_bytes:
        raise ValueError("Transcript export exceeds the bounded evidence budget")
    raw = file.read_bytes()
    events: list[StructuredHarnessEvent] = []
    ignored = 0
    incomplete = 0
    matched_session = False
    matched_version = False
    for index, line in enumerate(raw.splitlines(), 1):
        if len(line) > 512 * 1024:
            raise ValueError("Transcript record exceeds the bounded line budget")
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ValueError(f"Malformed structured transcript at line {index}") from exc
        if not isinstance(record, dict):
            raise ValueError(f"Structured transcript line {index} is not an object")
        timestamp = str(record.get("timestamp", ""))
        if harness_id == "claude":
            native_sid = record.get("sessionId")
            native_version = record.get("version")
            if native_sid and native_sid != session_id:
                raise ValueError("Export includes a different native harness session")
            if native_version and native_version != harness_version:
                raise ValueError("Native transcript version differs from the run manifest")
            matched_session |= native_sid == session_id
            matched_version |= native_version == harness_version
            if record.get("isSidechain") or record.get("isMeta"):
                ignored += 1
                continue
            message = record.get("message")
            if record.get("type") not in {"user", "assistant"} or not isinstance(message, dict):
                ignored += 1
                continue
            role = message.get("role")
            event_id = str(record.get("uuid") or message.get("id") or f"line-{index}")
            content = message.get("content", [])
            complete = role == "user" or message.get("stop_reason") in {"end_turn", "stop_sequence"}
        else:
            if record.get("type") == "session_meta":
                metadata = record.get("payload")
                if not isinstance(metadata, dict) or metadata.get("id") != session_id:
                    raise ValueError("Codex export session metadata differs from the run manifest")
                if metadata.get("cli_version") != harness_version:
                    raise ValueError("Codex export version differs from the run manifest")
                matched_session = matched_version = True
                continue
            message = record.get("payload")
            if record.get("type") != "response_item" or not isinstance(message, dict):
                ignored += 1
                continue
            event_id = str(message.get("id") or f"line-{index}")
            if message.get("type") in {"function_call", "custom_tool_call"}:
                events.append(StructuredHarnessEvent(
                    event_id, timestamp, "tool_call", tool_name=str(message.get("name", "")),
                    tool_arguments=redact_evidence(str(message.get("arguments") or message.get("input") or ""), secrets)))
                continue
            role = message.get("role")
            content = message.get("content", [])
            complete = role == "user" or message.get("phase") == "final_answer"
        if role not in {"user", "assistant"}:
            ignored += 1
            continue
        blocks = [{"type": "text", "text": content}] if isinstance(content, str) else content
        if not isinstance(blocks, list):
            raise ValueError("Unsupported native message content shape")
        text_parts = []
        for block in blocks:
            if not isinstance(block, dict):
                continue
            if block.get("type") in {"text", "input_text", "output_text"} and isinstance(block.get("text"), str):
                text_parts.append(block["text"])
            elif harness_id == "claude" and role == "assistant" and block.get("type") == "tool_use":
                arguments = json.loads(redact_evidence(json.dumps(block.get("input", {})), secrets))
                events.append(StructuredHarnessEvent(
                    str(block.get("id") or event_id), timestamp, "tool_call",
                    tool_name=str(block.get("name", "")), tool_arguments=arguments))
        text = redact_evidence("\n".join(text_parts), secrets)
        if text:
            if role == "assistant" and not complete:
                incomplete += 1
            events.append(StructuredHarnessEvent(event_id, timestamp, f"{role}_message", text=text,
                                                role=role, complete=complete))
    if not matched_session or not matched_version:
        raise ValueError("Export lacks native session/version markers; structured coverage is unverified")
    return TranscriptExport(harness_id, harness_version, session_id, hashlib.sha256(raw).hexdigest(),
                            tuple(events), ignored, incomplete)
