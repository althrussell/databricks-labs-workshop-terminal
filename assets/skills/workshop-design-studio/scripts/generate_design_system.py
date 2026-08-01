#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

from core import search_domain
from project_detect import detect


def clamp(value: int | None, default: int) -> int:
    return max(1, min(10, default if value is None else value))


def first(result: dict) -> dict:
    return (result.get("results") or [{}])[0]


def choose(result: dict, offset: int = 0) -> dict:
    """Choose a ranked candidate while preserving a safe fallback.

    Direction 1 uses the strongest match. Directions 2 and 3 deliberately use
    the next relevant candidates so creative exploration changes composition,
    type, palette, and art direction rather than merely renaming one concept.
    """
    items = result.get("results") or [{}]
    return items[min(max(offset, 0), len(items) - 1)]


def contrast_ratio(a: str, b: str) -> float | None:
    def luminance(value: str):
        value = value.strip().lstrip("#")
        if len(value) != 6:
            return None
        try:
            channels = [int(value[i:i + 2], 16) / 255 for i in (0, 2, 4)]
        except ValueError:
            return None
        linear = [
            c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4
            for c in channels
        ]
        return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]

    x, y = luminance(a), luminance(b)
    if x is None or y is None:
        return None
    high, low = max(x, y), min(x, y)
    return round((high + 0.05) / (low + 0.05), 2)


def signature(style: str, layout: str, dials: dict) -> str:
    combined = f"{style} {layout}".lower()
    if "workbench" in combined or "spatial" in combined:
        return "A context-preserving transition from overview into an inspectable detail surface."
    if "product" in combined or "ecommerce" in combined:
        return "A tactile product reveal or configuration interaction that keeps the primary action visible."
    if "editorial" in combined or "narrative" in combined or "gallery" in combined:
        return (
            "A carefully choreographed opening composition where typography and imagery "
            "establish the story in one memorable beat."
        )
    if dials["motion"] >= 7:
        return "One cinematic but interruptible transition that demonstrates the product's core value."
    return (
        "A distinctive hero or primary-work-surface interaction that makes the "
        "product promise immediately tangible."
    )


def resolve_direction(query: str, rank: int, mode: str, dials: dict) -> dict:
    biases = [
        "editorial distinctive typography narrative",
        "product utility interactive precise",
        "bold expressive immersive memorable",
    ]
    biased_query = f"{query} {biases[(rank - 1) % len(biases)]}"

    product_result = search_domain(query, "product", 1)
    product = first(product_result)
    candidate_offset = rank - 1
    style = choose(search_domain(biased_query, "style", 6), candidate_offset)
    palette = choose(
        search_domain(f"{biased_query} {style.get('style', '')}", "palette", 6),
        candidate_offset,
    )
    typography = choose(
        search_domain(f"{biased_query} {style.get('keywords', '')}", "typography", 6),
        candidate_offset,
    )
    layout = choose(
        search_domain(
            f"{biased_query} {product.get('recommended_pattern', '')} {style.get('style', '')}",
            "layout",
            6,
        ),
        candidate_offset,
    )
    motion_tier = (
        "subtle" if dials["motion"] <= 3
        else "standard" if dials["motion"] <= 7
        else "expressive"
    )
    motion = choose(
        search_domain(f"{biased_query} {motion_tier}", "motion", 6),
        candidate_offset,
    )
    imagery = choose(
        search_domain(
            f"{biased_query} {product.get('recommended_pattern', '')}",
            "imagery",
            6,
        ),
        candidate_offset,
    )
    voice = choose(
        search_domain(f"{biased_query} {product.get('product', '')}", "voice", 6),
        candidate_offset,
    )

    resolved_mode = mode
    if mode == "auto":
        resolved_mode = product.get("default_mode", "product-led")

    no_match_domains = []
    for domain, result in (
        ("product", product_result),
        ("style", search_domain(biased_query, "style", 1)),
        ("palette", search_domain(query, "palette", 1)),
        ("typography", search_domain(query, "typography", 1)),
        ("layout", search_domain(query, "layout", 1)),
        ("imagery", search_domain(query, "imagery", 1)),
        ("voice", search_domain(query, "voice", 1)),
    ):
        if result.get("count", 0) == 0:
            no_match_domains.append(domain)

    return {
        "rank": rank,
        "concept": style.get("style", "Purposeful Modern"),
        "product": product.get("product", "General Product"),
        "mode": resolved_mode,
        "goal": product.get("design_goal", "A coherent product-led experience"),
        "style": style,
        "palette": palette,
        "typography": typography,
        "layout": layout,
        "motion": motion,
        "imagery": imagery,
        "voice": voice,
        "signature_moment": signature(style.get("style", ""), layout.get("name", ""), dials),
        "avoid": [
            item for item in (
                product.get("avoid"),
                style.get("avoid"),
                layout.get("notes"),
            ) if item
        ],
        "dials": dials,
        "contrast": {
            "foreground_on_background": contrast_ratio(
                palette.get("foreground", ""), palette.get("background", "")
            ),
            "foreground_on_surface": contrast_ratio(
                palette.get("foreground", ""), palette.get("surface", "")
            ),
        },
        "retrieval": {
            "no_match_domains": no_match_domains,
            "fallback_used": bool(no_match_domains),
        },
    }


