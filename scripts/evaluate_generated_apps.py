#!/usr/bin/env python3
"""Control Tower-aware entry point for the first generated-app baseline cell.

Discovery is read-only. Draft/provision operations require exact owned single-seat
inputs and explicit --execute. Browser entry needs a genuine attendee login state.
This first slice records missing acceptance checks instead of calling a UI launch
or an infrastructure gate a successful app build.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from evals.generated_apps.adapters.control_tower import assess_target, select_target
from evals.generated_apps.adapters.ct_client import ControlTowerClient
from evals.generated_apps.report import RunReport, validate_budgets, write_evidence
from evals.generated_apps.simulator import NoviceSimulator, SimulatorBudget, load_simulator_scenario


def read_json(path):
    return json.loads(Path(path).read_text())


def validate_paths(args):
    """Never overwrite run inputs or private authentication state with evidence."""
    inputs = {Path(getattr(args, name)).resolve() for name in (
        "ownership", "create_spec", "inventory", "observations", "wt_browser_state", "budget"
    ) if getattr(args, name, None)}
    outputs = [Path(args.output).resolve()]
    if args.command == "prepare":
        outputs.append(Path(args.output).with_suffix(".ownership.json").resolve())
    if getattr(args, "export_inventory", None):
        outputs.append(Path(args.export_inventory).resolve())
    if len(set(outputs)) != len(outputs) or inputs.intersection(outputs):
        raise ValueError("Evidence destinations must be distinct from all run inputs")


def preflight_passed(result):
    """A malformed count or contradictory failed check cannot authorize spend."""
    checks = result.get("checks")
    return bool(
        type(result.get("blocking_failures")) is int and result["blocking_failures"] == 0
        and result.get("status") == "ready_to_provision"
        and isinstance(checks, list) and checks
        and all(isinstance(c, dict) and c.get("status") in {"pass", "warning"} for c in checks)
    )


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="command", required=True)
    for name in ("discover", "prepare", "poll"):
        command = sub.add_parser(name)
        command.add_argument("--profile", required=True)
        command.add_argument("--ct-url", required=True)
        command.add_argument("--output", required=True)
        if name in {"prepare", "poll"}:
            command.add_argument("--ownership", required=True)
        if name == "prepare":
            command.add_argument("--create-spec", required=True)
            command.add_argument("--execute", action="store_true")
            command.add_argument("--provision", action="store_true")
        if name == "poll":
            command.add_argument("--unit-id")
            command.add_argument("--export-inventory")
    plan = sub.add_parser("plan")
    plan.add_argument("--inventory", required=True)
    plan.add_argument("--instance", required=True)
    plan.add_argument("--output", required=True)
    preflight = sub.add_parser("preflight")
    preflight.add_argument("--inventory", required=True)
    preflight.add_argument("--instance", required=True)
    preflight.add_argument("--observations", required=True, help="Independently collected CT/attendee capability evidence")
    preflight.add_argument("--attendee-present", action="store_true")
    preflight.add_argument("--output", required=True)
    entry = sub.add_parser("entry")
    entry.add_argument("--inventory", required=True)
    entry.add_argument("--instance", required=True)
    entry.add_argument("--observations", required=True)
    entry.add_argument("--wt-browser-state", required=True, help="Private Playwright attendee auth state, never included in reports")
    entry.add_argument("--agent", required=True)
    entry.add_argument("--entry-path", choices=["wizard", "skip_wizard", "wizard_disabled"], default="wizard")
    entry.add_argument("--allow-industry-step", action="store_true")
    entry.add_argument("--industry")
    entry.add_argument("--budget", required=True)
    entry.add_argument("--output", required=True)
    return p


async def enter(args, report):
    from evals.generated_apps.adapters.browser import AuthenticatedBrowser, BrowserLaunchConfig, WorkshopBrowserDriver
    target = select_target(read_json(args.inventory), instance_name=args.instance)
    gate = assess_target(target, read_json(args.observations), attendee_present=True)
    report.provenance = {"target": target, "environment_gate": gate}
    if not gate["execution_ready"]:
        report.add("attendee_execution_ready", "failed", "CT/attendee execution prerequisites are not verified", args.observations)
        return
    report.add("attendee_execution_ready", "passed", "Independently observed CT/attendee gates", args.observations)
    budget = validate_budgets(read_json(args.budget))
    scenario = load_simulator_scenario()
    sim = NoviceSimulator(scenario, SimulatorBudget(max_replies=10, **{
        "total_seconds": budget["total_seconds"], "consultation_seconds": budget["consultation_seconds"]}))
    opening = sim.opening().text
    async with AuthenticatedBrowser(args.wt_browser_state) as browser:
        driver = WorkshopBrowserDriver(browser.page)
        evidence = await driver.enter(BrowserLaunchConfig(
            wt_url=target["url"], agent_id=args.agent, opening_message=opening,
            expected_attendee_email=target["attendee_email"], entry_path=args.entry_path,
            industry=args.industry, allow_industry_step=args.allow_industry_step,
            deadline_seconds=min(budget["total_seconds"], 180),
            run_deadline_seconds=budget["total_seconds"],
        ))
        # Preserve the exact created session before optional artifacts can fail.
        report.artifacts["entry"] = evidence.to_dict()
        folder = Path(args.output).parent / (report.run_id + "-artifacts")
        try:
            folder.mkdir(parents=True, exist_ok=True)
            await browser.page.screenshot(path=str(folder / "wt-entry.png"), full_page=True)
            # Only synthetic run evidence; no browser storage/cookies.
            report.artifacts["transport"] = driver.harness.evidence()
            report.artifacts["simulator"] = sim.evidence()
            report.artifacts["browser_screenshot"] = str(folder / "wt-entry.png")
            report.artifacts["budget"] = budget
            report.add("normal_ui_entry", "failed" if evidence.error_code else "passed",
                       evidence.error_detail or "Goal entered through normal WT UI", str(folder / "wt-entry.png"))
            report.add("requirements_and_advice", "unverified",
                       "PTY does not establish complete assistant turns; consultation has not been scored")
            report.add("budget", "unverified", "Configured limits alone are not enforcement evidence")
        finally:
            # Never leave a coding session unattended when only probing entry.
            if evidence.session_id:
                agent_label = next((a.get("label", "") for a in driver._agents or [] if a.get("id") == args.agent), "")
                try:
                    await driver.close_launched_session(agent_label)
                    report.add("session_cleanup", "passed", "Closed only the UI-created evaluation session", "entry.session_id")
                except Exception:
                    report.add("session_cleanup", "failed", "The evaluation session needs explicit operator cleanup")


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        validate_paths(args)
    except ValueError as error:
        print(json.dumps({"error_type": type(error).__name__, "verdict": "failed", "accepted": False}))
        return 2
    report = RunReport(str(uuid.uuid4()), args.command)
    operation_state = None
    try:
        if args.command in {"discover", "prepare", "poll"}:
            client = ControlTowerClient(args.ct_url, args.profile)
            if args.command == "discover":
                payload = client.discover()
                write_evidence(args.output, payload)
                print(json.dumps({"output": args.output, "runs": len(payload["runs"]), "existing_runs_disposable": False}))
                return 0
            ownership = read_json(args.ownership)
            if args.command == "prepare":
                create_spec = read_json(args.create_spec)
                # Prove both receipt destinations before any cloud mutation.
                receipt_path = str(Path(args.output).with_suffix(".ownership.json"))
                plan = client.create_draft(create_spec, ownership, execute=False)
                write_evidence(args.output, plan)
                write_evidence(receipt_path, plan["ownership"])
                payload = client.create_draft(create_spec, ownership, execute=args.execute)
                operation_state = payload
                # Persist ownership immediately so later failures cannot lose cleanup provenance.
                write_evidence(receipt_path, payload["ownership"])
                report.artifacts["ownership_receipt"] = receipt_path
                write_evidence(args.output, payload)
                if args.execute:
                    owned = payload["ownership"]
                    preflight = client.preflight(owned["run_id"], owned, execute=True)
                    payload["preflight"] = preflight
                    write_evidence(args.output, payload)
                    if args.provision and preflight_passed(preflight["result"]):
                        payload["provision"] = client.provision(owned["run_id"], owned, execute=True)
                write_evidence(args.output, payload)
                print(json.dumps({"output": args.output, "executed": payload["executed"], "result": payload.get("result"), "preflight": payload.get("preflight")}))
                if args.execute and not preflight_passed(payload.get("preflight", {}).get("result", {})):
                    return 1
                return 0
            owned = ownership.get("ownership", ownership)
            payload = client.owned_fetch_run(owned)
            if args.export_inventory:
                units = [u for u in payload["units"] if not args.unit_id or u["unit_id"] == args.unit_id]
                if len(units) != 1:
                    raise ValueError("Exact one-seat CT unit required for inventory export")
                target = client.export_target(payload["run"], units[0], disposable=True)
                write_evidence(args.export_inventory, {"schema_version": 1, "apps": [target]})
            write_evidence(args.output, payload)
            print(json.dumps({"output": args.output, "run_status": payload["run"]["status"], "units": [{"unit_id": u["unit_id"], "status": u["status"], "attendee": u["lab_user_email"]} for u in payload["units"]]}))
            return 0
        if args.command == "plan":
            target = select_target(read_json(args.inventory), instance_name=args.instance)
            payload = {"target": target, "mutations": [], "browser_identity_required": target["attendee_email"],
                       "accepted": False, "remaining": ["attendee execution proof", "structured harness turns", "real app build and independent acceptance"]}
            write_evidence(args.output, payload)
            print(json.dumps({"output": args.output, "mutations": 0}))
            return 0
        if args.command == "preflight":
            target = select_target(read_json(args.inventory), instance_name=args.instance)
            gate = assess_target(target, read_json(args.observations), attendee_present=args.attendee_present)
            report.provenance = {"target": target, "environment_gate": gate}
            report.add("attendee_execution_ready", "passed" if gate["execution_ready"] else "unverified",
                       "CT admission and attendee execution are separate gates", args.observations)
        elif args.command == "entry":
            asyncio.run(enter(args, report))
        write_evidence(args.output, report.to_dict())
        print(json.dumps({"output": args.output, "verdict": report.to_dict()["verdict"], "accepted": False}))
        return 0 if args.command == "preflight" and report.provenance["environment_gate"]["execution_ready"] else 1
    except Exception as error:
        # Default SDK and browser errors may contain URLs or tokens. Only stable type is public.
        if operation_state is not None:
            report.artifacts["operation_state"] = operation_state
        report.add("runner", "failed", "Runner operation failed: " + type(error).__name__)
        write_evidence(args.output, report.to_dict())
        print(json.dumps({"output": args.output, "verdict": "failed", "error_type": type(error).__name__}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
