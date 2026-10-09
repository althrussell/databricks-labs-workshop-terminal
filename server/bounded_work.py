"""Bounded background metadata work with one in-flight call per cache key."""

from concurrent.futures import Future
import threading
from typing import Callable, Hashable, TypeVar

T = TypeVar("T")


class Singleflight:
    def __init__(self, capacity: int = 1):
        self._lock = threading.Lock()
        self._flights: dict[Hashable, Future] = {}
        self._capacity = capacity

    def run(self, key: Hashable, work: Callable[[], T], timeout: float) -> T:
        with self._lock:
            future = self._flights.get(key)
            if future is None:
                if len(self._flights) >= self._capacity:
                    raise TimeoutError("Background work is at capacity")
                future = Future()
                self._flights[key] = future

                def perform():
                    try:
                        future.set_result(work())
                    except BaseException as exc:
                        future.set_exception(exc)
                    finally:
                        with self._lock:
                            self._flights.pop(key, None)

                threading.Thread(target=perform, daemon=True, name="workshop-bounded-work").start()
        return future.result(timeout=max(0, timeout))

    @property
    def active(self) -> int:
        with self._lock:
            return len(self._flights)
