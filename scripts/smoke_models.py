#!/usr/bin/env python3
"""Prove each comparison model can be an agent, not just answer a question.

The workshop's headline exercise asks an attendee to build the same thing twice
on different models and read the cost difference. That only works if every model
offered can hold a turn, call a tool, and edit a file — and tool-calling fidelity
across vendors is exactly where it stops working. Discovering that live, in front
of a room, is the failure this script exists to prevent.

Three checks per model, in increasing order of what an agent needs:

  turn       a plain question comes back with prose
  tool_call  a function the model must call, called, with parseable arguments
  file_edit  an ``apply_patch``-shaped call naming the right file and content —
             Codex's real file-edit mechanism, and the check that separates
             "answers well" from "can do the work"

It prints the matrix and the ``WORKSHOP_CODEX_COMPARE`` line that drops whatever
failed. Setting that line is how a failing model leaves the event without a
release; see ``server/models.comparison_supported``.

Auth: any token the workspace accepts on Unity AI Gateway — the app service
principal's, or your own while rehearsing. ``EXECUTE`` on the ``system.ai``
model services is held by all account users by default, so a personal token
rehearses the same path the event runs.

  export DATABRICKS_HOST=https://ws.cloud.databricks.com
  export DATABRICKS_TOKEN=...

  smoke_models.py                    # every comparison model
  smoke_models.py --profile glm      # just one
  smoke_models.py --json             # for CI
"""

from __future__ import annotations

import argparse
import json
import os
import sys

import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server import models  # noqa: E402

TIMEOUT = 120

CHECKS = ("turn", "tool_call", "file_edit")

_WEATHER_TOOL = {
    "type": "function",
    "function": {
        "name": "get_weather",
        "description": "Get the current weather for a city.",
        "parameters": {
            "type": "object",
            "properties": {"city": {"type": "string"}},
            "required": ["city"],
        },
    },
}

# Deliberately Codex's own shape rather than a friendlier one. A model that can
# call `edit_file(path, contents)` but cannot produce a well-formed patch body is
# a model that fails on the attendee's first real edit.
_PATCH_TOOL = {
    "type": "function",
    "function": {
        "name": "apply_patch",
        "description": (
            "Apply a patch to files. The input is a patch in the format:\n"
            "*** Begin Patch\n*** Update File: <path>\n@@\n-<old line>\n"
            "+<new line>\n*** End Patch"
        ),
        "parameters": {
            "type": "object",
            "properties": {"input": {"type": "string"}},
            "required": ["input"],
        },
    },
}


def _tool_calls(body: dict) -> list[dict]:
    choices = body.get("choices") or [{}]
    message = choices[0].get("message") or {}
    return message.get("tool_calls") or []


def _arguments(call: dict) -> dict:
    raw = (call.get("function") or {}).get("arguments")
    if isinstance(raw, dict):
        return raw
    try:
        parsed = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def judge_turn(body: dict) -> tuple[bool, str]:
    """Did it answer at all?"""
    choices = body.get("choices") or [{}]
    content = (choices[0].get("message") or {}).get("content") or ""
    text = content if isinstance(content, str) else json.dumps(content)
    if not text.strip():
        return False, "empty content"
    return True, text.strip().splitlines()[0][:60]


def judge_tool_call(body: dict) -> tuple[bool, str]:
    """Did it call the function it was given, with arguments we can read?"""
    calls = _tool_calls(body)
    if not calls:
        return False, "answered in prose instead of calling the tool"
    name = (calls[0].get("function") or {}).get("name")
    if name != "get_weather":
        return False, f"called {name!r}"
    city = str(_arguments(calls[0]).get("city", "")).lower()
    if "paris" not in city:
        return False, f"arguments not usable: {_arguments(calls[0])}"
    return True, "get_weather(city=Paris)"


def judge_file_edit(body: dict) -> tuple[bool, str]:
    """Did it produce a patch that names the file and carries the new line?"""
    calls = _tool_calls(body)
    if not calls:
        return False, "no patch call"
    name = (calls[0].get("function") or {}).get("name")
    if name != "apply_patch":
        return False, f"called {name!r}"
    patch = str(_arguments(calls[0]).get("input", ""))
    missing = [
        marker
        for marker in ("*** Begin Patch", "app.py", "+", "goodbye")
        if marker.lower() not in patch.lower()
    ]
    if missing:
        return False, f"patch missing {', '.join(missing)}"
    return True, "well-formed patch"


