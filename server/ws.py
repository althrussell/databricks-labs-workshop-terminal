"""WebSocket endpoints.

/ws/sessions/{id} — one socket per terminal:
  client -> server: {"t":"input","data":...} {"t":"resize","cols":..,"rows":..} {"t":"ping"}
  server -> client: {"t":"replay","data":...} (once, on attach)
                    {"t":"output","data":...} {"t":"exit"} {"t":"pong"}

/ws/events — one socket per browser tab: phase changes, broadcasts, nugget
refresh hints. Heartbeats on either socket feed presence and the idle reaper.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from contextvars import copy_context
from contextlib import suppress

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from .auth import get_ws_user
from .capture_journal import CaptureError
from .closeout_guard import CaptureClosedError, admitted_writer
from .events import event_hub
from .operational import metrics as operational_metrics
from .sessions import session_manager
from .users import user_manager

logger = logging.getLogger(__name__)

router = APIRouter()

# Each socket awaits one local operation at a time. Daemon workers keep remote
# journal I/O off the event loop without extending the process shutdown budget.
# The registry owns work after browser cancellation and is drained before PTYs
# and their lifetime tickets on ordinary shutdown.
_capture_workers: set = set()
_capture_workers_lock = threading.Lock()
_capture_accepting = True


class _CaptureWork:
    def __init__(self):
        self.done = threading.Event()
        self.cancelled = False
        self.started = False


def start_capture_operations() -> None:
    global _capture_accepting
    with _capture_workers_lock:
        if _capture_workers:
            raise RuntimeError("prior websocket capture work is still running")
        _capture_accepting = True


def freeze_capture_operations() -> None:
    global _capture_accepting
    with _capture_workers_lock:
        _capture_accepting = False


def drain_capture_operations(*, deadline: float) -> dict:
    freeze_capture_operations()
    with _capture_workers_lock:
        workers = list(_capture_workers)
    for worker in workers:
        worker.done.wait(max(0.0, deadline - time.monotonic()))
    with _capture_workers_lock:
        pending = len(_capture_workers)
    return {"complete": pending == 0,
            "issues": ["websocket_capture_pending"] if pending else []}


async def _capture_operation(operation):
    loop = asyncio.get_running_loop()
    result = loop.create_future()
    # Consume a late exception even when its socket has already disconnected.
    result.add_done_callback(lambda future: None if future.cancelled() else future.exception())
    work = _CaptureWork()
    context = copy_context()

    def run():
        value = error = None
        try:
            with _capture_workers_lock:
                if work.cancelled:
                    raise asyncio.CancelledError()
                work.started = True
            # Admission, the actual input and completion share this thread and
            # context. Caller cancellation never becomes its producer outcome.
            value = context.run(operation)
        except BaseException as exc:
            error = exc
        finally:
            with _capture_workers_lock:
                _capture_workers.discard(work)
                work.done.set()

        def deliver():
            if not result.done():
                if error is None:
                    result.set_result(value)
                else:
                    result.set_exception(error)

        try:
            loop.call_soon_threadsafe(deliver)
        except RuntimeError:
            pass  # Loop stopped; the worker's durable result is still retained.

    with _capture_workers_lock:
        if not _capture_accepting:
            raise CaptureClosedError("Workshop is stopping; new capture input is disabled")
        _capture_workers.add(work)
        try:
            threading.Thread(target=run, name="websocket-capture", daemon=True).start()
        except BaseException:
            _capture_workers.discard(work)
            raise
    try:
        return await asyncio.shield(result)
    except asyncio.CancelledError:
        with _capture_workers_lock:
            if not work.started:
                work.cancelled = True
        raise


def _check_input_open() -> None:
    """In-memory closeout fence for attendee input (no storage, no copy of state)."""
    from .closeout_guard import check_open
    from .event_emitter import event_emitter

    with _capture_workers_lock:
        accepting = _capture_accepting
    if not accepting:
        raise CaptureClosedError("Workshop is stopping; new capture input is disabled")
    check_open(event_emitter.journal)


def _writer(kind: str):
    from .event_emitter import event_emitter

    event_emitter._ensure_ready()
    return admitted_writer(event_emitter.journal, kind)


async def pump_terminal_output(websocket: WebSocket, queue, session_id: str) -> None:
    """Drain one terminal subscriber, closing slow consumers for replay."""
    while True:
        message = await queue.get()
        if message.get("t") == "overflow":
            logger.warning(
                "session socket %s slow consumer overflow; reconnecting",
                session_id[:8],
            )
            await websocket.close(code=4408, reason="terminal consumer overflow")
            return
        await websocket.send_text(json.dumps(message))
        if message.get("t") == "exit":
            await websocket.close(code=1000)
            return


@router.websocket("/ws/sessions/{session_id}")
async def session_socket(websocket: WebSocket, session_id: str):
    # Accept BEFORE validating so the 4403/4404 close code actually reaches the
    # browser. A pre-accept close surfaces as a generic 1006, which the client
    # can't distinguish from a network blip — that's what turned a vanished
    # session (restart/reap) or an auth failure into an endless reconnect storm.
    await websocket.accept()
    principal = await get_ws_user(websocket)
    if principal is None:
        return  # get_ws_user already closed 4403
    session = session_manager.get(session_id, principal.name)
    if session is None:
        await websocket.close(code=4404)
        return

    def attach():
        with _writer("terminal_socket_attach"):
            user = user_manager.get(principal.name)
            user.last_seen = time.time()
            user_manager.checkpoint(user)
        return user

    try:
        user = await _capture_operation(attach)
    except CaptureClosedError:
        await websocket.close(code=4410, reason="workshop is closing")
        return
    except CaptureError:
        await websocket.close(code=4413, reason="workshop capture is held")
        return

    replay, queue, exited = session_manager.attach_with_replay(session)
    operational_metrics.websocket_attached()
    tasks: set[asyncio.Task] = set()

    try:
        if replay:
            await websocket.send_text(json.dumps({"t": "replay", "data": replay}))
        if exited:
            await websocket.send_text(json.dumps(session.exit_message()))
            await websocket.close(code=1000)
            return

        async def pump_output():
            await pump_terminal_output(websocket, queue, session_id)

        async def pump_input():
            while True:
                raw = await websocket.receive_text()
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if not isinstance(msg, dict):
                    continue
                t = msg.get("t")
                if t not in {"input", "resize", "ping"}:
                    continue

                # Typing is never a capture producer: no retained writer, no
                # worker thread, no storage. Only the in-memory closeout fence.
                _check_input_open()
                if t == "input":
                    session.write_keystrokes(msg.get("data", ""))
                    user.last_seen = time.time()
                elif t == "resize":
                    session.resize(int(msg.get("cols", 80)), int(msg.get("rows", 24)))
                else:
                    session.touch()
                    user.last_seen = time.time()
                if t == "ping":
                    # Pong is transport delivery, not a captured producer. A
                    # closed/slow browser cannot invalidate completed local work.
                    await websocket.send_text(json.dumps({"t": "pong"}))

        output_task = asyncio.create_task(pump_output())
        input_task = asyncio.create_task(pump_input())
        tasks = {output_task, input_task}
        done, pending = await asyncio.wait(
            tasks, return_when=asyncio.FIRST_COMPLETED
        )
        for task in pending:
            task.cancel()
        for task in done:
            if task.cancelled():
                continue
            exc = task.exception()
            if exc and not isinstance(exc, (WebSocketDisconnect, asyncio.CancelledError)):
                raise exc
    except WebSocketDisconnect:
        pass  # PTY keeps running — the client may reattach
    except CaptureClosedError:
        await websocket.close(code=4410, reason="workshop is closing")
    except OSError as e:
        logger.warning("session socket %s I/O error: %s", session_id[:8], e)
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            with suppress(asyncio.CancelledError):
                await asyncio.gather(*tasks, return_exceptions=True)
        session_manager.unsubscribe(session, queue)
        operational_metrics.websocket_detached()


@router.websocket("/ws/events")
async def events_socket(websocket: WebSocket):
    await websocket.accept()  # accept first so a 4403 close code reaches the client
    principal = await get_ws_user(websocket)
    if principal is None:
        return  # get_ws_user already closed 4403
    def attach():
        with _writer("events_socket_attach"):
            user = user_manager.get(principal.name)
            if not user.first_seen:
                user.first_seen = time.time()
            user.last_seen = time.time()
            user_manager.checkpoint(user)
        return user

    # Subscribe before journal I/O yields: a broadcast sent immediately after
    # the accepted handshake must wait in this bounded queue, not disappear.
    # Output still starts only after capture admission/checkpoint succeeds.
    queue = event_hub.subscribe()
    try:
        user = await _capture_operation(attach)
    except CaptureClosedError:
        event_hub.unsubscribe(queue)
        await websocket.close(code=4410, reason="workshop is closing")
        return
    except CaptureError:
        event_hub.unsubscribe(queue)
        await websocket.close(code=4413, reason="workshop capture is held")
        return
    except BaseException:
        event_hub.unsubscribe(queue)
        raise

    operational_metrics.websocket_attached()

    async def pump_events():
        while True:
            message = await queue.get()
            await websocket.send_text(json.dumps(message))

    async def pump_pings():
        while True:
            raw = await websocket.receive_text()

            # Presence only: no retained writer for a browser ping.
            _check_input_open()
            user.last_seen = time.time()
            if raw == '{"t":"ping"}':
                await websocket.send_text(json.dumps({"t": "pong"}))

    events_task = asyncio.create_task(pump_events())
    pings_task = asyncio.create_task(pump_pings())
    try:
        done, _pending = await asyncio.wait({events_task, pings_task}, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            if task.cancelled():
                continue
            error = task.exception()
            if error and not isinstance(error, (WebSocketDisconnect, asyncio.CancelledError)):
                raise error
    except WebSocketDisconnect:
        pass
    except CaptureClosedError:
        await websocket.close(code=4410, reason="workshop is closing")
    finally:
        for task in (events_task, pings_task):
            task.cancel()
        with suppress(asyncio.CancelledError):
            await asyncio.gather(events_task, pings_task, return_exceptions=True)
        event_hub.unsubscribe(queue)
        operational_metrics.websocket_detached()
