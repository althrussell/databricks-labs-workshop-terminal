"""The attendee's build, made visible (W4).

One compact "Your build" view per attendee, built only from structured state:
the workflow record (W2), the evidence the browser recorded (W3), the help
request, and git. Missing events read as unknown; nothing is inferred from
terminal output, and there is no invented percent-complete.

Facilitators get coarse per-seat signals (needs a decision / ready to try /
blocked / help requested) on the presence response CT already polls. That is
the whole facilitator view: no keystrokes, prompts or reasoning leave the seat.

Export (WT-P6) runs the same ``workshop-export`` helper an agent runs, so the
volume copy keeps its exact path and name, which CT copies at teardown.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import threading
import time
from typing import Any

from . import design_workflow, projects

#: Coarse facilitator signals. ``working`` covers everything else.
STATUSES = ("not_started", "needs_decision", "ready_to_try", "blocked", "help_requested", "working")
_BLOCKING_GAPS = {"sign_in_blocked", "page_did_not_load", "app_not_found", "browser_failed_to_start"}
EXPORT_REUSE_SECONDS = 300
_exports: dict[str, dict] = {}
_export_lock = threading.Lock()
_emitted: dict[str, tuple] = {}


def active_project(user) -> Path | None:
    name = projects.read(user)["active"]
    if not name:
        return None
    path = projects.project_path(user, name)
    return path if path.is_dir() else None


def what_changed(project: Path, limit: int = 5) -> list[str]:
    """The latest commit subjects: what the attendee's build changed, in its own words."""
    try:
        done = subprocess.run(["git", "-C", str(project), "log", f"-{limit}", "--format=%s", "--", ".",
                               ":(exclude).workshop"], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return []
    return [line for line in done.stdout.splitlines() if line.strip()] if done.returncode == 0 else []


def _help_raised() -> bool:
    try:
        from . import help as help_module

        return bool(help_module.presence_fields().get("help_raised"))
    except Exception:  # noqa: BLE001 — an unready help store is "not raised", never an error
        return False


def _effective_record(project: Path, record: dict) -> dict:
    """Infer build progression when the project has already been scaffolded by workshop-init-project."""
    scaffolded = (
        (project / ".workshop/preparation.json").is_file()
        or (project / "PRODUCT.md").is_file()
        or (project / "DESIGN.md").is_file()
    )
    if not scaffolded:
        return record
    open_questions = [q for q in record.get("questions", []) if q.get("answer") is None]
    if open_questions:
        return record
    effective = dict(record)
    path_info = dict(record.get("path") or {})
    if not path_info.get("value"):
        path_info = {"value": "rapid", "chosen_by": "attendee", "source": "chat", "at": None}
        effective["path"] = path_info
    chosen = path_info.get("value")
    direction = record.get("direction") or {}
    if effective.get("phase") in {"understand", "choose_path"} and (
        chosen == "rapid" or (chosen == "guided" and direction.get("confirmed_by") == "attendee")
    ):
        effective["phase"] = "build"
    elif effective.get("phase") == "agree" and chosen == "guided" and direction.get("confirmed_by") == "attendee":
        effective["phase"] = "build"
    return effective


def status_for(record: dict, verification: dict, problems: list[str], *, help_raised: bool) -> str:
    path, phase = (record.get("path") or {}).get("value"), record.get("phase")
    open_questions = [q for q in record.get("questions", []) if q.get("answer") is None]
    direction = record.get("direction") or {}
    if help_raised:
        return "help_requested"
    gaps = record.get("evidence", {}).get("inspect", {}).get("gaps", [])
    if problems or (gaps and gaps[-1].get("code") in _BLOCKING_GAPS and verification.get("state") != "verified"):
        return "blocked"
    if (not path or open_questions or (path == "guided" and phase == "agree"
                                       and direction.get("confirmed_by") != "attendee")):
        return "needs_decision"
    preview = record.get("evidence", {}).get("build", {}).get("preview")
    if preview and (
        phase == "feedback"
        or (phase in {"inspect", "improve"} and verification.get("state") == "verified")
    ):
        return "ready_to_try"
    return "working"


def view(user, *, session=None) -> dict:
    """Everything the "Your build" area shows, for this attendee's project only."""
    project = active_project(user)
    state = projects.read(user)
    if project is None:
        return {"project": state["active"] or None, "status": "not_started", "phase": "understand",
                "path": None, "next_decision": "choose or name a build to start",
                "goal": state["goals"].get(state["active"], "") if state["active"] else "",
                "review": {"state": "none"}, "deploy": {"state": "not_deployed", "preview_url": None},
                "what_changed": [], "open_questions": [], "requests": [], "session": _session(session),
                "updated_at": None}
    raw_record = design_workflow.read(project)
    record = _effective_record(project, raw_record)
    verification = design_workflow.verification(record, project)
    problems = design_workflow.check(record, project)
    preview = record.get("evidence", {}).get("build", {}).get("preview")
    history = record.get("history") or []
    goal = state["goals"].get(project.name, "")
    if not goal:
        facts = [f.get("text", "") for f in record.get("facts", []) if isinstance(f, dict) and f.get("text")]
        if facts:
            goal = facts[0]
    return {
        "project": project.name,
        "goal": goal,
        "phase": record["phase"],
        "path": record["path"].get("value"),
        "status": status_for(record, verification, problems, help_raised=_help_raised()),
        "next_decision": design_workflow.workflow.next_step(record),
        "direction": record.get("direction"),
        "open_questions": [{"id": q["id"], "text": q["text"]} for q in record.get("questions", [])
                           if q.get("answer") is None],
        "review": verification,
        "problems": problems,
        # Deployment is tracked apart from review: a live preview can coexist
        # with review in progress, known defects or unverified checks.
        "deploy": {"state": "deployed" if preview else "not_deployed", "preview_url": preview},
        "what_changed": what_changed(project),
        "requests": [r for r in record.get("requests", []) if r.get("status") == "open"],
        "revision": record["revision"],
        "session": _session(session),
        "updated_at": history[-1]["at"] if history else None,
    }


def _session(session) -> dict:
    if session is None or getattr(session, "exited", True):
        return {"active": False}
    return {"active": True, "agent": session.agent_id, "label": session.label}


def journey(user) -> dict:
    """The facilitator's compact signal for one seat."""
    current = view(user)
    return {key: current.get(key) for key in ("phase", "path", "status", "next_decision", "updated_at")}


_phase_since: dict[str, tuple[str, float]] = {}
_outcomes: dict[str, str] = {}


def emit_if_changed(email: str, signal: dict) -> bool:
    """A compact ``journey.status`` event when the seat's signal changes (for CT insight).

    A phase change also emits ``journey.phase`` with the coarse time spent in
    the previous phase, so an operator can see where a seat's time went
    without anyone recording keystrokes.
    """
    key = (signal.get("phase"), signal.get("path"), signal.get("status"))
    if _emitted.get(email) == key:
        return False
    _emitted[email] = key
    from . import telemetry

    now = time.monotonic()
    previous = _phase_since.get(email)
    if previous is None or previous[0] != key[0]:
        if previous is not None:
            telemetry.emit("journey.phase", email, {"from": previous[0], "to": key[0], "path": key[1],
                                                    "seconds": int(now - previous[1])})
        _phase_since[email] = (str(key[0]), now)
    telemetry.emit("journey.status", email, {"phase": key[0], "path": key[1], "status": key[2],
                                              "outcome": "observed"})
    return True


def emit_outcome_if_changed(user) -> bool:
    """``journey.outcome`` once per new attendee account of their outcome (W6 measures)."""
    project = active_project(user)
    if project is None:
        return False
    outcome = design_workflow.read(project).get("outcome")
    if not isinstance(outcome, dict):
        return False
    stamp = f"{project.name}:{outcome.get('at')}"
    if _outcomes.get(user.email) == stamp:
        return False
    _outcomes[user.email] = stamp
    from . import telemetry

    telemetry.emit("journey.outcome", user.email, {key: outcome.get(key) for key in (
        "goal_explained", "decisions_shaped", "main_task", "second_change", "knows_next_step",
        "confidence", "by")})
    return True


# ---------------------------------------------------------------------------
# Attendee actions
# ---------------------------------------------------------------------------

def add_request(project: Path, kind: str, text: str) -> dict:
    """Queue a change or explanation for the agent's next safe boundary."""
    if kind not in {"change", "explain"}:
        raise ValueError("request is change or explain")
    text = text.strip()
    if not text:
        raise ValueError("say what you want changed or explained")
    workflow = design_workflow.workflow
    record = workflow.load(project)
    base = record["revision"]
    requests = record.setdefault("requests", [])
    entry = {"id": f"r{len(requests) + 1}", "kind": kind, "text": text[:1000], "by": "attendee",
             "status": "open", "at": workflow.now()}
    requests.append(entry)
    if kind == "change":
        record["brief_revision"] = record.get("brief_revision", 0) + 1
    workflow.save(project, record, expected_revision=None, base_revision=base)
    return entry


def decide(project: Path, *, question_id: str | None, answer: str | None, confirm_direction: bool,
           path: str | None) -> dict:
    """Resolve a pending decision from the UI into the same record chat uses."""
    workflow = design_workflow.workflow
    record = workflow.load(project)
    base = record["revision"]
    if path:
        workflow.choose_path(record, path, by="attendee", source="ui")
    if question_id:
        question = next((q for q in record["questions"] if q["id"] == question_id), None)
        if question is None:
            raise ValueError(f"no question {question_id}")
        if question.get("answer") is not None:
            raise ValueError("that question is already answered")
        question.update(answer=(answer or "").strip(), answered_by="attendee", answered_at=workflow.now())
        record["brief_revision"] = record.get("brief_revision", 0) + 1
    if confirm_direction:
        direction = record.get("direction")
        if not isinstance(direction, dict) or not direction.get("text"):
            raise ValueError("there is no proposed direction to confirm yet")
        direction.update(confirmed_by="attendee", confirmed_at=workflow.now())
        record["brief_revision"] = record.get("brief_revision", 0) + 1
    return workflow.save(project, record, expected_revision=None, base_revision=base)


def stop(session) -> dict:
    """Interrupt the agent's current step, and say exactly what that does."""
    if session is None or session.exited:
        return {"stopped": False, "detail": "No agent is running."}
    session.write_keystrokes("\x1b")
    return {"stopped": True, "agent": session.agent_id,
            "detail": (f"Sent Esc to {session.label}: it stops its current step and waits for you. "
                       "Anything it already started in Databricks, such as a deploy or a query, may still finish.")}


# ---------------------------------------------------------------------------
# Export (WT-P6)
# ---------------------------------------------------------------------------

def export(user, *, reason: str = "attendee", reuse: bool = False) -> dict:
    """Run ``workshop-export`` as the attendee; reuse a recent export when asked."""
    with _export_lock:
        cached = _exports.get(user.email)
        if reuse and cached and time.time() - cached["at"] < EXPORT_REUSE_SECONDS:
            return {**cached["result"], "reused": True}
        try:
            from . import browser_evidence

            if browser_evidence.browser_binary(allow_system_capture=True):
                root = projects.root(user)
                if root.is_dir():
                    for child in sorted(root.iterdir()):
                        if child.is_dir() and not child.is_symlink() and not child.name.startswith("."):
                            browser_evidence.capture_project_preview_screenshot(user, child)
        except Exception:  # noqa: BLE001 — screenshot capture never blocks code export
            pass
        helper = os.path.join(user.home, ".local", "bin", "workshop-export")
        if not os.path.isfile(helper):
            helper = os.path.join(os.path.dirname(__file__), "..", "assets", "bin", "workshop-export")
        done = subprocess.run([helper, "--json"], capture_output=True, text=True, timeout=600,
                              env={**user.shell_env(), "WORKSHOP_EXPORT_REASON": reason}, cwd=user.home)
        if done.returncode != 0:
            raise RuntimeError((done.stderr or done.stdout or "export failed").strip()[-600:])
        result = json.loads(done.stdout.strip().splitlines()[-1])
        local = result.get("local_zip", "")
        result = {**result, "path": result.get("volume_path"),
                  "bytes": os.path.getsize(local) if local and os.path.isfile(local) else 0, "reused": False}
        _exports[user.email] = {"at": time.time(), "result": result}
        return result


def has_projects(user) -> bool:
    root = projects.root(user)
    return root.is_dir() and any(child.is_dir() and not child.name.startswith(".") for child in root.iterdir())


def reset_for_tests() -> None:
    _exports.clear()
    _emitted.clear()
    _phase_since.clear()
    _outcomes.clear()


__all__ = ["STATUSES", "add_request", "decide", "emit_if_changed", "export", "has_projects", "journey",
           "status_for", "stop", "view"]
