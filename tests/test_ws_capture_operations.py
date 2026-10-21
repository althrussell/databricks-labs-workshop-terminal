"""Transport cancellation cannot invalidate completed capture producers."""

import asyncio
import json
import os
import threading
import time
from types import SimpleNamespace

import pytest
from starlette.websockets import WebSocketDisconnect


@pytest.fixture
def socket_capture(monkeypatch):
    from server import event_emitter, ws
    from server.capture_journal import CaptureJournal, MemoryCaptureStorage
    from server.closeout_guard import admitted_writer
    from server.event_emitter import EventEmitter
    from server.sessions import Session

    ws.start_capture_operations()
    binding = {"run_id": "socket-run", "unit_id": "socket-unit", "workspace_id": "123",
               "owner": "alice@example.com"}
    journal = CaptureJournal(MemoryCaptureStorage(), binding, {})
    source = EventEmitter(run_id=binding["run_id"], workspace_id=binding["workspace_id"],
                          ingest_url="", ingest_token="", journal=journal)
    monkeypatch.setattr(event_emitter, "event_emitter", source)
    user = SimpleNamespace(last_seen=0, first_seen=0)
    monkeypatch.setattr(ws, "user_manager", SimpleNamespace(get=lambda _: user, checkpoint=lambda _: None))
    session = Session(binding["owner"], "claude", "Claude", -12345, 999999)
    monkeypatch.setattr(session, "resize", lambda *_: None)

    async def authenticate(_):
        return SimpleNamespace(name=binding["owner"])

    monkeypatch.setattr(ws, "get_ws_user", authenticate)
    queue = asyncio.Queue()
    monkeypatch.setattr(ws, "session_manager", SimpleNamespace(
        get=lambda *_: session, attach_with_replay=lambda _: ("", queue, False),
        unsubscribe=lambda *_: None))
    monkeypatch.setattr(ws, "event_hub", SimpleNamespace(subscribe=lambda: queue, unsubscribe=lambda _: None))
    # The fix must not complete or replace either existing lifetime reservation.
    lifetimes = [admitted_writer(journal, kind, detached=True)
                 for kind in ("terminal_lifetime", "paired_host_lifetime")]
    before = journal.read()["closeout"]["writers"]
    fixture = SimpleNamespace(ws=ws, journal=journal, session=session, user=user, before=before)
    yield fixture
    assert ws.drain_capture_operations(deadline=time.monotonic() + 3)["complete"]
    for ticket in lifetimes:
        ticket.finish()
    ws.start_capture_operations()


class Socket:
    def __init__(self, messages=(), *, pong_error=None, block_pong=False):
        self.messages = list(messages)
        self.pong_error = pong_error
        self.block_pong = block_pong
        self.pong_started = asyncio.Event()
        self.closed = []

    async def accept(self):
        pass

    async def receive_text(self):
        if self.messages:
            return self.messages.pop(0)
        raise WebSocketDisconnect(1000)

    async def send_text(self, data):
        assert json.loads(data) == {"t": "pong"}
        self.pong_started.set()
        if self.pong_error:
            raise self.pong_error
        if self.block_pong:
            await asyncio.Event().wait()

    async def close(self, **kwargs):
        self.closed.append(kwargs)


@pytest.mark.parametrize("surface", ["terminal", "events"])
def test_disconnected_pong_retains_success_and_original_lifetimes(socket_capture, surface):
    f = socket_capture

    async def scenario():
        socket = Socket(['{"t":"ping"}'], pong_error=WebSocketDisconnect(1000))
        if surface == "terminal":
            await f.ws.session_socket(socket, "owned-session")
        else:
            await f.ws.events_socket(socket)
        assert socket.pong_started.is_set()

    asyncio.run(scenario())
    state = f.journal.read()["closeout"]
    assert state["writer_outcomes"] == []
    assert state["writers"] == f.before


@pytest.mark.parametrize("surface", ["terminal", "events"])
def test_cancelled_pong_is_not_a_failed_producer(socket_capture, surface):
    f = socket_capture

    async def scenario():
        socket = Socket(['{"t":"ping"}'], block_pong=True)
        handler = f.ws.session_socket(socket, "owned-session") if surface == "terminal" else f.ws.events_socket(socket)
        task = asyncio.create_task(handler)
        await asyncio.wait_for(socket.pong_started.wait(), timeout=2)
        # The local writer must already have finished before network delivery.
        assert f.journal.read()["closeout"]["writers"] == f.before
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    assert f.journal.read()["closeout"]["writer_outcomes"] == []
    assert f.journal.read()["closeout"]["writers"] == f.before


