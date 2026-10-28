"""Privacy-safe friction scanner and structured friction emitters for WT.

Scans tool-result error lines (never attendee prompt text) from local agent
transcripts and emits deduplicated ``friction.event`` records through
``telemetry.emit`` so Control Tower has a live and post-event operational
view of blockers without reading raw transcripts.
"""

from __future__ import annotations

import hashlib
import re
import threading
from dataclasses import dataclass
from typing import Any

from . import telemetry
from .diagnostics import clip, redact

FRICTION_TAXONOMY: frozenset[str] = frozenset(
    {
        "access/sign-in",
        "permissions/grants",
        "auth/CLI profile",
        "model/gateway",
        "tool unavailable",
        "agent harness",
        "Omnigent",
        "platform limit",
        "network",
        "demo data",
        "workshop tooling defect",
        "attendee UX",
        "capture/insight",
    }
)


@dataclass(frozen=True)
class _PatternRule:
    pattern: re.Pattern[str]
    category: str
    code: str
    severity: str
    tool: str
    attendee_visible: bool = False


_TOOL_FAILURE_RULES: tuple[_PatternRule, ...] = (
    _PatternRule(
        re.compile(r"(?i)not supported for this model|web[_ -]?search.*400"),
        category="tool unavailable",
        code="web_search_unsupported",
        severity="degraded",
        tool="web_search",
        attendee_visible=True,
    ),
    _PatternRule(
        re.compile(r"(?i)API Error:\s*400\b"),
        category="model/gateway",
        code="gateway_400_error",
        severity="degraded",
        tool="gateway",
        attendee_visible=True,
    ),
    _PatternRule(
        re.compile(r"(?i)Permission denied by hook|policy_eval_relay_failure"),
        category="Omnigent",
        code="omnigent_hook_denied",
        severity="blocked",
        tool="omnigent",
        attendee_visible=True,
    ),
    _PatternRule(
        re.compile(r"(?i)INSUFFICIENT_PERMISSIONS|PERMISSION_DENIED|does not have (?:USE CATALOG|USE SCHEMA|SELECT)"),
        category="permissions/grants",
        code="uc_permission_denied",
        severity="degraded",
        tool="sql",
        attendee_visible=True,
    ),
    _PatternRule(
        re.compile(r"(?i)profile .* not found|has no .* profile configured"),
        category="auth/CLI profile",
        code="cli_profile_not_found",
        severity="degraded",
        tool="cli",
        attendee_visible=False,
    ),
    _PatternRule(
        re.compile(r"(?i)OAuth is not configured for this host|no cached credentials"),
        category="auth/CLI profile",
        code="cli_oauth_not_configured",
        severity="degraded",
        tool="cli",
        attendee_visible=False,
    ),
    _PatternRule(
        re.compile(r"(?i)no browser renderer|browser inspection is disabled|browser_unavailable"),
        category="tool unavailable",
        code="browser_unavailable",
        severity="degraded",
        tool="browser",
        attendee_visible=False,
    ),
    _PatternRule(
        re.compile(r"(?i)\.bundle/.*state does not exist|bundle deploy"),
        category="workshop tooling defect",
        code="bundle_deploy_attempted",
        severity="noise",
        tool="bundle",
        attendee_visible=False,
    ),
)

_lock = threading.Lock()
_emitted_counts: dict[tuple[str, str], int] = {}


def emit_friction(
    attendee: str,
    *,
    source: str,
    category: str,
    code: str,
    severity: str = "degraded",
    attendee_visible: bool = False,
    auto_recovered: bool = False,
    agent: str = "",
    tool: str = "",
    evidence: str = "",
    count: int = 1,
    fingerprint: str | None = None,
) -> bool:
    """Emit a privacy-safe ``friction.event`` record to OTel and Control Tower."""
    cat = category if category in FRICTION_TAXONOMY else "workshop tooling defect"
    safe_evidence = clip(redact(evidence), 300)
    fp = fingerprint or hashlib.sha256(
        f"{attendee}:{source}:{cat}:{code}:{agent}:{tool}".encode("utf-8")
    ).hexdigest()[:24]
    payload: dict[str, Any] = {
        "source": source,
        "category": cat,
        "code": code,
        "severity": severity,
        "attendee_visible": bool(attendee_visible),
        "auto_recovered": bool(auto_recovered),
        "agent": agent,
        "tool": tool,
        "evidence": safe_evidence,
        "count": max(1, int(count)),
        "fingerprint": fp,
    }
    return telemetry.emit("friction.event", attendee or "system", payload)


def scan_error_lines(
    attendee: str,
    error_lines: list[str],
    *,
    agent: str = "",
) -> list[dict[str, Any]]:
    """Classify redacted tool-error first-lines and emit new occurrences."""
    counts_by_rule: dict[int, tuple[_PatternRule, int, str]] = {}
    for line in error_lines:
        for idx, rule in enumerate(_TOOL_FAILURE_RULES):
            if rule.pattern.search(line):
                prev = counts_by_rule.get(idx)
                if prev is None:
                    counts_by_rule[idx] = (rule, 1, line)
                else:
                    counts_by_rule[idx] = (rule, prev[1] + 1, prev[2])
                break

    emitted: list[dict[str, Any]] = []
    for _idx, (rule, total, sample) in counts_by_rule.items():
        key = (attendee, rule.code)
        with _lock:
            prior = _emitted_counts.get(key, 0)
            delta = total - prior
            if delta <= 0:
                continue
            _emitted_counts[key] = total
        emit_friction(
            attendee,
            source="wt.agent_tool",
            category=rule.category,
            code=rule.code,
            severity=rule.severity,
            attendee_visible=rule.attendee_visible,
            agent=agent,
            tool=rule.tool,
            evidence=sample,
            count=delta,
        )
        emitted.append({"code": rule.code, "category": rule.category, "tool": rule.tool, "count": delta})
    return emitted


def reset_for_tests() -> None:
    with _lock:
        _emitted_counts.clear()
