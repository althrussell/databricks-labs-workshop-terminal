"""Live-stream (SSE) and WebSocket network checker for attendee browsers.

Corporate proxies that buffer small ``text/event-stream`` chunks prevent
Omnigent's new Claude chat flow from receiving its session-ready event,
leaving the attendee staring at "The model did not finish starting" after 180s
while the server-side runner became interactive in 3s (EQL seat 22, 27 Oct 2026).

This module serves an SSE stream with the exact headers Omnigent uses, a tiny
HTTP ping endpoint, and a result recorder that persists per-attendee verdicts
and emits ``network.check`` to OTel and Control Tower.
"""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import logging
import os
import tempfile
import threading
import time
from typing import Any, Literal

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from . import config, telemetry
from .auth import Principal, get_current_user

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/network-check", tags=["network-check"])

SCHEDULE_SECONDS: tuple[float, ...] = (0.0, 1.0, 2.0, 3.0, 5.0, 8.0, 12.0, 17.0, 23.0, 30.0)
SMALL_EVENT_PAD = "x" * 64
FLUSH_EVENT_BYTES = 32 * 1024

_lock = threading.Lock()
_results: dict[str, dict[str, Any]] = {}
_loaded = False


def _store_path() -> str:
    return os.path.join(config.data_root(), "network_checks.json")


def _load_locked() -> None:
    global _loaded
    if _loaded:
        return
    _loaded = True
    path = _store_path()
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
        if isinstance(data, dict):
            for key, value in data.items():
                if isinstance(key, str) and isinstance(value, dict):
                    _results[key.strip().lower()] = value
    except (OSError, ValueError):
        pass


