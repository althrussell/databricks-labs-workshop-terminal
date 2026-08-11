#!/usr/bin/env python3
"""Pull diagnostics off one instance, or off the whole fleet, without a browser.

Written for the moment that started all of this: an attendee reports an error,
and the operator has a screenshot and nothing else. Everything here comes from
the admin diagnostics endpoints, so the answer arrives from a laptop in the room
rather than from a shell on the attendee's container.

Auth: a Databricks token whose principal is in the app's ADMIN_GROUP and has
CAN_USE on the app.

  export DATABRICKS_TOKEN=...

  # one instance
  pull_diagnostics.py errors  --url https://<app>.databricksapps.com
  pull_diagnostics.py logs    --url https://<app>...  --source runner
  pull_diagnostics.py sweep   --url https://<app>...
  pull_diagnostics.py summary --url https://<app>...

  # the fleet: one URL per line, '#' comments allowed
  pull_diagnostics.py errors --urls ./instances.txt
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import sys
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


def _post(base: str, path: str, token: str) -> dict:
    resp = requests.post(
        f"{base}{path}", headers={"Authorization": f"Bearer {token}"}, timeout=TIMEOUT
    )
    resp.raise_for_status()
    return resp.json()


def _when(epoch: float | None) -> str:
    if not epoch:
        return "-"
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%H:%M:%S")


def _fetch(base: str, args) -> dict:
    token = args.token
    if args.cmd == "errors":
        return _get(base, "/api/admin/diagnostics", token, limit=args.limit)
    if args.cmd == "logs":
        return _get(
            base,
            "/api/admin/diagnostics/logs",
            token,
            attendee=args.attendee,
            source=args.source,
            limit_bytes=args.bytes,
        )
    if args.cmd == "sweep":
        return _post(base, "/api/admin/diagnostics/sweep", token)
    return _get(base, "/api/admin/diagnostics", token, limit=args.limit)


def _render_errors(base: str, payload: dict) -> None:
    errors = payload.get("errors", [])
    print(f"\n=== {base} — {len(errors)} classified error(s) ===")
    for entry in errors:
        print(
            f"[{_when(entry.get('last_seen'))}] x{entry.get('count', 1):<3} "
            f"{entry.get('code')}  ({entry.get('source')}/{entry.get('logger')}) "
            f"{entry.get('attendee')}"
        )
        if entry.get("message"):
            print(f"    {entry['message']}")
        detail = (entry.get("detail") or "").strip().splitlines()
        for line in detail[-4:]:
            print(f"    | {line}")


def _render_summary(base: str, payload: dict) -> None:
    ready = payload.get("readyz", {})
    collector = payload.get("collector", {})
    errors = payload.get("errors", [])
    print(f"\n=== {base} ===")
    print(
        f"  ready={ready.get('ready')}  collector_running={collector.get('running')} "
        f"sweeps={collector.get('sweeps')}  distinct_errors={len(errors)}"
    )
    for host in payload.get("hosts", []):
        print(f"  host: {json.dumps(host, sort_keys=True)}")
    for snapshot in payload.get("identity", []):
        for plane, surfaces in (snapshot.get("planes") or {}).items():
            print(f"  identity[{snapshot.get('attendee')}/{plane}]: {surfaces}")
    for entry in errors[:5]:
        print(
            f"  x{entry.get('count', 1):<3} {entry.get('code')} "
            f"({entry.get('attendee')})"
        )


def _render_logs(base: str, payload: dict) -> None:
    for log in payload.get("logs", []):
        print(f"\n=== {base} :: {log['attendee']} :: {log['path']} ({log['size']}B) ===")
        print(log.get("tail", ""))


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("cmd", choices=["errors", "logs", "sweep", "summary"])
    parser.add_argument("--url", default=os.environ.get("WORKSHOP_APP_URL", ""))
    parser.add_argument("--urls", default="", help="File of app URLs, one per line")
    parser.add_argument("--token", default=os.environ.get("DATABRICKS_TOKEN", ""))
    parser.add_argument("--attendee", default="", help="Filter logs to one attendee")
    parser.add_argument("--source", default="", help="runner | host | server")
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument("--bytes", type=int, default=64 * 1024)
    parser.add_argument("--json", action="store_true", help="Raw JSON instead of text")
    args = parser.parse_args()

    targets = _targets(args)
    if not targets or not args.token:
        print(
            "error: --url/WORKSHOP_APP_URL (or --urls) and --token/DATABRICKS_TOKEN "
            "are required",
            file=sys.stderr,
        )
        return 2

    failures = 0
    # Fleet pulls run wide: an operator checking twenty instances mid-event is
    # doing it because something is already wrong.
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(16, len(targets))) as pool:
        results = list(pool.map(lambda base: (base, _try(base, args)), targets))

    for base, (payload, error) in results:
        if error is not None:
            failures += 1
            print(f"\n=== {base} — UNREACHABLE: {error} ===", file=sys.stderr)
            continue
        if args.json:
            print(json.dumps({"url": base, "payload": payload}, indent=2))
        elif args.cmd == "logs":
            _render_logs(base, payload)
        elif args.cmd == "errors":
            _render_errors(base, payload)
        elif args.cmd == "sweep":
            print(f"\n=== {base} — captured {payload.get('captured')} ===")
        else:
            _render_summary(base, payload)
    return 1 if failures else 0


def _try(base: str, args) -> tuple[dict, str | None]:
    try:
        return _fetch(base, args), None
    except requests.RequestException as error:
        return {}, str(error)[:200]


if __name__ == "__main__":
    sys.exit(main())
