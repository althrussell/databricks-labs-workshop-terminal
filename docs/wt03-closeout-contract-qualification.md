# WT-03 — closing UX and release qualification

**Checkpoint: 19 October 2026. Live acceptance remains incomplete.**

WT-03 is [PR107](https://github.com/althrussell/databricks-labs-workshop-terminal/pull/107),
branch `codex/closeout-contract-qualification`, and now targets `main`.
Its prerequisite [PR106](https://github.com/althrussell/databricks-labs-workshop-terminal/pull/106)
merged on 19 October 2026 as
`f3c664d0013e3616f095b2a0af0343cbbd2b4735`, after clean independent review,
114 exact-source focused passes and green CI. The operator explicitly authorized
merging PR107 after all testing, final review and CI gates pass. That authorization
does not mark the pending gates below complete. Delivery order remains
**WT-03 → CT-08** in the
[joint implementation plan](https://github.com/althrussell/databricks-labs-control-tower/blob/main/docs/event-readiness/2026-11-17-18/implementation-plan.md).

## Delivered boundary

| Surface | Behavior |
| --- | --- |
| Attendee lifecycle | Authenticated, read-only `/api/workshop-status` and config status expose only `open`, `closing`, `closed` and input availability. Reading never advances closeout. The existing durable barrier remains authoritative. |
| Closing and closed screen | Disable new attendee activity, launches, terminal input, preferences, project changes, certificates and Help submission. Resources and Help history remain readable. Closing sockets stop reconnecting. Failed or stale reads cannot reopen input. |
| Attendee copy | Plain workshop closing/ended wording. No collection, account-team, insight-extraction or follow-up notices are added. Operator capture diagnostics remain in the admin interface. |
| Saved product state | Full revisioned goals and preferences use the existing source-bound journal; HOME is a disposable cache. Remote commit owns acceptance and concurrent revision checks. Optional discovery/instruction/cache failures cannot reject an accepted save, and delayed discovery cannot overwrite a newer accepted goal. New sources never import unbound local state. |
| Workshop documentation | The default **AI Docs** destination is `https://naveenb83.github.io/ai-dev-day/`. Intentional deployment/live content-pack overrides remain authoritative. |
| Public dependency inputs | Check recognized root/paired Python lock/export/project sources, npm resolution/project sources, toolchain artifact publisher/pins and deployed registry settings. Nested requirement/constraint inputs are checked recursively. Unknown sources fail without exposing secret-bearing URLs. |
| Uploads | Dev, CT simulation, generated-app and CT-compatible deployment/observer/mirror paths validate a clean public source snapshot before client initialization. The bytes validated are the bytes uploaded. Read-only qualification, dry runs and cleanup remain usable. |
| Runtime/build | Existing CI, release and runtime installers use public PyPI/npm. Laptop managed-proxy transport stays local. Existing frozen/offline PEX assembly, hash/integrity checks, paired-app topology and operator-controlled deployment remain. |

No new capture granularity, orchestration infrastructure, Databricks Job, worker, principal,
database, notification channel, fleet-limit expansion or build/deploy system is
introduced. Standard per-seat paired resources continue through normal CT
provisioning. CT owns final Lakebase verification and deletion authorization.
WT closing/closed status is **not** a CT preservation receipt. Existing source
epochs are never reopened. An incomplete final collection remains held under the
existing protocol.

## Recorded implementation and qualification

The corrected runtime source is
`768c8772748d9b29b837d0df55f5ac2416caf036`. A real App restart exposed that the
former goal/preference files depended on disposable HOME storage. The correction
retains full revisioned product documents in the existing source-bound Workspace
capture journal, with owner checks, atomic revision checks, fail-closed primary
storage and guarded optional discovery projection. The corrected package is
deployed in a fresh +200 checkpoint; its acceptance remains pending below.

| Check | Recorded evidence and scope |
| --- | --- |
| Complete corrected-source Python 3.11 run | `/private/tmp/wt03-final-python311-suite.log`: **3,694 passed, 69 skipped, 9 sandbox-blocked failures**. `/private/tmp/wt03-final-sandbox-recheck.log`: unrestricted recheck **81 passed, 2 skipped**, covering all nine blocked cases. Combined unique result: **3,703 passed, 69 skipped**. This is combined regression evidence, not one all-passing process; optional skips are not executed acceptance evidence. |
| Frontend and rendered UI | **120 frontend tests passed**; TypeScript/Vite build and rendered/browser checks passed at the recorded corrected source. Tracked HTML references the current JavaScript/CSS assets. Closing/closed input barriers, readable Help/resources, default AI Docs, retained-source reload, transient refresh failure, startup Reload and mobile/desktop layout have local/rendered evidence. |
| Final independent retained-state review | **Clean**, with **61 focused passes**. Reviewed primary retained documents, revision/owner/source boundaries, optional projection ordering and instruction/cache acceptance semantics. |
| Final independent UI/API review | **Clean** at exact `768c877` versus `origin/main`, with **31 focused passes** from `tests/test_workshop_lifecycle.py` and `tests/test_release_package_qualification.py`. Reviewed full App lifecycle/handlers, Terminal, Help, Raise Hand, tips, status/middleware, default/custom docs and generated asset references. Fresh independent frontend rebuilding was not repeated because this review worktree has no `node_modules`; prior build/rendered evidence supplies that gate. |
| Corrected-source CI | All nine jobs passed at `768c877` in [push run 39208900654](https://github.com/althrussell/databricks-labs-workshop-terminal/actions/runs/39208900654) and [PR run 39208906119](https://github.com/althrussell/databricks-labs-workshop-terminal/actions/runs/39208906119). Any subsequent change needs its own current-head checks. |
| CI sourceguard ordering | **Implemented; current-head CI pending.** CI, release and skills checks gate all 11 dependency-resolving jobs on a public-only bootstrap independent of project/config/lock inputs. Python/npm fetches cannot race ahead of source validation. Poisoned-input ordering/redaction and public-input tests pass within 132 targeted guard tests. The prior nine-job CI runs apply to the earlier source; this correction adds the public-source preflight check. |
| Exact Linux release package | Existing Linux x86_64 / CPython 3.11 PEX build and offline smoke passed. Independent comparison matched all **522 runtime files** against exact Git blobs, plus source/embedded/release manifests. Package starts without network dependency installation; recorded cold startup was **4.639 seconds** against the 30-second budget. |
| Active deployment/runtime proof | Read-only proof matched all **522 runtime files**, CT bootstrap, owner, run/unit, release and artifact pins. Scope is **active Apps deployment source plus pinned UC package bytes**, with no process-memory inspection claim. |
| Positive CT provisioning | Normal qualification CT created the fresh +200-owned run with **zero blocking preflight failures** and deployed the exact corrected package. This proves provisioning/source pins, not completed attendee/restart/seal/teardown acceptance. |
| Global archive baseline | Fresh read-only baseline retained **9,699 rows across all 17 protected tables**, including held +199 evidence. One historical stream lacks a deployment-registry row; its full existing inventory stays protected and the gap is reported. Final post-deletion comparison remains pending. |
| Independent private verification tooling | **22 positive-verifier controls**, **67 global archive-comparator mutation controls** and **19 restart/product-helper controls** passed offline; independent review was clean. These controls and baseline preparation do not substitute for actual positive live receipts. |

## Exact corrected artifact and deployment identities

| Identity | Value |
| --- | --- |
| WT source | `768c8772748d9b29b837d0df55f5ac2416caf036` |
| CT/bootstrap source | `c63ee8c0ea003607845955a2667f0179f90115cf` |
| PEX SHA-256 | `a84fb6ba582215991f9afebb85dddd390a9e42aae969ee9bb3594e27125a54f5` |
| PEX size | **39,359,439 bytes** |
| Release-manifest SHA-256 | `e0f2997c81078a5a62792f6a5718ec852d068469b27b36f7d06e3fc91d314fc4` |
| Logical runtime SHA-256 | `5643c655ca2fe6e1f6bae37c283baccb53bc40e24fba47ff84b3207669f1bd50` |
| Independently verified runtime files | **522** |
| Positive run | `7b368d11-66ee-40e2-bf93-eb547d4595b7` |
| Positive unit | `f9acf97c-7452-4499-aa49-e6cfaf3619f0` |
| Positive workspace | `7474660014346237` |
| Attendee owner | `labuser+200@awsbricks.com` |
| Active deployment at recorded readback | `01f1cb87ab6514058ba536fb238192fb` — `SUCCEEDED`, compute `ACTIVE` |
| Global baseline file SHA-256 | `9c8d28345a42b57594174cf701e06770ce7afa09625f4966fde399bf595cf054` |
| Global baseline canonical receipt SHA-256 | `7e9c9d056b4a1927de35ac99079b973439b3502c75096f0386721e9ea0317923` |

The exact package/runtime proof is recorded privately in
`/private/tmp/wt03-runtime-positive-proof.json`. A normal App start can legitimately
create a new platform deployment/snapshot ID; subsequent receipts must record
that identity while retaining the application/source/artifact/owner bindings.
These identities apply to the corrected `768c877` runtime. Earlier +199 runtime
and earlier `66b4850` helper/build evidence retain their separate provenance and
must not be relabeled as a successful positive checkpoint of this runtime.

## +199 interrupted-terminal result — HELD

The separate run `6ec88d57-75fd-44b0-b8e8-6773d588051a` was restarted while an
original terminal-lifetime writer was active. Its original journal epoch retained
**105 accepted insight entries, ACK 105, one discovery record and one Help history
entry**. The replacement process could not prove durable completion of that
original writer. Closeout remains **HELD/INCOMPLETE**, with
`writer_drain_deadline_exceeded` and `writers_not_drained`.

This is negative fail-closed evidence only. It proves neither a complete final
archive nor ordinary teardown after an active-process interruption. No final
verified preservation receipt was established. A disappeared process is not a
completion receipt. Keep the original reservation, source epoch and accepted raw
records; do not force teardown, delete a reservation, fabricate a receipt, reopen
an epoch or weaken the protocol to obtain a pass. The global archive baseline
includes this held evidence. Positive +200 acceptance cannot erase or replace it.

## Positive +200 checkpoint — acceptance pending

| Gate | Required evidence | Current status |
| --- | --- | --- |
| Exact corrected runtime | Active Apps source/bootstrap and pinned UC bytes agree with exact Git/source/release/package/owner/run/unit pins. | **PASS** — 522 files independently verified. |
| Global baseline | All 17 protected tables and every original row remain in scope, including historical/held runs. | **PASS** — 9,699 rows captured; post-test comparison pending. |
| Actual attendee authentication and saves | Sign in as +200; save the full goal and preference and record accepted revisions/source owner. No PTY/agent launched before the supported restart. | **PENDING** |
| Normal restart before PTY | Ordinary Apps stop/start; fresh API/page returns exact accepted full goal and preference/revisions on the same bound original stream; no active admitted writer at stop. | **PENDING** |
| Real attendee activity | Actual attendee UI produces the selected discovery/Help/terminal or agent activity; sessions end normally with completion evidence. | **PENDING** |
| Live closing/closed UX | Normal CT final collection disables write/launch controls; Help history/resources stay readable and wording stays plain. | **PENDING** |
| Fresh SEALED Lakebase proof | Independently verify every observed source epoch, raw sequence/hash inventory, seal/final facts and persisted verified receipts before deletion authorization. | **PENDING** |
| Normal restart after SEALED | Ordinary Apps stop/start preserves the exact valid sealed operation, manifest/final outputs/product documents and original stream; fresh UI stays closed. Re-run independent verification. | **PENDING** |
| Ordinary teardown and pool return | Normal CT cleanup only after verified preservation; selected app/workspace deletion and +200 availability confirmed. | **PENDING** |
| Post-deletion/global proof | Fresh Lakebase proof equals pre-delete sealed evidence; global comparator preserves every original raw row, binding, immutable payload and verified receipt. +199 remains held without a fabricated receipt. | **PENDING** |
| Environment protection/restoration | Recheck Labs deployment, earlier archives and qualification release/configuration state after testing/cleanup. | **PENDING** — earlier protected-app checks passed. |
| Final delivery | Finish final independent review/current-head CI and the live gates above; update WT ledger and CT implementation plan, then merge under the operator's explicit authorization. | **PENDING** |

The milestone is **not yet fully E2E accepted**. A primary save, source binding,
journal audit, closeout or preservation failure stays a recorded hold and must be
investigated. Passing local tests, a closed WT screen or preserved individual
records cannot be promoted into a claim of a complete archive or authorized
resource deletion.

## Durable model-policy correction — live acceptance pending

The supported +200 App stop/start retained attendee goal, preferences and Help
history, but the replacement container lost its local model-policy file and
correctly held Claude while CT still had a successful delivery receipt. The
current correction retains the complete canonical policy in the existing
source-bound journal, compares revisions atomically, rejects unbound local-cache
imports and treats container cache writes as optional. Closing and sealed sources
restore the retained policy read-only and refuse further policy applies.

The correction passed **383 targeted tests** across model policy, retained state,
startup and closeout. Independent source/test review was **clean**, with **50
separate focused passes** and additional offline fault/race probes. Fresh
exact-source CI, package verification and positive restart/closeout/teardown
acceptance are **pending**. The deployed package recorded above remains from
`768c8772748d9b29b837d0df55f5ac2416caf036`; it does not contain this correction.
Earlier artifacts and test evidence retain their recorded provenance.

## Private evidence references

These are evidence references, not instructions to publish attendee records or
raw receipts. Record counts, hashes and outcomes in the final public ledger.

- `/private/tmp/wt03-final-python311-suite.log` — corrected-source full-suite process, including the nine sandbox-blocked failures.
- `/private/tmp/wt03-final-sandbox-recheck.log` — unrestricted subset covering all nine blocked cases.
- `/private/tmp/wt03-frontend-final-tests.log` — 120 frontend test results.
- `/private/tmp/wt03-package-768c877/source-proof.json` — exact Git/runtime/manifest comparison.
- `/private/tmp/wt03-package-768c877/release-benchmark.json` — existing offline package startup proof.
- `/private/tmp/wt03-runtime-offline-proof.json` — independent package proof, zero cloud calls.
- `/private/tmp/wt03-runtime-positive-proof.json` — active Apps deployment source/bootstrap and pinned UC package proof.
- `/private/tmp/wt03-global-before-positive.json` — full private 17-table global baseline.
- `/private/tmp/wt03-interrupted-session-negative.json` — held +199 interruption evidence.
- `/private/tmp/wt03-product-journal-inspect.py` and `/private/tmp/wt03-normal-restart.py` — reviewed exact-source inspection and supported stop/start helpers; prepared controls are not a live pass.
- Positive pre/post-restart, pre/post-deletion and global comparison receipts — **PENDING**.