REQUESTS = {
    "turn": {
        "messages": [
            {"role": "user", "content": "In one short sentence, what is a Delta table?"}
        ],
        "max_tokens": 200,
    },
    "tool_call": {
        "messages": [
            {"role": "user", "content": "What is the weather in Paris right now?"}
        ],
        "tools": [_WEATHER_TOOL],
        "tool_choice": "auto",
        "max_tokens": 300,
    },
    "file_edit": {
        "messages": [
            {
                "role": "user",
                "content": (
                    "The file app.py contains exactly one line:\n"
                    '    print("hello")\n'
                    'Change it to print("goodbye") using the apply_patch tool.'
                ),
            }
        ],
        "tools": [_PATCH_TOOL],
        "tool_choice": "auto",
        "max_tokens": 500,
    },
}

JUDGES = {
    "turn": judge_turn,
    "tool_call": judge_tool_call,
    "file_edit": judge_file_edit,
}


def run_check(host: str, token: str, model: str, check: str, post=None) -> dict:
    """One check against one model. Never raises — a failure is a result."""
    payload = dict(REQUESTS[check], model=models.service_name(model))
    send = post or _post
    try:
        body = send(f"{host}/ai-gateway/mlflow/v1/chat/completions", token, payload)
    except Exception as exc:  # noqa: BLE001 — an unreachable model is a verdict
        return {"check": check, "ok": False, "detail": str(exc)[:160]}
    ok, detail = JUDGES[check](body)
    return {"check": check, "ok": ok, "detail": detail}


def _post(url: str, token: str, payload: dict) -> dict:
    response = requests.post(
        url,
        headers={"Authorization": f"Bearer {token}"},
        json=payload,
        timeout=TIMEOUT,
    )
    if response.status_code != 200:
        raise RuntimeError(f"HTTP {response.status_code}: {response.text[:120]}")
    return response.json()


def smoke(host: str, token: str, candidates: dict[str, str], post=None) -> list[dict]:
    results = []
    for profile, model in candidates.items():
        checks = [run_check(host, token, model, check, post) for check in CHECKS]
        results.append(
            {
                "profile": profile,
                "model": model,
                "supported": all(check["ok"] for check in checks),
                "checks": checks,
            }
        )
    return results


def render(results: list[dict]) -> str:
    lines = [f"{'profile':<10} {'model':<34} " + "  ".join(f"{c:<10}" for c in CHECKS)]
    for row in results:
        marks = "  ".join(
            f"{('pass' if check['ok'] else 'FAIL'):<10}" for check in row["checks"]
        )
        lines.append(f"{row['profile']:<10} {row['model']:<34} {marks}")
    for row in results:
        for check in row["checks"]:
            if not check["ok"]:
                lines.append(f"  {row['profile']}/{check['check']}: {check['detail']}")
    supported = [row["profile"] for row in results if row["supported"]]
    lines.append("")
    lines.append(
        f"WORKSHOP_CODEX_COMPARE={','.join(supported)}"
        if supported
        else "WORKSHOP_CODEX_COMPARE=  # nothing passed — do not run the comparison"
    )
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default=os.environ.get("DATABRICKS_HOST", ""))
    parser.add_argument("--token", default=os.environ.get("DATABRICKS_TOKEN", ""))
    parser.add_argument("--profile", action="append", default=[])
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    if not args.host or not args.token:
        print("DATABRICKS_HOST and DATABRICKS_TOKEN are required", file=sys.stderr)
        return 2

    # Unfiltered on purpose: the point is to measure everything we might offer,
    # including whatever a previous run's WORKSHOP_CODEX_COMPARE dropped.
    candidates = {
        name: models.service_name(
            os.environ.get(f"CODEX_COMPARE_{name.upper()}", "").strip() or default
        )
        for name, (default, _label) in models.COMPARISON_MODELS.items()
        if not args.profile or name in args.profile
    }
    results = smoke(args.host.rstrip("/"), args.token, candidates)
    print(json.dumps(results, indent=2) if args.json else render(results))
    return 0 if all(row["supported"] for row in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
