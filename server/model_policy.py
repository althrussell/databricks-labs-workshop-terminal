"""Live, versioned model policy and governed AI Gateway service contract.

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
GATEWAY_CONFIG_VERSION = 1

_SYSTEM_SERVICE = re.compile(r"system\.ai\.[a-z0-9][a-z0-9._-]*")
_CUSTOM_SERVICE = re.compile(
    r"[a-z0-9][a-z0-9_-]*\.[a-z0-9][a-z0-9_-]*\.[a-z0-9][a-z0-9._-]*"
)
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
            raise ValueError("service_name must be fully qualified as system.ai.<model>")
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


@dataclass(frozen=True)
class GatewayServiceConfig:
    mode: str
    version: int | None
    claude_driver: str
    claude_frontier: str
    claude_standard: str
    claude_fast: str
    codex: str
    errors: tuple[str, ...]

    @property
    def required(self) -> bool:
        return self.mode == "required"

    @property
    def configured(self) -> bool:
        return not self.errors and self.version == GATEWAY_CONFIG_VERSION

    def claude_services(self) -> dict[str, str]:
        return {
            "driver": self.claude_driver,
            "frontier": self.claude_frontier,
            "standard": self.claude_standard,
            "fast": self.claude_fast,
        }


def gateway_service_config(
    env: Mapping[str, str] | None = None,
) -> GatewayServiceConfig:
    env = os.environ if env is None else env
    mode = (env.get("WORKSHOP_AI_GATEWAY_MODE") or "optional").strip().lower()
    errors: list[str] = []
    if mode not in {"disabled", "optional", "required"}:
        errors.append("WORKSHOP_AI_GATEWAY_MODE must be disabled, optional, or required")
    raw_version = (env.get("WORKSHOP_AI_GATEWAY_CONFIG_SCHEMA") or "").strip()
    try:
        version = int(raw_version) if raw_version else None
    except ValueError:
        version = None
        errors.append("WORKSHOP_AI_GATEWAY_CONFIG_SCHEMA must be an integer")
    if version not in {None, GATEWAY_CONFIG_VERSION}:
        errors.append(
            f"unsupported governed service config version {version}; expected "
            f"{GATEWAY_CONFIG_VERSION}"
        )

    names = {
        "claude_driver": (env.get("WORKSHOP_CLAUDE_DRIVER_SERVICE") or "").strip(),
        "claude_frontier": (env.get("WORKSHOP_CLAUDE_OPUS_SERVICE") or "").strip(),
        "claude_standard": (env.get("WORKSHOP_CLAUDE_SONNET_SERVICE") or "").strip(),
        "claude_fast": (env.get("WORKSHOP_CLAUDE_HAIKU_SERVICE") or "").strip(),
        "codex": (env.get("WORKSHOP_CODEX_SERVICE") or "").strip(),
    }
    any_names = any(names.values())
    if mode == "required" or any_names or version is not None:
        if version != GATEWAY_CONFIG_VERSION:
            errors.append(
                f"WORKSHOP_AI_GATEWAY_CONFIG_SCHEMA={GATEWAY_CONFIG_VERSION} is required"
            )
        for role, name in names.items():
            if not name:
                errors.append(f"governed service for {role} is missing")
                continue
            if name != name.lower() or not _CUSTOM_SERVICE.fullmatch(name):
                errors.append(f"governed service for {role} must be a lowercase UC name")
            elif name.startswith("system.ai."):
                errors.append(
                    f"governed service for {role} must not use the global system.ai schema"
                )
    return GatewayServiceConfig(
        mode=mode,
        version=version,
        errors=tuple(errors),
        **names,
    )


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
    return snapshot.catalogue() if snapshot.revision else None


def direct_service_allowed(service_name: str, capability: str) -> bool:
    service_name = models.service_name(service_name)
    return service_name in store.snapshot().allowed_services(capability)


def governed_status(
    env: Mapping[str, str],
    available: models.Catalogue | None,
) -> dict[str, object]:
    service_config = gateway_service_config(env)
    snapshot = store.snapshot()
    missing: list[str] = []
    wrong_wire: list[str] = []
    catalogue = {
        models.catalogue_key(name): wires for name, wires in (available or {}).items()
    }
    expected = {
        **{
            service: models.ANTHROPIC_MESSAGES
            for service in service_config.claude_services().values()
            if service
        },
        **({service_config.codex: models.OPENAI_RESPONSES} if service_config.codex else {}),
    }
    for service, wire in expected.items():
        key = models.catalogue_key(service)
        if key not in catalogue:
            missing.append(service)
        elif wire not in (catalogue[key] or frozenset()):
            wrong_wire.append(service)
    identity_missing = [
        name
        for name in ("WORKSHOP_RUN_ID", "WORKSHOP_UNIT_ID", "WORKSHOP_RELEASE_SHA")
        if not (env.get(name) or "").strip()
    ]
    ok = (
        not service_config.errors
        and service_config.configured
        and snapshot.revision > 0
        and available is not None
        and not missing
        and not wrong_wire
        and not identity_missing
    )
    return {
        "required": service_config.required,
        "mode": service_config.mode,
        "config_version": service_config.version,
        "configured": service_config.configured,
        "policy_revision": snapshot.revision,
        "services": sorted(expected),
        "missing_services": sorted(missing),
        "wrong_wire_services": sorted(wrong_wire),
        "missing_identity": identity_missing,
        "errors": list(service_config.errors),
        "verified": ok,
    }


__all__ = [
    "GATEWAY_CONFIG_VERSION",
    "GatewayServiceConfig",
    "ModelPolicyRequest",
    "PolicyEntry",
    "RevisionConflict",
    "StalePolicy",
    "direct_catalogue",
    "direct_service_allowed",
    "gateway_service_config",
    "governed_status",
    "request_tags",
    "store",
]
