"""Durable live override for the Control Tower event deadline.

The deployment environment remains the bootstrap default.  Control Tower can
extend a running event through the authenticated admin API; the override is
written under ``DATA_ROOT`` so an App restart does not silently restore the old
deadline.  Deadlines are monotonic within one deployment baseline because this
contract is for extensions, not early shutdown.  A changed deployment value
establishes a new baseline and invalidates the old live override.
"""

from __future__ import annotations

from collections.abc import Mapping
import json
import os
import tempfile
import threading

from . import config


ENV_NAME = "WORKSHOP_EVENT_ENDS_AT"
SCHEMA_VERSION = 2


class StaleEventDeadline(ValueError):
    def __init__(self, current_epoch: int):
        self.current_epoch = current_epoch
        super().__init__(f"event deadline is already {current_epoch}")


class EventDeadlineStore:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._loaded_key: tuple[str, int | None] | None = None
        self._event_ends_at: int | None = None

    def _path(self) -> str:
        return os.path.join(config.data_root(), "event-deadline-v1.json")

    @staticmethod
    def _fallback(env: Mapping[str, str]) -> int | None:
        raw = (env.get(ENV_NAME) or "").strip()
        if not raw:
            return None
        try:
            value = int(float(raw))
        except ValueError:
            return None
        return value if value > 0 else None

    @staticmethod
    def _discard_stale(path: str) -> None:
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
        except OSError:
            # A read-only or temporarily unavailable DATA_ROOT must not make
            # readiness fail. The mismatched record remains ignored.
            pass

    def _load_locked(self, deployment_event_ends_at: int | None) -> None:
        path = self._path()
        loaded_key = (path, deployment_event_ends_at)
        if loaded_key == self._loaded_key:
            return
        self._loaded_key = loaded_key
        self._event_ends_at = None
        try:
            with open(path, encoding="utf-8") as handle:
                raw = json.load(handle)
            if (
                raw.get("schema_version") != SCHEMA_VERSION
                or "deployment_event_ends_at" not in raw
            ):
                self._discard_stale(path)
                return
            stored_deployment = raw["deployment_event_ends_at"]
            if stored_deployment is not None:
                stored_deployment = int(stored_deployment)
                if stored_deployment <= 0:
                    raise ValueError("invalid deployment event deadline")
            if stored_deployment != deployment_event_ends_at:
                self._discard_stale(path)
                return
            value = int(raw.get("event_ends_at"))
            if value > 0:
                self._event_ends_at = value
        except (OSError, TypeError, ValueError):
            self._event_ends_at = None

    def snapshot(self, env: Mapping[str, str] | None = None) -> int | None:
        env = os.environ if env is None else env
        deployment_event_ends_at = self._fallback(env)
        with self._lock:
            self._load_locked(deployment_event_ends_at)
            return (
                self._event_ends_at
                if self._event_ends_at is not None
                else deployment_event_ends_at
            )

    def apply(self, event_ends_at: int) -> tuple[int, bool]:
        event_ends_at = int(event_ends_at)
        if event_ends_at <= 0:
            raise ValueError("event_ends_at must be a positive Unix epoch")
        deployment_event_ends_at = self._fallback(os.environ)
        with self._lock:
            self._load_locked(deployment_event_ends_at)
            current = (
                self._event_ends_at
                if self._event_ends_at is not None
                else deployment_event_ends_at
            )
            if current is not None and event_ends_at < current:
                raise StaleEventDeadline(current)
            if current == event_ends_at:
                return event_ends_at, False
            self._write_locked(event_ends_at, deployment_event_ends_at)
            self._event_ends_at = event_ends_at
            return event_ends_at, True

    def _write_locked(
        self,
        event_ends_at: int,
        deployment_event_ends_at: int | None,
    ) -> None:
        path = self._path()
        directory = os.path.dirname(path)
        os.makedirs(directory, exist_ok=True)
        fd, temporary = tempfile.mkstemp(
            prefix=".event-deadline-", suffix=".tmp", dir=directory
        )
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(
                    {
                        "schema_version": SCHEMA_VERSION,
                        "deployment_event_ends_at": deployment_event_ends_at,
                        "event_ends_at": event_ends_at,
                    },
                    handle,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            os.chmod(path, 0o600)
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass

    def reset_for_tests(self) -> None:
        with self._lock:
            self._loaded_key = None
            self._event_ends_at = None


store = EventDeadlineStore()


def effective_environment(
    env: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Copy ``env`` with the durable live deadline applied when present."""
    source = os.environ if env is None else env
    effective = dict(source)
    deadline = store.snapshot(source)
    if deadline is not None:
        effective[ENV_NAME] = str(deadline)
    return effective


__all__ = [
    "ENV_NAME",
    "EventDeadlineStore",
    "StaleEventDeadline",
    "effective_environment",
    "store",
]
