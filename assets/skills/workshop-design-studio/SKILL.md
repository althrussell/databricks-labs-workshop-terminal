---
name: workshop-design-studio
description: 'Mandatory creative direction, design-system, and visual-quality skill for ANY interface built in the workshop. Use whenever creating, changing, reviewing, or finishing a web app, website, dashboard, internal tool, AI experience, portfolio, campaign page, or other visual UI. Runs autonomously: it never asks the attendee design questions and never explains its own process. Brand-neutral — infer or preserve the product brand instead of imposing Databricks styling. Skip only for backend-only, infrastructure-only, or non-visual tasks.'
metadata:
  version: 3.0.0
  workshop_default: true
  design_scope: universal
---

# Workshop Design Studio

Every interface an attendee leaves with should look like a senior product
designer, brand designer, and frontend engineer worked on it. Not "clean UI" —
a coherent, distinctive, credible product that reads as intentionally designed.

Attendees are not designers, and most are not engineers. They will not ask for
this and should never be made to think about it. You do the work; they get the
result.

## Autonomous operation — this overrides everything below

This skill runs silently. Four rules, and they are not negotiable:

1. **Never ask the attendee a design question.** No "which direction do you
   prefer", no palette choices, no layout options, no brand questionnaire.
   Infer product, audience, and tone from what they asked for and the data they
   are working with. Where evidence is thin, decide and record the assumption in
   `.design-studio/creative-brief.md`.
2. **Generate three directions internally, present none.** Explore genuinely
   different options, pick the one that best serves the audience and the primary
   task, write down why, and build it. The exploration is real; the deliberation
   is private.
3. **Never narrate the process.** Do not mention design systems, creative
   directions, dials, moodboards, critique passes, audits, or this skill by
   name. The attendee hears what their product does, never how it was made.
4. **Never let design become a blocker they can see.** No pausing to confirm,
   no "before I continue" checkpoints. If you need a decision, make it.

The one exception: if the attendee *asks* about branding, colours, or design —
or supplies a brand kit — engage with them directly. Then design is the topic
they raised, and discussing it is the point.

### How to talk about the result

The workshop instructions record whether this attendee is technical or
business-oriented. Match it:

- **Business** — outcomes only. "Your order tracker is live — your team can see
  what's late at a glance and update a status without leaving the page."
- **Technical** — architecture is welcome, design vocabulary still is not.
  Describe the components and data flow, not the type scale.

Never say "I applied a design system" or "I ran a critique pass". That is
process. It is the part they hired you not to think about.

## The deployment platform is not the brand

An attendee's app may look like a customer brand, a consumer product, a premium
editorial site, a playful learning experience, or a cinematic AI product. Never
impose Databricks colours, density, typography, or console chrome unless the
attendee asks for platform-native styling or the existing product clearly
requires it.

Running on Databricks Apps is a deployment fact, not an art direction.

## Companion skills

The workshop builds AppKit apps (Node.js + TypeScript + React), so assume
AppKit unless detection says otherwise:

- `databricks-apps` — scaffolding, plugins, auth, deployment.
- `databricks-app-design` — data-UI decisions when the app shows KPIs, reports,
  charts, tables, query results, or a Genie assistant. It maps those to real
  AppKit components.
- `databricks-lakebase` — persistence when the app saves data.

This skill owns creative direction and visual quality. Those skills own
framework mechanics and data-UI component choice. When they conflict on
composition, this skill wins; when they conflict on API shape, they win.

For the rare non-AppKit project, run detection and preserve whatever stack is
already there:

```bash
python3 <skill-dir>/scripts/project_detect.py --root . --json
```

## Workflow

Six phases. In a time-boxed workshop, run the fast path: same six phases, no
attendee involvement, one route done properly.

### 1. Discover

Establish, without asking:

- product type and what it is for;
- audience and the emotional register that suits them;
- the primary task or decision the interface exists to serve;
- brand evidence already present — assets, colours, type, copy voice;
- content volume and density;
- stack, component library, styling system, build commands.

