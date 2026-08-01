#!/usr/bin/env python3
"""Detect framework, styling, brand evidence, and build commands."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

SKIP = {"node_modules", ".git", "dist", "build", "static", ".next", ".venv"}
ASSET_EXTENSIONS = {".svg", ".png", ".jpg", ".jpeg", ".webp", ".avif"}
BRAND_WORDS = {"brand", "logo", "wordmark", "identity", "mark", "icon"}


def package_json(root: Path) -> dict:
    path = root / "package.json"
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def detect_brand_assets(root: Path, limit: int = 30) -> list[str]:
    assets = []
    for path in root.rglob("*"):
        if any(part in SKIP for part in path.parts):
            continue
        if not path.is_file() or path.suffix.lower() not in ASSET_EXTENSIONS:
            continue
        name = path.stem.lower()
        if any(word in name for word in BRAND_WORDS):
            assets.append(str(path.relative_to(root)))
            if len(assets) >= limit:
                break
    return sorted(assets)


def detect(root: Path) -> dict:
    package = package_json(root)
    deps = {
        **(package.get("dependencies") or {}),
        **(package.get("devDependencies") or {}),
    }
    stacks: list[str] = []
    signals: list[str] = []

    def add_stack(name: str, signal: str) -> None:
        if name not in stacks:
            stacks.append(name)
        signals.append(signal)

    if "@databricks/appkit" in deps or (root / "appkit.plugins.json").exists():
        add_stack("appkit", "AppKit dependency or plugin manifest")
    if "next" in deps:
        add_stack("nextjs", "next dependency")
    if "react" in deps:
        add_stack("react", "react dependency")
    if "vue" in deps:
        add_stack("vue", "vue dependency")
    if "svelte" in deps or "@sveltejs/kit" in deps:
        add_stack("svelte", "svelte dependency")
    if "@angular/core" in deps:
        add_stack("angular", "Angular dependency")
    if "astro" in deps:
        add_stack("astro", "Astro dependency")
    if (root / "pubspec.yaml").exists():
        add_stack("flutter", "pubspec.yaml")
    if (root / "Package.swift").exists() or list(root.glob("*.xcodeproj")):
        add_stack("swift", "Swift project marker")
    if any(root.glob("*.html")) or (root / "index.html").exists():
        add_stack("html", "HTML entrypoint")
    if not stacks:
        stacks.append("unknown")

    styling: list[str] = []
    style_dependencies = {
        "tailwindcss": "tailwind",
        "@databricks/appkit-ui": "appkit-ui",
        "@mui/material": "mui",
        "@chakra-ui/react": "chakra",
        "styled-components": "styled-components",
        "@emotion/react": "emotion",
        "sass": "sass",
    }
    for dependency, label in style_dependencies.items():
        if dependency in deps and label not in styling:
            styling.append(label)

    for css in root.rglob("*.css"):
        if any(part in SKIP for part in css.parts):
            continue
        try:
            text = css.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if "@tailwind" in text or '@import "tailwindcss"' in text:
            if "tailwind" not in styling:
                styling.append("tailwind")
        if ":root" in text and "--" in text and "css-variables" not in styling:
            styling.append("css-variables")

    package_manager = (
        "pnpm" if (root / "pnpm-lock.yaml").exists()
        else "yarn" if (root / "yarn.lock").exists()
        else "bun" if (root / "bun.lockb").exists() or (root / "bun.lock").exists()
        else "npm" if (root / "package-lock.json").exists()
        else "unknown"
    )

    scripts = package.get("scripts") or {}
    build_commands = {
        key: value for key, value in scripts.items()
        if key in {"dev", "build", "test", "lint", "typecheck", "check", "preview"}
    }

    brand_assets = detect_brand_assets(root)
    primary_stack = next(
        (item for item in ("appkit", "nextjs", "vue", "svelte", "react", "html")
         if item in stacks),
        stacks[0],
    )

    return {
        "root": str(root.resolve()),
        "stack": primary_stack,
        "stacks": stacks,
        "signals": signals,
        "package_manager": package_manager,
        "styling": styling,
        "brand_assets": brand_assets,
        "brand_evidence": bool(brand_assets),
        "has_design_system": (root / ".design-studio" / "MASTER.md").exists(),
        "scripts": scripts,
        "build_commands": build_commands,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    result = detect(args.root.resolve())
    print(
        json.dumps(result, indent=2)
        if args.json
        else "\n".join(f"{key}: {value}" for key, value in result.items())
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
