"""Live, versioned policy for direct access to approved ``system.ai`` models.

Control Tower owns the desired model pool.  Workshop Terminal keeps the last
successfully applied revision in ``DATA_ROOT`` so a process restart cannot make
the policy disappear while CT is reconciling it again.  Reads are lock-free
from the caller's perspective and updates replace one immutable snapshot under
one lock; callers therefore see either the old policy or the new one, never a
partially-updated pool.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from . import config, models

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


class PolicyStore:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._loaded_path = ""
        self._snapshot = PolicySnapshot()

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

    def snapshot(self) -> PolicySnapshot:
        with self._lock:
            self._load_locked()
            return self._snapshot

    def apply(self, request: ModelPolicyRequest) -> tuple[PolicySnapshot, bool]:
        payload = _canonical_payload(request)
        fingerprint = _fingerprint(payload)
        with self._lock:
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
