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
import time

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from .auth import get_ws_user
from .events import event_hub
from .sessions import session_manager
from .users import user_manager

logger = logging.getLogger(__name__)

router = APIRouter()


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

    user = user_manager.get(principal.name)
    user.last_seen = time.time()

    queue: asyncio.Queue = asyncio.Queue()
    with session.lock:
        session.subscribers.add(queue)

    try:
        replay = session.replay_text()
        if replay:
            await websocket.send_text(json.dumps({"t": "replay", "data": replay}))
        if session.exited:
            await websocket.send_text(json.dumps({"t": "exit"}))

        async def pump_output():
            while True:
                message = await queue.get()
                await websocket.send_text(json.dumps(message))

        async def pump_input():
            while True:
                raw = await websocket.receive_text()
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                t = msg.get("t")
                if t == "input":
                    session.write_input(msg.get("data", ""))
                    user.last_seen = time.time()
                elif t == "resize":
                    session.resize(int(msg.get("cols", 80)), int(msg.get("rows", 24)))
                elif t == "ping":
                    session.touch()
                    user.last_seen = time.time()
                    await websocket.send_text(json.dumps({"t": "pong"}))

        output_task = asyncio.create_task(pump_output())
        input_task = asyncio.create_task(pump_input())
        done, pending = await asyncio.wait(
            {output_task, input_task}, return_when=asyncio.FIRST_COMPLETED
        )
        for task in pending:
            task.cancel()
        for task in done:
            exc = task.exception()
            if exc and not isinstance(exc, (WebSocketDisconnect, asyncio.CancelledError)):
                raise exc
    except WebSocketDisconnect:
        pass  # PTY keeps running — the client may reattach
    except OSError as e:
        logger.warning("session socket %s I/O error: %s", session_id[:8], e)
    finally:
        with session.lock:
            session.subscribers.discard(queue)


@router.websocket("/ws/events")
async def events_socket(websocket: WebSocket):
    await websocket.accept()  # accept first so a 4403 close code reaches the client
    principal = await get_ws_user(websocket)
    if principal is None:
        return  # get_ws_user already closed 4403
    user = user_manager.get(principal.name)
    if not user.first_seen:
        user.first_seen = time.time()
    user.last_seen = time.time()

    queue = event_hub.subscribe()

    async def pump_events():
        while True:
            message = await queue.get()
            await websocket.send_text(json.dumps(message))

    async def pump_pings():
        while True:
            raw = await websocket.receive_text()
            user.last_seen = time.time()
            if raw == '{"t":"ping"}':
                await websocket.send_text(json.dumps({"t": "pong"}))

    events_task = asyncio.create_task(pump_events())
    pings_task = asyncio.create_task(pump_pings())
    try:
        await asyncio.wait({events_task, pings_task}, return_when=asyncio.FIRST_COMPLETED)
    except WebSocketDisconnect:
        pass
    finally:
        for task in (events_task, pings_task):
            task.cancel()
        event_hub.unsubscribe(queue)