### 2. Direct

```bash
python3 <skill-dir>/scripts/generate_design_system.py \
  "<product + audience + tone + key task>" \
  --project-name "<name>" --directions 3 \
  --expression 7 --motion 5 --density 5 --depth 6 --brand-fidelity 8
```

The three directions must differ in composition and personality, not just
colour. Each covers concept, layout pattern, typographic character, palette
strategy, art direction, depth and motion, a signature moment, and its risks.

Select one yourself. Record the choice and the reason in the creative brief.
Do not show the attendee a menu.

### 3. Systemise

```bash
python3 <skill-dir>/scripts/generate_design_system.py \
  "<resolved product brief>" --project-name "<name>" \
  --persist --output-dir . \
  --mode <brand-led|product-led|platform-native|experimental> \
  --expression <1-10> --motion <1-10> --density <1-10> \
  --depth <1-10> --brand-fidelity <1-10>
```

Produces `.design-studio/` containing `MASTER.md`, `design-system.json`,
`creative-brief.md`, `pages/`, `audit.json`, and `verification.md`.

Read `MASTER.md` before any later page. Page overrides refine the system; they
never start a second visual language.

```bash
python3 <skill-dir>/scripts/render_moodboard.py \
  --system .design-studio/design-system.json \
  --output .design-studio/moodboard.html --project-name "<name>"

python3 <skill-dir>/scripts/build_implementation_brief.py \
  --root . --system .design-studio/design-system.json \
  --output .design-studio/IMPLEMENTATION.md

python3 <skill-dir>/scripts/check_contrast.py \
  --system .design-studio/design-system.json
```

These are your reference material, not a deliverable to present. They travel
with the attendee in the promote handoff pack.

**Design modes.** `brand-led` when identity exists; `product-led` to derive one
from product and audience; `platform-native` only on request; `experimental`
for deliberately bold work. Default to brand-led where there is brand evidence,
otherwise product-led. Never default to platform-native because the app happens
to run on Databricks.

**Design dials.** `expression` (restrained to memorable), `motion` (static to
choreographed), `density` (spacious to data-dense), `depth` (flat to layered),
`brand-fidelity` (reinvention to strict adherence). Constraints, not decoration
quotas.

### 4. Build

**Composition before components.** Every page needs a clear visual entry point,
one dominant message or work surface, deliberate hierarchy, a composition that
suits the product rather than a default card grid, responsive transformation
rather than shrinking, and real content instead of lorem ipsum.

Use cards only when content is genuinely modular. A landing page may want
full-bleed sections; an investigation tool, continuous panes; a dashboard,
dense comparison tables. Choose the right grammar.

**Brand.** Use supplied assets consistently. Where none exist, build a compact
system: name and one-line promise, colour roles with contrast-checked
foregrounds, display and body type with real fallbacks, one icon family, an
imagery approach, a shape and elevation language, and a copy voice. Do not
invent a logo unless asked — a strong wordmark beats an improvised mark.

**Signature moment.** Exactly one or two, appropriate to the product: a
cinematic hero, an interactive before/after, a live data transformation, a
command palette, a meaningful scroll transition, a delightful empty state, a
polished AI thinking trace. It must aid understanding or emotion. Do not spread
glow, parallax, and animation across everything.

**Assets,** in order of preference: attendee-supplied and existing project
assets; official brand assets used correctly; locally generated SVG, CSS art,
diagrams and patterns; properly licensed local downloads; clearly marked
placeholders. Never hotlink production images. Never use emoji as structural
icons.

**Framework discipline.** Preserve the stack and package manager. Prefer
existing primitives, but do not let a component library dictate composition.
Create semantic tokens rather than scattered raw values. For AppKit, apply the
brand through supported theme variables and application CSS, and verify
component exports against the installed version. Do not add a heavy animation
library for one effect.

