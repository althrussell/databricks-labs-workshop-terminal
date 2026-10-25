#!/usr/bin/env python3
"""Check the composition references (W5): structure, states, honesty and contrast.

Static checks run on every PR. ``--compile PROJECT`` copies each reference into
a scaffolded APX app so ``apx dev check`` / ``apx build`` compile them against
the pinned toolchain (scripts/check_apx_toolchain.py does this in CI).
``--write-baselines`` records each reference's measured checks; rendered
screenshots are added from a live terminal with ``workshop-browser inspect``
and stay ``pending_owner_review`` until a person approves them.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import shutil
import sys

ROOT = Path(__file__).resolve().parents[1]
REFERENCES = ROOT / "assets" / "design-references"
PATTERNS = ("queue-detail", "analytical-exploration", "guided-form", "conversational-workspace", "learning-interaction")
EXAMPLES = ("default", "long-label", "empty", "loading", "error")
TEXT_CONTRAST = 4.5
NON_TEXT_CONTRAST = 3.0

_NO_OP = re.compile(r"=>\s*\{\s*\}|=>\s*undefined\b|=>\s*null\s*\}")
_PLACEHOLDER = re.compile(r"\b(mock|lorem|ipsum|todo|fixme|placeholder data)\b", re.IGNORECASE)
_IMPORT = re.compile(r'^import\s+(?:type\s+)?[^;]*?from\s+"([^"]+)"', re.MULTILINE)


def _tags(source: str, tag: str) -> list[str]:
    """Each ``<tag ...>`` opening tag's attributes, reading past ``=>`` inside JSX braces."""
    found = []
    for match in re.finditer(rf"<{tag}\b", source):
        depth, quote, index = 0, "", match.end()
        while index < len(source):
            char = source[index]
            if quote:
                quote = "" if char == quote else quote
            elif char in "\"'`":
                quote = char
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
            elif char == ">" and depth == 0:
                break
            index += 1
        found.append(source[match.end():index])
    return found


def _luminance(hex_colour: str) -> float:
    value = hex_colour.lstrip("#")
    channels = [int(value[i:i + 2], 16) / 255 for i in (0, 2, 4)]
    linear = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def contrast(a: str, b: str) -> float:
    lighter, darker = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (lighter + 0.05) / (darker + 0.05)


def check_tokens(tokens: dict) -> dict:
    colours = tokens["colors"]
    pairs = []
    problems = []
    for kind, minimum in (("text_pairs", TEXT_CONTRAST), ("non_text_pairs", NON_TEXT_CONTRAST)):
        for fore, back in tokens[kind]:
            ratio = round(contrast(colours[fore], colours[back]), 2)
            pairs.append({"pair": [fore, back], "ratio": ratio, "minimum": minimum})
            if ratio < minimum:
                problems.append(f"{fore} on {back} contrast {ratio} < {minimum}")
    return {"pairs": pairs, "problems": problems}


def check_reference(name: str, folder: Path) -> dict:
    """The measured checks for one reference; ``problems`` empty means it passes."""
    problems: list[str] = []
    readme_path, source_path = folder / "README.md", folder / "Reference.tsx"
    if not readme_path.is_file() or not source_path.is_file():
        return {"name": name, "problems": ["missing README.md or Reference.tsx"]}
    readme, source = readme_path.read_text(), source_path.read_text()
    for needed in ("**Job:**", "Narrow layout", "States:", "Checks:"):
        if needed not in readme:
            problems.append(f"README lacks {needed!r}")
    for example in EXAMPLES:
        if f'"{example}"' not in source:
            problems.append(f"no {example!r} example")
    imports = set(_IMPORT.findall(source))
    if imports - {"react"}:
        problems.append(f"imports beyond react: {sorted(imports - {'react'})}")
    if _NO_OP.search(source):
        problems.append("a no-op callback")
    if _PLACEHOLDER.search(source):
        problems.append("placeholder or mock wording")
    if "sample" not in source.lower():
        problems.append("sample data is not labelled as sample")
    buttons = _tags(source, "button")
    controls = buttons + _tags(source, "select") + _tags(source, "input")
    if not controls:
        problems.append("no control: a primary action needs an observable outcome")
    for attributes in buttons:
        if "type=" not in attributes:
            problems.append("a button without an explicit type")
        if "focus-visible:" not in attributes:
            problems.append("a button without a visible focus style")
    if 'role="status"' not in source and 'role="alert"' not in source:
        problems.append("outcomes are not announced (role=status/alert)")
    if "md:" not in source and "max-w-" not in source:
        problems.append("no narrow-layout behaviour (responsive columns or a single readable column)")
    if "throw new Error(" in source and "nothing was" not in source.lower():
        problems.append("an unconnected action does not say that nothing happened")
    return {"name": name, "examples": list(EXAMPLES), "buttons": len(buttons), "controls": len(controls),
            "imports": sorted(imports), "problems": problems}


def check_all() -> dict:
    tokens = json.loads((REFERENCES / "tokens.json").read_text())
    results = {name: check_reference(name, REFERENCES / name) for name in PATTERNS}
    token_result = check_tokens(tokens)
    extra = sorted(p.name for p in REFERENCES.iterdir() if p.is_dir() and p.name not in PATTERNS and not p.name.startswith("_"))
    problems = [f"{name}: {problem}" for name, result in results.items() for problem in result["problems"]]
    problems += token_result["problems"] + [f"unexpected reference folder {name}" for name in extra]
    return {"references": results, "contrast": token_result, "problems": problems}


def copy_into(project: Path) -> list[Path]:
    """Place each reference in a scaffolded APX app's UI so its check/build compiles them."""
    targets = sorted(project.glob("src/*/ui/components"))
    if not targets:
        raise SystemExit(f"no src/*/ui/components in {project}")
    destination = targets[0] / "design-references"
    destination.mkdir(parents=True, exist_ok=True)
    written = []
    for name in PATTERNS:
        target = destination / f"{name}.tsx"
        shutil.copyfile(REFERENCES / name / "Reference.tsx", target)
        written.append(target)
    return written


def write_baselines(report: dict) -> None:
    for name, result in report["references"].items():
        baseline = {
            "schema_version": 1,
            "reference": name,
            "status": "pending_owner_review",
            "measured": {"examples": result["examples"], "buttons": result["buttons"], "controls": result["controls"],
                         "imports": result["imports"], "problems": result["problems"]},
            "contrast": report["contrast"]["pairs"],
            "screenshots": [],
            "how_to_capture": ("In a live terminal: render each example in an APX app, then "
                               "`workshop-browser inspect <url> --both` and add the screenshot paths "
                               "and digests here. A person approves them by setting status to "
                               "`approved` with their name; pixels alone never approve a design."),
        }
        (REFERENCES / name / "baseline.json").write_text(json.dumps(baseline, indent=2) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--compile", type=Path, metavar="PROJECT", help="copy references into a scaffolded APX app")
    parser.add_argument("--write-baselines", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    report = check_all()
    if args.compile:
        report["copied"] = [str(path) for path in copy_into(args.compile)]
    if args.write_baselines:
        write_baselines(report)
    if args.json:
        print(json.dumps(report, indent=2))
    elif report["problems"]:
        print("\n".join(report["problems"]))
    else:
        print(f"{len(PATTERNS)} composition references pass")
    return 1 if report["problems"] else 0


if __name__ == "__main__":
    sys.exit(main())
