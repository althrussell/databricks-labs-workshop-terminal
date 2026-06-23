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
in this project MUST be built with **AppKit** (React + Vite + TypeScript)
following the `databricks-apps-python` skill — scaffold with
`databricks apps init` and apply the AppKit UX defaults from the skill's
`7-appkit-ux.md`.

Do **not** reach for a Python framework (Streamlit / Dash / Gradio / Flask /
FastAPI / Reflex). The only exception is when the user **explicitly and
insistently** asks for a specific Python framework — confirm that's really what
they want, then proceed. Otherwise it is always AppKit.

If the app needs to save data, provision Lakebase (Postgres) non-interactively
via the `databricks-lakebase-provisioned` skill — never click resources together
in the Databricks UI. Apps with no saved state skip Lakebase.
