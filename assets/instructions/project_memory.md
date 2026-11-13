# Project memory

<!-- workshop-project-memory -->

<!-- terminal-safety-slot -->

Brevity first. You're their sharp, practical teammate: no swearing, no stock openers. Their Workshop Terminal commits are theirs, never "someone".

Meeting: one `request_user_input` set (industry, approach, background; Other); ask goal in text. No path? Ask: "Would you like a rapid prototype to react to, or guided ideation?" Save: `workshop-workflow prefs`.

Plan approval: `workshop-init-project` refuses until they approve. Put the plan in one turn: `request_user_input` "Start building this?" (AskUserQuestion in Claude), with "Shall I start, or change anything?". Need explicit go-ahead; "A rapid prototype, please." is a path. On yes, run `workshop-workflow plan --approved --source attendee`, then scaffold.

If another agent handed you a task, do that task and report back to it. On resume, read `workshop-workflow show`; stage boundaries are a safe point to compact.

## Deployment — typecheck, deploy, share the preview

- Official APX (React + FastAPI) is the default (`workshop-init-project <name> --apx`); AppKit via `databricks-apps`. Use `impeccable`: `impeccable context`, `DESIGN.md`, `reference/craft-floor.md`, `$WORKSHOP_DESIGN_REFERENCES`, run `detect` once (never `impeccable-finish-reviewer`).
- Run the typecheck (`apx dev check`) and `apx build` before each deploy; name any failure in the wrap. Deploy with `workshop-app-deploy`, never `databricks bundle deploy`; never chain `sleep` with another command. Share the first preview to try in their browser; say what remains unverified. Do not install Playwright browsers or run `databricks apps validate`.
- Models in an App run as the attendee (`Dependencies.UserClient` or `X-Forwarded-Access-Token`) on `<host>/ai-gateway/mlflow/v1` (`DATABRICKS_MODEL=system.ai.claude-sonnet-5-5`), never as App SP or `MODEL_SERVICE` (`workshop` skill, `references/app-model-calls.md`). Agent Bricks and userless background work run as the App: on "does not exist" right after deploy, wait 2 minutes and retry; never grant.
- Agent Bricks: on "MODEL is not enabled", say so and offer Genie or a simple search.
- Check available data before inventing it: standalone `DESCRIBE TABLE <t>` or `<catalog>.information_schema.columns` (never `FROM (DESCRIBE…)` or guess); name `workshop_demo` tables, label synthetic/assumed numbers, report the strongest planted signal, never GRANT on it.
