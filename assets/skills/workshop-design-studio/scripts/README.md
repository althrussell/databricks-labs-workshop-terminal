# Workshop Design Studio tools

All tools use only the Python standard library.

## Detect the project

```bash
python3 scripts/project_detect.py --root . --json
```

Reports stack, package manager, styling systems, build commands, and brand assets.

## Search the local corpus

```bash
python3 scripts/search.py "premium travel editorial" --domain style
python3 scripts/search.py "responsive form accessibility" --stack react
```

Domains: product, style, palette, typography, layout, imagery, voice, motion,
UX, chart, and icon.

## Generate and persist a direction

```bash
python3 scripts/generate_design_system.py \
  "premium family travel planner warm editorial" \
  --project-name Wayfinder \
  --directions 3 \
  --json

python3 scripts/generate_design_system.py \
  "premium family travel planner warm editorial" \
  --project-name Wayfinder \
  --persist --output-dir .
```

## Produce implementation resources

```bash
python3 scripts/render_moodboard.py
python3 scripts/build_implementation_brief.py --root .
python3 scripts/check_contrast.py
```

## Audit and gate the result

```bash
python3 scripts/audit_project.py \
  --root . \
  --json .design-studio/audit.json \
  --markdown .design-studio/audit.md

python3 scripts/quality_gate.py \
  --root . \
  --config templates/quality-gate.json
```

Static checks do not replace browser, keyboard, screen-reader, performance, or
human visual review. They catch common omissions and generic-pattern sprawl.