def _save_locked() -> None:
    path = _store_path()
    parent = os.path.dirname(path)
    try:
        os.makedirs(parent, exist_ok=True)
        fd, temp = tempfile.mkstemp(prefix=".network-checks-", suffix=".json", dir=parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(_results, handle, sort_keys=True)
                handle.write("\n")
            os.replace(temp, path)
        finally:
            try:
                os.unlink(temp)
            except FileNotFoundError:
                pass
    except OSError:
        logger.debug("network check persistence unavailable", exc_info=True)


def _client_ip(headers) -> str:
    forwarded = (headers.get("x-forwarded-for") or "").split(",")[0].strip()
    candidate = forwarded or (headers.get("x-real-ip") or "").strip()
    if not candidate:
        return ""
    try:
        addr = ipaddress.ip_address(candidate)
    except ValueError:
        return ""
    if addr.is_loopback or addr.is_unspecified:
        return ""
    return str(addr)


def network_label_for(headers) -> str:
    """Short non-reversible hash grouping seats behind the same egress IP."""
    ip = _client_ip(headers)
    if not ip:
        return ""
    run_id = os.environ.get("WORKSHOP_RUN_ID", "").strip()
    digest = hashlib.sha256(f"wt-net-v1:{run_id}:{ip}".encode("utf-8")).hexdigest()
    return digest[:12]


class NetworkCheckResultIn(BaseModel):
    verdict: Literal["ok", "slow", "blocked"]
    first_event_ms: int | None = Field(default=None, ge=0, le=600_000)
    max_lag_ms: int | None = Field(default=None, ge=0, le=600_000)
    received_count: int = Field(default=0, ge=0, le=100)
    expected_count: int = Field(default=11, ge=1, le=100)
    clumped: bool = False
    ws_rtt_ms: int | None = Field(default=None, ge=0, le=600_000)
    http_rtt_ms: int | None = Field(default=None, ge=0, le=600_000)
    browser_family: str = Field(default="", max_length=64)


def classify_verdict(
    *,
    first_event_ms: int | None,
    max_lag_ms: int | None,
    received_count: int,
    expected_count: int,
    clumped: bool,
    max_gap_ms: int | None = None,
) -> Literal["ok", "slow", "blocked"]:
    """Server-side reference verdict maths matching frontend/src/networkCheck.ts.

    Grades the live-update stream only; the WebSocket RTT is reported alongside
    and never downgrades a clean stream.
    """
    if (
        received_count == 0
        or clumped
        or (first_event_ms is not None and first_event_ms > 15_000)
        or (max_lag_ms is not None and max_lag_ms >= 15_000)
        or (max_gap_ms is not None and max_gap_ms > 45_000)
    ):
        return "blocked"
    if (
        received_count < expected_count
        or (max_lag_ms is not None and max_lag_ms >= 3_000)
    ):
        return "slow"
    return "ok"


def is_outage_shaped(
    *, received_count: int, http_rtt_ms: int | None, ws_rtt_ms: int | None
) -> bool:
    """Nothing came back at all, matching ``isOutageShaped`` in networkCheck.ts.

    Still a ``blocked`` verdict, but a dropped connection rather than a proxy
    holding back small events, so it must not be met with proxy advice.
    """
    return received_count == 0 and http_rtt_ms is None and ws_rtt_ms is None


@router.get("/ping")
def ping(_: Principal = Depends(get_current_user)) -> dict[str, Any]:
    return {"ok": True, "server_time_ms": int(time.time() * 1000)}


@router.get("/stream")
async def stream(
    fast: bool = Query(default=False),
    _: Principal = Depends(get_current_user),
) -> StreamingResponse:
    schedule = tuple(t * 0.01 for t in SCHEDULE_SECONDS) if fast else SCHEDULE_SECONDS

    async def generate():
        started = time.monotonic()
        for seq, target_s in enumerate(schedule):
            delay = target_s - (time.monotonic() - started)
            if delay > 0:
                await asyncio.sleep(delay)
            elapsed_ms = int(round((time.monotonic() - started) * 1000))
            payload = json.dumps(
                {
                    "seq": seq,
                    "server_elapsed_ms": elapsed_ms,
                    "flush": False,
                    "pad": SMALL_EVENT_PAD,
                },
                separators=(",", ":"),
            )
            yield f"data: {payload}\n\n"
        elapsed_ms = int(round((time.monotonic() - started) * 1000))
        flush_payload = json.dumps(
            {
                "seq": len(schedule),
                "server_elapsed_ms": elapsed_ms,
                "flush": True,
                "pad": "f" * FLUSH_EVENT_BYTES,
            },
            separators=(",", ":"),
        )
        yield f"data: {flush_payload}\n\n"

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/result")
def record_result(
    body: NetworkCheckResultIn,
    request: Request,
    principal: Principal = Depends(get_current_user),
) -> dict[str, Any]:
    email = principal.name.strip().lower()
    checked_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    label = network_label_for(request.headers)
    record: dict[str, Any] = {
        "verdict": body.verdict,
        "checked_at": checked_at,
        "first_event_ms": body.first_event_ms,
        "max_lag_ms": body.max_lag_ms,
        "received_count": body.received_count,
        "expected_count": body.expected_count,
        "clumped": body.clumped,
        "ws_rtt_ms": body.ws_rtt_ms,
        "http_rtt_ms": body.http_rtt_ms,
        "browser_family": body.browser_family.strip()[:64],
        "network_label": label,
        "outage": is_outage_shaped(
            received_count=body.received_count,
            http_rtt_ms=body.http_rtt_ms,
            ws_rtt_ms=body.ws_rtt_ms,
        ),
    }
    with _lock:
        _load_locked()
        _results[email] = record
        _save_locked()
    telemetry.emit("network.check", email, record)
    return record


def get_for(email: str) -> dict[str, Any] | None:
    key = (email or "").strip().lower()
    if not key:
        return None
    with _lock:
        _load_locked()
        value = _results.get(key)
        return dict(value) if isinstance(value, dict) else None


def reset_for_tests() -> None:
    global _loaded
    with _lock:
        _results.clear()
        _loaded = False
