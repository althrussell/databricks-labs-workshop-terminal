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

Initial local verification passed 182 focused backend checks and all 111 frontend
checks; the production frontend rebuilt successfully. The final helper/worker
suite passes 18 checks, including actual Git-worktree skill inheritance. The full
backend run identified obsolete test-fixture references to the removed cache and
binary-only readiness fixtures, which have been updated. Its package-source check
also detects the existing laptop-only `uv.lock` proxy rewrite; that file is kept
out of this change. CI will verify the committed public-source lock.
