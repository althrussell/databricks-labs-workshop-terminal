"""Paired journeys across harnesses and both paths (W6).

Six briefs (vague problem, precise request, consequential rule, existing
branded app, multi-step workflow, follow-up and resume) × every qualified
harness × rapid and guided.

``smoke`` is CI-safe: it drives the real ``workshop-workflow`` helper the way an
agent would, with no model call, and checks the consistency guarantees the
product makes regardless of which model answers:

- path selection is honoured, and only the attendee chooses it;
- guided cannot build before the attendee confirms a direction;
- an inferred fact stays an assumption until the attendee confirms it;
- a settled question is not asked again;
- the brief and stage survive an agent switch (a fresh process resumes from the
  record) and a rapid↔guided switch keeps every fact;
- an unsupported capability is disclosed as a gap, never a pass;
- the outcome is the attendee's account, given at feedback.

``python -m evals.generated_apps.pilot`` runs the broader live pilot by hand.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

SCENARIOS = Path(__file__).parent / "scenarios" / "paired"
HELPER = Path(__file__).resolve().parents[2] / "assets" / "bin" / "workshop-workflow"
#: Harnesses and the skill provider each one discovers. Omnigent drives
#: Claude Code, so it is qualified through the Claude path.
HARNESSES = {"claude": "claude", "codex": "codex", "omnigent": "claude"}
PATHS = ("rapid", "guided")


def scenarios() -> list[dict]:
    return [json.loads(path.read_text()) for path in sorted(SCENARIOS.glob("*.json"))]


class _Workflow:
    """One agent process's view: every call is a fresh helper run against the record."""

    def __init__(self, project: Path, harness: str) -> None:
        self.project, self.harness = project, harness

    def run(self, *args: str, ok: bool = True) -> subprocess.CompletedProcess:
        env = {**os.environ, "WORKSHOP_USER_EMAIL": "attendee@example.com", "WORKSHOP_HARNESS": self.harness}
        result = subprocess.run([sys.executable, str(HELPER), "--project", str(self.project), *args],
                                capture_output=True, text=True, env=env, timeout=60)
        if ok and result.returncode:
            raise AssertionError(f"{self.harness}: workshop-workflow {' '.join(args)} failed: {result.stderr.strip()}")
        return result

    def record(self) -> dict:
        return json.loads(self.run("show", "--json").stdout)


def smoke(scenario: dict, harness: str, path: str, project: Path) -> dict:
    """Run one scripted journey; returns the invariants and whether each held."""
    subprocess.run(["git", "init", "-q", str(project)], check=True)
    first, second = harness, ("codex" if harness != "codex" else "claude")
    agent = _Workflow(project, first)
    checks: dict[str, bool] = {}

    refused = agent.run("path", path, "--by", "agent", ok=False)
    checks["only the attendee chooses the path"] = refused.returncode != 0
    agent.run("path", path, "--by", "attendee", "--source", scenario["entry"])
    for fact in scenario["attendee_facts"]:
        agent.run("fact", fact, "--by", "attendee")
    agent.run("fact", "The app needs sign-in for every user.")  # inferred by the agent
    record = agent.record()
    checks["an inferred fact stays an assumption"] = any(
        a["text"].startswith("The app needs sign-in") for a in record["assumptions"])
    if scenario.get("question"):
        question_id = agent.run("ask", scenario["question"]["q"]).stdout.strip().splitlines()[0]
        agent.run("answer", question_id, scenario["question"]["a"])
    agent.run("phase", "choose_path")
    if path == "guided":
        early = agent.run("phase", "build", ok=False)
        checks["guided cannot build before an agreed direction"] = early.returncode != 0
        agent.run("phase", "shape")
        agent.run("phase", "agree")
        agent.run("direction", scenario["brief"], "--confirmed-by", "attendee")

    # An agent switch: the other harness resumes from the committed record.
    resumed = _Workflow(project, second)
    before = agent.record()
    after = resumed.record()
    checks["brief and stage survive an agent switch"] = (
        after["phase"] == before["phase"] and after["facts"] == before["facts"]
        and after["path"]["value"] == path)
    answered = {q["text"] for q in after["questions"] if q.get("answer")}
    checks["settled questions are not asked again"] = len(answered) == len(
        [q for q in after["questions"] if q.get("answer")])

    # Switching path keeps everything the attendee said.
    other = "guided" if path == "rapid" else "rapid"
    resumed.run("path", other, "--by", "attendee", "--source", "chat")
    switched = resumed.record()
    checks["a path switch keeps every fact"] = switched["facts"] == before["facts"]
    resumed.run("path", path, "--by", "attendee", "--source", "chat")
    if path == "guided" and not resumed.record().get("direction", {}) and scenario.get("brief"):
        resumed.run("direction", scenario["brief"], "--confirmed-by", "attendee")

    resumed.run("phase", "build", "--evidence", "build=apx build passed", "--evidence", "preview=https://example.invalid/app")
    resumed.run("phase", "inspect", "--evidence", "preview=https://example.invalid/app",
                "--evidence", "gap=browser unavailable in this smoke run")
    inspected = resumed.record()
    gaps = inspected["evidence"].get("inspect", {}).get("gaps") or []
    checks["an unsupported capability is a disclosed gap"] = bool(gaps) and not inspected["evidence"]["inspect"].get("artifacts")
    checks["no review is claimed without evidence"] = "checked" not in inspected["evidence"].get("inspect", {})
    early_outcome = resumed.run("outcome", "--by", "attendee", "--goal", "yes", "--decisions", "yes",
                                "--main-task", "yes", "--second-change", "requested", "--next-step", "yes",
                                "--confidence", "4", ok=False)
    checks["the outcome waits for feedback"] = early_outcome.returncode != 0
    resumed.run("phase", "feedback", "--evidence", "checked=primary action and reload")
    by_agent = resumed.run("outcome", "--by", "agent", "--goal", "yes", "--decisions", "yes", "--main-task", "yes",
                           "--second-change", "made", "--next-step", "yes", "--confidence", "5", ok=False)
    checks["the agent cannot report the attendee's outcome"] = by_agent.returncode != 0
    resumed.run("outcome", "--by", "attendee", "--goal", "yes", "--decisions", "partly", "--main-task", "yes",
                "--second-change", "requested", "--next-step", "yes", "--confidence", "4")
    outcome = resumed.record().get("outcome") or {}
    checks["the attendee's own account is kept"] = outcome.get("by") == "attendee" and outcome.get("decisions_shaped") == "partly"
    return {"scenario": scenario["scenario_id"], "harness": harness, "provider": HARNESSES[harness],
            "path": path, "checks": checks, "passed": all(checks.values())}


def run_smoke(selected: list[str] | None = None) -> list[dict]:
    results = []
    for scenario in scenarios():
        if selected and scenario["scenario_id"] not in selected:
            continue
        for harness in HARNESSES:
            for path in PATHS:
                with tempfile.TemporaryDirectory(prefix="wt-paired-") as temporary:
                    results.append(smoke(scenario, harness, path, Path(temporary)))
    return results


if __name__ == "__main__":
    outcome = run_smoke(sys.argv[1:] or None)
    failed = [row for row in outcome if not row["passed"]]
    print(json.dumps({"runs": len(outcome), "failed": failed}, indent=2))
    sys.exit(1 if failed else 0)
