"""Failure-preserving evidence for real generated-app evaluations.

Checks are observations, not model claims. Missing checks never become a pass.
This is runner bookkeeping; model-quality scoring uses MLflow native APIs.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .adapters.harness import redact_evidence

REQUIRED_ACCEPTANCE = (
    "attendee_execution_ready", "normal_ui_entry", "exact_prompt_delivery",
    "requirements_and_advice", "agreed_scope", "real_deployment",
    "critical_user_task", "persisted_backend_update", "app_restart_persistence",
    "first_preview_ux", "final_ux", "budget", "cleanup",
)


@dataclass(frozen=True)
class Check:
    name: str
    status: str
    detail: str
    evidence: tuple[str, ...] = ()

    def __post_init__(self):
        if self.status not in {"passed", "failed", "unverified"}:
            raise ValueError("Check status must be passed, failed or unverified")
        if self.status == "passed" and not self.evidence:
            raise ValueError("Passing observations require evidence references")


@dataclass
class RunReport:
    run_id: str
    stage: str
    scenario_id: str = "novice-bakery-order-queue-v1"
    synthetic_attendee: bool = True
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    provenance: dict = field(default_factory=dict)
    checks: list[Check] = field(default_factory=list)
    artifacts: dict = field(default_factory=dict)

    def add(self, name: str, status: str, detail: str, *evidence: str):
        if any(check.name == name for check in self.checks):
            raise ValueError("A check cannot overwrite an earlier result")
        self.checks.append(Check(name, status, detail, tuple(evidence)))

    def to_dict(self) -> dict:
        indexed = {check.name: check for check in self.checks}
        missing = [name for name in REQUIRED_ACCEPTANCE if name not in indexed]
        failed = [c.name for c in self.checks if c.status == "failed"]
        unverified = [c.name for c in self.checks if c.status == "unverified"]
        accepted = bool(self.stage == "completed" and not missing and not failed and not unverified)
        return {
            "schema_version": 1, "run_id": self.run_id, "stage": self.stage,
            "scenario_id": self.scenario_id, "synthetic_attendee": self.synthetic_attendee,
            "created_at": self.created_at, "provenance": self.provenance,
            "checks": [asdict(c) for c in self.checks], "artifacts": self.artifacts,
            "accepted": accepted, "verdict": "accepted" if accepted else "failed" if failed else "unverified",
            "missing_acceptance_checks": missing, "failed_checks": failed,
            "unverified_checks": unverified,
            "limitations": ["Preflight, UI entry, transport and screenshots alone do not establish app acceptance."],
        }


def write_evidence(path: str | Path, payload: object, *, secrets: tuple[str, ...] = ()) -> str:
    """Atomically write redacted evidence; authentication state is never copied."""
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    data = redact_evidence(json.dumps(payload, indent=2, allow_nan=False), secrets) + "\n"
    # Verify redaction left valid JSON; do not write partially corrupt evidence.
    json.loads(data)
    temporary = destination.with_name(destination.name + ".tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(data)
        temporary.replace(destination)
        destination.chmod(0o600)
    finally:
        if temporary.exists():
            temporary.unlink()
    return hashlib.sha256(data.encode()).hexdigest()


def validate_budgets(value: object) -> dict:
    if not isinstance(value, dict):
        raise ValueError("A live run requires an explicit budget")
    result = {}
    for name, maximum in (("total_seconds", 1800), ("consultation_seconds", 180),
                          ("token_ceiling", 1000000), ("spend_ceiling_usd", 100)):
        number = value.get(name)
        if isinstance(number, bool) or not isinstance(number, (int, float)):
            raise ValueError("All deadline, token and spend limits must be explicit numbers")
        if not math.isfinite(number) or not 0 < number <= maximum:
            raise ValueError("Live evaluation budgets exceed the first-cell bounds")
        result[name] = number
    if result["consultation_seconds"] > result["total_seconds"]:
        raise ValueError("Consultation cannot exceed the overall run deadline")
    return result