@pytest.mark.parametrize("partial", [False, True])
def test_actual_pty_failure_stays_held(socket_capture, monkeypatch, partial):
    f = socket_capture
    original = os.write
    attempts = []

    def write(fd, data):
        if fd != -12345:
            return original(fd, data)
        attempts.append(bytes(data))
        if partial and len(attempts) == 1:
            return 2
        raise OSError("PTY write failed")

    monkeypatch.setattr("server.sessions.os.write", write)
    asyncio.run(f.ws.session_socket(Socket([json.dumps({"t": "input", "data": "hello"})]), "owned-session"))
    state = f.journal.read()["closeout"]
    assert state["writers"] == f.before
    outcome, = state["writer_outcomes"]
    assert outcome["kind"] == "terminal_socket_message" and outcome["status"] == "failed"
    assert attempts == ([b"hello", b"llo"] if partial else [b"hello"])


def test_input_order_is_preserved_and_blocked_storage_does_not_block_loop(socket_capture, monkeypatch):
    f = socket_capture
    entered, release = threading.Event(), threading.Event()
    loop_thread = threading.get_ident()
    original_create = f.journal.storage.create
    writes = []

    def slow_create(name, content):
        assert threading.get_ident() != loop_thread
        if not entered.is_set():
            entered.set()
            assert release.wait(2)
        return original_create(name, content)

    monkeypatch.setattr(f.journal.storage, "create", slow_create)
    monkeypatch.setattr(f.session, "_write_input", lambda data: writes.append(data))

    async def scenario():
        task = asyncio.create_task(f.ws.session_socket(Socket([
            json.dumps({"t": "input", "data": "first"}),
            json.dumps({"t": "input", "data": "second"}),
        ]), "owned-session"))
        try:
            for _ in range(200):
                if entered.is_set():
                    break
                await asyncio.sleep(0.005)
            assert entered.is_set() and not release.is_set()
            # Another asynchronous request can run while the journal is blocked.
            assert await asyncio.wait_for(asyncio.sleep(0, result="responsive"), 0.2) == "responsive"
        finally:
            release.set()
        await asyncio.wait_for(task, 3)

    asyncio.run(scenario())
    monkeypatch.setattr(f.journal.storage, "create", original_create)
    assert writes == ["first", "second"]
    assert f.journal.read()["closeout"]["writer_outcomes"] == []


def test_cancelled_admitted_input_is_owned_until_success(socket_capture, monkeypatch):
    f = socket_capture
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    writes = []

    def write(data):
        entered.set()
        assert release.wait(3)
        writes.append(data)
        finished.set()

    monkeypatch.setattr(f.session, "_write_input", write)

    async def scenario():
        task = asyncio.create_task(f.ws.session_socket(Socket([json.dumps({"t": "input", "data": "once"})]), "owned-session"))
        try:
            for _ in range(200):
                if entered.is_set():
                    break
                await asyncio.sleep(0.005)
            assert entered.is_set()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            state = f.journal.read()["closeout"]
            assert len(state["writers"]) == len(f.before) + 1
            assert state["writer_outcomes"] == []
            assert len(f.ws._capture_workers) == 1
        finally:
            release.set()
        for _ in range(200):
            if finished.is_set() and not f.ws._capture_workers:
                break
            await asyncio.sleep(0.005)
        assert not f.ws._capture_workers

    asyncio.run(scenario())
    assert writes == ["once"]
    assert f.journal.read()["closeout"]["writers"] == f.before
    assert f.journal.read()["closeout"]["writer_outcomes"] == []


def test_cancelled_before_worker_start_never_admits(socket_capture, monkeypatch):
    f = socket_capture
    queued, calls = [], []

    class Thread:
        def __init__(self, *, target, name, daemon):
            assert daemon is True and name == "websocket-capture"
            queued.append(target)

        def start(self):
            pass

    monkeypatch.setattr(f.ws.threading, "Thread", Thread)

    async def scenario():
        task = asyncio.create_task(f.ws._capture_operation(lambda: calls.append("admitted")))
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        queued.pop()()
        await asyncio.sleep(0)

    asyncio.run(scenario())
    assert calls == [] and not f.ws._capture_workers
    assert f.journal.read()["closeout"]["writers"] == f.before


