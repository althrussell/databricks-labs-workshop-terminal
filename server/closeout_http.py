"""Track accepted HTTP producers through their entire ASGI response lifetime."""

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass

import anyio
from starlette.responses import JSONResponse

from .capture_journal import CaptureError
from .closeout_guard import CaptureClosedError, admitted_writer
from .workshop_lifecycle import CLOSING_DETAIL, UNAVAILABLE_DETAIL


@dataclass
class _RequestOutcome:
    # The mutable holder follows the admitted request into a sync route's
    # worker thread. Setting a fresh ContextVar there would not flow back to
    # the middleware's async context.
    retained_model_policy_refusal: bool = False


_request_outcome: ContextVar[_RequestOutcome | None] = ContextVar(
    "capture_http_request_outcome", default=None
)


def retained_model_policy_pending_refusal() -> None:
    """The launch gate retained its diagnostic before accepting session input.

    Only the exact early model-policy gate may use this internal marker. It is
    not an HTTP header or a general exemption for 503/capture failures.
    """
    outcome = _request_outcome.get()
    if outcome is not None:
        outcome.retained_model_policy_refusal = True


def produces_capture(scope) -> bool:
    path = scope.get("path", "")
    if scope["type"] != "http" or not path.startswith("/api/"):
        return False
    if path.startswith("/api/admin/capture-closeout/") or path in {
        "/api/admin/insight-events/ack", "/api/admin/help/ack",
    }:
        return False
    return scope.get("method") in {"POST", "PUT", "PATCH", "DELETE"} or path == "/api/certificate"


class CloseoutAdmissionMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if not produces_capture(scope):
            await self.app(scope, receive, send)
            return
        from .event_emitter import event_emitter

        def reserve():
            event_emitter._ensure_ready()
            return admitted_writer(event_emitter.journal, "http:" + scope["method"] + ":" + scope["path"][:80])

        try:
            ticket = await anyio.to_thread.run_sync(reserve)
        except CaptureClosedError:
            await JSONResponse({"detail": CLOSING_DETAIL}, status_code=409)(scope, receive, send)
            return
        except CaptureError:
            await JSONResponse({"detail": UNAVAILABLE_DETAIL}, status_code=503)(scope, receive, send)
            return
        failed = False
        outcome = _RequestOutcome()
        outcome_token = _request_outcome.set(outcome)

        async def response(message):
            nonlocal failed
            if message["type"] == "http.response.start" and message["status"] >= 500:
                known_refusal = (
                    scope.get("method") == "POST"
                    and scope.get("path") == "/api/sessions"
                    and message["status"] == 503
                    and outcome.retained_model_policy_refusal
                )
                if not known_refusal:
                    failed = True
            await send(message)

        ticket.__enter__()
        try:
            await self.app(scope, receive, response)
        except BaseException:
            failed = True
            raise
        finally:
            try:
                await anyio.to_thread.run_sync(lambda: ticket.finish(failed=failed))
            finally:
                try:
                    ticket.__exit__(None, None, None)
                finally:
                    _request_outcome.reset(outcome_token)