### 5. Refine

A first pass is never the last. Two critique loops, both silent.

**Pass A — composition and personality.** Does the page carry a strong visual
idea? Is the primary task obvious in five seconds? Could this layout belong to
any generic SaaS template? Do type, colour, imagery, and spacing tell one
story? Fix the highest-impact problems before polishing details.

**Pass B — craft and interaction.** Alignment, spacing rhythm, optical balance,
line length. Icon consistency, border weights, radii, shadows. Hover, focus,
pressed, loading, empty, error, success, disabled. Motion timing, easing, and
reduced-motion alternatives. Reflow at 375, 768, 1024, and 1440px. Copy quality.

### 6. Verify

```bash
python3 <skill-dir>/scripts/audit_project.py --root . \
  --json .design-studio/audit.json --markdown .design-studio/audit.md

python3 <skill-dir>/scripts/quality_gate.py --root . \
  --config <skill-dir>/templates/quality-gate.json
```

Then the project's own checks. For AppKit that is `databricks apps validate`,
which runs `appkit lint`, `tsc --noEmit`, and the smoke test — extend
`tests/smoke.spec.ts` with the visual assertions in
`templates/playwright.visual.spec.ts` (focus visibility, no horizontal overflow
at 375px) rather than adding a second Playwright setup.

Verify light and dark themes where supported, the four breakpoints, keyboard
navigation and visible focus, contrast and non-colour status cues, every
interaction state, reduced motion, font and image loading, no horizontal
overflow, and no console errors. Record evidence in
`.design-studio/verification.md`.

**The gate is blocking.** A red quality gate or a failed audit means the build
is not done. Fix it, or say plainly what is broken. Never report a build as
live and never offer promote on a red gate.

## Anti-patterns

Always reject: generic template composition unrelated to the product; a wall of
identical cards as the default grammar; arbitrary style mixing; inaccessible
contrast or colour-only meaning; emoji as structural icons; fabricated metrics,
testimonials, or customer logos; placeholder copy presented as finished; broken
responsive states; motion without a reduced-motion path; inconsistent icons,
radii, and spacing; remote image hotlinks; hidden loading, empty, and error
states; declaring success without build and visual verification.

Use contextually, judged by fit and execution rather than banned outright:
gradients, glass, blur, large type, unconventional grids, 3D, strong shadows,
maximalism, brutalism, dark mode, and motion. None of these is quality by
itself.

## Fast path — the workshop default

Roughly 45–60 minutes, no attendee involvement in any design decision:

1. **5 min** — detect stack, product, audience, brand, primary task.
2. **5 min** — generate three directions, select one, record why.
3. **5 min** — persist the compact design system.
4. **25 min** — build one complete primary experience, not five unfinished pages.
5. **10 min** — both critique passes, applied.
6. **5 min** — audit, quality gate, build, responsive and accessibility check.

One extraordinary route beats a broad generic prototype.

## Done means

- a distinct, coherent visual identity that serves product, audience, and task;
- nothing that still looks like an untouched framework starter;
- type, colour, layout, imagery, and motion following one concept;
- intentionally composed responsive states;
- all core interaction and product states present;
- accessibility, contrast, and reduced-motion checks passing;
- audit and quality gate green, and the project's own build passing;
- `.design-studio/` recording the direction and the verification evidence;
- and the attendee never having been asked a single design question.

## References

Read only what the current phase needs: `operating-model.md`,
`creative-direction.md`, `product-archetypes.md`, `brand-art-direction.md`,
`layout-composition.md`, `typography.md`, `colour-and-contrast.md`,
`depth-and-effects.md`, `motion.md`, `imagery-and-assets.md`,
`responsive-design.md`, `accessibility.md`, `forms-and-states.md`,
`data-visualisation.md`, `appkit-compatibility.md`,
`critique-and-refinement.md`, `signature-moments.md`,
`contextual-anti-patterns.md`, `workshop-fast-path.md`.
