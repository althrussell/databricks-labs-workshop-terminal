# Project memory

<!-- workshop-project-memory -->

<!-- terminal-safety-slot -->

If another agent handed you a task, do that task and report back to it. On resume, read `workshop-workflow show` and README; stage boundaries are a safe point to compact.

## Deployment — typecheck, deploy, share the preview

- Ask if no path is recorded: "Would you like a rapid prototype to react to, or guided ideation" ("Shall I build this direction, or refine it further?"). Check available data before inventing it.
- Official APX (React + FastAPI) is the default (`workshop-init-project <name> --apx`, `apx`; AppKit via `databricks-apps`). Use `impeccable`: `impeccable context`, `DESIGN.md`, `reference/craft-floor.md`, `$WORKSHOP_DESIGN_REFERENCES`, and run `detect` once (no phone viewports or `impeccable-finish-reviewer`).
- Run `apx dev check` and `apx build`, deploy with `workshop-app-deploy` (never `databricks bundle deploy`), share first preview to try in their browser (or desktop `workshop-browser`; its `--deployed` opens the live app signed in as the attendee), say what remains unverified. Do not install Playwright browsers or run `databricks apps validate`.