def test_shutdown_is_bounded_and_freezes_new_admission(socket_capture, monkeypatch):
    from server import main

    f = socket_capture
    entered, release = threading.Event(), threading.Event()
    written = []

    def write(data):
        assert threading.current_thread().daemon
        entered.set()
        assert release.wait(3)
        written.append(data)

    monkeypatch.setattr(f.session, "_write_input", write)

    async def scenario():
        task = asyncio.create_task(f.ws.session_socket(Socket([json.dumps({"t": "input", "data": "accepted"})]), "owned-session"))
        try:
            for _ in range(200):
                if entered.is_set():
                    break
                await asyncio.sleep(0.005)
            assert entered.is_set()
            # No default asyncio executor exists to keep process exit waiting.
            assert asyncio.get_running_loop()._default_executor is None
            start = time.monotonic()
            outcome = main._shutdown_runtime(None, [], timeout=0.05)
            assert time.monotonic() - start < 0.5
            assert not outcome["complete"]
            assert {"websocket_capture_pending", "shutdown_deadline_exceeded"} & set(outcome["issues"])
            with pytest.raises(f.ws.CaptureClosedError):
                await f.ws._capture_operation(lambda: pytest.fail("late admission"))
            state = f.journal.read()["closeout"]
            assert len(state["writers"]) == len(f.before) + 1
            assert state["writer_outcomes"] == []
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        finally:
            release.set()
        for _ in range(200):
            if not f.ws._capture_workers:
                break
            await asyncio.sleep(0.005)

    asyncio.run(scenario())
    assert written == ["accepted"]
    assert f.journal.read()["closeout"]["writer_outcomes"] == []



def test_cancelled_admitted_input_retains_its_late_failure(socket_capture, monkeypatch):
    f = socket_capture
    entered, release = threading.Event(), threading.Event()
    unhandled = []

    def write(_data):
        entered.set()
        assert release.wait(3)
        raise OSError("accepted PTY write failed after browser cancellation")

    monkeypatch.setattr(f.session, "_write_input", write)

    async def scenario():
        asyncio.get_running_loop().set_exception_handler(lambda _loop, context: unhandled.append(context))
        task = asyncio.create_task(f.ws.session_socket(Socket([json.dumps({"t": "input", "data": "held"})]), "owned-session"))
        try:
            for _ in range(200):
                if entered.is_set():
                    break
                await asyncio.sleep(0.005)
            assert entered.is_set()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert len(f.journal.read()["closeout"]["writers"]) == len(f.before) + 1
        finally:
            release.set()
        for _ in range(200):
            if not f.ws._capture_workers:
                break
            await asyncio.sleep(0.005)
        await asyncio.sleep(0)
        assert not f.ws._capture_workers

    asyncio.run(scenario())
    state = f.journal.read()["closeout"]
    assert state["writers"] == f.before
    outcome, = state["writer_outcomes"]
    assert outcome["kind"] == "terminal_socket_message" and outcome["status"] == "failed"
    assert unhandled == []


def test_shutdown_finishes_accepted_input_before_terminal_drain(socket_capture, monkeypatch):
    from server import insight_summary, main

    f = socket_capture
    entered, release = threading.Event(), threading.Event()
    order = []

    def write(_data):
        entered.set()
        assert release.wait(3)
        order.append("input_completed")

    def terminal_drain(**_kwargs):
        assert order == ["input_completed"]
        assert not f.ws._capture_workers
        order.append("terminal_drain")
        return {"complete": True, "issues": []}

    monkeypatch.setattr(f.session, "_write_input", write)
    monkeypatch.setattr(main, "session_manager", SimpleNamespace(drain_for_closeout=terminal_drain))
    for service in (main.credential_manager, main.entitlement_manager,
                    main.log_collector, main.obo.obo_watcher):
        monkeypatch.setattr(service, "stop", lambda: None)
    monkeypatch.setattr(main.remote_host_manager, "stop", lambda **_kwargs: {"complete": True, "issues": []})
    monkeypatch.setattr(main.identity, "drain_for_closeout", lambda **_kwargs: {"complete": True, "issues": []})
    monkeypatch.setattr(insight_summary, "drain_for_closeout", lambda *_args: {"complete": True, "issues": []})
    monkeypatch.setattr(main.user_content, "drain_sync_for_closeout", lambda *_args, **_kwargs: {"complete": True, "issues": []})
    monkeypatch.setattr(main, "_checkpoint_shutdown_users", lambda: {"complete": True, "issues": []})

    def release_after_freeze():
        for _ in range(200):
            if not f.ws._capture_accepting:
                release.set()
                return
            time.sleep(0.005)
        release.set()

    async def scenario():
        task = asyncio.create_task(f.ws.session_socket(Socket([json.dumps({"t": "input", "data": "accepted"})]), "owned-session"))
        releaser = None
        try:
            for _ in range(200):
                if entered.is_set():
                    break
                await asyncio.sleep(0.005)
            assert entered.is_set()
            releaser = threading.Thread(target=release_after_freeze, daemon=True)
            releaser.start()
            outcome = main._shutdown_runtime(None, [], timeout=2)
            assert outcome["complete"], outcome
            assert outcome["outcomes"][0] == {"kind": "websocket_capture", "complete": True, "issues": []}
            await asyncio.wait_for(task, 1)
        finally:
            release.set()
            if releaser:
                releaser.join(1)

    asyncio.run(scenario())
    assert order == ["input_completed", "terminal_drain"]
    state = f.journal.read()["closeout"]
    assert state["writers"] == f.before and state["writer_outcomes"] == []


