#!/usr/bin/env python3
"""Open normal attendee login; save private state only for the exact assigned user."""
import argparse
import asyncio
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.run_generated_app_journey import validate_receipt
from evals.generated_apps.report import write_evidence


async def authenticate(receipt_path, state_path, evidence_path, *, prior_state=None, timeout_seconds=600):
    from playwright.async_api import async_playwright
    if not 1 <= timeout_seconds <= 600:
        raise ValueError("Sign-in wait must be bounded to ten minutes")
    for path in (state_path, evidence_path):
        if Path(path).exists() or Path(path).is_symlink():
            raise ValueError("Fresh state and evidence outputs required")
    if len({Path(x).resolve() for x in (receipt_path, state_path, evidence_path)}) != 3:
        raise ValueError("Distinct input/state/evidence paths required")
    raw = Path(receipt_path).read_bytes()
    binding = validate_receipt(json.loads(raw))
    result = {"schema_version": 1, "control_tower_requests": 0, "status": "awaiting_normal_attendee_sign_in",
        "app_id": binding["app"]["id"], "expected_email": binding["attendee"]["email"],
        "operator_bearer_in_browser": False, "authentication_state_in_evidence": False}
    write_evidence(evidence_path, result)
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        context = await browser.new_context(storage_state=prior_state, viewport={"width": 1440, "height": 900})
        page = await context.new_page()
        done = asyncio.Event()
        tasks = set()
        async def observe(response):
            if response.url != binding["app"]["url"] + "/api/config" or not response.ok or done.is_set(): return
            body = await response.json()
            user = body.get("user", {}).get("email", "").casefold()
            if user != binding["attendee"]["email"].casefold():
                result["wrong_identity_observed"] = True; write_evidence(evidence_path, result); return
            state = await context.storage_state()
            fd = os.open(state_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as handle: json.dump(state, handle)
            result.update(status="genuine_attendee_identity_verified", identity=user)
            write_evidence(evidence_path, result); done.set()
        def on_response(response):
            task = asyncio.create_task(observe(response)); tasks.add(task); task.add_done_callback(tasks.discard)
        page.on("response", on_response)
        try:
            await page.goto(binding["app"]["url"], wait_until="domcontentloaded", timeout=60000)
            print(json.dumps({"status": "browser_open", "app_url": binding["app"]["url"]}), flush=True)
            await asyncio.wait_for(done.wait(), timeout=timeout_seconds)
        except asyncio.TimeoutError:
            result["status"] = "normal_sign_in_timeout"; write_evidence(evidence_path, result)
        finally:
            if tasks: await asyncio.gather(*tasks, return_exceptions=True)
            await browser.close()
    return result


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--receipt", required=True)
    p.add_argument("--state-output", required=True)
    p.add_argument("--evidence-output", required=True)
    p.add_argument("--prior-state")
    args = p.parse_args(argv)
    result = asyncio.run(authenticate(args.receipt, args.state_output, args.evidence_output, prior_state=args.prior_state))
    print(json.dumps({"status": result["status"]}))
    return 0 if result["status"] == "genuine_attendee_identity_verified" else 2


if __name__ == "__main__":
    raise SystemExit(main())
