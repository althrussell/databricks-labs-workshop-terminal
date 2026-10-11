# R08: project continuity

R08's source ZIP export is already merged in PR #96. The remaining change adds
small, optional **My builds**, **New build** and **Change goal** controls to WT.
They work with onboarding skipped or disabled. Attendees can also start a build
directly in a harness with `workshop-init-project`.

Each build keeps its own files and goal. Continuing an existing build opens the
selected harness in that project's directory; a new build keeps its scaffold
target absent. Changing a goal saves it and, when an agent is open, types a change
request **unsent**. The UI tells the attendee to press Enter. Switching builds
requires stopping the current agent, with an inline explanation that written
files remain and the conversation ends.

Selection is stored in the attendee home. `.workshop/project.json` travels with
source and is committed by project setup so isolated workers and recovered
projects retain context. The latest source context takes precedence over an older
home cache. Project instructions tell agents to preserve existing frameworks,
read the README brief and PRODUCT/DESIGN notes, and keep these notes current when
the attendee changes direction. Named Omnigent launches resume only a matching
project conversation, rather than the latest conversation for another build.

**Check saved builds** uses the existing Workspace-sync location under the
attendee's Workspace home. **Recover source** downloads into a new local folder,
refreshes workshop-managed policy/skills, and preserves custom assets. It never
overwrites a local build. Recovery restores the synced source copy, which may
exclude recent edits; it does not restore the original Git history or native
conversation. Builds known to have existed are not silently scaffolded again
when their local source is missing. Export the take-home ZIP before event teardown.

Local validation on 11 October includes real project/session APIs with harmless
shells and rendered WT desktop/phone journeys: select A, edit its goal, create B
separately, return to A, switch harness and reload. The tests check the shell's
actual directory, preserved source and goal, unsent prompt delivery, and usable
content width. Existing wizard rendered checks pass alongside this journey.
Recovery tests check exact source/context preservation, current setup, canonical
Workspace paths, missing credentials, failed downloads and refusal to overwrite.
These are local regression checks, not live model/app-build qualification.

Live preparation caught a save-hook defect: Codex's shorter symlinked home did
not match Git's physical project path, so a commit could silently skip Workspace
sync. The hook now resolves the projects directory before comparing it. A real
commit regression failed with the old hook and passes with the fix; 97 focused
content/project/session checks pass. The take-home ZIP was independently verified
before runtime replacement. The corrected package still needs live sync/recovery
qualification.

The existing CI browser job runs both wizard and project journeys. Screenshots,
transcripts and generated application source remain outside Git. Live recovery
and warm replacement on the reused CT-compatible Labs WT
remain acceptance work; no CT code, grants or working deployment have changed.

The live Claude Opus 5.5 journey built a lending library with onboarding disabled.
After the operator supplied the Lakebase startup error and the agent repaired
its schema initialization, the attendee browser borrowed and returned a
book and verified persistence after reload. **Change goal** inserted an unsent
request; the agent added title/author search and a 21-day period for new loans,
preserving existing loans. Those changes worked in the browser, and the initial
preview's phone controls were usable. A temporary isolated Git worktree retained
the current goal/brief and readable Impeccable, APX and Agent Bricks skill assets
and references. This establishes asset delivery, not an observed worker model run.

Codex 0.162.1 with GPT Sol 6.1 created and committed a separate printable
`club-agenda` build. Independent readback of the two-build ZIP verified the agenda
and byte-for-byte preservation of all 64 library files. Returning to the library,
reloading WT and opening Codex restored the correct directory and current 21-day
goal, APX framework and search behavior from its saved notes. Codex performed that
continuation without changing files or deploying. Its custom-model metadata
warning was non-blocking; the model completed both tasks. Its commit also
reproduced the known home-alias sync skip on the older deployed runtime.

The save failure was a simulator mismatch: it used `CAN_EDIT`, whereas current
CT grants `CAN_MANAGE` on the attendee's `projects` subdirectory. The CLI's leading
error confirms directory creation requires Manage. The WT-owned runner and its
readback regression now match CT; correcting the already-provisioned isolated
test folder is awaiting explicit approval. CT remains untouched. Independent ZIP
inspection also caught the new portable goal being omitted and APX `.build` and
`__dist__` output being included. Export now carries only `.workshop/project.json`
from that runtime directory and excludes those generated folders, including
regressions that reject linked context files/directories. The corrected export
still needs live readback on the updated runtime.

All nine PR CI jobs pass for candidate `73784f7`. Its PEX SHA-256 is
`ba9cf1b4bb3ab4143798990cfd10fa82db7939241b521c0f7358956214be0894`, and all
512 logical runtime files match the committed source. That corrected candidate
is prepared for the reused WT app; it has not replaced the active `41e24ab`
runtime yet. Source save/recovery, corrected export readback and warm replacement
remain live acceptance work before merging #98.