def render(direction: dict, query: str, project: str) -> str:
    s = direction["style"]
    c = direction["palette"]
    t = direction["typography"]
    l = direction["layout"]
    m = direction["motion"]
    i = direction.get("imagery", {})
    v = direction.get("voice", {})

    avoid = "\n".join(f"- {item}" for item in direction["avoid"]) or "- No specific local match; validate manually."
    fallback = (
        "\n> Some domains had no local match. The listed choices include a disclosed general fallback.\n"
        if direction["retrieval"]["fallback_used"] else ""
    )

    return f"""# {project} — Design System

## Brief

- Query: {query}
- Product archetype: {direction['product']}
- Mode: {direction['mode']}
- Design goal: {direction['goal']}
- Creative concept: {direction['concept']}
{fallback}
## Design dials

- Expression: {direction['dials']['expression']}/10
- Motion: {direction['dials']['motion']}/10
- Density: {direction['dials']['density']}/10
- Depth: {direction['dials']['depth']}/10
- Brand fidelity: {direction['dials']['brand_fidelity']}/10

## Composition

- Layout: {l.get('name', 'Custom composition')}
- Structure: {l.get('structure', 'Define one dominant surface')}
- Responsive transformation: {l.get('responsive', 'Recompose by priority')}

## Visual language

- Style: {s.get('style', 'Purposeful modern')}
- Description: {s.get('description', '')}
- Depth model: {s.get('depth', '')}

## Colour

- Palette: {c.get('name', 'Custom')}
- Background: `{c.get('background', '')}`
- Foreground: `{c.get('foreground', '')}`
- Primary: `{c.get('primary', '')}`
- Accent: `{c.get('accent', '')}`
- Surface: `{c.get('surface', '')}`
- Muted: `{c.get('muted', '')}`
- Foreground/background contrast: {direction['contrast']['foreground_on_background']}
- Foreground/surface contrast: {direction['contrast']['foreground_on_surface']}

## Typography

- System: {t.get('name', 'Existing brand typography')}
- Display: `{t.get('display_stack', '')}`
- Body: `{t.get('body_stack', '')}`
- Notes: {t.get('notes', '')}

## Imagery and art direction

- Direction: {i.get('name', 'Product-appropriate visual system')}
- Treatment: {i.get('direction', '')}
- Implementation: {i.get('implementation', '')}
- Avoid: {i.get('avoid', '')}

## Content voice

- Voice: {v.get('name', 'Confident Clear')}
- Headlines: {v.get('headline', '')}
- Body: {v.get('body', '')}
- Microcopy: {v.get('microcopy', '')}
- Avoid: {v.get('avoid', '')}

## Motion

- Pattern: {m.get('name', 'Micro feedback')}
- Timing: {m.get('duration', '150-300ms')}
- Easing: `{m.get('easing', 'ease-out')}`
- Guardrail: {m.get('guardrail', 'Respect reduced motion')}

## Signature moment

{direction['signature_moment']}

## Avoid

{avoid}
"""


