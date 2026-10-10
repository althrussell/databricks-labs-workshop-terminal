# R05: context-aware app UX

R05 replaces competing workshop design instructions with upstream Impeccable.
AppKit remains the default implementation; explicit framework and visual
requests are honored. The goal, audience, primary task and available data guide
each app's UX rather than a shared shell or palette.

## Delivery

- The reviewed manifest independently pins npm launcher 4.1.0, native engine
  0.1.14 and compiled skill bundle 4.5.2. Bootstrap invokes the supported
  installer with the local-bundle override and `--no-hooks`. All shipped
  artifact sources are public. Laptop refresh/check uses its configured npm
  proxy without recording that proxy in shipped configuration.
- The compiled upstream skill is the packaged fallback. The native binary
  stays outside Git and project copies; the skill launcher finds the verified
  shared engine on PATH. `scripts/refresh_impeccable.py --check` runs the real
  installer and native discovery smoke in an isolated temporary project.
- The Databricks clone retains raw commit/content verification. A deterministic
  projection removes the retired app-design skill and routes its callers to
  Impeccable. The delivered content has a separate manifest digest. Runtime
  bootstrap and vendored refresh apply the same projection.
- Packaged and shared trees retire both old UX skills. Warm fork refresh,
  managed home-link reconciliation and project refresh retire their old copies.
  Current Codex discovery uses `.agents/skills`; managed legacy links are
  removed while attendee-owned assets and notes are retained.
- One short workshop policy in `workshop_contract.md` integrates PRODUCT.md
  context with the existing README brief and optional wizard. It overrides
  mandatory interviews, concept choices and review machinery for ordinary
  workshop builds. Impeccable owns craft; platform skills own APIs/scaffolds.
- The forked APX guide treats CRUD/sidebar/table patterns as optional API
  examples and removes fixed build-time estimates and mandatory documentation.
  Refreshing/pinning APX tooling and comparing frameworks remain R07 work.
- Claude 2.1.296 and Codex 0.162.1 were refreshed from the latest-release audit
  before qualification. The chosen live models remain Opus 5.5 and GPT Sol 6.1.
- Project preparation excludes installed skill assets from AppKit's root-wide
  ESLint scan, without changing application rules or upstream skill bytes. A
  clean tracked configuration is committed for workers; attendee edits remain
  uncommitted and unknown/symlinked configurations are preserved.

## Validation status on 10 October

The pinned supported installer and native engine discovery passed on macOS.
Focused regressions cover raw/projected provenance, retired routes, warm trees,
home links, project copies, preserved notes and real Git worktree content.
CI adds the same supported-installer check on Linux.

All eight CI jobs passed on `effb42e`: Python 3.11/3.12, frontend build/tests,
packaged runtime, actual Linux Impeccable installation, rendered wizard,
evaluator API and Agent Bricks CLI. Raw receipts and screenshots remain outside
Git.

Live tests reuse `wt-eval-r03-1009-f601-wt`, with wizard and evaluation disabled,
as `labuser+1@awsbricks.com`. They use ordinary attendee sentences without
framework or visual instructions. No CT code, CT call or working CT deployment
was changed. The current live WT package is `effb42ee570bc4cf275be270d3edf40bfd54e39a`
with SHA256 `26cd4dc7e9f7a7aaa91339331026222e4ec1c65051ed1b8bb570dfd460bd4c30`,
deployment `01f1c48de81411c3a8e4e8156cb0371a`. Its startup log independently
witnesses that package digest being loaded from Unity Catalog. The deployed
snapshot and unchanged model policy were verified. Native direct launch with
wizard off passed, and the actual project helper/skill/engine fingerprints and
absence of retired home/project assets match the reviewed inputs. APX has not
been live qualified here.

