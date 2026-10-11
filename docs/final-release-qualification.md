# Final WT release qualification

Status on 11 October: R08 live continuity/recovery acceptance and focused R09
regressions pass. Latest-toolchain release qualification and CT integration remain. This report maps the existing checks to the
remaining release scope. It adds no attendee review workflow or fleet campaign.
Raw receipts, screenshots, archives and generated source remain outside Git.

## R09: focused regression coverage

All nine jobs passed for PR #98 revision `7cbc433a847f9ff18f7b55e8aaff8592d73da86f`.
These are source and
packaged-runtime regressions, not proof of a live attendee journey.

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

The qualified runtime is `59244b180b2f89445a6909286cb3c4723b5f4158`, PEX SHA-256
`0e3cfa67975199fc51f60cf4e6528a7e584a6410d0e5e6cde10b29740be436e6`.
All 512 logical runtime files match the checkout. Deployment
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
| Workspace save and warm recovery | The owner-approved isolated `CAN_MANAGE` correction matches CT; unrelated ACL entries and CT were untouched. Both builds recovered through My builds after warm replacement. Codex made a real one-file commit; the corrected detached hook recorded fresh success and its exact saved bytes were independently downloaded. Native command cleanup failed before this fix and has a real process-group regression. |

The detailed [R08 report](project-continuity-r08.md) retains the native/browser
outcomes and outstanding checks. Do not infer app correctness from a successful
deployment state alone: the library backend failed after deployment, while the
browser request and operator logs exposed the startup error.

## Remaining closeout sequence

1. Merge the accepted R08 implementation with green CI. Its detailed
   [report](project-continuity-r08.md) retains the observed outcomes and fixes.
2. Qualify current direct tooling, including APX/Bun and separate Impeccable
   channels, in an isolated worktree that preserves the owner's local lock edits.
   Run the affected Linux installers, lifecycle checks and reproducible package
   jobs; reuse completed live journeys rather than restarting a campaign.
3. Use the final package on this same deployment for affected checks and a useful
   analytical APX build with a concise experienced-user iteration. Retain the
   library's assisted first-startup limitation; do not claim an unassisted pass.
4. Publish the verified immutable WT release. Verify released manifest, artifact
   and logical runtime content; repeat affected checks when runtime bytes change.
5. Create the separately requested CT feature PR afterward: Agent Bricks opt-in
   and WT release selection, latest published stable by default. Qualify one
   disposable CT→WT deployment without changing CT's working Labs deployment.

R08 is accepted and R09's focused regressions are in CI. A published final
release, latest-toolchain live qualification and CT integration remain R10/ship
work. Completed R01–R07 retain their recorded outcomes and limits.
