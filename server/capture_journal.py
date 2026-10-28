"""Create-only, hash-chained capture operations in the app's existing workspace.

An operation contains only a JSON delta. Creating revision N exclusively is the
cross-container arbitration point: a competing writer reloads N and retries its
pure state mutation. No retained file is overwritten or deleted. Optional
checkpoints accelerate cold recovery and are anchored to a committed operation's
state digest; the operation history remains available for a full audit.

Local and memory stores are explicit test/standalone seams. Only Workspace Files
survive replacement of a Databricks Apps container. Callers must not interpret a
local path as evidence of redeploy durability.
"""

from __future__ import annotations

import contextlib
import contextvars
import copy
import hashlib
import json
import logging
import os
import re
import tempfile
import threading
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol, TypeVar

from .runtime_timing import stage as timing_stage
from .capture_io import storage_failure

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1
_OPERATION = re.compile(r"^(\d{20})\.json$")
_CHECKPOINT = re.compile(r"^checkpoint-(\d{20})\.json$")
_REQUIRED_BINDING = frozenset({"run_id", "unit_id", "workspace_id", "owner"})
T = TypeVar("T")


class CaptureError(RuntimeError):
    """Capture could not be safely accepted or read."""


class CaptureHeldError(CaptureError):
    """Retained history is corrupt, incompatible, incomplete or misbound."""


class CaptureStorageError(CaptureError):
    """The retained store is unavailable; accepted state must remain held."""

    def __init__(self, message: str = "capture storage unavailable", *, diagnostics: dict[str, Any] | None = None):
        super().__init__(message)
        self.diagnostics = {} if diagnostics is None else dict(diagnostics)


class CaptureCapacityError(CaptureError):
    """A configured limit prevents accepting new capture state."""


class CaptureStorage(Protocol):
    durable: bool
    mode: str

    def list_names(self) -> list[str]: ...
    def read(self, name: str) -> bytes | None: ...
    def create(self, name: str, content: bytes) -> bool: ...


def _safe_name(name: str) -> None:
    if not (_OPERATION.fullmatch(name) or _CHECKPOINT.fullmatch(name)):
        raise ValueError("invalid capture journal object name")


class MemoryCaptureStorage:
    """Explicit volatile store, for tests and standalone development only."""

    durable = False
    mode = "memory"

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self._lock = threading.Lock()

    def list_names(self) -> list[str]:
        with self._lock:
            return sorted(self.objects)

    def read(self, name: str) -> bytes | None:
        _safe_name(name)
        with self._lock:
            return self.objects.get(name)

    def create(self, name: str, content: bytes) -> bool:
        _safe_name(name)
        with self._lock:
            if name in self.objects:
                return False
            self.objects[name] = bytes(content)
            return True


