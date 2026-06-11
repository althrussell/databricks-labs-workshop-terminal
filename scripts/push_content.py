#!/usr/bin/env python3
"""Steer a live workshop-terminal app from anywhere.

Auth: a Databricks token (PAT or SP OAuth) whose principal is a member of the
app's ADMIN_GROUP (default platform_admins) and has CAN_USE on the app.

  export WORKSHOP_APP_URL=https://<app>.databricksapps.com
  export DATABRICKS_TOKEN=...

  push_content.py state
  push_content.py phase build
  push_content.py broadcast "Break ends at 2pm" [--level info|success|warning] [--ttl 300]
  push_content.py pack ./event_pack.json
  push_content.py presence
"""

from __future__ import annotations

import argparse
import json
import os
import sys

import requests


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--url", default=os.environ.get("WORKSHOP_APP_URL", ""), help="App URL (or WORKSHOP_APP_URL)")
    parser.add_argument("--token", default=os.environ.get("DATABRICKS_TOKEN", ""), help="Token (or DATABRICKS_TOKEN)")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("state")
    sub.add_parser("presence")
    phase = sub.add_parser("phase")
    phase.add_argument("name")
    broadcast = sub.add_parser("broadcast")
    broadcast.add_argument("message")
    broadcast.add_argument("--level", default="info", choices=["info", "success", "warning"])
    broadcast.add_argument("--ttl", type=int, default=300)
    pack = sub.add_parser("pack")
    pack.add_argument("file")

    args = parser.parse_args()
    if not args.url or not args.token:
        print("error: --url/WORKSHOP_APP_URL and --token/DATABRICKS_TOKEN are required", file=sys.stderr)
        return 2

    base = args.url.rstrip("/")
    headers = {"Authorization": f"Bearer {args.token}"}

    if args.cmd == "state":
        resp = requests.get(f"{base}/api/admin/state", headers=headers, timeout=30)
    elif args.cmd == "presence":
        resp = requests.get(f"{base}/api/admin/presence", headers=headers, timeout=30)
    elif args.cmd == "phase":
        resp = requests.post(f"{base}/api/admin/phase", headers=headers, json={"phase": args.name}, timeout=30)
    elif args.cmd == "broadcast":
        resp = requests.post(
            f"{base}/api/admin/broadcast", headers=headers,
            json={"message": args.message, "level": args.level, "ttl_s": args.ttl}, timeout=30,
        )
    else:  # pack
        with open(args.file) as f:
            body = json.load(f)
        resp = requests.post(f"{base}/api/admin/content-pack", headers=headers, json=body, timeout=30)

    try:
        print(json.dumps(resp.json(), indent=2))
    except ValueError:
        print(resp.text)
    if resp.status_code >= 400:
        print(f"error: HTTP {resp.status_code}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
