#!/usr/bin/env python3
"""Check WCAG contrast for colours in a persisted design-system JSON."""
from __future__ import annotations

import argparse
import json
from pathlib import Path


def luminance(value: str) -> float:
    value = value.strip().lstrip("#")
    if len(value) == 3:
        value = "".join(char * 2 for char in value)
    if len(value) != 6:
        raise ValueError(f"expected #RRGGBB, got {value!r}")
    channels = [int(value[index:index + 2], 16) / 255 for index in (0, 2, 4)]
    linear = [
        channel / 12.92
        if channel <= 0.04045
        else ((channel + 0.055) / 1.055) ** 2.4
        for channel in channels
    ]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def ratio(a: str, b: str) -> float:
    high, low = sorted((luminance(a), luminance(b)), reverse=True)
    return round((high + 0.05) / (low + 0.05), 2)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--system", type=Path, default=Path(".design-studio/design-system.json"))
    parser.add_argument("--minimum", type=float, default=4.5)
    args = parser.parse_args()

    data = json.loads(args.system.read_text(encoding="utf-8"))
    palette = data.get("palette", {})
    foreground = palette.get("foreground")
    pairs = {
        "foreground/background": (foreground, palette.get("background")),
        "foreground/surface": (foreground, palette.get("surface")),
    }
    results = {}
    failures = []
    for name, (a, b) in pairs.items():
        if not a or not b:
            failures.append(f"{name}: missing colour")
            continue
        value = ratio(a, b)
        results[name] = value
        if value < args.minimum:
            failures.append(f"{name}: {value} < {args.minimum}")

    print(json.dumps({"ok": not failures, "ratios": results, "failures": failures}, indent=2))
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