class LocalCaptureStorage:
    """Fsynced create-only test store; not durable across container replacement."""

    durable = False
    mode = "local"

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.mkdir(mode=0o700, parents=True, exist_ok=True)

    def list_names(self) -> list[str]:
        return sorted(
            item.name
            for item in self.path.iterdir()
            if not item.name.startswith(".capture-stage-")
        )

    def read(self, name: str) -> bytes | None:
        _safe_name(name)
        try:
            return (self.path / name).read_bytes()
        except FileNotFoundError:
            return None

    def create(self, name: str, content: bytes) -> bool:
        _safe_name(name)
        descriptor, temporary = tempfile.mkstemp(
            prefix=".capture-stage-", dir=self.path
        )
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            try:
                os.link(temporary, self.path / name)
            except FileExistsError:
                return False
            directory = os.open(self.path, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
            return True
        finally:
            os.unlink(temporary)


class WorkspaceCaptureStorage:
    """Workspace Files adapter using the already-initialized app SDK client.

    The pinned SDK 0.151.0's WorkspaceExt.upload accepts bytes,
    format=ImportFormat.AUTO and overwrite=False, and documents
    RESOURCE_ALREADY_EXISTS for an existing object. download returns BinaryIO.
    No principal, permission or directory outside the supplied root is changed.
    """

    durable = True
    mode = "workspace"

    @timing_stage("capture.workspace_init")
    def __init__(self, client: Any, path: str) -> None:
        if not path.startswith("/Workspace/") or any(
            part in {".", ".."} for part in path.split("/")
        ):
            raise ValueError("capture root must be an absolute Workspace Files path")
        self.client = _bounded_capture_client(client)
        self.path = path.rstrip("/")
        # mkdirs is idempotent; it fails when a path component is a file.
        self.client.workspace.mkdirs(self.path)

    @timing_stage("capture.workspace_list")
    def list_names(self) -> list[str]:
        names = []
        for entry in self.client.workspace.list(self.path):
            if not entry.path:
                raise CaptureHeldError(
                    "capture listing contains an object without a path"
                )
            # Workspace APIs can return /Users/... for /Workspace/Users/... .
            parent = self.path.removeprefix("/Workspace") + "/"
            canonical = entry.path.removeprefix("/Workspace")
            if not canonical.startswith(parent) or "/" in canonical[len(parent) :]:
                raise CaptureHeldError("capture listing contains an unexpected path")
            object_type = getattr(entry.object_type, "value", entry.object_type)
            if object_type != "FILE":
                raise CaptureHeldError("capture listing contains a non-file object")
            names.append(canonical[len(parent) :])
        return sorted(names)

    @timing_stage("capture.workspace_read")
    def read(self, name: str) -> bytes | None:
        from databricks.sdk.errors import NotFound
        from databricks.sdk.service.workspace import ExportFormat

        _safe_name(name)
        try:
            stream = self.client.workspace.download(
                f"{self.path}/{name}", format=ExportFormat.AUTO
            )
        except NotFound:
            return None
        try:
            return stream.read()
        finally:
            close = getattr(stream, "close", None)
            if callable(close):
                close()

    @timing_stage("capture.workspace_create")
    def create(self, name: str, content: bytes) -> bool:
        from databricks.sdk.errors import ResourceAlreadyExists
        from databricks.sdk.service.workspace import ImportFormat

        _safe_name(name)
        try:
            self.client.workspace.upload(
                f"{self.path}/{name}",
                content,
                format=ImportFormat.AUTO,
                overwrite=False,
            )
        except ResourceAlreadyExists:
            return False
        return True


def _bounded_capture_client(client):
    """Reuse the app's existing identity with request/retry timeout settings.

    Do not mutate its shared SDK configuration or discover another credential.
    These are best-effort SDK/socket limits, not a total I/O deadline (for
    example Retry-After can exceed the retry limit). Lease expiry still fences
    any caller returning after ownership has expired.
    The pinned SDK accepts these timeout fields through Config, not through
    WorkspaceClient's constructor or workspace.download's arguments.
    """
    from databricks.sdk import WorkspaceClient
    from databricks.sdk.config import Config
    from databricks.sdk.credentials_provider import credentials_strategy

    if not isinstance(client, WorkspaceClient):
        return client  # Explicit storage/client seams used by standalone tests.

    @credentials_strategy("capture-app-identity", [])
    def existing_identity(_config):
        return client.config.authenticate

    return WorkspaceClient(config=Config(
        host=client.config.host, workspace_id=client.config.workspace_id,
        auth_type="capture-app-identity", credentials_strategy=existing_identity,
        http_timeout_seconds=10, retry_timeout_seconds=15,
    ))


class _AuditStorage:
    """Read-only view which yields to the owner's lease between I/O calls."""

    def __init__(self, storage, progress):
        self.storage, self.progress = storage, progress
        self.durable, self.mode = storage.durable, storage.mode

    def list_names(self):
        self.progress()
        value = self.storage.list_names()
        self.progress()
        return value

    def read(self, name):
        self.progress()
        value = self.storage.read(name)
        self.progress()
        return value

    def create(self, name, content):
        raise CaptureHeldError("a full audit requires existing retained history")


def canonical_json(value: Any) -> bytes:
    """The same finite, literal-Unicode canonical JSON representation as CT."""

    def validate(node: Any, depth: int = 0) -> None:
        if depth > 100:
            raise ValueError("capture JSON nesting exceeds supported depth")
        if type(node) is dict:
            if any(type(key) is not str for key in node):
                raise ValueError("capture JSON object keys must be strings")
            for item in node.values():
                validate(item, depth + 1)
        elif type(node) is list:
            for item in node:
                validate(item, depth + 1)
        elif type(node) not in (str, int, float, bool, type(None)):
            raise ValueError("capture state contains a non-JSON value")

    validate(value)
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _digest(value: Any) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


def _same_json(before: Any, after: Any) -> bool:
    """Python considers True == 1; canonical JSON deliberately does not."""
    if type(before) is not type(after):
        return False
    if isinstance(before, dict):
        return before.keys() == after.keys() and all(
            _same_json(value, after[key]) for key, value in before.items()
        )
    if isinstance(before, list):
        return len(before) == len(after) and all(
            _same_json(left, right) for left, right in zip(before, after)
        )
    return before == after


def _delta(
    before: Any, after: Any, path: list[str] | None = None
) -> list[dict[str, Any]]:
    at = path or []
    if _same_json(before, after):
        return []
    if isinstance(before, dict) and isinstance(after, dict):
        operations: list[dict[str, Any]] = []
        for key in sorted(before.keys() - after.keys()):
            operations.append({"op": "delete", "path": [*at, key]})
        for key in sorted(after):
            if key not in before:
                operations.append(
                    {"op": "set", "path": [*at, key], "value": after[key]}
                )
            else:
                operations.extend(_delta(before[key], after[key], [*at, key]))
        return operations
    if isinstance(before, list) and isinstance(after, list):
        start = 0
        while start < min(len(before), len(after)) and _same_json(
            before[start], after[start]
        ):
            start += 1
        suffix = 0
        while suffix < min(len(before), len(after)) - start and _same_json(
            before[len(before) - suffix - 1], after[len(after) - suffix - 1]
        ):
            suffix += 1
        end = len(after) - suffix
        return [
            {
                "op": "splice",
                "path": at,
                "start": start,
                "delete": len(before) - start - suffix,
                "items": after[start:end],
            }
        ]
    return [{"op": "set", "path": at, "value": after}]


def _apply(state: dict[str, Any], operations: Any) -> dict[str, Any]:
    if not isinstance(operations, list):
        raise CaptureHeldError("capture operation patch must be a list")
    value: Any = copy.deepcopy(state)
    for operation in operations:
        if not isinstance(operation, dict):
            raise CaptureHeldError("capture operation patch entry must be an object")
        path = operation.get("path")
        if not isinstance(path, list) or any(not isinstance(key, str) for key in path):
            raise CaptureHeldError("capture patch path is invalid")
        parent = value
        for key in path[:-1]:
            if not isinstance(parent, dict) or key not in parent:
                raise CaptureHeldError("capture patch parent does not exist")
            parent = parent[key]
        kind = operation.get("op")
        if kind == "set" and set(operation) == {"op", "path", "value"}:
            if not path:
                value = copy.deepcopy(operation["value"])
            elif isinstance(parent, dict):
                parent[path[-1]] = copy.deepcopy(operation["value"])
            else:
                raise CaptureHeldError("capture set target is invalid")
        elif kind == "delete" and set(operation) == {"op", "path"}:
            if not path or not isinstance(parent, dict) or path[-1] not in parent:
                raise CaptureHeldError("capture delete target does not exist")
            del parent[path[-1]]
        elif kind == "splice" and set(operation) == {
            "op",
            "path",
            "start",
            "delete",
            "items",
        }:
            target = (
                value
                if not path
                else parent.get(path[-1])
                if isinstance(parent, dict)
                else None
            )
            start, count, items = (
                operation["start"],
                operation["delete"],
                operation["items"],
            )
            if (
                not isinstance(target, list)
                or type(start) is not int
                or type(count) is not int
                or start < 0
                or count < 0
                or start + count > len(target)
                or not isinstance(items, list)
            ):
                raise CaptureHeldError("capture splice range is invalid")
            target[start : start + count] = copy.deepcopy(items)
        else:
            raise CaptureHeldError("unsupported capture patch operation")
    if not isinstance(value, dict):
        raise CaptureHeldError("capture state must be an object")
    return value


class CaptureJournal:
    """Atomic retained JSON state. Mutation callbacks can run more than once.

    Callbacks must only mutate their supplied private state and return a value;
    they must not send network requests, publish UI messages or change globals.
    Domain exceptions from a callback propagate without poisoning store health.

    With ``write_behind`` a transaction commits to this process's in-memory
    state and returns without storage I/O; one background flusher appends the
    accumulated change as a single operation every ``flush_interval`` seconds.
    The retained format and hash chain are unchanged. Callers that publish
    state outside this process (CT collection, closeout, shutdown) call
    :meth:`flush` first, so nothing observed elsewhere can be lost on a crash.
    Attendee paths therefore never wait on Workspace storage.
    """

    def __init__(
        self,
        storage: CaptureStorage,
        binding: dict[str, str],
        initial_state: dict[str, Any] | None = None,
        *,
        max_record_bytes: int = 2 * 1024 * 1024,
        max_state_bytes: int = 32 * 1024 * 1024,
        max_operations: int = 100_000,
        max_retries: int = 64,
        checkpoint_interval: int = 128,
        write_behind: bool | None = None,
        flush_interval: float = 2.0,
    ) -> None:
        if (
            not isinstance(binding, dict)
            or set(binding) != _REQUIRED_BINDING
            or any(
                not isinstance(value, str) or not value.strip()
                for value in binding.values()
            )
        ):
            raise ValueError(
                "capture binding requires nonempty run_id/unit_id/workspace_id/owner"
            )
        if any(
            type(limit) is not int or limit < 1
            for limit in (
                max_record_bytes,
                max_state_bytes,
                max_operations,
                max_retries,
                checkpoint_interval,
            )
        ):
            raise ValueError("capture journal limits must be positive integers")
        self.storage = storage
        self.binding = dict(binding)
        self.initial_state = copy.deepcopy(
            {} if initial_state is None else initial_state
        )
        if not isinstance(self.initial_state, dict):
            raise ValueError("initial capture state must be an object")
        if len(canonical_json(self.initial_state)) > max_state_bytes:
            raise CaptureCapacityError(
                "initial capture state exceeds retained capacity"
            )
        self.max_record_bytes, self.max_state_bytes = max_record_bytes, max_state_bytes
        self.max_operations, self.max_retries = max_operations, max_retries
        self.checkpoint_interval = checkpoint_interval
        self._lock = threading.RLock()
        self._state: dict[str, Any] = {}
        self._revision = -1
        self._head_hash = ""
        self._error: str | None = None
        self._held = False
        self._checkpoint_error: str | None = None
        self._last_storage_failure: dict[str, Any] | None = None
        if write_behind is None:
            write_behind = os.environ.get("WT_CAPTURE_WRITE_BEHIND", "").strip() == "1"
        if not isinstance(flush_interval, (int, float)) or flush_interval <= 0:
            raise ValueError("capture flush interval must be positive")
        self.write_behind = bool(write_behind)
        self.flush_interval = float(flush_interval)
        # Write-behind: ``_state`` is live; ``_durable_state`` is the state at
        # ``_revision``. Pending entries keep each mutator and its context so a
        # foreign append can be rebased by replaying them (callbacks may rerun).
        self._durable_state: dict[str, Any] = {}
        self._pending: list[tuple[Callable[[dict[str, Any]], Any], contextvars.Context]] = []
        self._flush_lock = threading.Lock()
        self._flush_wake = threading.Event()
        self._flusher: threading.Thread | None = None
        self._closed = False
        self._flush_error: str | None = None
        self._last_flush_at: float | None = None
        self._flush_failures = 0

    def health(self) -> dict[str, Any]:
        with self._lock:
            value: dict[str, Any] = {
                "ok": self._error is None,
                "held": self._held,
                "durable": self.storage.durable,
                "mode": self.storage.mode,
                "revision": self._revision,
                "error": self._error,
                "checkpoint_error": self._checkpoint_error,
                "operation_capacity_exhausted": self._revision >= self.max_operations,
            }
            if self.write_behind:
                value.update(write_behind=True, pending_operations=len(self._pending),
                             flush_error=self._flush_error, last_flush_at=self._last_flush_at)
            if self._last_storage_failure is not None:
                # Historical observation, retained even after a healthy retry.
                value["last_storage_failure"] = dict(self._last_storage_failure)
            return value

    def mark_held(self, error: str | CaptureHeldError) -> None:
        """Hold semantically invalid producer state found by a domain validator.

        JSON/hash validity cannot establish help/discovery/revision invariants.
        This is intentionally sticky for this process, just like a broken chain.
        """
        with self._lock:
            held = (
                error
                if isinstance(error, CaptureHeldError)
                else CaptureHeldError(error)
            )
            self._fault(held)

    def _fault(self, error: CaptureError) -> None:
        self._error = str(error)
        if isinstance(error, CaptureHeldError):
            self._held = True

    def _storage_failure(self, operation: str, error: Exception, attempts: int) -> CaptureStorageError:
        diagnostics = storage_failure(error, operation=operation, attempts=attempts)
        self._last_storage_failure = diagnostics
        action = "listing" if operation == "list" else operation
        return CaptureStorageError(
            f"capture storage {action} failed", diagnostics=dict(diagnostics)
        )

    @staticmethod
    def _retry_storage(diagnostics: dict, attempts: int, started: float) -> bool:
        # The SDK already retries. Only recover short transient failures here;
        # do not multiply an SDK call which has already consumed its budget.
        # These are attempt/pause bounds, not a total synchronous I/O deadline.
        delay = min(0.25, 0.05 * (2 ** max(0, attempts - 1)))
        if (not diagnostics["retryable"] or attempts >= 3
                or time.monotonic() - started + delay > 2.5):
            return False
        time.sleep(delay)
        return True

    def _storage_call(self, operation: str, call: Callable[[], T]) -> T:
        started = time.monotonic()
        attempts = 0
        while True:
            attempts += 1
            try:
                return call()
            except CaptureError:
                raise
            except Exception as error:
                failure = self._storage_failure(operation, error, attempts)
                if not self._retry_storage(failure.diagnostics, attempts, started):
                    raise failure from error

    def _storage_read(self, name: str) -> bytes | None:
        return self._storage_call("read", lambda: self.storage.read(name))

    def _document(self, raw: bytes | None, *, name: str) -> dict[str, Any]:
        if raw is None:
            raise CaptureHeldError(f"capture history object is missing: {name}")
        if not isinstance(raw, bytes) or len(raw) > self.max_record_bytes:
            raise CaptureHeldError("capture history object exceeds supported size")
        try:

            def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
                value: dict[str, Any] = {}
                for key, item in pairs:
                    if key in value:
                        raise ValueError("duplicate capture JSON key")
                    value[key] = item
                return value

            doc = json.loads(
                raw,
                object_pairs_hook=unique_object,
                parse_constant=lambda _: (_ for _ in ()).throw(ValueError()),
            )
            if not isinstance(doc, dict):
                raise ValueError()
            claimed = doc.get("hash")
            body = {key: value for key, value in doc.items() if key != "hash"}
            if not isinstance(claimed, str) or claimed != _digest(body):
                raise ValueError()
        except (ValueError, TypeError, RecursionError) as error:
            raise CaptureHeldError("capture history object is corrupt") from error
        if (
            doc.get("schema_version") != SCHEMA_VERSION
            or type(doc.get("schema_version")) is not int
        ):
            raise CaptureHeldError("unsupported capture journal schema")
        if doc.get("binding") != self.binding:
            raise CaptureHeldError("capture history binding does not match this source")
        return doc

    def _validate_operation(self, operation: dict[str, Any], revision: int) -> None:
        expected = {
            "schema_version",
            "revision",
            "binding",
            "previous_hash",
            "operation_id",
            "patch",
            "state_sha256",
            "hash",
        }
        previous = operation.get("previous_hash")
        if (
            set(operation) != expected
            or type(operation.get("revision")) is not int
            or operation["revision"] != revision
            or not isinstance(operation.get("operation_id"), str)
            or re.fullmatch(r"[0-9a-f]{32}", operation["operation_id"]) is None
            or not isinstance(operation.get("state_sha256"), str)
            or re.fullmatch(r"[0-9a-f]{64}", operation["state_sha256"]) is None
            or not isinstance(previous, str)
            or (
                previous != ""
                if revision == 0
                else re.fullmatch(r"[0-9a-f]{64}", previous) is None
            )
            or not isinstance(operation.get("patch"), list)
        ):
            raise CaptureHeldError("capture operation schema is invalid")

    def _encode(self, body: dict[str, Any]) -> bytes:
        try:
            raw = canonical_json({**body, "hash": _digest(body)})
        except (ValueError, TypeError, RecursionError) as error:
            raise CaptureCapacityError(
                "capture state must contain finite JSON values"
            ) from error
        if len(raw) > self.max_record_bytes:
            raise CaptureCapacityError(
                "capture operation exceeds the retained record limit"
            )
        return raw

    def _create_resolved(self, name: str, raw: bytes) -> bool:
        """An uncertain create is successful only after exact byte readback."""
        started = time.monotonic()
        attempts = 0
        while True:
            attempts += 1
            try:
                created = self.storage.create(name, raw)
                break
            except CaptureHeldError:
                raise
            except Exception as error:
                failure = self._storage_failure("create", error, attempts)
                existing = self._confirm_created(name, record_missing=False)
                if existing == raw:
                    return True
                if existing is not None:
                    return False
                if not self._retry_storage(failure.diagnostics, attempts, started):
                    raise failure from error
                # The exact revision/operation ID/bytes are retried, never the
                # producer's state-derived mutation after an ambiguous commit.
        # Even a normal success is verified. This also catches a broken adapter
        # that silently overwrites or returns before the object is readable.
        existing = self._confirm_created(name)
        if existing == raw:
            return True
        if existing is None:
            raise CaptureStorageError(
                "capture storage create did not become readable",
                diagnostics=dict(self._last_storage_failure),
            )
        if created:
            raise CaptureHeldError("capture storage contradicted create-only write")
        return False

    def _confirm_created(self, name: str, *, record_missing: bool = True) -> bytes | None:
        """Poll only the same immutable object after uncertain visibility."""
        # Confirmation has its own small window after a slow upload. It never
        # authorizes resubmitting a create whose own retry window has expired.
        started = time.monotonic()
        attempts = 0
        while True:
            attempts += 1
            existing = self._storage_read(name)
            if existing is not None:
                return existing
            diagnostics = {"operation": "create_readback", "http_status": None,
                           "timeout": "none", "attempts": attempts, "retryable": True}
            if record_missing:
                self._last_storage_failure = diagnostics
            if not self._retry_storage(diagnostics, attempts, started):
                return None

    @timing_stage("capture.journal_refresh")
    def _refresh(self, *, audit: bool = False, observer=None) -> None:
        if self._held:
            raise CaptureHeldError(self._error or "capture history is held")
        names = self._storage_call("list", self.storage.list_names)
        revisions: list[int] = []
        checkpoints: list[int] = []
        for name in names:
            match = _OPERATION.fullmatch(name)
            checkpoint = _CHECKPOINT.fullmatch(name)
            if match:
                revisions.append(int(match[1]))
            elif checkpoint:
                checkpoints.append(int(checkpoint[1]))
            else:
                raise CaptureHeldError(
                    "capture directory contains an unsupported object"
                )
        revisions.sort()
        if len(revisions) > self.max_operations + 1:
            raise CaptureHeldError("capture history exceeds supported operation count")
        if revisions != list(range(len(revisions))):
            raise CaptureHeldError(
                "capture history contains a missing or duplicate revision"
            )
        if checkpoints and (not revisions or max(checkpoints) > revisions[-1]):
            raise CaptureHeldError("capture checkpoint has no committed operation")
        if self._revision >= len(revisions):
            raise CaptureHeldError(
                "previously observed capture history has disappeared"
            )
        if not revisions:
            body = {
                "schema_version": SCHEMA_VERSION,
                "revision": 0,
                "binding": self.binding,
                "previous_hash": "",
                "operation_id": uuid.uuid4().hex,
                "patch": [{"op": "set", "path": [], "value": self.initial_state}],
                "state_sha256": _digest(self.initial_state),
            }
            self._create_resolved(f"{0:020d}.json", self._encode(body))
            revisions = [0]
        if audit:
            self._revision, self._head_hash, self._state = -1, "", {}
        # A fresh process may trust an immutable checkpoint only after matching
        # its contents to the hash and state digest of its committed operation.
        if self._revision < 0 and checkpoints and not audit:
            revision = max(checkpoints)
            checkpoint_name = f"checkpoint-{revision:020d}.json"
            checkpoint = self._document(
                self._storage_read(checkpoint_name), name=checkpoint_name
            )
            operation_name = f"{revision:020d}.json"
            anchor = self._document(
                self._storage_read(operation_name), name=operation_name
            )
            self._validate_operation(anchor, revision)
            state = checkpoint.get("state")
            if (
                set(checkpoint)
                != {
                    "schema_version",
                    "revision",
                    "binding",
                    "head_hash",
                    "state",
                    "hash",
                }
                or type(checkpoint.get("revision")) is not int
                or checkpoint["revision"] != revision
                or type(anchor.get("revision")) is not int
                or anchor["revision"] != revision
                or checkpoint.get("head_hash") != anchor["hash"]
                or not isinstance(state, dict)
                or _digest(state) != anchor.get("state_sha256")
            ):
                raise CaptureHeldError(
                    "capture checkpoint does not match its committed operation"
                )
            self._check_state(state, retained=True)
            self._revision, self._head_hash, self._state = (
                revision,
                anchor["hash"],
                state,
            )
        for revision in revisions:
            if revision <= self._revision:
                continue
            name = f"{revision:020d}.json"
            operation = self._document(self._storage_read(name), name=name)
            self._validate_operation(operation, revision)
            if operation.get("previous_hash") != self._head_hash:
                raise CaptureHeldError(
                    "capture operation chain does not match its predecessor"
                )
            state = _apply(self._state, operation.get("patch"))
            self._check_state(state, retained=True)
            if _digest(state) != operation.get("state_sha256"):
                raise CaptureHeldError("capture state digest does not match operation")
            if audit and observer is not None:
                # Proof consumers see only a contiguous prefix after all
                # canonical/hash/binding/state checks have succeeded. Audit
                # callers must not consume proofs if later replay fails.
                observer(revision=revision, operation_hash=operation["hash"],
                         before=copy.deepcopy(self._state), after=copy.deepcopy(state))
            self._revision, self._head_hash, self._state = (
                revision,
                operation["hash"],
                state,
            )
        self._error = None

    def _check_state(self, state: dict[str, Any], *, retained: bool = False) -> None:
        try:
            encoded = canonical_json(state)
        except (ValueError, TypeError, RecursionError) as error:
            cls = CaptureHeldError if retained else CaptureCapacityError
            raise cls("capture state must contain finite JSON values") from error
        if len(encoded) > self.max_state_bytes:
            cls = CaptureHeldError if retained else CaptureCapacityError
            raise cls("capture state exceeds the retained capacity limit")

    @timing_stage("capture.journal_read")
    def read(self) -> dict[str, Any]:
        with self._lock:
            try:
                if self.write_behind:
                    self._ensure_loaded()
                else:
                    self._refresh()
            except CaptureError as error:
                self._fault(error)
                raise
            return copy.deepcopy(self._state)

    def peek(self, key: str) -> Any:
        """A copy of one top-level value, without copying the whole state."""
        with self._lock:
            try:
                if self.write_behind:
                    self._ensure_loaded()
                else:
                    self._refresh()
            except CaptureError as error:
                self._fault(error)
                raise
            return copy.deepcopy(self._state.get(key))

    @timing_stage("capture.journal_audit")
    def audit(self, *, observer=None) -> dict[str, Any]:
        """Replay every retained operation, including history before checkpoints."""
        if not self.write_behind:
            with self._lock:
                try:
                    self._refresh(audit=True, observer=observer)
                except CaptureError as error:
                    self._fault(error)
                    raise
                return copy.deepcopy(self._state)
        # Audit proves what storage retains, so everything accepted first
        # becomes retained. Producers wait only for this rare closeout replay.
        with self._flush_lock:
            self._flush_locked()
            with self._lock:
                try:
                    if self._pending:
                        raise CaptureStorageError("capture changed while preparing its audit")
                    self._refresh(audit=True, observer=observer)
                except CaptureError as error:
                    self._fault(error)
                    raise
                self._durable_state = copy.deepcopy(self._state)
                return copy.deepcopy(self._state)

    def audit_snapshot(self, *, progress: Callable[[], None], observer=None) -> CaptureJournal:
        """Fully replay a private snapshot without locking/rewinding live state.

        This still reads every original operation, including old checkpoint
        prefixes. A caller can renew its fenced lease between reads while normal
        status and ACK transactions continue on this journal. The returned
        read-only journal may refresh/validate a subsequently appended suffix.
        It must never replace the live journal's newer cache.
        """
        self.flush()
        snapshot = CaptureJournal(
            _AuditStorage(self.storage, progress), self.binding, self.initial_state,
            max_record_bytes=self.max_record_bytes, max_state_bytes=self.max_state_bytes,
            max_operations=self.max_operations, max_retries=self.max_retries,
            checkpoint_interval=self.checkpoint_interval, write_behind=False,
        )
        try:
            snapshot.audit(observer=observer)
        except CaptureHeldError as error:
            self.mark_held(error)
            raise
        return snapshot

    @timing_stage("capture.journal_write")
    def transaction(self, mutator: Callable[[dict[str, Any]], T], *, durable: bool = False) -> T:
        """Commit ``mutator``. ``durable`` returns only once retained, and a
        storage failure leaves state unchanged; use it for cold, CT-initiated
        writes whose caller must know the outcome. Attendee paths never do."""
        if self.write_behind and not durable:
            return self._transaction_in_memory(mutator)
        if self.write_behind:
            with self._flush_lock:
                while True:
                    self._flush_locked()
                    with self._lock:
                        if not self._pending:
                            try:
                                return self._transaction_retained(mutator)
                            finally:
                                # Nothing is pending, so live state is retained state.
                                self._durable_state = copy.deepcopy(self._state)
        return self._transaction_retained(mutator)

    def _transaction_retained(self, mutator: Callable[[dict[str, Any]], T]) -> T:
        with self._lock:
            for _ in range(self.max_retries):
                try:
                    self._refresh()
                except CaptureError as error:
                    self._fault(error)
                    raise
                state = copy.deepcopy(self._state)
                # Caller/domain exceptions do not become storage-health faults.
                result = mutator(state)
                # Import lazily: the admission guard uses this journal's error
                # types and canonical representation, never another store.
                from .closeout_guard import authorize_mutation

                authorize_mutation(self, self._state, state)
                try:
                    if not isinstance(state, dict):
                        raise CaptureCapacityError("capture state must be an object")
                    self._check_state(state)
                    patch = _delta(self._state, state)
                    if not patch:
                        return result
                    revision = self._revision + 1
                    if revision > self.max_operations:
                        raise CaptureCapacityError(
                            "capture operation capacity is exhausted"
                        )
                    body = {
                        "schema_version": SCHEMA_VERSION,
                        "revision": revision,
                        "binding": self.binding,
                        "previous_hash": self._head_hash,
                        "operation_id": uuid.uuid4().hex,
                        "patch": patch,
                        "state_sha256": _digest(state),
                    }
                    raw = self._encode(body)
                    if not self._create_resolved(f"{revision:020d}.json", raw):
                        continue
                    doc = json.loads(raw)
                    self._revision, self._head_hash, self._state = (
                        revision,
                        doc["hash"],
                        copy.deepcopy(state),
                    )
                    self._error = None
                    self._maybe_checkpoint()
                    return result
                except CaptureError as error:
                    self._fault(error)
                    raise
            error = CaptureStorageError("capture write contention exceeded retry limit")
            self._fault(error)
            raise error

    def _ensure_loaded(self) -> None:
        """Load retained history once; afterwards this process owns the head."""
        if self._held:
            raise CaptureHeldError(self._error or "capture history is held")
        if self._revision < 0:
            self._refresh()
            self._durable_state = copy.deepcopy(self._state)

    def _transaction_in_memory(self, mutator: Callable[[dict[str, Any]], T]) -> T:
        from .closeout_guard import authorize_mutation

        with self._lock:
            try:
                self._ensure_loaded()
            except CaptureError as error:
                self._fault(error)
                raise
            state = copy.deepcopy(self._state)
            result = mutator(state)
            authorize_mutation(self, self._state, state)
            try:
                if not isinstance(state, dict):
                    raise CaptureCapacityError("capture state must be an object")
                self._check_state(state)
                patch = _delta(self._state, state)
                if not patch:
                    return result
                if self._revision + 1 > self.max_operations:
                    raise CaptureCapacityError("capture operation capacity is exhausted")
                # Keep the per-operation limit a producer error, as before.
                self._encode({"schema_version": SCHEMA_VERSION, "revision": self._revision + 1,
                              "binding": self.binding, "previous_hash": self._head_hash,
                              "operation_id": "0" * 32, "patch": patch, "state_sha256": "0" * 64})
            except CaptureError as error:
                self._fault(error)
                raise
            self._state = state
            self._pending.append((mutator, contextvars.copy_context(), patch))
            self._start_flusher()
            return result

    def flush(self) -> None:
        """Durability barrier: return only after accepted state is retained."""
        if not self.write_behind:
            return
        with self._flush_lock, self._process_file_lock():
            self._flush_locked()

    @contextlib.contextmanager
    def _process_file_lock(self):
        try:
            import fcntl

            lock_dir = config.data_root()
            os.makedirs(lock_dir, exist_ok=True)
            lock_path = os.path.join(lock_dir, ".capture.lock")
            fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        except Exception:  # noqa: BLE001
            yield
            return
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
            try:
                os.close(fd)
            except OSError:
                pass

    def close(self) -> None:
        """Stop the background flusher after retaining everything accepted."""
        self._closed = True
        self._flush_wake.set()
        self.flush()

    def _start_flusher(self) -> None:
        if self._closed or (self._flusher is not None and self._flusher.is_alive()):
            return
        self._flusher = threading.Thread(target=self._flush_loop, name="capture-flusher", daemon=True)
        self._flusher.start()

    def _flush_loop(self) -> None:
        delay = self.flush_interval
        while not self._closed:
            # A fixed cadence coalesces bursts into one retained operation.
            self._flush_wake.wait(timeout=delay)
            if self._closed:
                return
            try:
                self.flush()
                with self._lock:
                    self._flush_error, self._flush_failures = None, 0
                delay = self.flush_interval
            except Exception as error:  # noqa: BLE001 — retried; never reaches producers
                with self._lock:
                    self._flush_failures += 1
                    self._flush_error = str(error)
                    failures = self._flush_failures
                delay = min(30.0, self.flush_interval * (2 ** min(failures, 4)))
                logger.warning("capture flush failed (attempt %s); retrying in %.0fs: %s", failures, delay, error)

    def _flush_locked(self) -> None:
        """Append accepted changes as one operation (or one per change if large)."""
        for _ in range(self.max_retries):
            with self._lock:
                if self._held:
                    raise CaptureHeldError(self._error or "capture history is held")
                if not self._pending:
                    return
                pending = list(self._pending)
                base_revision, base_hash = self._revision, self._head_hash
                base_state = self._durable_state
                target = copy.deepcopy(self._state)
            try:
                steps = [(_delta(base_state, target), target)]
                self._encode({"patch": steps[0][0]})
            except CaptureCapacityError:
                # A large burst is retained change by change, as it was accepted.
                steps, state = [], base_state
                for _mutator, _context, patch in pending:
                    state = _apply(state, patch)
                    steps.append((patch, state))
            committed = self._append_steps(base_revision, base_hash, steps)
            with self._lock:
                if committed is not None:
                    revision, head_hash = committed
                    self._revision, self._head_hash, self._durable_state = revision, head_hash, steps[-1][1]
                    del self._pending[:len(pending)]
                    self._error = None
                    self._last_flush_at = time.time()
                    checkpoint = (revision, head_hash, self._durable_state)
                else:
                    self._rebase_locked()
                    checkpoint = None
            if checkpoint is not None:
                self._maybe_checkpoint(*checkpoint)
        error = CaptureStorageError("capture write contention exceeded retry limit")
        with self._lock:
            self._fault(error)
        raise error

    def _append_steps(self, revision: int, head_hash: str, steps: list[tuple[list, dict[str, Any]]]) -> tuple[int, str] | None:
        """Storage I/O for a flush; ``None`` when another process advanced the head."""
        for patch, state in steps:
            revision += 1
            if revision > self.max_operations:
                raise CaptureCapacityError("capture operation capacity is exhausted")
            body = {
                "schema_version": SCHEMA_VERSION,
                "revision": revision,
                "binding": self.binding,
                "previous_hash": head_hash,
                "operation_id": uuid.uuid4().hex,
                "patch": patch,
                "state_sha256": _digest(state),
            }
            raw = self._encode(body)
            if not self._create_resolved(f"{revision:020d}.json", raw):
                return None
            head_hash = json.loads(raw)["hash"]
        return revision, head_hash

    def _rebase_locked(self) -> None:
        """Another process appended: load its history, then replay ours on top."""
        from .closeout_guard import authorize_mutation

        pending = list(self._pending)
        self._state = copy.deepcopy(self._durable_state)
        self._refresh()
        self._durable_state = copy.deepcopy(self._state)
        state, rebuilt, dropped = self._state, [], 0
        for mutator, context, _patch in pending:
            candidate = copy.deepcopy(state)
            try:
                context.run(mutator, candidate)
                context.run(authorize_mutation, self, state, candidate)
            except Exception:  # noqa: BLE001 — the foreign history now refuses it
                dropped += 1
                continue
            patch = _delta(state, candidate)
            if patch:
                rebuilt.append((mutator, context, patch))
                state = candidate
        self._state, self._pending = state, rebuilt
        logger.warning("capture history advanced in another process; rebased %s change(s), dropped %s",
                       len(rebuilt), dropped)

    def _maybe_checkpoint(self, revision: int | None = None, head_hash: str | None = None,
                          state: dict[str, Any] | None = None) -> None:
        revision = self._revision if revision is None else revision
        head_hash = self._head_hash if head_hash is None else head_hash
        state = self._state if state is None else state
        if revision == 0 or revision % self.checkpoint_interval:
            return
        try:
            body = {
                "schema_version": SCHEMA_VERSION,
                "revision": revision,
                "binding": self.binding,
                "head_hash": head_hash,
                "state": state,
            }
            raw = self._encode(body)
            if not self._create_resolved(f"checkpoint-{revision:020d}.json", raw):
                raise CaptureHeldError(
                    "capture checkpoint conflicts with committed state"
                )
            self._checkpoint_error = None
        except CaptureError as error:
            # The accepted operation is already committed. A failed optional
            # accelerator cannot turn that success into an ambiguous append.
            self._checkpoint_error = str(error)


__all__ = [
    "CaptureCapacityError",
    "CaptureError",
    "CaptureHeldError",
    "CaptureJournal",
    "CaptureStorageError",
    "LocalCaptureStorage",
    "MemoryCaptureStorage",
    "WorkspaceCaptureStorage",
    "canonical_json",
]
