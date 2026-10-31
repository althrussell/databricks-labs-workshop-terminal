# Claude Code on Databricks — Workshop Edition

<!-- terminal-safety-slot -->

<!-- workshop-teammate-slot -->

<!-- workshop-contract-slot -->

## Databricks essentials

- Profiles are preset: never ask the attendee to choose one, whatever a skill
  says. `databricks …` (`DEFAULT`) builds, deploys, provisions, and reads;
  `databricks-me …` (`me`) reads as the attendee for row/column security. In
  Python: `WorkspaceClient(profile="DEFAULT")` builds, `profile="me"`
  reads. Run `workshop-grant-me` for instant access. Never query `system.*`
  tables (`system.billing`, `system.access`, `system.compute`).
- Data lives in their catalog `$WORKSHOP_CATALOG` (`$WORKSHOP_SCHEMA` when
  set), the read-only `$WORKSHOP_DEMO_CATALOG`, and anything they upload.
  Query `$WORKSHOP_DEMO_CATALOG` directly and join freely across all three;
  never copy a demo table just to read it. Write new derived tables or views
  in `$WORKSHOP_CATALOG`; app write-back goes to Lakebase. Copy demo rows only
  to edit them or apply governance to them, and then only the rows you need.
  If the demo data only partly fits, extend it: their upload, or labelled
  synthetic rows. If a permission blocks a read, run `workshop-grant-me` and
  say so. Create objects only in `$WORKSHOP_CATALOG`, never a new catalog.
- Official APX (React + FastAPI) is the default (`workshop-init-project <name>
  --apx`, `apx`; AppKit via `databricks-apps`). Use Streamlit only if asked.
  Saved records use `databricks-lakebase` (autoscaling); a Genie Agent uses
  `databricks-genie-agents`; other chat uses `workshop-agent-bricks-cli`. For take-home code, use
  `workshop-export`; `~/.claude/skills` has `workshop`.

## Deployment — typecheck, deploy, share the preview

Run `apx dev check` and `apx build`, then deploy only with
`workshop-app-deploy` (`deploy_databricks_app` MCP), never `databricks bundle
deploy`. Share the URL as a preview. A deploy timeout resumes when rerun.

apx preview: URL from `apx dev start`, not `$DATABRICKS_APP_PORT`.

<!-- discovery-anchor -->

If something breaks, read the error and fix what it names. Never print credentials.
