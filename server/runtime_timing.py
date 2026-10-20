"""Bounded, content-free process timing for the existing operator diagnostics.

Durations are inclusive monotonic wall time, not CPU or native HTTP-attempt
counts. Only fixed stage/tool labels are retained; arguments, return values,
paths, identities, exception messages and attendee content never enter here.
"""
from __future__ import annotations

from collections import deque
from contextlib import contextmanager
from contextvars import ContextVar
import threading
import time
import uuid

_STAGES = frozenset({
    "install.total", "install.venv", "install.dependencies", "install.skills_git",
    "artifact.fetch", "artifact.download", "artifact.extract",
    "integrity.file_hash", "integrity.artifact_hash", "integrity.directory_hash",
    "proof.total", "proof.binary", "proof.version_command", "proof.skills", "proof.python_tool",
    "readiness.total", "identity.auth", "identity.validation", "identity.request", "identity.obo_capture",
    "capture.initialize", "capture.workspace_init", "capture.workspace_list",
    "capture.workspace_read", "capture.workspace_create", "capture.journal_read",
    "capture.journal_refresh", "capture.journal_write", "capture.journal_audit",
})
_COMPONENTS = frozenset({"runtime", "node", "claude", "codex", "databricks", "tmux",
                         "omnigent", "skills", "apx", "bun", "uv", "agentbricks"})
_component: ContextVar[str] = ContextVar("runtime_timing_component", default="runtime")
_lock = threading.Lock()
_aggregates: dict[tuple[str, str], dict] = {}
_recent: deque[dict] = deque(maxlen=128)
_process_id = uuid.uuid4().hex
_started_at = time.time()
_unsupported = 0


@contextmanager
def stage(name: str, *, component: str | None = None):
    """Measure one fixed scope; nested measurements retain the current tool."""
    global _unsupported
    selected = component if component is not None else _component.get()
    if name not in _STAGES or selected not in _COMPONENTS:
        with _lock:
            _unsupported += 1
        yield
        return
    key = (name, selected)
    started_at, started = time.time(), time.monotonic()
    with _lock:
        aggregate = _aggregates.setdefault(key, {
            "stage": name, "component": selected, "started": 0, "completed": 0,
            "errors": 0, "inflight": 0, "total_ms": 0.0, "max_ms": 0.0, "last_ms": None,
        })
        aggregate["started"] += 1
        aggregate["inflight"] += 1
    token = _component.set(selected)
    failed = False
    try:
        yield
    except BaseException:
        failed = True
        raise
    finally:
        elapsed = max(0.0, (time.monotonic() - started) * 1000)
        _component.reset(token)
        with _lock:
            aggregate["completed"] += 1
            aggregate["errors"] += int(failed)
            aggregate["inflight"] -= 1
            aggregate["total_ms"] += elapsed
            aggregate["max_ms"] = max(aggregate["max_ms"], elapsed)
            aggregate["last_ms"] = elapsed
            _recent.append({"stage": name, "component": selected, "started_at": started_at,
                            "duration_ms": elapsed, "outcome": "raised" if failed else "returned"})


def snapshot() -> dict:
    with _lock:
        return {
            "process_id": _process_id, "started_at": _started_at,
            "clock": "monotonic", "duration_unit": "ms", "durations_are_inclusive": True,
            "count_unit": "function_or_scope_invocations", "retention": "current_process",
            "error_count": "exceptions_escaping_scope",
            "unsupported_labels": _unsupported, "recent_limit": _recent.maxlen,
            "stages": [dict(value) for _, value in sorted(_aggregates.items())],
            "recent": [dict(value) for value in _recent],
        }
