# Final WT release qualification

Status on 11 October: regression coverage is in place; final live recovery and
release qualification are pending. This report maps the existing checks to the
remaining release scope. It adds no attendee review workflow or fleet campaign.
Raw receipts, screenshots, archives and generated source remain outside Git.

## R09: focused regression coverage

All nine jobs passed for PR #98 revision
`39429aa60123ce920884a8e3753534c8c8034147` in
[CI run 38109844572](https://github.com/althrussell/databricks-labs-workshop-terminal/actions/runs/38109844572).
The corresponding push run `38109842643` also passed. These are source and
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

The current live runtime is `41e24ab`; the corrected runtime candidate is
`73784f7d73b3859110d8ac43af4de3f213ba38db`. Its PEX SHA-256 is
`ba9cf1b4bb3ab4143798990cfd10fa82db7939241b521c0f7358956214be0894`.
Independent package inspection matched all 512 runtime files to committed source.
The active source/configuration and rollback copy were independently inspected
before any runtime replacement. Package verification does not qualify recovery.

| Journey | Observed outcome and limit |
| --- | --- |
| Direct Claude and Codex entry with wizard off | Both native harnesses ran. Claude Code 2.1.296 used Opus 5.5; Codex 0.162.1 used GPT Sol 6.1. The existing Codex custom-model metadata warning did not prevent completion. |
| Task-appropriate UX | The [R07 comparison](ux-stack-comparison-r07.md) supports the selected APX + Impeccable default. Earlier [repair/garden results](context-aware-ux-r05.md) retain their framework, identity and rendering limits. They are dated results, not new final-package claims. |
| Library primary workflow | The live preview borrowed and returned books and preserved changes on reload. Phone controls were usable. First startup failed on Lakebase's shared `public` schema; the agent repaired its own schema after receiving the operator's log error. The unassisted first-preview criterion remains limited by that intervention. |
| Attendee iteration | Change goal saved an unsent request. Claude added author/title search and a 21-day period for new loans, retained earlier loans, and redeployed. Browser search, borrow and reload passed. |
| Multiple builds and harness handoff | Codex created a separate printable agenda. Independent ZIP readback preserved all 64 library files. Returning to the library, reloading WT and opening Codex recovered its correct current goal and framework. |
| Project-only skill/context delivery | A temporary detached worktree retained current product context and actual APX, Impeccable and Agent Bricks assets. It establishes asset delivery; no worker model execution is claimed. |
| Take-home source preservation | A two-build ZIP was independently downloaded and verified before replacement. The old helper omits portable project context and includes APX build output. Corrected helper readback is pending. |
| Workspace save and warm recovery | Pending. The simulator granted `CAN_EDIT` where current CT grants scoped `CAN_MANAGE`; the actual directory-creation failure confirms the mismatch. The simulator correction and regression are committed. Updating the existing isolated test grant requires explicit approval. |

The detailed [R08 report](project-continuity-r08.md) retains the native/browser
outcomes and outstanding checks. Do not infer app correctness from a successful
deployment state alone: the library backend failed after deployment, while the
browser request and operator logs exposed the startup error.

## Remaining closeout sequence

1. Apply the specifically approved isolated projects-folder correction. Save
   both builds through the normal native path and independently read back their
   source and current portable context from Workspace.
2. Deploy the verified candidate on the same WT identity/compute. Preserve its
   resources, environment and model policy; verify the deployed snapshot,
   loaded package and readiness. Keep the recorded rollback source available.
3. Recover both builds from fresh HOME, verify exact source/context and refusal
   to overwrite, then qualify an actual Codex commit through the corrected hook.
   Export again and independently verify portable context and excluded build
   output. Reuse the completed browser workflows unless a relevant change fails.
4. Close R08/R09/R10 accurately, merge the green implementation and publish an
   immutable tagged WT release. Verify its released manifest, artifact and
   logical runtime content; repeat affected checks if runtime bytes change.
5. Create the separately requested CT feature PR after the release exists:
   Agent Bricks opt-in and WT release selection, latest published stable by
   default. Qualify one disposable CT→WT deployment without changing CT's
   working Labs deployment.

CT integration, a published release and corrected live recovery are outstanding;
this report is not event acceptance or a reason to reopen completed R01–R07.
