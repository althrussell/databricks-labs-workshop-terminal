"""The broader live pilot (W6), run by hand before an event rollout.

``plan`` prints the run matrix: every paired brief × qualified harness × both
paths, repeated to expose variation, with the rubric each reviewer scores.
``report results.jsonl`` aggregates what reviewers recorded. It keeps functional
evidence apart from blinded visual judgement, and counts incomplete runs
instead of dropping them.

Pilot size follows the event's needs; nothing here mandates a 36/72-build
experiment, and there is no Impeccable-removal comparison. Guided ideation is
judged on ambiguous problems and rapid prototyping on well-specified work.

A result line is one run:

    {"scenario": "...", "harness": "claude|codex|omnigent", "path": "rapid|guided",
     "run": 1, "complete": true,
     "consistency": {"<check>": true},          # from the workflow record and journey.status
     "design_thinking": {"<check>": true},
     "product": {"<check>": true},              # actual data and actions, in the deployed app
     "visual": {"reviewer_blind": true, "hierarchy": 1-5, "density": 1-5,
                "coherence": 1-5, "responsive": 1-5, "accessibility": 1-5},
     "outcome": {...},                          # the attendee's journey.outcome
     "evidence": ["<screenshot or record path>"]}
"""
from __future__ import annotations

import argparse
from collections import defaultdict
import json
from pathlib import Path
import statistics
import sys

from .paired import HARNESSES, PATHS, scenarios

RUBRIC = {
    "consistency": ["path selection honoured", "the right Impeccable workflow ran",
                    "no settled question asked again", "brief and stage survived an agent switch",
                    "unsupported capabilities disclosed"],
    "design_thinking": ["identified the job and the friction", "asked questions whose answers changed the design",
                        "synthesised answers", "offered substantive options where useful",
                        "explained its recommendation", "recorded the attendee's real choices"],
    "product": ["the intended task can be completed", "the next action is findable",
                "relevant information can be compared", "mistakes can be recovered from",
                "feedback improved the result"],
    "visual": ["hierarchy", "density", "coherence", "responsive", "accessibility"],
    "attendee": ["can explain their own goal and direction", "their decisions affected the build",
                 "can use the main task and agent behaviour", "made or requested a second change",
                 "knows how to continue"],
}


def plan(repeats: int) -> list[dict]:
    return [{"scenario": s["scenario_id"], "brief": s["brief"], "entry": s["entry"], "harness": harness,
             "path": path, "run": run, "judged_on": s["evaluated_path"] == path}
            for s in scenarios() for harness in HARNESSES for path in PATHS for run in range(1, repeats + 1)]


def report(lines: list[dict], expected: int | None = None) -> dict:
    """Aggregate pilot results honestly: incomplete runs stay visible; visual is scored apart."""
    groups: dict[tuple[str, str, str], list[dict]] = defaultdict(list)
    for line in lines:
        groups[(line["scenario"], line["harness"], line["path"])].append(line)
    rows = []
    for (scenario, harness, path), runs in sorted(groups.items()):
        complete = [run for run in runs if run.get("complete")]
        functional = {section: _rate(complete, section) for section in ("consistency", "design_thinking", "product")}
        visual = [run.get("visual") or {} for run in complete if (run.get("visual") or {}).get("reviewer_blind")]
        scores = [statistics.mean(v[key] for key in RUBRIC["visual"] if isinstance(v.get(key), (int, float)))
                  for v in visual if any(isinstance(v.get(key), (int, float)) for key in RUBRIC["visual"])]
        rows.append({"scenario": scenario, "harness": harness, "path": path, "runs": len(runs),
                     "incomplete": len(runs) - len(complete), "functional_pass_rate": functional,
                     "visual_mean": round(statistics.mean(scores), 2) if scores else None,
                     "visual_spread": round(max(scores) - min(scores), 2) if len(scores) > 1 else None,
                     "unblinded_visual_ignored": sum(1 for run in complete if run.get("visual")
                                                     and not run["visual"].get("reviewer_blind"))})
    return {"runs": len(lines), "expected": expected, "missing": (expected - len(lines)) if expected else None,
            "groups": rows}


def _rate(runs: list[dict], section: str) -> float | None:
    values = [bool(ok) for run in runs for ok in (run.get(section) or {}).values()]
    return round(sum(values) / len(values), 2) if values else None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m evals.generated_apps.pilot", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    planned = sub.add_parser("plan")
    planned.add_argument("--repeats", type=int, default=2)
    reported = sub.add_parser("report")
    reported.add_argument("results", type=Path)
    reported.add_argument("--expected", type=int)
    args = parser.parse_args(argv)
    if args.command == "plan":
        print(json.dumps({"runs": plan(args.repeats), "rubric": RUBRIC}, indent=2))
        return 0
    lines = [json.loads(line) for line in args.results.read_text().splitlines() if line.strip()]
    print(json.dumps(report(lines, args.expected), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
