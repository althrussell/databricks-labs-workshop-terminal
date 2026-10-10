# R04: reliable preparation

Status: implemented; live Labs qualification pending.

Required instructions, helpers and skill links are prepared before a local CLI
or remote Omnigent host starts. Preparation runs again on reuse, so failed writes
and deleted files can recover. Git/npm conveniences are optional. A failed
required write returns an actionable error and starts no harness process.
Preparation is independent of wizard completion and evaluation qualification.

Harness readiness includes Node and the Databricks CLI. Optional Agent Bricks
installation is independent. Failed dependency cards offer a setup retry; retries
serialize with the existing installer and repair only the requested path.

Before the background skills installer verifies the shared tree, preparation uses
this package's skills. Publication reconciles early links. Managed launcher links
follow shared upgrades; attendee skill links, agents, npm settings, Git aliases
and custom MCP entries are retained.

`workshop-init-project` validates names, refreshes bounded workshop sections and
retains attendee notes. Unbounded legacy instructions become a quoted reference
with an explicit retirement notice. Known additive notes stay active. Real skill
assets are committed under `.agents/skills`, with Claude discovery links, so an
isolated worktree can read them. A manifest tracks workshop-owned copies for
subsequent refresh/retirement. Unrelated staged work is excluded from setup commits.

Default helper stdout is a usable project path. Progress and warnings use stderr.
`--json` reports scaffold, memory and commit outcomes and exits nonzero when
preparation is incomplete. `.workshop/preparation.json` is local, uncommitted state.

Qualification is focused on one reused CT-compatible Labs WT deployment. Local
regressions cover failed-write recovery, fresh/reused homes, dependency retry,
wizard enabled/off direct launches, UI-first remote preparation, project migration,
unrelated staged work and actual skill inheritance in a Git worktree. Live checks
must confirm package identity and normal Claude/Codex preparation on the owned
test app. Raw evidence stays outside Git. CT code and the working CT deployment
remain outside this workstream.

Local verification passed 2,992 backend checks (62 opt-in/environment skips) and
all 111 frontend checks; the production frontend rebuilt successfully. This run
excluded the package-source test that detects the existing laptop-only `uv.lock`
proxy rewrite; that file is kept out of this change. CI passed both Python
versions, the public-source lock check, rendered wizard tests, frontend checks,
evaluator API checks, and reproducible packaged-runtime checks on the initial
candidate. The Agent Bricks check caught a missing fresh-scaffold commit; the
helper now commits its freshly generated source while adoption retains unrelated
attendee work. The final candidate must pass that check and live qualification.
