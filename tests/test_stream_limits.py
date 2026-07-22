"""Bounded terminal/event fanout and byte-capped replay."""

import asyncio
import json
import threading

from server.events import EventHub
from server import ws as ws_module
from server.sessions import (
    SCROLLBACK_MAX_BYTES,
    ByteScrollback,
    Session,
    SessionManager,
    Utf8StreamDecoder,
)


def test_scrollback_is_byte_capped_and_utf8_safe_for_large_chunks():
    scrollback = ByteScrollback(max_bytes=10)

    scrollback.append("prefix-🙂🙂🙂")

    replay = scrollback.text()
    assert len(replay.encode("utf-8")) <= 10
    assert replay.endswith("🙂🙂")
    assert "\ufffd" not in replay


def test_scrollback_never_exceeds_default_cap():
    scrollback = ByteScrollback()
    scrollback.append("x" * (SCROLLBACK_MAX_BYTES * 2))

    assert len(scrollback.text().encode("utf-8")) == SCROLLBACK_MAX_BYTES


def test_utf8_decoder_preserves_characters_split_across_pty_reads():
    decoder = Utf8StreamDecoder()
    encoded = "ready 🙂 now".encode("utf-8")

    output = decoder.decode(encoded[:8]) + decoder.decode(encoded[8:10])
    output += decoder.decode(encoded[10:]) + decoder.flush()

    assert output == "ready 🙂 now"
    assert "\ufffd" not in output


def test_terminal_slow_consumer_is_signalled_instead_of_dropping_output():
    manager = SessionManager()
    session = Session("alice@example.com", "bash", "Terminal", -1, -1)
    subscriber = manager.subscribe(session, max_queue=2)

    subscriber.put_nowait({"t": "output", "data": "one"})
    subscriber.put_nowait({"t": "output", "data": "two"})
    subscriber.put_nowait({"t": "output", "data": "three"})

    assert subscriber.overflowed is True
    assert subscriber.overflow_count == 1
    assert subscriber.get_nowait() == {"t": "overflow"}
    assert manager.queue_metrics()["terminal"]["overflows"] == 1


def test_event_slow_consumer_drops_oldest_and_tracks_depth():
    hub = EventHub(max_queue=2)
    queue = hub.subscribe()

    queue.put_nowait({"n": 1})
    queue.put_nowait({"n": 2})
    queue.put_nowait({"n": 3})

    assert queue.get_nowait() == {"n": 2}
    assert queue.get_nowait() == {"n": 3}
    assert hub.metrics() == {
        "subscribers": 1,
        "queue_capacity": 2,
        "current_depth": 0,
        "max_depth": 2,
        "overflows": 1,
        "policy": "drop_oldest",
    }


def test_terminal_fanout_overflow_callback_is_safe_on_event_loop():
    async def exercise():
        manager = SessionManager()
        session = Session("alice@example.com", "bash", "Terminal", -1, -1)
        manager.attach_loop(asyncio.get_running_loop())
        subscriber = manager.subscribe(session, max_queue=1)

        manager._fanout(session, {"t": "output", "data": "one"})
        manager._fanout(session, {"t": "output", "data": "two"})
        await asyncio.sleep(0)

        assert subscriber.overflowed is True

    asyncio.run(exercise())


def test_atomic_replay_registration_delivers_concurrent_output_exactly_once():
    assert hasattr(SessionManager, "attach_with_replay")
    assert hasattr(SessionManager, "publish_output")

    class ImmediateLoop:
        @staticmethod
        def call_soon_threadsafe(callback, *args):
            callback(*args)

    for _ in range(100):
        manager = SessionManager()
        manager._loop = ImmediateLoop()
        session = Session("alice@example.com", "bash", "Terminal", -1, -1)
        session.scrollback.append("before-")
        barrier = threading.Barrier(2)
        attached = {}

        def attach():
            barrier.wait()
            replay, queue, exited = manager.attach_with_replay(session)
            attached.update(replay=replay, queue=queue, exited=exited)

        def publish():
            barrier.wait()
            manager.publish_output(session, "during")

        attach_thread = threading.Thread(target=attach)
        publish_thread = threading.Thread(target=publish)
        attach_thread.start()
        publish_thread.start()
        attach_thread.join()
        publish_thread.join()

        queued = ""
        while not attached["queue"].empty():
            message = attached["queue"].get_nowait()
            if message["t"] == "output":
                queued += message["data"]
        assert attached["replay"].startswith("before-")
        assert (attached["replay"] + queued).count("during") == 1
        assert attached["exited"] is False


def test_slow_terminal_socket_closes_then_reconnect_replays_all_output():
    assert hasattr(ws_module, "pump_terminal_output")
    assert hasattr(SessionManager, "attach_with_replay")
    assert hasattr(SessionManager, "publish_output")

    class ControlledWebSocket:
        def __init__(self):
            self.sent = []
            self.closed = []
            self.send_started = asyncio.Event()
            self.release_send = asyncio.Event()

        async def send_text(self, payload):
            self.sent.append(json.loads(payload))
            if len(self.sent) == 1:
                self.send_started.set()
                await self.release_send.wait()

        async def close(self, code, reason):
            self.closed.append((code, reason))

    async def exercise():
        manager = SessionManager()
        manager.attach_loop(asyncio.get_running_loop())
        session = Session("alice@example.com", "bash", "Terminal", -1, -1)
        _, queue, _ = manager.attach_with_replay(session, max_queue=1)
        websocket = ControlledWebSocket()
        pump = asyncio.create_task(
            ws_module.pump_terminal_output(websocket, queue, "session-1")
        )

        manager.publish_output(session, "one")
        await websocket.send_started.wait()
        manager.publish_output(session, "two")
        manager.publish_output(session, "three")
        await asyncio.sleep(0)
        websocket.release_send.set()
        await pump

        assert websocket.closed == [(4408, "terminal consumer overflow")]
        replay, _, _ = manager.attach_with_replay(session)
        assert replay == "onetwothree"

    asyncio.run(exercise())


def test_terminal_max_depth_survives_unsubscribe():
    manager = SessionManager()
    session = Session("alice@example.com", "bash", "Terminal", -1, -1)
    queue = manager.subscribe(session, max_queue=3)
    queue.put_nowait({"n": 1})
    queue.put_nowait({"n": 2})

    manager.unsubscribe(session, queue)

    assert manager.queue_metrics()["terminal"]["max_depth"] == 2


def test_event_max_depth_survives_unsubscribe():
    hub = EventHub(max_queue=3)
    queue = hub.subscribe()
    queue.put_nowait({"n": 1})
    queue.put_nowait({"n": 2})

    hub.unsubscribe(queue)

    assert hub.metrics()["max_depth"] == 2
