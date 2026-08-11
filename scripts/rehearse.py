#!/usr/bin/env python3
"""Rehearse an event at its real width before the room fills.

Two instances passing acceptance says the contract holds. It does not say the
fleet does: the failures that matter at scale are the ones where every instance
is individually fine and the room is not — a credential that expires inside the
event window on half the fleet, an Omnigent app that only some instances can
reach, one attendee's diagnostics quietly missing because the collector never
started there.

This asks every instance the same questions at once and reports by exception.
Exit code is non-zero if any instance would fail an attendee, so it can gate a
release rather than inform one.

  export DATABRICKS_TOKEN=...        # admin principal, CAN_USE on every app
  rehearse.py --urls ./instances.txt --event-hours 8
  rehearse.py --urls ./instances.txt --json > rehearsal.json
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import sys

import requests

TIMEOUT = 45

# The checks whose failure means this instance cannot serve an attendee. Kept
# here rather than trusting ``ready`` alone so a soft check turning red never
# blocks a fleet, and so the reason is nameable in the report.
HARD_CHECKS = (
    "topology",
    "attendee_identity",
    "credentials",
    "credential_durability",
    "installers",
    "supply_chain",
    "session_state",
    "release_pins",
)


def _get(base: str, path: str, token: str, **params) -> dict:
    resp = requests.get(
        f"{base}{path}",
        headers={"Authorization": f"Bearer {token}"},
        params={k: v for k, v in params.items() if v not in ("", None)},
        timeout=TIMEOUT,
    )
    # /readyz answers 503 with the full report; that is the interesting case.
    if resp.status_code not in (200, 503):
        resp.raise_for_status()
    return resp.json()


def inspect(base: str, token: str, event_hours: float) -> dict:
    ready = _get(base, "/readyz", token)
    diagnostics = _get(base, "/api/admin/diagnostics", token, limit=20)
    checks = ready.get("checks") or {}
    durability = ready.get("durability") or {}
    collector = diagnostics.get("collector") or {}

    problems: list[str] = []
    for name in HARD_CHECKS:
        check = checks.get(name)
        if check is None:
            problems.append(f"{name}: not reported by this build")
        elif not check.get("ok"):
            problems.append(f"{name}: {check.get('detail', 'red')}")
    if not collector.get("running"):
        problems.append(
            "diagnostics collector is not running — a failure here would be "
            "invisible to the operator"
        )
    needed = event_hours * 3600
    app_expires = durability.get("app_credential_expires_in")
    if (
        not durability.get("app_plane_rotating")
        and isinstance(app_expires, (int, float))
        and app_expires < needed
    ):
        problems.append(
            f"app credential expires in {int(app_expires)}s, inside a "
            f"{event_hours:g}h event, and is not rotating"
        )
    if durability.get("attendee_plane_renewing") is False:
        problems.append("nothing is renewing the attendee credential")

    return {
        "url": base,
        "ready": bool(ready.get("ready")),
        "problems": problems,
        "durability": durability,
        "distinct_errors": len(diagnostics.get("errors") or []),
        "release_manifest": ready.get("release_manifest") or {},
    }


def _try(base: str, token: str, event_hours: float) -> dict:
    try:
        return inspect(base, token, event_hours)
    except requests.RequestException as error:
        return {
            "url": base,
            "ready": False,
            "problems": [f"unreachable: {str(error)[:160]}"],
            "durability": {},
            "distinct_errors": 0,
            "release_manifest": {},
        }


def _fleet_drift(results: list[dict]) -> list[str]:
    """Instances that disagree about what they are running.

    A fleet where one instance carries a different release is a fleet where one
    attendee has a different workshop, and it will be discovered by them.
    """
    manifests: dict[str, list[str]] = {}
    for result in results:
        key = json.dumps(result.get("release_manifest") or {}, sort_keys=True)
        manifests.setdefault(key, []).append(result["url"])
    if len(manifests) <= 1:
        return []
    ordered = sorted(manifests.values(), key=len, reverse=True)
    return [
        f"release drift: {len(group)} instance(s) differ from the majority "
        f"({', '.join(group[:3])}{'…' if len(group) > 3 else ''})"
        for group in ordered[1:]
    ]


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--urls", default="", help="File of app URLs, one per line")
    parser.add_argument("--url", default=os.environ.get("WORKSHOP_APP_URL", ""))
    parser.add_argument("--token", default=os.environ.get("DATABRICKS_TOKEN", ""))
    parser.add_argument("--event-hours", type=float, default=8.0)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    if args.urls:
        with open(args.urls) as handle:
            targets = [
                line.strip().rstrip("/")
                for line in handle
                if line.strip() and not line.lstrip().startswith("#")
            ]
    else:
        targets = [args.url.rstrip("/")] if args.url else []

    if not targets or not args.token:
        print(
            "error: --urls (or --url/WORKSHOP_APP_URL) and --token/DATABRICKS_TOKEN "
            "are required",
            file=sys.stderr,
        )
        return 2

    with concurrent.futures.ThreadPoolExecutor(max_workers=min(24, len(targets))) as pool:
        results = list(
            pool.map(lambda base: _try(base, args.token, args.event_hours), targets)
        )
    drift = _fleet_drift(results)

    if args.json:
        print(json.dumps({"instances": results, "fleet": drift}, indent=2))
    else:
        bad = [r for r in results if r["problems"]]
        print(
            f"\n=== rehearsal: {len(results)} instance(s), "
            f"{len(results) - len(bad)} would serve an attendee ==="
        )
        for result in bad:
            print(f"\nFAIL {result['url']}")
            for problem in result["problems"]:
                print(f"     - {problem}")
        for warning in drift:
            print(f"\nWARN {warning}")
        if not bad and not drift:
            print("all instances green; no release drift")

    return 1 if any(r["problems"] for r in results) or drift else 0


if __name__ == "__main__":
    sys.exit(main())
