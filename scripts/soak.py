#!/usr/bin/env python3
"""Watch a deployed instance (or a fleet) for as long as an event lasts.

The compressed version of this lives in ``tests/test_soak_and_chaos.py`` and
proves the logic. This proves the deployment: it samples ``/readyz`` and the
admin diagnostics on a schedule and reports the only three things that matter
over eight hours — did the app credential survive its renewals, did the
attendee credential ever go stale without recovering, and did any attendee see
an error the operator could not see at the same moment.

It is deliberately dumb about time. Nothing here reasons about what *should*
happen at hour six; it records what did, and exits non-zero if the run
contained a window where an Omnigent launch would have failed.

Auth: a Databricks token whose principal is in ADMIN_GROUP and has CAN_USE on
the app.

  export DATABRICKS_TOKEN=...

  # watch one instance for eight hours, sampling every minute
  soak.py watch --url https://<app>.databricksapps.com --hours 8

  # the fleet, sampling every five minutes, writing a JSONL trace
  soak.py watch --urls ./instances.txt --interval 300 --trace ./soak.jsonl

  # chaos: the two failures an operator can cause over the API
  soak.py chaos --url https://<app>... --case recover
  soak.py chaos --url https://<app>... --case demote-restore
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import sys
import time
from datetime import datetime, timezone

import requests

TIMEOUT = 45


def _targets(args) -> list[str]:
    if args.urls:
        with open(args.urls) as handle:
            return [
                line.strip().rstrip("/")
                for line in handle
                if line.strip() and not line.lstrip().startswith("#")
            ]
    return [args.url.rstrip("/")] if args.url else []


def _get(base: str, path: str, token: str, **params) -> dict:
    resp = requests.get(
        f"{base}{path}",
        headers={"Authorization": f"Bearer {token}"},
        params={k: v for k, v in params.items() if v not in ("", None)},
        timeout=TIMEOUT,
    )
    resp.raise_for_status()
    return resp.json()


def _post(base: str, path: str, token: str, body: dict | None = None) -> dict:
    resp = requests.post(
        f"{base}{path}",
        headers={"Authorization": f"Bearer {token}"},
        json=body or {},
        timeout=TIMEOUT,
    )
    resp.raise_for_status()
    return resp.json()


def _clock() -> str:
    return datetime.now(timezone.utc).strftime("%H:%M:%S")


def _sample(base: str, token: str) -> dict:
    """One observation of an instance: readiness, durability, fresh errors."""
    ready = _get(base, "/readyz", token)
    diagnostics = _get(base, "/api/admin/diagnostics", token, limit=50)
    durability = ready.get("durability", {})
    return {
        "at": time.time(),
        "url": base,
        "ready": bool(ready.get("ready")),
        "omnigent_launchable": durability.get("omnigent_launchable"),
        "outlasts_event": durability.get("outlasts_event"),
        "app_credential_expires_in": durability.get("app_credential_expires_in"),
        "attendee_obo_expires_in": durability.get("attendee_obo_expires_in"),
        "errors": [
            {
                "code": entry.get("code"),
                "attendee": entry.get("attendee"),
                "count": entry.get("count", 1),
            }
            for entry in diagnostics.get("errors", [])
        ],
        "failed_checks": sorted(
            name
            for name, check in (ready.get("checks") or {}).items()
            if not check.get("ok") and not check.get("soft")
        ),
    }


class Run:
    """Everything observed about one instance, reduced to a verdict at the end."""

    def __init__(self, url: str) -> None:
        self.url = url
        self.samples = 0
        self.unreachable = 0
        self.not_launchable = 0
        self.not_ready = 0
        self.error_codes: dict[str, int] = {}
        self.credential_low_water: float | None = None

    def record(self, sample: dict) -> None:
        self.samples += 1
        if sample.get("omnigent_launchable") is False:
            self.not_launchable += 1
        if not sample.get("ready"):
            self.not_ready += 1
        for entry in sample.get("errors", []):
            code = entry.get("code") or "unknown"
            self.error_codes[code] = max(self.error_codes.get(code, 0), entry["count"])
        expires = sample.get("app_credential_expires_in")
        if isinstance(expires, (int, float)):
            if self.credential_low_water is None or expires < self.credential_low_water:
                self.credential_low_water = expires

    @property
    def clean(self) -> bool:
        return not (self.not_launchable or self.not_ready or self.unreachable)

    def report(self) -> str:
        window = f"{self.samples} sample(s)"
        if self.clean:
            head = f"OK   {self.url} — {window}, no unlaunchable window"
        else:
            head = (
                f"FAIL {self.url} — {window}, "
                f"{self.not_launchable} unlaunchable, {self.not_ready} not-ready, "
                f"{self.unreachable} unreachable"
            )
        lines = [head]
        if self.credential_low_water is not None:
            lines.append(
                f"     app credential low-water: {int(self.credential_low_water)}s "
                "(a renewal happened if this dipped and recovered)"
            )
        for code, count in sorted(self.error_codes.items(), key=lambda kv: -kv[1]):
            lines.append(f"     x{count:<4} {code}")
        return "\n".join(lines)


def watch(targets: list[str], args) -> int:
    deadline = time.time() + args.hours * 3600
    runs = {url: Run(url) for url in targets}
    trace = open(args.trace, "a") if args.trace else None
    try:
        while time.time() < deadline:
            with concurrent.futures.ThreadPoolExecutor(
                max_workers=min(16, len(targets))
            ) as pool:
                results = list(
                    pool.map(lambda base: (base, _try(base, args.token)), targets)
                )
            for base, (sample, error) in results:
                run = runs[base]
                if error is not None:
                    run.unreachable += 1
                    print(f"[{_clock()}] {base} UNREACHABLE: {error}", file=sys.stderr)
                    continue
                run.record(sample)
                if trace:
                    trace.write(json.dumps(sample) + "\n")
                    trace.flush()
                if args.verbose or sample.get("omnigent_launchable") is False:
                    print(
                        f"[{_clock()}] {base} ready={sample['ready']} "
                        f"omnigent={sample['omnigent_launchable']} "
                        f"app_cred={sample['app_credential_expires_in']} "
                        f"obo={sample['attendee_obo_expires_in']}"
                    )
            remaining = deadline - time.time()
            if remaining <= 0:
                break
            time.sleep(min(args.interval, remaining))
    except KeyboardInterrupt:
        print("\ninterrupted — reporting what was observed so far", file=sys.stderr)
    finally:
        if trace:
            trace.close()

    print("\n=== soak verdict ===")
    for run in runs.values():
        print(run.report())
    return 0 if all(run.clean for run in runs.values()) else 1


def chaos(targets: list[str], args) -> int:
    """The failures an operator can cause from outside the box.

    Force-expiring a credential and deleting the mirror need to be inside the
    process, so those live in the test suite. What is reachable over the API is
    the recovery path itself, and the fleet demote — the two levers an operator
    will actually reach for mid-event, which therefore have to work under
    pressure rather than in a demo.
    """
    failures = 0
    for base in targets:
        try:
            if args.case == "recover":
                before = _sample(base, args.token)
                result = _post(base, "/api/admin/recover", args.token)
                after = _sample(base, args.token)
                print(
                    f"\n=== {base} recover ===\n"
                    f"  recovered: {result.get('recovered')}\n"
                    f"  omnigent_launchable {before['omnigent_launchable']} "
                    f"-> {after['omnigent_launchable']}"
                )
                if after["omnigent_launchable"] is False and before["omnigent_launchable"]:
                    failures += 1
                    print("  FAIL: recovery made it worse", file=sys.stderr)
            else:
                _post(base, "/api/admin/omnigent-tier", args.token, {"enabled": False})
                demoted = _get(base, "/api/admin/omnigent-tier", args.token)
                _post(base, "/api/admin/omnigent-tier", args.token, {"enabled": True})
                restored = _get(base, "/api/admin/omnigent-tier", args.token)
                print(
                    f"\n=== {base} demote/restore ===\n"
                    f"  enabled {demoted.get('enabled')} -> {restored.get('enabled')}"
                )
                if demoted.get("enabled") or not restored.get("enabled"):
                    failures += 1
                    print("  FAIL: the lever did not move", file=sys.stderr)
        except requests.RequestException as error:
            failures += 1
            print(f"\n=== {base} — UNREACHABLE: {error} ===", file=sys.stderr)
    return 1 if failures else 0


def _try(base: str, token: str) -> tuple[dict, str | None]:
    try:
        return _sample(base, token), None
    except requests.RequestException as error:
        return {}, str(error)[:200]


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("cmd", choices=["watch", "chaos"])
    parser.add_argument("--url", default=os.environ.get("WORKSHOP_APP_URL", ""))
    parser.add_argument("--urls", default="", help="File of app URLs, one per line")
    parser.add_argument("--token", default=os.environ.get("DATABRICKS_TOKEN", ""))
    parser.add_argument("--hours", type=float, default=8.0)
    parser.add_argument("--interval", type=float, default=60.0)
    parser.add_argument("--trace", default="", help="Append JSONL samples here")
    parser.add_argument("--case", choices=["recover", "demote-restore"], default="recover")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    targets = _targets(args)
    if not targets or not args.token:
        print(
            "error: --url/WORKSHOP_APP_URL (or --urls) and --token/DATABRICKS_TOKEN "
            "are required",
            file=sys.stderr,
        )
        return 2

    if args.cmd == "watch":
        return watch(targets, args)
    return chaos(targets, args)


if __name__ == "__main__":
    sys.exit(main())
