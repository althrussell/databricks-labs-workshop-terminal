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

import copy
import hashlib
import json
import os
import re
import tempfile
import threading
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol, TypeVar

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

    def __init__(self, client: Any, path: str) -> None:
        if not path.startswith("/Workspace/") or any(
            part in {".", ".."} for part in path.split("/")
        ):
            raise ValueError("capture root must be an absolute Workspace Files path")
        self.client = client
        self.path = path.rstrip("/")
        # mkdirs is idempotent; it fails when a path component is a file.
        self.client.workspace.mkdirs(self.path)

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

    def health(self) -> dict[str, Any]:
        with self._lock:
            return {
                "ok": self._error is None,
                "held": self._held,
                "durable": self.storage.durable,
                "mode": self.storage.mode,
                "revision": self._revision,
                "error": self._error,
                "checkpoint_error": self._checkpoint_error,
            }

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

    def _storage_read(self, name: str) -> bytes | None:
        try:
            return self.storage.read(name)
        except CaptureError:
            raise
        except Exception as error:
            raise CaptureStorageError("capture storage read failed") from error

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
        try:
            created = self.storage.create(name, raw)
        except Exception as error:
            existing = self._storage_read(name)
            if existing == raw:
                return True
            if existing is not None:
                return False
            raise CaptureStorageError(
                "capture storage create failed without a committed object"
            ) from error
        # Even a normal success is verified. This also catches a broken adapter
        # that silently overwrites or returns before the object is readable.
        existing = self._storage_read(name)
        if existing == raw:
            return True
        if existing is None:
            raise CaptureStorageError("capture storage create did not become readable")
        if created:
            raise CaptureHeldError("capture storage contradicted create-only write")
        return False

    def _refresh(self, *, audit: bool = False) -> None:
        if self._held:
            raise CaptureHeldError(self._error or "capture history is held")
        try:
            names = self.storage.list_names()
        except CaptureError:
            raise
        except Exception as error:
            raise CaptureStorageError("capture storage listing failed") from error
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

    def read(self) -> dict[str, Any]:
        with self._lock:
            try:
                self._refresh()
            except CaptureError as error:
                self._fault(error)
                raise
            return copy.deepcopy(self._state)

    def audit(self) -> dict[str, Any]:
        """Replay every retained operation, including history before checkpoints."""
        with self._lock:
            try:
                self._refresh(audit=True)
            except CaptureError as error:
                self._fault(error)
                raise
            return copy.deepcopy(self._state)

    def transaction(self, mutator: Callable[[dict[str, Any]], T]) -> T:
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

    def _maybe_checkpoint(self) -> None:
        if self._revision == 0 or self._revision % self.checkpoint_interval:
            return
        try:
            body = {
                "schema_version": SCHEMA_VERSION,
                "revision": self._revision,
                "binding": self.binding,
                "head_hash": self._head_hash,
                "state": self._state,
            }
            raw = self._encode(body)
            if not self._create_resolved(f"checkpoint-{self._revision:020d}.json", raw):
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
