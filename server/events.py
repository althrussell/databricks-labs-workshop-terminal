"""App-level event hub: fans out phase changes, broadcasts, and presence
updates to every connected /ws/events client."""

from __future__ import annotations

import asyncio
import threading


class EventHub:
    def __init__(self):
        self._subscribers: set[asyncio.Queue] = set()
        self._lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None

    def attach_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue()
        with self._lock:
            self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        with self._lock:
            self._subscribers.discard(queue)

    def publish(self, message: dict) -> None:
        """Thread-safe publish from any thread (admin routes, reapers, ...)."""
        loop = self._loop
        if loop is None:
            return
        with self._lock:
            subscribers = list(self._subscribers)
        for queue in subscribers:
            loop.call_soon_threadsafe(queue.put_nowait, message)


event_hub = EventHub()
