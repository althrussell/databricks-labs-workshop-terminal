# Project memory — Databricks Workshop

<!-- workshop-project-memory -->

This file is committed to the repo so the rule below travels with the project
into every agent and harness. Claude reads `CLAUDE.md`; Codex reads `AGENTS.md`;
Omnigent's sub-agents read whichever applies — including the Codex worker, which
runs in an isolated `CODEX_HOME` + git worktree and therefore only ever sees a
*committed, project-level* `AGENTS.md`. That is why this content is duplicated
into both files and committed on the first commit.

## Always build apps with AppKit

AppKit is the required baseline for every app. Every app, dashboard, tool, or UI
in this project MUST be built with **AppKit** (Node.js + TypeScript + React) via
the **`databricks-apps`** skill — scaffold with `databricks apps manifest` then
`databricks apps init --features <plugins>`.

Also required:

- **`workshop-design-studio`** for anything with a visible interface, every
  time. It carries the visual baseline and ready-made AppKit patterns — start
  from those rather than inventing layout from scratch.
- **`databricks-app-design`** whenever the app shows any data — KPI page,
  report, chart, table, query results, or a Genie/chat assistant. It sets chart
  choice, semantic color, and AI-result provenance, mapped to real AppKit
  components.
- **`databricks-lakebase`** when the app needs to save data. Provision it
  non-interactively — never click resources together in the Databricks UI. Apps
  with no saved state skip Lakebase.

**Where they overlap, the split is by surface.** `databricks-apps` owns
scaffolding, APIs, and deployment. **Inside a data surface** — charts, KPIs,
tables, query results, Genie answers — `databricks-app-design` owns the
decisions, and on any chart-vocabulary conflict it wins outright. **Everywhere
else** — page composition, navigation, brand, typography, spacing, motion,
empty-state character — `workshop-design-studio` owns it. An app with no data
surface (a game, a landing page, a toy) uses the design studio only.

### Design runs silently

Never ask the user a design question — no palette, layout, or creative-direction
choices — and never narrate the process. Do not mention design systems or
baselines; describe what the product *does*. Infer the brand from the product,
and do not impose Databricks styling on it. The exception is when the user
raises design or supplies a brand kit themselves, which makes it their topic and
worth discussing properly.

### The visual baseline — non-negotiable, applied while you build

Apply this as you write components, not as a pass afterwards.
`workshop-design-studio` has ready-made AppKit patterns for the app shell,
first-run state, KPI row, chart card, table, empty/loading/error states, and
forms — start from those.

- **Type does the hierarchy** — a real scale with a genuinely large primary
  heading. Never a page where everything is 14-16px.
- **Space generously and consistently**, on one rhythm. Cramped default padding
  is the clearest tell of an untouched template.
- **One accent colour, used for meaning** — the primary action, the live value,
  the thing that changed. Colour as decoration is worse than no colour.
- **Give the page a focal point.** If everything competes equally, nothing reads.
- **Real loading, empty, and error states** for anything asynchronous.
- **Considered surfaces** — deliberate background, border, and elevation, not
  stock cards on stock grey.
- **Motion on state change**, brief and purposeful, honouring reduced motion.
- **Accessible by construction:** contrast at least 4.5:1, visible focus states,
  alt text on meaningful images, and layouts that survive a narrow window. There
  is no gate that will catch these later.
- **One memorable moment per app.**

After the first deploy, take one look at your own work against that list, fix
what is cheap, and describe the change in product terms. One pass, in context —
no script, no browser run, no document.

Do **not** reach for a Python framework (Streamlit / Dash / Gradio / Flask /
FastAPI / Reflex), and do not default to `databricks-apps-python` — that is the
Python-backend alternative. The only exception is when the user **explicitly and
insistently** asks for a specific Python framework — confirm that's really what
they want, then proceed. Otherwise it is always AppKit.

## Tempo — get something on their screen fast

- **Show something early.** Build a thin but real version, deploy it, and hand
  over the URL as soon as it renders. Then keep improving it. Never disappear
  into a long build with nothing on screen.
- **Iterate against the live URL** — change, redeploy, say what to look at.
- **At most one or two questions**, and only when the answer changes what you
  build. Trivial or self-contained asks get zero questions.
- **Short todo lists**, named by outcome, only for work the attendee can see.
- **Scaffold minimally** — only the AppKit features the app actually needs.
- **Never announce process.** Describe what the product does.

## The ship gate — typecheck, deploy, open the URL

**Ship when the app is deployed and the URL loads.** That is the whole gate:

1. **Typecheck and build** (`npx tsc --noEmit`, then the build). Seconds, no
   browser. Confirm AppKit API signatures with
   `npx @databricks/appkit docs <section>` before writing against them, and
   never write `as unknown as <T>`.
2. **Deploy, then open the URL once** to confirm it responds.
3. **Hand over the URL** and keep improving against it.

**Do not** run `databricks apps validate`, install Playwright browsers, or
write or update `tests/smoke.spec.ts` unless the user asks for tests or a
deploy has already failed and you are debugging it. Never install Chromium as a
condition of calling something done. This overrides the `databricks-apps`
skill's instruction to always update the smoke test before validating.

If something breaks, read the actual error and fix what it names. No root-cause
ceremony, no test-first ritual.

## Documents — only when they ask

Never generate a document unprompted — no architecture spec, security review,
Jira stories, test cases, or build prompt unless the user asks. Do not pitch
documentation after a build. When they do ask, use the **`promote`** skill.

Keep `README.md` current instead: one line on what the app is for, plus the
live URL once it exists.
