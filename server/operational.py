"""Bounded, secret-free process telemetry for Control Tower."""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Callable
from typing import Any

_STATUS_CODES = (409, 429, 503)
_MAX_COUNTER = 2**63 - 1


class ProcessMetrics:
    def __init__(self, *, max_counter: int = _MAX_COUNTER) -> None:
        self._max = max(1, int(max_counter))
        self._lock = threading.Lock()
        self._http = {code: 0 for code in _STATUS_CODES}
        self._ws_current = 0
        self._ws_total = 0

    def _increment(self, value: int) -> int:
        return min(self._max, value + 1)

    def record_http(self, status_code: int) -> None:
        if status_code not in self._http:
            return
        with self._lock:
            self._http[status_code] = self._increment(self._http[status_code])

    def websocket_attached(self) -> None:
        with self._lock:
            self._ws_current = self._increment(self._ws_current)
            self._ws_total = self._increment(self._ws_total)

    def websocket_detached(self) -> None:
        with self._lock:
            self._ws_current = max(0, self._ws_current - 1)

    def snapshot(self) -> dict[str, dict[str, int]]:
        with self._lock:
            return {
                "http_responses": {
                    str(code): self._http[code] for code in _STATUS_CODES
                },
                "websockets": {
                    "current": self._ws_current,
                    "total": self._ws_total,
                },
            }


def linux_rss_bytes() -> int | None:
    """Return Linux resident bytes without adding a process-inspection dependency."""
    try:
        with open("/proc/self/statm", encoding="ascii") as handle:
            resident_pages = int(handle.read().split()[1])
        return resident_pages * int(os.sysconf("SC_PAGE_SIZE"))
    except (OSError, ValueError, IndexError):
        return None


def _session_snapshot() -> dict[str, int]:
    from .sessions import session_manager

    terminal = session_manager.queue_metrics()["terminal"]
    return {
        "pty_processes": session_manager.count_all(),
        "terminal_subscribers": int(terminal["subscribers"]),
        "terminal_overflows": int(terminal["overflows"]),
    }


def _event_snapshot() -> dict[str, int]:
    from .events import event_hub

    value = event_hub.metrics()
    return {
        "subscribers": int(value["subscribers"]),
        "overflows": int(value["overflows"]),
    }


def build_snapshot(
    *,
    installer_status_fn: Callable[[], dict] | None = None,
    credential_status_fn: Callable[[], dict] | None = None,
    entitlement_status_fn: Callable[[], dict] | None = None,
    session_snapshot_fn: Callable[[], dict] = _session_snapshot,
    event_snapshot_fn: Callable[[], dict] = _event_snapshot,
    process_rss_fn: Callable[[], int | None] = linux_rss_bytes,
    process_metrics: ProcessMetrics | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    """Build a fixed-shape snapshot, deliberately selecting safe fields only."""
    if installer_status_fn is None:
        from .bootstrap.install import status as installer_status_fn
    if credential_status_fn is None:
        from .credentials import credential_manager

        credential_status_fn = credential_manager.status
    if entitlement_status_fn is None:
        from .entitlements import entitlement_manager

        entitlement_status_fn = entitlement_manager.status

    current_time = time.time() if now is None else now
    installer = installer_status_fn()
    steps = installer.get("steps", {})
    step_values = [
        step for step in steps.values() if isinstance(step, dict)
    ] if isinstance(steps, dict) else []
    durations = [
        int(step["duration_ms"])
        for step in step_values
        if isinstance(step.get("duration_ms"), (int, float))
    ]
    started = [
        float(step["started_at"])
        for step in step_values
        if isinstance(step.get("started_at"), (int, float))
    ]
    completed = [
        float(step["completed_at"])
        for step in step_values
        if isinstance(step.get("completed_at"), (int, float))
    ]
    if started:
        bootstrap_end = (
            current_time
            if any(step.get("status") in {"pending", "running"} for step in step_values)
            else max(completed, default=current_time)
        )
        bootstrap_duration_ms = max(
            0,
            round((bootstrap_end - min(started)) * 1000),
        )
    else:
        bootstrap_duration_ms = max(durations, default=0)
    credentials = credential_status_fn()
    last_success = credentials.get("last_successful_at")
    freshness = (
        max(0, round(current_time - float(last_success)))
        if isinstance(last_success, (int, float))
        else None
    )
    entitlements = entitlement_status_fn()
    handoff = entitlements.get("handoff", {})
    summary = handoff.get("summary", {}) if isinstance(handoff, dict) else {}
    entitlement_ok_value = entitlements.get("ok")
    entitlement_ok = (
        entitlement_ok_value if isinstance(entitlement_ok_value, bool) else None
    )
    entitlement_error_present = bool(entitlements.get("last_error"))
    last_reconcile = entitlements.get("last_reconcile")
    entitlement_freshness = (
        max(0, round(current_time - float(last_reconcile)))
        if isinstance(last_reconcile, (int, float))
        else None
    )
    sessions = session_snapshot_fn()
    events = event_snapshot_fn()
    counters = (process_metrics or metrics).snapshot()
    terminal_current = max(0, int(sessions.get("terminal_subscribers", 0)))
    websocket_current = max(
        counters["websockets"]["current"],
        terminal_current,
    )
    websocket_total = max(counters["websockets"]["total"], websocket_current)

    return {
        "bootstrap": {
            "duration_ms": bootstrap_duration_ms,
            "errors": sum(step.get("status") == "error" for step in step_values),
            "complete": sum(step.get("status") == "complete" for step in step_values),
            "total": len(step_values),
        },
        "http_responses": counters["http_responses"],
        "websockets": {
            "current": websocket_current,
            "total": websocket_total,
            "overflows": max(0, int(sessions.get("terminal_overflows", 0))),
            "event_subscribers": max(0, int(events.get("subscribers", 0))),
            "event_overflows": max(0, int(events.get("overflows", 0))),
        },
        "pty": {"processes": max(0, int(sessions.get("pty_processes", 0)))},
        "process": {"rss_bytes": process_rss_fn()},
        "credentials": {
            "state": str(credentials.get("state") or "unknown"),
            "source": str(credentials.get("source") or "unknown"),
            "freshness_seconds": freshness,
            "expires_in_seconds": credentials.get("token_expires_in"),
        },
        "entitlements": {
            "enabled": entitlements.get("enabled") is True,
            "ok": entitlement_ok,
            "failure": entitlement_ok is False or entitlement_error_present,
            "error_present": entitlement_error_present,
            "freshness_seconds": entitlement_freshness,
            "handoff_failures": max(0, int(summary.get("failed", 0))),
        },
    }


class OperationalHealthReporter:
    def __init__(
        self,
        emitter,
        *,
        snapshot_fn: Callable[[], dict] = build_snapshot,
        interval: float = 60.0,
    ) -> None:
        self._emitter = emitter
        self._snapshot = snapshot_fn
        self.interval = min(3600.0, max(10.0, float(interval)))

    def emit_once(self) -> bool:
        if not self._emitter.enabled:
            return False
        try:
            self._emitter.emit(
                "operational.health",
                "system",
                self._snapshot(),
            )
        except Exception:  # noqa: BLE001 - telemetry never blocks app paths
            return False
        return True

    def run(self, stop: threading.Event) -> None:
        while not stop.wait(self.interval):
            self.emit_once()


metrics = ProcessMetrics()


__all__ = [
    "OperationalHealthReporter",
    "ProcessMetrics",
    "build_snapshot",
    "linux_rss_bytes",
    "metrics",
]
