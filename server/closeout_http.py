"""Track accepted HTTP producers through their entire ASGI response lifetime."""

from __future__ import annotations

import anyio
from starlette.responses import JSONResponse

from .capture_journal import CaptureError
from .closeout_guard import CaptureClosedError, admitted_writer


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
            await JSONResponse({"detail": "Workshop is closing; new input is disabled."}, status_code=409)(scope, receive, send)
            return
        except CaptureError:
            await JSONResponse({"detail": "Workshop capture is held; retry when persistence is available."}, status_code=503)(scope, receive, send)
            return
        failed = False

        async def response(message):
            nonlocal failed
            if message["type"] == "http.response.start" and message["status"] >= 500:
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
                ticket.__exit__(None, None, None)
