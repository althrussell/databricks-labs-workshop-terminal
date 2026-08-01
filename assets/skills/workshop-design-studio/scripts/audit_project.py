#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

SKIP = {"node_modules", "dist", "build", "static", ".git", ".next", "coverage", ".venv"}
TEXT_EXT = {".tsx", ".ts", ".jsx", ".js", ".css", ".scss", ".html", ".vue", ".svelte"}


def source_files(root: Path):
    for path in root.rglob("*"):
        if any(part in SKIP for part in path.parts):
            continue
        if path.is_file() and path.suffix in TEXT_EXT:
            yield path


def add(findings, severity, code, message, path=None, evidence=None):
    findings.append({
        "severity": severity,
        "code": code,
        "message": message,
        "path": str(path) if path else None,
        "evidence": evidence,
    })


def audit(root: Path) -> dict:
    findings = []
    totals = {"files": 0, "hex": 0, "cards": 0, "gradients": 0, "shadows": 0, "animations": 0}
    has_focus = False
    has_reduced = False
    has_responsive = False
    has_states = set()
    has_tokens = False

    for path in source_files(root):
        totals["files"] += 1
        text = path.read_text(encoding="utf-8", errors="ignore")

        if "focus-visible" in text or ":focus" in text:
            has_focus = True
        if "prefers-reduced-motion" in text:
            has_reduced = True
        if re.search(r"\b(sm|md|lg|xl|2xl):", text) or "@media" in text:
            has_responsive = True
        if re.search(r"--[a-z0-9-]+\s*:", text):
            has_tokens = True
        for state in ("loading", "empty", "error", "success", "disabled"):
            if state in text.lower():
                has_states.add(state)

        hexes = re.findall(r"#[0-9a-fA-F]{3,8}\b", text)
        totals["hex"] += len(hexes)
        totals["cards"] += len(
            re.findall(
                r"\bCard\b|className=[^\n]*(?:rounded-(?:xl|2xl)|shadow-(?:lg|xl))",
                text,
            )
        )
        totals["gradients"] += text.count("gradient")
        totals["shadows"] += len(
            re.findall(r"shadow-(?:md|lg|xl|2xl)|box-shadow", text)
        )
        totals["animations"] += len(
            re.findall(r"animate-|animation\s*:|transition-all", text)
        )

        if re.search(r"<img\b(?![^>]*\balt=)[^>]*>", text, re.I):
            add(findings, "critical", "IMG_ALT", "Image element without alt text", path)
        if re.search(r"<(button|a)\b[^>]*>\s*[\U0001F300-\U0001FAFF]", text):
            add(
                findings,
                "high",
                "EMOJI_ICON",
                "Emoji appears to be used as a structural icon",
                path,
            )
        if re.search(
            r"https?://(?:images\.unsplash|source\.unsplash|picsum|pexels)\.",
            text,
        ):
            add(
                findings,
                "medium",
                "REMOTE_IMAGE",
                "Remote image hotlink detected; store production assets locally",
                path,
            )
        if re.search(r"w-\[\d{4,}px\]|width\s*:\s*\d{4,}px", text):
            add(
                findings,
                "high",
                "FIXED_WIDTH",
                "Large fixed width may break responsive layouts",
                path,
            )
        if (
            len(hexes) > 8
            and "token" not in path.name.lower()
            and "theme" not in path.name.lower()
        ):
            add(
                findings,
                "medium",
                "RAW_COLOUR",
                "Many raw colour values in a feature file; consolidate semantic tokens",
                path,
                len(hexes),
            )

    if not (root / ".design-studio/MASTER.md").exists():
        add(findings, "high", "NO_DESIGN_SYSTEM", "Missing .design-studio/MASTER.md")
    if not has_tokens:
        add(findings, "medium", "NO_TOKENS", "No semantic CSS variables detected")
    if not has_focus:
        add(findings, "critical", "NO_FOCUS", "No visible focus styling detected")
    if totals["animations"] and not has_reduced:
        add(
            findings,
            "critical",
            "NO_REDUCED_MOTION",
            "Animation exists without prefers-reduced-motion handling",
        )
    if not has_responsive:
        add(
            findings,
            "high",
            "NO_RESPONSIVE",
            "No responsive breakpoint or media-query evidence detected",
        )
    if totals["cards"] > 14:
        add(
            findings,
            "medium",
            "CARD_WALL",
            "High card-pattern count may indicate generic card-wall composition",
            evidence=totals["cards"],
        )
    if totals["gradients"] > 10:
        add(
            findings,
            "medium",
            "GRADIENT_SPRAWL",
            "Many gradient usages; verify they belong to one art direction",
            evidence=totals["gradients"],
        )
    if totals["shadows"] > 18:
        add(
            findings,
            "medium",
            "SHADOW_SPRAWL",
            "Many strong shadow usages; verify a coherent depth model",
            evidence=totals["shadows"],
        )

    missing_states = {"loading", "empty", "error"} - has_states
    if missing_states:
        add(
            findings,
            "medium",
            "STATE_COVERAGE",
            "No code evidence for states: " + ", ".join(sorted(missing_states)),
        )

    order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    findings.sort(key=lambda item: (order[item["severity"]], item["code"]))
    counts = {
        severity: sum(1 for item in findings if item["severity"] == severity)
        for severity in order
    }
    return {
        "root": str(root),
        "summary": counts,
        "totals": totals,
        "findings": findings,
    }


def markdown(result: dict) -> str:
    lines = [
        "# Design Studio Audit",
        "",
        f"- Critical: {result['summary']['critical']}",
        f"- High: {result['summary']['high']}",
        f"- Medium: {result['summary']['medium']}",
        "",
    ]
    for finding in result["findings"]:
        lines.extend([
            f"## {finding['severity'].upper()} — {finding['code']}",
            "",
            finding["message"]
            + (f" (`{finding['path']}`)" if finding.get("path") else ""),
            "",
        ])
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--json", type=Path)
    parser.add_argument("--markdown", type=Path)
    args = parser.parse_args()

    result = audit(args.root.resolve())
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(result, indent=2), encoding="utf-8")
    if args.markdown:
        args.markdown.parent.mkdir(parents=True, exist_ok=True)
        args.markdown.write_text(markdown(result), encoding="utf-8")
    print(json.dumps(result, indent=2))
    return 1 if result["summary"]["critical"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