def test_closeout_barrier_still_rejects_actual_input(socket_capture, monkeypatch):
    from server.closeout_guard import control_transaction

    f = socket_capture
    monkeypatch.setattr(f.session, "_write_input", lambda _: pytest.fail("input crossed closeout"))
    control_transaction(f.journal, lambda state: state["closeout"].update(phase="CLOSING"))
    socket = Socket([json.dumps({"t": "input", "data": "late"})])
    asyncio.run(f.ws.session_socket(socket, "owned-session"))
    assert socket.closed == [{"code": 4410, "reason": "workshop is closing"}]
    assert f.journal.read()["closeout"]["writer_outcomes"] == []


@pytest.mark.parametrize("outcome", ["success", "held", "cancelled"])
def test_immediate_event_is_queued_during_attach_without_early_output(socket_capture, monkeypatch, outcome):
    from server.capture_journal import CaptureHeldError
    from server.events import EventHub

    f = socket_capture
    entered, release = threading.Event(), threading.Event()
    hub = EventHub()
    monkeypatch.setattr(f.ws, "event_hub", hub)

    def checkpoint(_user):
        entered.set()
        assert release.wait(3)
        if outcome == "held":
            raise CaptureHeldError("checkpoint persistence is held")

    monkeypatch.setattr(f.ws.user_manager, "checkpoint", checkpoint)

    class BroadcastSocket(Socket):
        def __init__(self):
            super().__init__()
            self.sent = []
            self.delivered = asyncio.Event()

        async def receive_text(self):
            await asyncio.Event().wait()

        async def send_text(self, data):
            self.sent.append(json.loads(data))
            self.delivered.set()

    async def scenario():
        hub.attach_loop(asyncio.get_running_loop())
        socket = BroadcastSocket()
        task = asyncio.create_task(f.ws.events_socket(socket))
        try:
            for _ in range(200):
                if entered.is_set():
                    break
                await asyncio.sleep(0.005)
            assert entered.is_set() and hub.metrics()["subscribers"] == 1
            message = {"t": "broadcast", "message": "Immediate notice"}
            hub.publish(message)
            await asyncio.sleep(0)
            assert hub.metrics()["current_depth"] == 1
            assert socket.sent == []  # admission/checkpoint still owns output
            if outcome == "cancelled":
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
                assert hub.metrics()["subscribers"] == 0
            release.set()
            if outcome == "success":
                await asyncio.wait_for(socket.delivered.wait(), 2)
                assert socket.sent == [message]
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            elif outcome == "held":
                await asyncio.wait_for(task, 2)
                assert socket.closed == [{"code": 4413, "reason": "workshop capture is held"}]
                assert socket.sent == []
            else:
                assert socket.sent == []
        finally:
            release.set()
            if not task.done():
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
        for _ in range(200):
            if not f.ws._capture_workers:
                break
            await asyncio.sleep(0.005)
        assert not f.ws._capture_workers
        assert hub.metrics()["subscribers"] == 0

    asyncio.run(scenario())
    state = f.journal.read()["closeout"]
    assert state["writers"] == f.before
    if outcome == "held":
        failed, = state["writer_outcomes"]
        assert failed["kind"] == "events_socket_attach" and failed["status"] == "failed"
    else:
        assert state["writer_outcomes"] == []
