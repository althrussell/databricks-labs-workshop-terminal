"""App-level event hub: fans out phase changes, broadcasts, and presence
updates to every connected /ws/events client."""

from __future__ import annotations

import asyncio
import threading

EVENT_SUBSCRIBER_QUEUE_SIZE = 64


class EventSubscriberQueue(asyncio.Queue):
    """Bounded latest-event queue using a documented drop-oldest policy."""

    def __init__(self, maxsize: int, on_overflow=None):
        super().__init__(maxsize=maxsize)
        self.overflow_count = 0
        self.max_depth = 0
        self._on_overflow = on_overflow

    def put_nowait(self, item) -> None:
        if self.full():
            self.get_nowait()
            self.overflow_count += 1
            if self._on_overflow is not None:
                self._on_overflow()
        super().put_nowait(item)
        self.max_depth = max(self.max_depth, self.qsize())


class EventHub:
    def __init__(self, max_queue: int = EVENT_SUBSCRIBER_QUEUE_SIZE):
        self._subscribers: set[EventSubscriberQueue] = set()
        self._lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._max_queue = max_queue
        self._overflows = 0
        self._max_depth = 0

    def attach_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def subscribe(self) -> EventSubscriberQueue:
        def record_overflow() -> None:
            with self._lock:
                self._overflows += 1

        queue = EventSubscriberQueue(self._max_queue, record_overflow)
        with self._lock:
            self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: EventSubscriberQueue) -> None:
        with self._lock:
            self._subscribers.discard(queue)
            self._max_depth = max(self._max_depth, queue.max_depth)

    def publish(self, message: dict) -> None:
        """Thread-safe publish from any thread (admin routes, reapers, ...)."""
        loop = self._loop
        if loop is None:
            return
        with self._lock:
            subscribers = list(self._subscribers)
        for queue in subscribers:
            loop.call_soon_threadsafe(self._put_event, queue, message)

    def _put_event(self, queue: EventSubscriberQueue, message: dict) -> None:
        queue.put_nowait(message)
        with self._lock:
            self._max_depth = max(self._max_depth, queue.max_depth)

    def metrics(self) -> dict:
        with self._lock:
            subscribers = list(self._subscribers)
            overflows = self._overflows
            observed_max = self._max_depth
        return {
            "subscribers": len(subscribers),
            "queue_capacity": self._max_queue,
            "current_depth": sum(queue.qsize() for queue in subscribers),
            "max_depth": max(
                [observed_max, *(queue.max_depth for queue in subscribers)],
                default=0,
            ),
            "overflows": overflows,
            "policy": "drop_oldest",
        }


event_hub = EventHub()
