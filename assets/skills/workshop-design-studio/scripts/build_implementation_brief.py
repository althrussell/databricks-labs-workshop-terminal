#!/usr/bin/env python3
"""Turn the persisted design system and repository detection into an implementation brief."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from project_detect import detect  # noqa: E402


def bullets(items) -> str:
    values = [str(item) for item in items if item]
    return "\n".join(f"- {item}" for item in values) or "- None recorded"


def render(system: dict, project: dict) -> str:
    style = system.get("style", {})
    palette = system.get("palette", {})
    typography = system.get("typography", {})
    layout = system.get("layout", {})
    imagery = system.get("imagery", {})
    voice = system.get("voice", {})
    motion = system.get("motion", {})

    return f'''# Implementation Brief

## Mission

Build a complete, credible **{system.get('product', 'product')}** experience.
The selected concept is **{system.get('concept', 'Purposeful Modern')}** and the
primary design goal is:

> {system.get('goal', 'Create a coherent product experience.')}

The deployment platform is not the brand. Implement the selected identity rather
than substituting host-platform styling.

## Repository

- Primary stack: {project.get('stack')}
- Detected stacks: {', '.join(project.get('stacks', []))}
- Styling: {', '.join(project.get('styling', [])) or 'not detected'}
- Package manager: {project.get('package_manager')}
- Existing brand evidence: {project.get('brand_evidence')}
- Existing brand assets:
{bullets(project.get('brand_assets', []))}

## Art direction

- Style: {style.get('style')}
- Composition: {layout.get('name')} — {layout.get('structure')}
- Imagery: {imagery.get('name')} — {imagery.get('direction')}
- Voice: {voice.get('name')} — {voice.get('headline')}
- Typography: {typography.get('name')}
- Display stack: `{typography.get('display_stack')}`
- Body stack: `{typography.get('body_stack')}`
- Palette: {palette.get('name')}
- Background `{palette.get('background')}`, foreground `{palette.get('foreground')}`,
  primary `{palette.get('primary')}`, accent `{palette.get('accent')}`
- Motion: {motion.get('name')} — {motion.get('implementation')}
- Signature moment: {system.get('signature_moment')}

## Build sequence

1. Preserve existing functional behaviour and framework conventions.
2. Establish semantic tokens and global typography.
3. Implement the page composition and responsive transformations.
4. Build the primary journey with real content.
5. Add loading, empty, error, success, disabled, permission, and long-content states.
6. Implement the signature moment and reduced-motion alternative.
7. Refine composition/personality, then craft/interaction.
8. Run the audit, quality gate, typecheck, tests, production build, and screenshots.

## Acceptance gates

- The result has a recognisable product identity and cannot be mistaken for a generic template.
- The primary task or message is obvious within five seconds.
- The design uses one coherent type, colour, shape, icon, imagery, depth, and motion language.
- The selected composition—not a default wall of cards—drives the route.
- Responsive layouts are deliberately recomposed at 375, 768, 1024, and 1440px.
- Keyboard focus, contrast, accessible names, and reduced motion work.
- Loading, empty, error, success, permission, and long-content states are credible.
- The production build passes and verification evidence is recorded.

## Avoid

{bullets(system.get('avoid', []))}
'''


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--system", type=Path, default=Path(".design-studio/design-system.json"))
    parser.add_argument("--output", type=Path, default=Path(".design-studio/IMPLEMENTATION.md"))
    args = parser.parse_args()

    system_path = args.system if args.system.is_absolute() else args.root / args.system
    output_path = args.output if args.output.is_absolute() else args.root / args.output
    system = json.loads(system_path.read_text(encoding="utf-8"))
    project = detect(args.root.resolve())
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(render(system, project), encoding="utf-8")
    print(output_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
