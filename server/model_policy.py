"""Live, versioned policy for direct access to approved ``system.ai`` models.

Control Tower owns the desired model pool. Production stores the last accepted
revision in the existing source-bound capture journal. Container files are only
a disposable cache; a replacement process reads the same retained policy and a
new event never imports an unbound local policy.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import tempfile
import threading
from collections.abc import Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from . import config, models
from .capture_journal import CaptureHeldError, canonical_json

logger = logging.getLogger(__name__)

POLICY_SCHEMA_VERSION = 1

_SYSTEM_SERVICE = re.compile(r"system\.ai\.[a-z0-9][a-z0-9._-]*")
_CAPABILITIES = frozenset({"claude", "codex", "chat", "embedding"})
_PRINCIPAL_CLASSES = frozenset({"lab_user", "wt_sp", "helper_sp"})


class PolicyEntry(BaseModel):
    """The stable subset of a CT model-pool snapshot consumed by WT."""

    model_config = ConfigDict(extra="forbid")

    service_name: str
    enabled: bool = True
    capabilities: list[Literal["claude", "codex", "chat", "embedding"]] = Field(
        min_length=1
    )
    principal_classes: list[Literal["lab_user", "wt_sp", "helper_sp"]] = Field(
        min_length=1
    )
    limit_profile: dict = Field(default_factory=dict)

    @field_validator("service_name")
    @classmethod
    def _system_service_name(cls, value: str) -> str:
        if value != value.strip() or value != value.lower():
            raise ValueError("service_name must be trimmed lowercase")
        if not _SYSTEM_SERVICE.fullmatch(value):
            raise ValueError(
                "service_name must be fully qualified as system.ai.<model>"
            )
        return value

    @field_validator("capabilities", "principal_classes")
    @classmethod
    def _unique_values(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)):
            raise ValueError("values must be unique")
        return value


class ModelPolicyRequest(BaseModel):
    """Idempotent wire contract implemented for CT-PR8."""

    model_config = ConfigDict(extra="forbid")

    revision: int = Field(ge=1)
    pool: list[PolicyEntry]
    denied_models: list[str]
    restart_processes: bool = False

    @field_validator("denied_models")
    @classmethod
    def _denied_names(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)):
            raise ValueError("denied_models must be unique")
        if value != sorted(value):
            raise ValueError("denied_models must be sorted")
        for name in value:
            if name != name.strip() or name != name.lower():
                raise ValueError("denied model names must be trimmed lowercase")
            if not _SYSTEM_SERVICE.fullmatch(name):
                raise ValueError(
                    "denied model names must be fully qualified as system.ai.<model>"
                )
        return value

    @model_validator(mode="after")
    def _complete_non_overlapping_snapshot(self) -> "ModelPolicyRequest":
        if self.restart_processes:
            raise ValueError("live policy updates may not restart agent processes")
        names = [entry.service_name for entry in self.pool]
        if len(names) != len(set(names)):
            raise ValueError("pool service names must be unique")
        overlap = set(names) & set(self.denied_models)
        if overlap:
            raise ValueError(
                "pool and denied_models overlap: " + ", ".join(sorted(overlap))
            )
        if any(not entry.enabled for entry in self.pool):
            raise ValueError("pool must contain enabled entries only")
        return self


class StalePolicy(ValueError):
    def __init__(self, current_revision: int) -> None:
        self.current_revision = current_revision
        super().__init__(
            f"model policy revision is stale; current revision is {current_revision}"
        )


class RevisionConflict(ValueError):
    def __init__(self, current_revision: int) -> None:
        self.current_revision = current_revision
        super().__init__(
            f"model policy revision {current_revision} already has different content"
        )


@dataclass(frozen=True)
class PolicySnapshot:
    revision: int = 0
    pool: tuple[PolicyEntry, ...] = ()
    denied_models: tuple[str, ...] = ()
    fingerprint: str = ""

    def allowed_services(self, capability: str | None = None) -> tuple[str, ...]:
        return tuple(
            sorted(
                entry.service_name
                for entry in self.pool
                if entry.enabled
                and "wt_sp" in entry.principal_classes
                and (capability is None or capability in entry.capabilities)
            )
        )

    def catalogue(self) -> dict[str, frozenset[str]]:
        wires_for = {
            "claude": models.ANTHROPIC_MESSAGES,
            "codex": models.OPENAI_RESPONSES,
            "chat": models.CHAT_COMPLETIONS,
        }
        catalogue: dict[str, frozenset[str]] = {}
        for entry in self.pool:
            if not entry.enabled or "wt_sp" not in entry.principal_classes:
                continue
            wires = frozenset(
                wires_for[capability]
                for capability in entry.capabilities
                if capability in wires_for
            )
            catalogue[models.catalogue_key(entry.service_name)] = wires
        return catalogue


def _canonical_payload(request: ModelPolicyRequest) -> dict:
    return {
        "schema_version": POLICY_SCHEMA_VERSION,
        "revision": request.revision,
        "pool": sorted(
            (entry.model_dump(mode="json") for entry in request.pool),
            key=lambda entry: entry["service_name"],
        ),
        "denied_models": list(request.denied_models),
    }


def _fingerprint(payload: Mapping[str, object]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class _PolicyReadOperation:
    store: object
    journal: object
    source: bytes
    snapshot: PolicySnapshot


_policy_operation: ContextVar[_PolicyReadOperation | None] = ContextVar("wt_policy_read_operation", default=None)


class PolicyStore:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._loaded_path = ""
        self._snapshot = PolicySnapshot()
        self._journal = None

    def configure_capture(self, journal) -> None:
        """Attach the existing source before startup consumers can use models."""
        with self._lock:
            self._check_binding(journal)
            snapshot = self._retained(journal.read(), journal)
            self._journal = journal
            self._snapshot = snapshot

    @staticmethod
    def _check_binding(journal) -> None:
        from .capture import source_binding

        if (canonical_json(journal.binding) != canonical_json(source_binding())
                or (not config.local_dev() and not journal.storage.durable)):
            raise CaptureHeldError("model policy persistence does not match its source")

    def _bound_journal(self):
        journal = self._journal
        if journal is None:
            if not config.local_dev():
                raise CaptureHeldError("model policy persistence is not initialized")
            return None
        self._check_binding(journal)
        return journal

    @staticmethod
    def _retained(state: dict, journal) -> PolicySnapshot:
        from .closeout_guard import closeout_state

        closeout_state(state)
        if "model_policy" not in state:
            return PolicySnapshot()
        try:
            raw = state["model_policy"]
            if (not isinstance(raw, dict)
                    or set(raw) != {"schema_version", "revision", "pool", "denied_models"}
                    or type(raw["schema_version"]) is not int
                    or raw["schema_version"] != POLICY_SCHEMA_VERSION):
                raise ValueError("invalid policy snapshot")
            request = ModelPolicyRequest.model_validate({
                "revision": raw["revision"], "pool": raw["pool"],
                "denied_models": raw["denied_models"], "restart_processes": False,
            })
            payload = _canonical_payload(request)
            if canonical_json(payload) != canonical_json(raw):
                raise ValueError("noncanonical policy snapshot")
            return PolicySnapshot(revision=request.revision, pool=tuple(request.pool),
                                  denied_models=tuple(request.denied_models),
                                  fingerprint=_fingerprint(payload))
        except (ValueError, TypeError, KeyError, AttributeError) as error:
            held = CaptureHeldError("retained model policy is invalid")
            journal.mark_held(held)
            raise held from error

    def _path(self) -> str:
        return os.path.join(config.data_root(), "model-policy-v1.json")

    def _load_locked(self) -> None:
        path = self._path()
        if path == self._loaded_path:
            return
        self._loaded_path = path
        self._snapshot = PolicySnapshot()
        try:
            with open(path, encoding="utf-8") as handle:
                raw = json.load(handle)
            if raw.get("schema_version") != POLICY_SCHEMA_VERSION:
                return
            request = ModelPolicyRequest.model_validate(
                {
                    "revision": raw.get("revision"),
                    "pool": raw.get("pool"),
                    "denied_models": raw.get("denied_models"),
                    "restart_processes": False,
                }
            )
            payload = _canonical_payload(request)
            self._snapshot = PolicySnapshot(
                revision=request.revision,
                pool=tuple(request.pool),
                denied_models=tuple(request.denied_models),
                fingerprint=_fingerprint(payload),
            )
        except (OSError, ValueError, TypeError):
            self._snapshot = PolicySnapshot()

    def _fresh_snapshot_locked(self, journal) -> PolicySnapshot:
        if journal is not None:
            self._snapshot = self._retained(journal.read(), journal)
        else:
            self._load_locked()
        return self._snapshot

    @staticmethod
    def _operation_source(journal) -> bytes:
        return canonical_json(journal.binding) if journal is not None else b""

    def snapshot(self) -> PolicySnapshot:
        with self._lock:
            journal = self._bound_journal()
            operation = _policy_operation.get()
            if operation is not None and operation.store is self:
                if (journal is not operation.journal
                        or self._operation_source(journal) != operation.source
                        or self._snapshot != operation.snapshot):
                    raise ModelPolicyConfigurationError("model policy changed during configuration; retry")
                if journal is not None and journal.health().get("ok") is not True:
                    # Preserve sticky/storage faults rather than serving an
                    # operation snapshot after another producer found damage.
                    return self._fresh_snapshot_locked(journal)
                return operation.snapshot
            return self._fresh_snapshot_locked(journal)

    @contextmanager
    def read_operation(self):
        """Reuse one source-bound snapshot only during a config operation.

        Independent operations always refresh the retained journal. A final
        fresh read proves that its policy/source still matches before the
        caller can publish success; concurrent revision or source changes fail
        closed. This does not cache journal transactions or closeout auditing.
        """
        previous = _policy_operation.get()
        if previous is not None and previous.store is self:
            yield self.snapshot()
            return
        with self._lock:
            journal = self._bound_journal()
            snapshot = self._fresh_snapshot_locked(journal)
            operation = _PolicyReadOperation(self, journal, self._operation_source(journal), snapshot)
        token = _policy_operation.set(operation)
        try:
            yield snapshot
            with self._lock:
                current_journal = self._bound_journal()
                if (current_journal is not operation.journal
                        or self._operation_source(current_journal) != operation.source
                        or self._fresh_snapshot_locked(current_journal) != snapshot):
                    raise ModelPolicyConfigurationError("model policy changed during configuration; retry")
        finally:
            _policy_operation.reset(token)

    def apply(self, request: ModelPolicyRequest) -> tuple[PolicySnapshot, bool]:
        payload = _canonical_payload(request)
        fingerprint = _fingerprint(payload)
        with self._lock:
            journal = self._bound_journal()
            if journal is not None:
                def apply_retained(state):
                    from .closeout_guard import CaptureClosedError, closeout_state

                    barrier = closeout_state(state)
                    if barrier is not None and barrier["phase"] != "OPEN":
                        raise CaptureClosedError("Workshop is closing; new capture input is disabled")
                    current = self._retained(state, journal)
                    if request.revision < current.revision:
                        raise StalePolicy(current.revision)
                    if request.revision == current.revision:
                        if fingerprint != current.fingerprint:
                            raise RevisionConflict(current.revision)
                        return current, False
                    state["model_policy"] = payload
                    return PolicySnapshot(
                        revision=request.revision, pool=tuple(request.pool),
                        denied_models=tuple(request.denied_models), fingerprint=fingerprint,
                    ), True

                snapshot, changed = journal.transaction(apply_retained)
                self._snapshot = snapshot
                try:
                    self._write_locked(payload)
                except Exception as error:
                    # A cache failure cannot undo an accepted primary commit.
                    logger.warning("model policy cache unavailable (%s)", type(error).__name__)
                return snapshot, changed
            self._load_locked()
            current = self._snapshot
            if request.revision < current.revision:
                raise StalePolicy(current.revision)
            if request.revision == current.revision:
                if fingerprint != current.fingerprint:
                    raise RevisionConflict(current.revision)
                return current, False

            self._write_locked(payload)
            self._snapshot = PolicySnapshot(
                revision=request.revision,
                pool=tuple(request.pool),
                denied_models=tuple(request.denied_models),
                fingerprint=fingerprint,
            )
            return self._snapshot, True

    def _write_locked(self, payload: Mapping[str, object]) -> None:
        path = self._path()
        directory = os.path.dirname(path)
        os.makedirs(directory, exist_ok=True)
        fd, temporary = tempfile.mkstemp(
            prefix=".model-policy-", suffix=".tmp", dir=directory
        )
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, sort_keys=True, separators=(",", ":"))
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
            self._loaded_path = ""
            self._snapshot = PolicySnapshot()
            self._journal = None


store = PolicyStore()


class ModelPolicyConfigurationError(ValueError):
    """An applied policy cannot provide a model required by a WT role."""


def request_tags(agent: str, env: Mapping[str, str] | None = None) -> str:
    """JSON value required by ``Databricks-Ai-Gateway-Request-Tags``."""
    env = os.environ if env is None else env
    values = {
        "workshop_run_id": (env.get("WORKSHOP_RUN_ID") or "unassigned").strip()
        or "unassigned",
        "workshop_unit_id": (env.get("WORKSHOP_UNIT_ID") or "unassigned").strip()
        or "unassigned",
        "agent": agent,
        "wt_release": (env.get("WORKSHOP_RELEASE_SHA") or "unknown").strip()
        or "unknown",
    }
    return json.dumps(values, sort_keys=True, separators=(",", ":"))


def direct_catalogue() -> dict[str, frozenset[str]] | None:
    snapshot = store.snapshot()
    if snapshot.revision:
        return snapshot.catalogue()
    # A CT-managed terminal must not fall back to workspace-wide discovery
    # while its policy is still in flight. Returning an authoritative empty
    # catalogue makes wizard, summary, comparison, and config writers fail
    # closed; the session endpoint also returns the attendee-facing 503.
    return {} if config.model_policy_required() else None


def direct_service_allowed(service_name: str, capability: str) -> bool:
    service_name = models.service_name(service_name)
    return service_name in store.snapshot().allowed_services(capability)


def resolve_service(role_name: str, available: models.Catalogue) -> str:
    """Resolve a CLI role without ever escaping an applied CT policy."""

    snapshot = store.snapshot()
    if not snapshot.revision:
        return models.resolve(role_name, available)

    catalogue = snapshot.catalogue()
    for candidate in models.chain(role_name):
        if models.serves(catalogue, candidate, role_name):
            return models.service_name(candidate)

    capability = "codex" if role_name == "codex" else "claude"
    for service_name in snapshot.allowed_services(capability):
        if models.serves(catalogue, service_name, role_name):
            return service_name
    raise ModelPolicyConfigurationError(
        f"current model policy has no approved {capability} service for {role_name}"
    )


__all__ = [
    "ModelPolicyRequest",
    "PolicyEntry",
    "ModelPolicyConfigurationError",
    "RevisionConflict",
    "StalePolicy",
    "direct_catalogue",
    "direct_service_allowed",
    "resolve_service",
    "request_tags",
    "store",
]
