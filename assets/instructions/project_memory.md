# Project memory — Databricks Workshop

<!-- workshop-project-memory -->

This file is committed to the repo so the rule below travels with the project
into every agent and harness. Claude reads `CLAUDE.md`; Codex reads `AGENTS.md`;
Omnigent's sub-agents read whichever applies — including the Codex worker, which
runs in an isolated `CODEX_HOME` + git worktree and therefore only ever sees a
*committed, project-level* `AGENTS.md`. That is why this content is duplicated
into both files and committed on the first commit.

## Always build apps with AppKit

AppKit is the required baseline for every app. Every app, interactive tool, or
custom UI in this project MUST be built with **AppKit** (Node.js + TypeScript + React) via
the **`databricks-apps`** skill.

Custom agent backends use **`workshop-agent-bricks-cli`** and its supported
Python `DurableAgentServer` runtime when the optional CLI is enabled. This is
an explicit exception for the backend; any attendee-facing UI still uses
AppKit and the workshop design skills. Scaffold with
`workshop-init-project <name> --agentbricks`. Deploy that backend with
`agentbricks deploy`, and its separate AppKit UI with the workshop app tool.

Scaffold with `workshop-init-project <name> --appkit` (add
`-- --features <plugins>` to pass AppKit flags through). Do not run
`databricks apps init` directly: it always creates a subdirectory named after
the app and refuses to write into an existing directory, so calling it by hand
produces `<name>/<name>` and a manual `mv` that overwrites this file.

Also required:

- **`workshop-design-studio`** for anything with a visible interface, every
  time. It carries the visual baseline and ready-made AppKit patterns — start
  from those rather than inventing layout from scratch.
- **`databricks-app-design`** whenever the app shows any data — KPI page,
  report, chart, table, query results, or a Genie/chat assistant. It sets chart
  choice, semantic color, and AI-result provenance, mapped to real AppKit
  components.
- **`databricks-lakebase`** for shared or database-backed saved data. Provision it
  non-interactively — never click resources together in the Databricks UI. Apps
  with no saved state skip Lakebase. A clearly labeled browser-only demo can
  use browser storage; do not represent it as shared staff data.

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
  alt text on meaningful images, and layouts that survive a narrow window. Check
  the rendered result with the available tooling.
- **One memorable moment per app.**

After the first deploy, inspect the primary screen and useful task as described
in the workshop interaction contract. Fix observed defects and describe the
change in product terms.

For attendee-facing UI, do **not** reach for a Python framework (Streamlit / Dash / Gradio / Flask /
FastAPI / Reflex), and do not default to `databricks-apps-python` — that is the
Python-backend alternative. The only exception is when the user **explicitly and
insistently** asks for a specific Python framework — confirm that's really what
they want, then proceed. Otherwise it is always AppKit.

<!-- workshop-contract-slot -->

## Deployment — typecheck, deploy, share the preview

Use these deployment steps alongside the contract's practical task checks:

1. **Typecheck and build** (`npx tsc --noEmit`, then the build). Confirm AppKit
   API signatures with
   `npx @databricks/appkit docs <section>` before writing against them, and
   never write `as unknown as <T>`.
2. **Deploy with the `deploy_databricks_app` Workshop MCP tool**, passing this
   project's absolute directory (resolve it with `pwd`) and target. Relative
   MCP project paths are rejected. It uses `databricks apps deploy`, polls
   `databricks apps get`, waits for the exact deployment and app compute, and
   returns the live URL. Do not substitute bare `databricks bundle deploy` or
   arbitrary `sleep`/log-tail loops. If MCP is unavailable, run
   `workshop-app-deploy --project "$PWD" --target <target>`. A timeout on the
   deploy command is not a failed deploy; retrying the tool resumes it.
3. **Hand over the URL** and keep improving against it.

<!-- discovery-anchor -->

If something breaks, read the actual error and fix what it names. No root-cause
ceremony, no test-first ritual.

## Documents — only when they ask

Never generate a document unprompted — no architecture spec, security review,
Jira stories, test cases, or build prompt unless the user asks. Do not pitch
documentation after a build. When they do ask, use the **`promote`** skill.

Keep `README.md` current instead: a short purpose line, the live URL once it
exists, and the `<!-- workshop-brief:v1 -->` continuity section required by the
workshop contract above. Preserve attendee-authored notes.