def persist(
    output: Path,
    project: str,
    query: str,
    direction: dict,
    page: str | None,
    force: bool,
) -> dict:
    studio = output / ".design-studio"
    studio.mkdir(parents=True, exist_ok=True)
    master = studio / "MASTER.md"
    system_json = studio / "design-system.json"
    created = []

    if not master.exists() or force:
        master.write_text(render(direction, query, project), encoding="utf-8")
        system_json.write_text(
            json.dumps(direction, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        created.extend([str(master), str(system_json)])

    brief = studio / "creative-brief.md"
    if not brief.exists() or force:
        brief.write_text(
            f"""# Creative brief

- Product: {direction['product']}
- Audience: <!-- fill -->
- Primary action: <!-- fill -->
- Desired emotion: <!-- fill -->
- Existing brand evidence: <!-- fill -->
- Selected concept: {direction['concept']}
- Signature moment: {direction['signature_moment']}
""",
            encoding="utf-8",
        )
        created.append(str(brief))

    defaults = {
        "audit.json": "{}\n",
        "verification.md": """# Verification

- [ ] Build
- [ ] Responsive screenshots
- [ ] Keyboard and visible focus
- [ ] Contrast
- [ ] Loading, empty, error, success
- [ ] Reduced motion
- [ ] Critique pass A recorded
- [ ] Critique pass B recorded
""",
    }
    for name, seed in defaults.items():
        path = studio / name
        if not path.exists():
            path.write_text(seed, encoding="utf-8")
            created.append(str(path))

    if page:
        pages = studio / "pages"
        pages.mkdir(exist_ok=True)
        slug = re.sub(r"[^a-z0-9-]+", "-", page.lower()).strip("-")
        page_path = pages / f"{slug}.md"
        if not page_path.exists() or force:
            page_path.write_text(
                f"""# Page override: {page}

Inherit `.design-studio/MASTER.md`.

## Purpose
<!-- Primary task or message -->

## Composition override
<!-- Only differences from master -->

## Signature interaction

## States
- loading
- empty
- error
- success
- permission
- responsive
""",
                encoding="utf-8",
            )
            created.append(str(page_path))

    return {
        "directory": str(studio),
        "created": created,
        "master_preserved": master.exists() and not force and str(master) not in created,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Generate Workshop Design Studio directions and design systems"
    )
    parser.add_argument("query")
    parser.add_argument("--project-name", "-p", default="Workshop Project")
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--directions", type=int, default=1)
    parser.add_argument(
        "--mode",
        choices=["auto", "brand-led", "product-led", "platform-native", "experimental"],
        default="auto",
    )
    parser.add_argument("--expression", type=int)
    parser.add_argument("--motion", type=int)
    parser.add_argument("--density", type=int)
    parser.add_argument("--depth", type=int)
    parser.add_argument("--brand-fidelity", type=int)
    parser.add_argument("--persist", action="store_true")
    parser.add_argument("--output-dir", type=Path, default=Path.cwd())
    parser.add_argument("--page")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    dials = {
        "expression": clamp(args.expression, 6),
        "motion": clamp(args.motion, 4),
        "density": clamp(args.density, 5),
        "depth": clamp(args.depth, 5),
        "brand_fidelity": clamp(args.brand_fidelity, 7),
    }
    inferred_mode = args.mode
    if args.mode == "auto":
        project = detect(args.root.resolve())
        brand_phrases = (
            "brand kit", "brand guide", "existing brand", "our brand",
            "our company", "match our", "preserve our", "branded",
        )
        if project.get("brand_evidence") or any(
            phrase in args.query.lower() for phrase in brand_phrases
        ):
            inferred_mode = "brand-led"

    count = max(1, min(3, args.directions))
    directions = [
        resolve_direction(args.query, i + 1, inferred_mode, dials)
        for i in range(count)
    ]
    result = {
        "query": args.query,
        "project": args.project_name,
        "directions": directions,
    }

    if args.persist:
        result["persistence"] = persist(
            args.output_dir.resolve(),
            args.project_name,
            args.query,
            directions[0],
            args.page,
            args.force,
        )

    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    elif len(directions) > 1:
        for direction in directions:
            print(f"\n## Direction {direction['rank']}: {direction['concept']}")
            print(f"- Product: {direction['product']}")
            print(f"- Mode: {direction['mode']}")
            print(f"- Layout: {direction['layout'].get('name')}")
            print(f"- Palette: {direction['palette'].get('name')}")
            print(f"- Typography: {direction['typography'].get('name')}")
            print(f"- Imagery: {direction['imagery'].get('name')}")
            print(f"- Voice: {direction['voice'].get('name')}")
            print(f"- Signature moment: {direction['signature_moment']}")
            print(f"- Avoid: {'; '.join(direction['avoid'])}")
            if direction["retrieval"]["fallback_used"]:
                print(
                    "- Retrieval note: general fallback used for "
                    + ", ".join(direction["retrieval"]["no_match_domains"])
                )
    else:
        print(render(directions[0], args.query, args.project_name))
        if args.persist:
            print("Persisted:", result["persistence"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