| Path | Observed result |
| --- | --- |
| Claude 2.1.296 / Opus 5.5 | Created a phone repair-cafe queue, read Impeccable and ran context once. The original preview failed contrast; attendee feedback repaired it. A fresh final-package build has readable scoped colours. Its reused-origin saved-data failure was also repaired through attendee feedback: earlier jobs returned, and independent add/finish/reload passed at 390px without horizontal overflow. |
| Codex 0.162.1 / GPT Sol 6.1 | Created a distinct garden collection and planting plan from a simple phone-app request. Native trace confirms seven Impeccable instruction/reference reads and one context invocation. Used scoped product tokens under the corrected policy. Build and saved-plan tests passed; it shared the preview and accurately disclosed unavailable native browser checks. Independent browser tasks remain pending new-app attendee consent. |
| Isolated native discovery | Actual Codex `skills/list` with empty HOME/CODEX_HOME in a detached Git worktree discovered enabled repository Impeccable, neither retired UX skill, and no discovery errors. Reading the actual asset and running its launcher returned the reviewed skill digest and engine 0.1.14. |

The generated repair queue and garden collection choose different navigation,
composition, typography and content for their tasks, rather than sharing a
dashboard shell. Neither build required a design interview or concept picker.
The repair preview visibly labels sample data and browser-only storage; the
garden source includes those disclosures, with browser verification pending.
Native handoffs distinguish compilation and local checks from rendered browser
verification.

The Codex build also exposed an integration defect: AppKit's `eslint .` scanned
Impeccable's bundled JavaScript, producing 740 skill-asset problems plus three
in the scaffold's `server/example.test.ts`. The corrected actual helper was
downloaded as a checksum-verified fixture and run against the existing live
project. The 740 skill problems disappeared; the three application problems
remained and the committed configuration retained the exclusions for worktrees.
All 26 preparation regressions pass, including preservation of attendee edits,
application rules and unrelated staged files. This narrow live helper check is
followed by deployment and native fingerprint qualification of the complete
package above. The initial garden build took 12m 27s, including
deployment recovery; it is not a comparison measurement against APX.

The first Claude/Opus 5.5 repair-desk preview exposed an AppKit theme collision:
generic `--card` was overridden for browser dark mode while custom text stayed
dark. Compilation and simulated DOM checks missed the rendered contrast defect.
The workshop integration now requires scoped product tokens or consistent use
of the framework theme. The feedback repair passed; the fresh Codex build's
independent browser qualification is still pending. The failed first Claude
preview is retained. A fresh Claude session on the final package rebuilt the
same phone workflow as Repair Desk in approximately four minutes, loaded the
actual platform/Impeccable skills and used scoped product tokens. Its rendered
preview has readable colours. Reusing the existing app origin exposed a separate
continuity failure: old saved rows use `owner` and `finishedAt`, while the new
loader requires `visitor` and `status` and silently filters them out. Waiting and
Finished both showed zero. No reset or new save was performed before recovery;
ordinary attendee feedback requested restoration without clearing the saved
repairs. Claude read the earlier deployment's format and added compatible loading
and five focused storage regressions. The corrected deployment
`01f1c492494b1040a621c738fb9a726a` succeeded on the existing app identity/compute;
its selected deployed source was independently retrieved. Browser reload restored
six waiting and two finished jobs, including the previously saved Blue desk fan.
In the 390px phone viewport, adding Green hand mixer, finishing it and reloading
preserved the new record and all earlier jobs. The form's submit control was
reachable within the viewport, the text was readable and there was no horizontal
overflow. Carry schema compatibility and preserving earlier app data into R08's
focused continuity checks. This repair does not erase the initial saved-data
failure.

Data discovery remains a material limitation: Claude consulted the supplied
demo-table index but did not query the working catalog, so its original claim
that no repair-cafe data existed in the workspace was too broad. Codex attempted
a targeted working-schema lookup and accurately recorded that it was unavailable
before using labelled samples. Carry bounded lookup and truthful fallback claims
into the small R06 follow-up.

Bring the focused R07 framework comparison forward next: refresh/pin the real
APX CLI and skill, then compare one representative attendee brief under the same
Impeccable guidance. Use task usability, reliable setup and time to preview to
choose the default; Genie-builder familiarity is secondary. No framework switch
or APX live qualification is claimed by R05.
