# Final WT release qualification

Status on 11 October: R08 continuity/recovery, R09 focused regressions and R10
single-deployment WT qualification pass, with the limitations recorded below.
The final current-toolchain candidate passed its analytical app build and concise
iteration. Immutable release publication and the separate CT integration remain.
This report maps the checks to the release scope. It adds no attendee review
workflow or fleet campaign.
Raw receipts, screenshots, archives and generated source remain outside Git.

## R09: focused regression coverage

All nine jobs passed for final PR #98 revision
`c5dc748aaf94f2fbf8c5adf8143a5cc5e2e015f9` and current-toolchain PR #99 revision
`bf544a3fd7879ffb634d78e0e1ac339258a3b1ec`. These are source and
packaged-runtime regressions; the live attendee journeys are recorded separately.

| Requirement | Existing checks |
| --- | --- |
| Public dependency sources and immutable runtime | Both Python CI jobs validate the committed lock and deployment exports. The package job builds twice, compares bytes, then runs the real entry point without networking. `tests/test_release_artifact.py` covers the manifest/content contract. |
| Official APX and one UX authority | The Ubuntu 22.04 APX job runs the real installer, helper, MCP handshake, checks and build. The Impeccable job verifies its supported installer and discovery. Provenance, preparation and content tests retain custom assets and retire competing managed skills. |
| Optional Agent Bricks | The Linux CLI job runs its actual installer and offline project lifecycle. Readiness and preparation tests cover enabled/disabled delivery. This does not establish a deployed custom agent. |
| Wizard enabled, skipped and disabled | `tests/test_wizard_rendered.py` drives launch, reload, partial delivery, failed services and pending-write races with both offered bare harnesses. It runs in the rendered CI job. |
| Separate builds and changing goals | `tests/test_projects.py` covers A→B→A, current source context, launch directories, stale selection and owner separation. `tests/test_projects_rendered.py` exercises the controls, unsent change request, reconnect and harness switch at desktop/phone widths. |
| Save-hook outcome | `tests/test_user_content.py` makes real commits through the shipped hook, including Codex's symlinked HOME. It verifies success, visible failure and preserved failure reporting after registry loss. The shortened-HOME regression failed with the previous hook. |
| Recovery and take-home export | Project tests cover exact source/context recovery, credential preparation, failed downloads and refusal to overwrite. `tests/test_workshop_export.py` reads the ZIP, checks portable context, excludes runtime/build products and verifies upload readback and failure messages. |
| Identity, credentials and recovery | Existing readiness, app-identity, OBO, tab-closed and compressed chaos tests remain in both Python CI jobs. Local controlled-clock tests are retained; overnight live soak and fleet rehearsal are outside this release scope. |
| Retained Omnigent paths | Existing remote tests bind conversations to the selected build and preserve attendee isolation. The offline package smoke retains the supported lifecycle. Omnigent is disabled on this Labs test deployment; these checks do not prove a live worker model run. |

## R10: observed single-deployment results

The isolated WT app is `wt-eval-r03-1009-f601-wt`; the attendee browser is
`labuser+1@awsbricks.com`. Existing WT compute and the disposable `repair-desk`
preview are reused. No Control Tower code or working deployment is changed.

R08 used qualified runtime `59244b180b2f89445a6909286cb3c4723b5f4158`, PEX SHA-256
`0e3cfa67975199fc51f60cf4e6528a7e584a6410d0e5e6cde10b29740be436e6`.
All 512 logical runtime files matched that qualified source revision. Deployment
`01f1c53099bc178e8bc36078fbdcbfec` succeeded on the same identity/compute;
resources and model policy were preserved. `/readyz` is HTTP 200, `ready:true`
after attendee reconnect. Disabled test-app telemetry is a soft amber check.

