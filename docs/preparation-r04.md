# R04: reliable preparation

Status: implemented and qualified on the reused Labs deployment; PR94 closeout.

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
evaluator API checks, Agent Bricks CLI lifecycle and reproducible packaged-runtime
checks on the final implementation candidate. The Agent Bricks check caught a
missing fresh-scaffold commit; the helper now commits its freshly generated source
while adoption retains unrelated attendee work.

Live qualification on 10 October reused `wt-eval-r03-1009-f601-wt` as
`labuser+1@awsbricks.com`, with evaluation disabled. The implementation revision
was `e204fa8`; its verified PEX SHA-256 was
`d2290ad02c3418c8e825d92cb303bef6fa4043bc4ba690cbe84e76c27126e8d1`.
Deployment `01f1c47e44251c589bcd59a181ae879c` succeeded. The deployed source
snapshot and runtime bootstrap digest both matched the candidate. The existing
CT-compatible model-policy snapshot verified successfully; no CT requests or
workspace grant mutations were made.

- With onboarding skipped, Claude Code 2.1.295 / Opus 5.5 created a plain test
  project using the delivered helper. It returned `ready: true`, committed
  project policy, the README brief and actual skill assets, and reported Node
  24.21.0 and Databricks CLI 1.20.0.
- The helper and project-memory template were moved to recoverable test backups.
  Codex 0.162.0 / GPT Sol 6.1 launch restored both before the agent ran. The
  restored files matched their backups byte for byte; the agent did not restore
  them. Reusing the project returned `adopted` / `up_to_date` and retained an
  untracked attendee note with its original content and digest.
- Codex created a detached Git worktree and read its real, committed AppKit
  skill, project policy and README brief. The worker tree was clean.
- Warm redeploy `01f1c47f5dc41873b24c2d6b8ccd2158` used the same app identity,
  compute, package and configured data root, with onboarding explicitly disabled.
  Required content was rebuilt successfully and normal direct harness launch
  remained available.

The warm redeploy started a fresh local home: the test project/note from the
previous process did not survive it. This qualifies preparation after redeploy,
not project persistence. Recovery of earlier workspace-backed projects after
runtime replacement remains an explicit R08/R10 release check. No generated app
or workspace project was deleted by this qualification.

Codex displayed its existing custom-model fallback-metadata warning but completed
the checks. It also advertised the newly available 0.162.1 patch; this qualification
used the immutable 0.162.0 toolchain. Refresh and requalify the toolchain before
the final release. Omnigent was disabled in this Labs deployment; its UI-first
preparation and recovery were verified through local regressions.

When reusing stopped app compute, starting it can relaunch the prior package with
its prior environment overrides. Wait for that deployment to finish before
submitting the candidate, pass the candidate environment explicitly, and verify
both the immutable source snapshot and the package digest in the runtime startup
log. A successful source snapshot alone does not establish the running package.
Screenshots, receipts and raw outputs are outside Git under the private R04
qualification directory.
