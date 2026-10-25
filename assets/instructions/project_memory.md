# Project memory — Databricks Workshop

<!-- workshop-project-memory -->

<!-- terminal-safety-slot -->

This file is committed as both `CLAUDE.md` and `AGENTS.md` so every agent,
including an Omnigent worker in an isolated worktree, receives it. When you
resume this project, read `.workshop/project.json`, `workshop-workflow show`,
the README workshop brief and `PRODUCT.md`/`DESIGN.md`. Keep other builds
separate and preserve the attendee's framework and written work.

<!-- workshop-contract-slot -->

## Deployment — typecheck, deploy, share the preview

Scaffold new apps with `workshop-init-project <name> --apx` (`--agentbricks`
for a custom agent backend, deployed with `agentbricks deploy`). Use
`databricks-lakebase` for shared saved data. Deploy with the
`deploy_databricks_app` MCP tool (absolute project path from `pwd`, target
`dev` for APX) or `workshop-app-deploy --project "$PWD" --target <target>`; a
timeout resumes, it is not a failure. If something breaks, read the error and
fix what it names.

<!-- discovery-anchor -->

Write documents only when asked. For take-home code use `workshop-export`
(`--project` for one project), show its download steps, and remind the
attendee to download before teardown; the Workspace sync and the Volume are
deleted after the workshop.
