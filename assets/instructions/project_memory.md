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

- **`databricks-app-design`** whenever the app shows any data — KPI page,
  report, chart, table, query results, or a Genie/chat assistant. It sets
  layout, charts, semantic color, loading/empty/error states, and AI-result
  provenance, mapped to real AppKit components.
- **`databricks-lakebase`** when the app needs to save data. Provision it
  non-interactively — never click resources together in the Databricks UI. Apps
  with no saved state skip Lakebase.

Do **not** reach for a Python framework (Streamlit / Dash / Gradio / Flask /
FastAPI / Reflex), and do not default to `databricks-apps-python` — that is the
Python-backend alternative. The only exception is when the user **explicitly and
insistently** asks for a specific Python framework — confirm that's really what
they want, then proceed. Otherwise it is always AppKit.

## Validate before calling an AppKit build done

1. Update `tests/smoke.spec.ts` selectors to match the real UI first — the
   template's "Minimal Databricks App" heading and "hello world" text are gone
   from your app, so validation fails until you do. Playwright locators only
   (`getByRole`, `getByText`, `getByPlaceholder`, `getByLabel`); there is no
   `getByLabelText`. Keep asserted result sets under 1 MB.
2. Run `databricks apps validate` — it runs `appkit lint`
   (no `as unknown as <T>` double assertions), `tsc --noEmit`, and the smoke
   test. Confirm AppKit API signatures with
   `npx @databricks/appkit docs <section>` before writing against them.
3. Do not report success or offer Promote while validation is red.

## After a build completes — always offer Promote

When any build or deployment succeeds, make this offer exactly once:

> "Your build is live! Want me to generate handoff docs — architecture spec,
> security review, Jira stories, test cases, and a build prompt — and upload
> them to your Databricks Volume? Just say yes."

If the attendee agrees:
1. Ask for a one-sentence description of what was built (if not already clear from context).
2. Generate each document from the conversation and description:
   - `architecture.md` — components, data flow, Databricks services used
   - `security.md` — auth model, data access, risks, recommendations
   - `jira_stories.md` — 4–6 sprint-ready user stories with acceptance criteria
   - `test_cases.md` — unit, integration, and E2E test scenarios
   - `build_prompt.md` — a single self-contained prompt to recreate the app
3. Write each to `/tmp/promote/<doc>.md`, then upload:
   ```sh
   PROMOTE_PATH="/Volumes/${WORKSHOP_CATALOG}/${WORKSHOP_SCHEMA}/promote/<user-email>/$(date +%Y%m%d-%H%M%S)"
   databricks files upload /tmp/promote/<doc>.md "${PROMOTE_PATH}/<doc>.md" --overwrite
   ```
4. Report the full Volume path to the attendee.
