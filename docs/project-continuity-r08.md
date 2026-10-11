# R08: project continuity

R08 live acceptance passed on 11 October on the reused isolated Labs WT app
`wt-eval-r03-1009-f601-wt`, as `labuser+1@awsbricks.com`. PR #98 adds optional
**My builds**, **New build** and **Change goal** controls and qualifies source
save/recovery and the corrected take-home export. Onboarding was disabled.

Each build keeps its own files and goal. Continuing a build opens the selected
harness in that directory. A new build keeps its scaffold target absent. Changing
a goal saves it and types a request **unsent** when an agent is open; the attendee
is told to press Enter. Switching builds explains that stopping the current
agent keeps files but ends its conversation. Direct harness setup remains usable.

Selection lives in the attendee home. Committed `.workshop/project.json` travels
with source so recovered projects and isolated workers retain the current goal.
Source context takes precedence over an older home cache. Agents preserve the
existing framework and read/update the README brief and PRODUCT/DESIGN notes.
Named Omnigent launches resume only a matching project conversation.

**Check saved builds** uses the attendee's normal Workspace-sync location.
**Recover source** downloads into a fresh local directory, refreshes managed
policy/skills and preserves custom assets. It refuses to overwrite local work.
Recovery restores the synced source copy, which may exclude unsaved edits; it
does not restore the original Git history or native conversation. An earlier
missing build is never silently scaffolded again. Export before event teardown.

Local API and rendered desktop/phone checks exercise A→change A→B→A, actual shell
launch directories, unsent request delivery, harness switches and reconnect.
Recovery regressions cover exact source/context, canonical paths, failed
downloads, credential preparation and refusal to overwrite local work. Existing
wizard rendered checks run alongside project journeys in CI.

The live Claude Opus 5.5 build created an APX lending library after exploring the
prepared data and disclosing its sample books/members. Initial startup failed on
Lakebase's shared `public` schema. The agent repaired its own schema after the
operator supplied the error log; this remains an assisted first-startup limit.
Browser borrow/return/reload and 390px controls passed. Change goal requested
title/author search and 21-day new loans, retaining earlier loans; the working
search, new loan and reload were independently exercised.

Codex 0.162.1 / GPT Sol 6.1 created a separate printable `club-agenda`. Returning
to the library and opening Codex restored its current goal, framework and saved
notes without changing its application. A project-only detached worktree carried
the actual APX, Impeccable and Agent Bricks assets and current context. That probe
establishes asset delivery, not a worker model run.

Live qualification exposed and fixed four continuity gaps:

- The WT-owned simulator had `CAN_EDIT` on the attendee projects folder, while
  current CT provisions scoped `CAN_MANAGE`. The owner explicitly approved the
  isolated WT correction. Independent ACL readback preserved every unrelated
  entry. No CT code, configuration or working deployment changed.
- Codex's symlinked HOME missed Git's physical project path. The hook now compares
  physical paths. Native command cleanup could also kill its background save
  before the result was recorded. The complete worker now starts its own session;
  a real commit/process-group regression failed before the fix and passes after it.
- Export omitted portable project goals and included APX `.build`/`__dist__`
  products. It now includes only `.workshop/project.json` from runtime state and
  excludes generated output. Linked context files/directories are rejected.
- Recovery appended blank README prompts after an already completed brief.
  Preparation now retains that README exactly; the recovery regression verifies it.

The qualified runtime is `59244b180b2f89445a6909286cb3c4723b5f4158`, PEX SHA-256
`0e3cfa67975199fc51f60cf4e6528a7e584a6410d0e5e6cde10b29740be436e6`, deployment
`01f1c53099bc178e8bc36078fbdcbfec`. All 512 runtime files independently match the
checkout; the subsequent change only corrected a test wording assertion. Both
projects were saved, recovered through My builds after warm replacement, and
reopened with their current goals. A second WT tab updated its inventory when
recovery completed, removing the stale recovery action. Local regressions also
verify the backend refuses stale requests without touching existing files.

On that deployed runtime, native Codex committed only `RECOVERY_HOOK_CHECK.md`.
The shipped hook recorded a fresh `exit=0`; independent operator download verified
its exact content in Workspace. No direct sync or hook modification was used for
this acceptance commit. Both builds were then exported using `workshop-export`.
Independent downloads matched all 56 source/context/check files across Workspace
and ZIP, preserved both current goals and all original application code, and
confirmed generated output was excluded. The helper printed the assigned Volume
and Catalog → Volumes → File options → Download file steps.

Readiness after attendee reconnect is HTTP 200, `ready:true`; disabled isolated
test telemetry remains a soft amber check. Resources, model policy, app identity
and existing compute were preserved. All nine CI jobs pass at `7cbc433`; the 94
focused local checks and 11 take-home checks pass. Screenshots, raw receipts, ZIPs
and generated source stay outside Git. Final latest-toolchain release and the
separate disposable CT integration remain release work, not R08 acceptance gaps.