| Journey | Observed outcome and limit |
| --- | --- |
| Direct Claude and Codex entry with wizard off | Both native harnesses ran. Claude Code 2.1.296 used Opus 5.5; Codex 0.162.1 used GPT Sol 6.1. The existing Codex custom-model metadata warning did not prevent completion. |
| Task-appropriate UX | The [R07 comparison](ux-stack-comparison-r07.md) supports the selected APX + Impeccable default. Earlier [repair/garden results](context-aware-ux-r05.md) retain their framework, identity and rendering limits. They are dated results, not new final-package claims. |
| Library primary workflow | The live preview borrowed and returned books and preserved changes on reload. Phone controls were usable. First startup failed on Lakebase's shared `public` schema; the agent repaired its own schema after receiving the operator's log error. The unassisted first-preview criterion remains limited by that intervention. |
| Attendee iteration | Change goal saved an unsent request. Claude added author/title search and a 21-day period for new loans, retained earlier loans, and redeployed. Browser search, borrow and reload passed. |
| Multiple builds and harness handoff | Codex created a separate printable agenda. Independent ZIP readback preserved all 64 library files. Returning to the library, reloading WT and opening Codex recovered its correct current goal and framework. |
| Project-only skill/context delivery | A temporary detached worktree retained current product context and actual APX, Impeccable and Agent Bricks assets. It establishes asset delivery; no worker model execution is claimed. |
| Take-home source preservation | Corrected helper exported both recovered builds. Independent ZIP/Workspace downloads matched 56 source/context/check files, both goals and all original application files; APX build output was excluded. The helper displayed the assigned Volume download steps. |
| Analytical build and experienced-user iteration | Native Codex/GPT Sol 6.1 built Film Club with official APX and Impeccable, explored prepared `workshop_demo.media.content`, and reused `repair-desk`. First preview completed in 7m19s; an existing-app bundle conflict was diagnosed and repaired by the agent without a technical rescue prompt. Browser title search, genre selection/comparison, empty state, shortlist add/remove, redeploy/reload persistence and 390px controls passed. Ratings were unavailable and disclosed rather than invented. With Discuss technical choices saved, a 1m35s iteration added a working runtime filter; 90 minutes returned 2,030 films and saved longer films remained shortlisted. The snapshot is read-only and the shortlist is browser-local, both visibly disclosed. |
| Workspace save and warm recovery | The owner-approved isolated `CAN_MANAGE` correction matches CT; unrelated ACL entries and CT were untouched. Both builds recovered through My builds after warm replacement. Codex made a real one-file commit; the corrected detached hook recorded fresh success and its exact saved bytes were independently downloaded. Native command cleanup failed before this fix and has a real process-group regression. |

The detailed [R08 report](project-continuity-r08.md) retains the native/browser
outcomes and outstanding checks. Do not infer app correctness from a successful
deployment state alone: the library backend failed after deployment, while the
browser request and operator logs exposed the startup error.

## Latest-toolchain candidate and ship sequence

The affected live checks used candidate
`4ec275267a33803c2afaf0460ffa1520d578f3b7`, PEX SHA-256
`2faca79b4fc39b59797e6a6ef50bb5eedd03e0d17e2883b3a060aa505bf3b2b4`,
deployment `01f1c5341aac1b11807b5efce79221bf`. All 512 runtime files independently
match the rebased #99 checkout; the rebase changes ancestry only. The reused WT
app remains ready after attendee reconnect, with current enabled CLI pins and no
hard readiness failures. No CT calls, permission mutations or new compute were
needed for this refresh. The earlier owner-approved projects-folder correction is
retained separately in R08's record.

[Toolchain qualification](toolchain-release-refresh.md) records 48 current pins
out of 49 audited components. Omnigent retains its explicit paired-server hold
and is disabled here; worker asset delivery does not establish worker model
execution. The library's assisted initial startup remains a limitation. Film
Club's initial preview and iteration required no technical rescue prompt.

1. Merge the toolchain refresh with green CI and publish the verified immutable
   WT release through the existing tagged PEX/manifest workflow.
2. Verify the published manifest, artifact digest, dependency payload and logical
   runtime content. Reuse completed journeys when only release identity changes;
   repeat affected live checks if runtime code or dependencies differ.
3. Create the separately requested CT feature PR afterward: Agent Bricks opt-in
   and WT release selection, latest published stable by default. Qualify one
   disposable CT→WT deployment without changing CT's working Labs deployment.

Before warm replacement, the operator retained the prior immutable package and
source/environment snapshot outside Git. The existing
[release rollback contract](release-artifact.md) and
[operator runbook](operator-runbook.md) remain the recovery instructions. Roll
back only the affected isolated deployment; preserve its assigned resources and
attendee Workspace source. No fleet campaign or attendee review gate is added.

R01–R10 retain their recorded outcomes and limits. WT qualification is complete;
immutable publication and the separately scoped CT feature/integration follow-up
are the remaining ship work.
