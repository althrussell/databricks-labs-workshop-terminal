#!/usr/bin/env python3
"""Mode 2 acceptance: local Omnigent on fresh compute, driven over the real PTY.

Boots an Omnigent session against a deployed app, drives a model round-trip
through the rotating token file, then detaches and reattaches to prove the PTY
survives the socket. Not part of the test suite: it needs a live deployment.

Omnigent paints a full-screen TUI, so raw output is replayed into a terminal
emulator and assertions run against the rendered screen. Matching on the byte
stream instead would see cells that were later overdrawn.

  DATABRICKS_CONFIG_PROFILE=labs python scripts/regress_mode2.py --url https://...
"""

from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
import time

import httpx
import pyte
import websockets

COLS, ROWS = 120, 40


def token(profile: str) -> str:
    out = subprocess.run(
        ["databricks", "auth", "token", "-p", profile],
        capture_output=True, text=True, check=True,
    ).stdout
    return json.loads(out)["access_token"]


class Term:
    """A live terminal emulator fed by the session websocket."""

    def __init__(self) -> None:
        self.screen = pyte.Screen(COLS, ROWS)
        self.stream = pyte.Stream(self.screen)
        self.bytes = 0

    def feed(self, data: str) -> None:
        self.stream.feed(data)
        self.bytes += len(data)

    def render(self) -> str:
        return "\n".join(line.rstrip() for line in self.screen.display)

    def tail(self, n: int = 18) -> str:
        lines = [l for l in self.render().splitlines() if l.strip()]
        return "\n".join(f"      {l}" for l in lines[-n:])


class Result:
    def __init__(self) -> None:
        self.checks: list[tuple[str, bool, str]] = []

    def add(self, name: str, ok: bool, detail: str = "") -> None:
        self.checks.append((name, ok, detail))
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""), flush=True)

    @property
    def ok(self) -> bool:
        return all(c[1] for c in self.checks)


async def pump(ws, term: Term, seconds: float, until=None) -> bool:
    """Feed the emulator for a window; stop early once `until(render)` holds."""
    deadline = time.time() + seconds
    while time.time() < deadline:
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=1.0)
        except asyncio.TimeoutError:
            if until and until(term.render()):
                return True
            continue
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            continue
        data = msg.get("data")
        if isinstance(data, str) and msg.get("t") in ("output", "replay", None):
            term.feed(data)
            if until and until(term.render()):
                return True
    return bool(until and until(term.render()))


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", required=True)
    ap.add_argument("--profile", default="labs")
    ap.add_argument("--agent", default="omnigent")
    ap.add_argument("--boot-seconds", type=float, default=180.0)
    ap.add_argument("--answer-seconds", type=float, default=240.0)
    ap.add_argument("--expect-boot", default="ready")
    args = ap.parse_args()

    base = args.url.rstrip("/")
    ws_base = base.replace("https://", "wss://")
    hdr = {"Authorization": f"Bearer {token(args.profile)}"}
    r = Result()

    print(f"\n=== mode 2 acceptance: agent={args.agent} ===", flush=True)

    async with httpx.AsyncClient(timeout=120.0, headers=hdr, follow_redirects=True) as c:
        steps = (await c.get(f"{base}/api/setup-status")).json().get("steps", {})
        if args.agent == "omnigent":
            omni = steps.get("omnigent", {})
            r.add("omnigent 0.8.2 installed",
                  omni.get("status") == "complete" and omni.get("actual_version") == "0.8.2",
                  f"status={omni.get('status')} version={omni.get('actual_version')}")

        resp = await c.post(f"{base}/api/sessions", json={"agent_id": args.agent})
        r.add("session created", resp.status_code == 200, f"HTTP {resp.status_code}")
        if resp.status_code != 200:
            print("   ", resp.text[:400])
            return 1
        body = resp.json()
        sid = (body.get("session") or body).get("id")

        try:
            term = Term()
            # --- boot ---------------------------------------------------------
            async with websockets.connect(f"{ws_base}/ws/sessions/{sid}", additional_headers=hdr,
                                          max_size=None, open_timeout=60) as ws:
                await ws.send(json.dumps({"t": "resize", "cols": COLS, "rows": ROWS}))
                booted = await pump(ws, term, args.boot_seconds,
                                    until=lambda s: args.expect_boot.lower() in s.lower())
                r.add(f"{args.agent} TUI booted", booted,
                      f"prompt reached, {term.bytes} bytes rendered")
                if not booted:
                    print("    --- screen ---\n" + term.tail())

                # --- model round-trip through the gateway ---------------------
                # The expected answer must not appear in the prompt, or the
                # terminal echoing typed input would satisfy this on its own.
                probe = "What is nineteen plus twenty-three? Reply with digits only."
                await ws.send(json.dumps({"t": "input", "data": probe}))
                await asyncio.sleep(1.5)
                await ws.send(json.dumps({"t": "input", "data": "\r"}))
                hit = await pump(ws, term, args.answer_seconds,
                                 until=lambda s: "42" in s)
                r.add("model round-trip via AI Gateway", hit,
                      "model produced 42, a token absent from the prompt"
                      if hit else "no model answer observed")
                print("    --- screen after probe ---\n" + term.tail(20))

            # --- reattach after socket close ---------------------------------
            await asyncio.sleep(3)
            async with websockets.connect(f"{ws_base}/ws/sessions/{sid}", additional_headers=hdr,
                                          max_size=None, open_timeout=60) as ws2:
                fresh = Term()
                await pump(ws2, fresh, 40.0, until=lambda s: bool(s.strip()))
                r.add("reattach replays live PTY", fresh.bytes > 0,
                      f"{fresh.bytes} bytes replayed into a new socket")
                await ws2.send(json.dumps({"t": "ping"}))
                pong = False
                try:
                    for _ in range(10):
                        m = json.loads(await asyncio.wait_for(ws2.recv(), timeout=8.0))
                        if m.get("t") == "pong":
                            pong = True
                            break
                except (asyncio.TimeoutError, json.JSONDecodeError):
                    pass
                r.add("session still live after reattach", pong,
                      "ping/pong ok" if pong else "no pong")
        finally:
            await c.delete(f"{base}/api/sessions/{sid}")
            print(f"  cleaned up session {sid[:8]}", flush=True)

    print(f"\n=== {'ALL PASS' if r.ok else 'FAILURES PRESENT'} ===")
    for n, ok, _ in r.checks:
        print(f"  {'PASS' if ok else 'FAIL'}  {n}")
    return 0 if r.ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
