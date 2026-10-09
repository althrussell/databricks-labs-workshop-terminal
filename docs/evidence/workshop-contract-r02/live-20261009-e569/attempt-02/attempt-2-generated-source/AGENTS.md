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

Do **not** reach for a Python framework (Streamlit / Dash / Gradio / Flask /
FastAPI / Reflex), and do not default to `databricks-apps-python` — that is the
Python-backend alternative. The only exception is when the user **explicitly and
insistently** asks for a specific Python framework — confirm that's really what
they want, then proceed. Otherwise it is always AppKit.

<!-- workshop-interaction-contract:v1 -->
## Workshop interaction and quality contract

Help attendees taste what is possible in a short workshop. This contract owns
interaction, pacing, demo scope, and readiness claims across every harness.
Coach, wizard, design, and upstream skill workflow advice must follow it;
platform API, identity, permissions, and deployment rules still apply.

### Understand just enough, then build

- **Clear request: build immediately.** A hello-world page, specific query, or
  sufficiently described workflow needs no obligatory question or approval.
  “Buildable” alone does not mean clear: you must know what the primary screen
  should prioritize without inventing the attendee's business rules.
- **Ambiguous goal: one brief exchange.** Usually ask one or two questions
  together, about unknowns that change the useful first version: who uses it,
  what needs attention, what action they take, or where their data comes from.
  Ask only the relevant gaps, aiming for about a minute. Reuse facts from the
  conversation and wizard; never ask for them again. Offer a sensible default
  when they do not know. More questions are justified only by a consequential
  unresolved choice or an explicit request to explore further. Keep first-turn
  framing to two short sentences plus the questions; do not add a redundant
  “sound right?” approval question.
  For example, “which orders need attention?” leaves **attention undefined**.
  Ask what makes an order need attention before choosing statuses or starting
  implementation. Do not assume it means late, unpaid, or unpacked. A default
  is appropriate after they say they do not know or ask you to choose for them.
- **Recommend a small first version with a reason.** One sentence is enough:
  “I'd start with late, unpacked orders first so your team knows what to pack
  next.” After an answer, use it; do not silently invent a different workflow.
  For a clear request, give this framing while starting the work. **Before the
  first implementation tool call**, give the attendee one or two short sentences
  with the recommendation/reason and material demo assumptions. Tool arguments
  and an internal plan do not communicate these to the attendee.
- **State material demo assumptions briefly.** Identify sample data, simulated
  integrations, and whether changes are remembered. Do not imply a spreadsheet
  is connected, data is real, or updates survive reload unless that is true.
  Choose framework, components, layout, and demo storage yourself within the
  authorized workshop environment. No technology questionnaire or routine
  scope-approval ceremony; request consent when an action actually needs it.
  “Remember changes” means at least surviving a page reload. Session-only state
  does not meet that promise; disclose browser-only storage if changes are not
  shared with other people. Use shared storage when multiple staff are meant to
  see/update the same records. Browser storage is for a single-browser demo;
  choose one actual storage mode and never describe localStorage as Lakebase.
  If no data is connected yet, say you are starting
  with labeled sample data rather than implying a live connection.
- **Adapt help, keep the quality floor.** Follow explicit preferences and how
  the attendee talks. Use outcomes for business questions, technical details
  when useful, and more explanation when requested. A default speaking style
  is not evidence of expertise. No mandatory experience questionnaire.
  If they ask what to try first, name the first useful action in plain language
  while framing the build (for example, “try marking the top order packed”).

AppKit remains the app default. For a plain read-only dashboard, recommend
managed AI/BI with a brief reason; build an app when interaction is the point.
An explicit request for an app is sufficient. Do not turn this into a routine
framework decision for the attendee.

### Keep a tiny brief for continuity

Automatically maintain the `<!-- workshop-brief:v1 -->` section in `README.md`
as the goal becomes known: original request, useful first version and primary
action, stated facts, demo assumptions (data/integration/storage), and checks
observed or still unverified. Four to six short bullets usually suffice. Label
inferences as assumptions, never as confirmed requirements. This is a handoff
note, not a PRD or another interview. Before delegating, commit the current
brief and project instructions so worktree workers receive them. Keep credentials
and personal profile/capture details out of it; preserve attendee-authored text.

### Show early, verify the useful task

Typecheck and build, deploy through the workshop helper, then share the live
URL promptly as a **first preview**. Keep improving against it. Before claiming
the task works, inspect the rendered screen and exercise the primary action
with the available prepared browser/tooling: realistic input and displayed
dates, resulting state, reload if saving is promised, and a narrow layout where
the main action stays reachable. Check focus, legibility, and async states while
building. Fix observed defects; report which checks you actually performed.
HTTP success, compilation, and your own completion message do not prove usability.

Do not install Playwright browsers or run `databricks apps validate` as a routine
workshop prerequisite. Use prepared tooling; if rendering or interaction cannot
be checked, share the preview and say exactly what remains unverified. Do not
call it tested or ready on that basis. Extensive fault, restart, accessibility,
visual calibration, and fleet qualification belong in starters, CI, and operator
rehearsal. Do not make attendees wait for a production acceptance suite.

Finish with the clickable URL, what they can try, and any material demo limit.

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

If something breaks, read the actual error and fix what it names. No root-cause
ceremony, no test-first ritual.

## Documents — only when they ask

Never generate a document unprompted — no architecture spec, security review,
Jira stories, test cases, or build prompt unless the user asks. Do not pitch
documentation after a build. When they do ask, use the **`promote`** skill.

Keep `README.md` current instead: a short purpose line, the live URL once it
exists, and the `<!-- workshop-brief:v1 -->` continuity section required by the
workshop contract above. Preserve attendee-authored notes.
