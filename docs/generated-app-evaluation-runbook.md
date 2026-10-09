# R01 generated-app evaluation: simulation first

Current status, 9 October: **R01 is complete with a failed baseline**. The
[closeout and inspected app evidence](evidence/generated-app-r01/live-20261009-68bd/CLOSEOUT.md)
record one eligible Claude `2.1.283` / AppKit `0.76.1` run from the unchanged
CT-pinned September PEX, source `440232b953a5050c965838b42a594e1592ee3d33`.
The WT-owned package runner exercised CT-compatible provisioning, exact source
readback, revisioned model policy and own-SP Claude/Codex/wizard inference.
The genuine assigned labuser used the normal WT UI with wizard Skip. Simple
nontechnical inputs produced a new deployed app and a new Lakebase project.

The agent asked useful workflow questions but built without scope agreement.
Independent UI testing added an order, marked it packed once, and observed that
state after reload and in a fresh context. The valid due date rendered as
**Invalid Date**. Mobile status/action columns were offscreen inside a horizontal
table even though page overflow was false. Full app acceptance remains false;
independent Lakebase storage/restart checks and calibrated UX scoring were not
completed. No failure rate or all-harness conclusion follows from this one cell.

Setup journeys 1–12 retain their original evidence and are excluded from quality
scoring. Driver fixes cover authoritative wizard state, exact folder trust,
current CLI prompt recognition, bounded native records, and question/answer
projection. The eligible run used ordinary prose; live structured-question answer
correlation remains unverified. The initial jumping behind the wizard was caused
by evaluator background-card retries, not established as a WT product defect.

All isolated run resources were cleaned before their original expiry, with final
absence readbacks, preserved attendee projects identity, and other principals'
permissions unchanged. Both private authentication exports were removed and were
never archived. CT stayed on the same RUNNING/ACTIVE deployment; its code, events,
configuration and permanent groups were not changed. Earlier expired e21f/d94e
runs were also cleaned and supply setup evidence only.

The nontechnical bakery user remains the R01 scenario. WT supports all experience
levels; optional, changeable help preferences and developer/data-practitioner
cells remain R02/R03 qualification work. Passing isolated remediation and the
later CT-integrated deployment are separate from this failed baseline closeout.

## Current CT-compatible package workflow

Use `scripts/deploy_ct_compatible_wt_test.py` for a fresh package test. The older
snapshot command later in this runbook is retained for historical evidence and
instrumented development experiments; it does not reproduce CT package mode.
The example input is [ct-compatible-wt-test.json](examples/ct-compatible-wt-test.json).
Choose a fresh marker and an explicitly approved current model-policy snapshot.

1. Read the pinned release through `scripts/fetch_wt_test_release.py`, supplying
   CT's read-only staged release volume and reviewed manifest digest. Verify the
   manifest/PEX and record its exact revision. Never grant the test SP access to
   CT's shared release/toolchain volumes or add it to CT's permanent groups.
2. Plan locally, then execute the same spec with a fresh output. The runner creates
   the app/SP, attendee `CAN_MANAGE` and OBO scopes, disposable operator group,
   operator-owned catalog, package volume and exact model/schema/warehouse/home
   ACLs. It copies only immutable pinned toolchain blobs into an owned volume,
   records missing/corrupt non-strict mirror fallbacks, uploads six independently
   hashed bootstrap/observer files, and converges compute before deploying.
3. Qualify the exact recorded deployment before seeding or using the browser.
   Qualification waits for that deployment, verifies its source snapshot and
   package, acknowledges the real policy endpoint and invokes each required model
   route as the WT app's own SP. A successful policy PUT alone is insufficient.
4. Plan and seed the bakery fixture, then authenticate normally as the assigned
   attendee. Browser state is private and separate from operator credentials.
5. Run the unchanged simple user input through the native harness, collect an
   explicit outcome, then independently verify any generated app. Keep actual
   CT integration and fleet qualification as separate later stages.
6. Clean receipt-identified resources and exact test-SP deltas. Preserve failed
   receipts and the attendee home directory. Expired bounds cannot be extended.

```bash
uv run --frozen python scripts/deploy_ct_compatible_wt_test.py \
  --spec docs/examples/ct-compatible-wt-test.json \
  --release-manifest /private/tmp/wt-test-release/release-manifest.json \
  --artifact /private/tmp/wt-test-release/workshop-terminal.pex \
  --output /private/tmp/wt-test-plan.json

# Same command with --execute and a fresh --output wt-test-deploy.json.
uv run --frozen python scripts/qualify_ct_compatible_wt_test.py qualify \
  --receipt /private/tmp/wt-test-deploy.json --output /private/tmp/wt-test-qualified.json

uv run --frozen --with-requirements evals/generated_apps/requirements-browser.txt \
  python scripts/authenticate_generated_app_attendee.py \
  --receipt /private/tmp/wt-test-deploy.json \
  --state-output /private/tmp/wt-test-attendee-state.json \
  --evidence-output /private/tmp/wt-test-sign-in.json

uv run --frozen --with-requirements evals/generated_apps/requirements-browser.txt \
  python scripts/run_generated_app_journey.py \
  --receipt /private/tmp/wt-test-deploy.json --qualification /private/tmp/wt-test-qualified.json \
  --wt-browser-state /private/tmp/wt-test-attendee-state.json \
  --budget /private/tmp/wt-test-budget.json --agent claude --mode build \
  --entry-path skip_wizard --qualify-startup \
  --generated-access-seed /private/tmp/wt-test-seed.json --execute \
  --output /private/tmp/wt-test-journey.json

uv run --frozen python scripts/qualify_ct_compatible_wt_test.py cleanup \
  --receipt /private/tmp/wt-test-deploy.json --output /private/tmp/wt-test-cleanup.json
```

The browser packages are external evaluation dependencies; they do not enter WT's
PEX. Preinstall the matching Chromium (`playwright install chromium`) before
opening the attendee test. The startup arm records the real CLI screen and only
confirms the exact owned-project-folder trust prompt. It supplies no technical
build instructions. Wizard-enabled and disabled entry paths are distinct cells.
The budget file and independent app acceptance steps are described below.

If an external observer correction changes only its separately hashed files,
preserve the failed journey and original deployment/seed receipts, requalify the
new deployment, and rebind the unchanged seed with read-only verification:
`scripts/seed_generated_app_fixture.py --receipt <new-deployment-receipt> --rebind-seed <verified-seed-receipt> --output <fresh-seed-receipt>`.
That continuation cannot change fixture identity, rows, epoch or expiry, and
cannot be used for an attendee app repair. A repeated journey starts a fresh
native session and remains a separately recorded run.

SDK requests and retries are bounded; package/mirror operations use a 180-second
HTTP timeout and stream binaries in 1 MiB chunks. Continuations retain the original
bounds and immutable source/ownership identity. Cleanup preflights identities,
preserves all other UC grants, and verifies deletion absence. The permissions API
cannot delete an ACL through PATCH: cleanup reads the shared ACL twice, restores
all remaining direct entries through SET, and verifies every other principal and
inherited permission is unchanged. Concurrent or expanded ACLs fail closed.

The following paragraphs preserve the earlier August-checkout chronology; they
do not describe the current-release package workflow above.

Status on 8 October 2026: the audit, R01 evaluation foundation, and executable
standalone consultation/build-observation command are implemented. Local tests
have run; the recorded deployment, seed, journey, and result regressions totaled
289 passes, and the latest focused independent-result suite passed 47 tests.
The isolated `wt-eval-r01-1008-b53c-wt` run is historical: its window expired at
`2026-10-08T08:39:59Z`. A fresh isolated run is being prepared. The b53c
[cleanup receipt](evidence/generated-app-r01/live-20261008-b53c/cleanup.json)
verifies removal of its exact test model grants, app, catalog, copied source, and
operator group, with absence readbacks and other principals' permissions unchanged.
The fresh `wt-eval-r01-1008-c72d` specification is prepared; deployment and canary
qualification remain outstanding.

The b53c receipts preserve its successful deployment, genuine
`labuser+1@awsbricks.com` browser identity, all 419 copied-source hashes, and 20
independently verified seed records. Two exact external setup corrections retained
the same table UUID and original failure receipt. The strict-wizard cell stopped
at the mandatory industry gate. A separate Retail-selected cell preserved the
build-now starter but reached native startup/model-access blockers without a
qualified model conversation. Its UI cleanup failed; the exact owned session was
then closed through the genuine attendee API and independently verified absent.
Both outcomes remain recorded separately.

After subsequent user authorization, five direct model-service EXECUTE grants for
the b53c app's own SP were applied and independently verified. The
[dated follow-up](evidence/generated-app-r01/live-20261008-b53c/README.md) and
[unchanged grant receipt](evidence/generated-app-r01/live-20261008-b53c/model-grants.json)
record Sonnet 5, Opus 5, Haiku 4.5, GPT 5.6 Terra, and GPT 5.4 Mini access.
CT groups and existing principals' permissions were unchanged. This supersedes
the earlier pending-approval/missing-permission status while retaining its
original evidence. No app-SP wire canary, generated app, or independent app
acceptance was completed before expiry. Attendee-facing consultation, wizard,
starter, and design-policy remediation remains R02 onward.

The preserved [live evidence summary](evidence/generated-app-r01/live-20261008-b53c/summary.json)
includes immutable deployment/seed receipts, both wizard cells, actual industry-gate
PNG/ARIA, model-service availability and grant readbacks, and the user's model-error
screenshot. It contains no browser authentication state. `/readyz` reported ready
despite the initially missing model EXECUTE access; gateway URL resolution, OAuth
health, and subsequent grant readbacks are insufficient model-invocation canaries.
A fresh run must include an actual app-SP call and novice cell. Neither earlier
cell supplies a UX score. The original summary and receipt bytes are unchanged;
the follow-up carries the later grant result and expired-run status.

The [acceptance contract](generated-app-e2e-acceptance.md) defines the pass. This
runbook describes what can be executed now and what remains to complete that pass.

## Qualification order

The [workshop pacing guidance](app-quality-audit-and-remediation-plan.md#workshop-pacing-and-scope)
governs future remediation: a small compelling demo, usually one short exchange,
sensible defaults and practical checks. [R02](workshop-interaction-contract-r02.md)
is committed and pushed with policy delivery tests and bounded native MLflow
model probes. Three genuine-labuser Labs attempts are preserved in the
[R02 live closeout](evidence/workshop-contract-r02/live-20261009-e569/CLOSEOUT.md).
Observer failures and a simulated generated-SP permission gap prevented generated
app acceptance; native assistant/question persistence during those builds remains
unresolved. A subsequent
[direct Computer/Browser probe](evidence/workshop-contract-r02/ui-question-capture-20261009-6b90/README.md)
qualified Claude's plain-text and structured question/reply UI paths, including
an inline custom answer and two selected fields. The collector correlated both
completed cells, but that does not explain the prior missing assistant records.
The next Claude build can use the actual browser UI, with collector evidence
recorded independently. Codex exited during startup twice and its question
delivery remains unverified. Actual CT integration remains separate.
The detailed independent evaluator is operator work; it must not turn the
attendee's build into production planning or a prolonged acceptance ceremony.
Current R01 artifacts and evaluator behavior retain their recorded policy and
budgets. Qualify revised consultation criteria separately before comparing future
runs; do not silently reinterpret the failed baseline.

For this small operator transport gate, force one known question without an app
build, capture the visible question before answering, deliver the ordinary reply
through WT's actual terminal, and verify the agent's acknowledgement. Cover plain
text and the native question form, including custom text and multiple selections.
Keep each cell bounded and retain failed attempts. On the pinned Claude form,
moving to `Type something` already focuses its inline editor: populate it before
Enter, because Enter on an empty editor cancels the form. Verify the combined
review screen before final submission. A missing native record is an observer
result; it must not prevent a separately qualified UI driver from answering a
question it can actually see. This gate adds no scope-agreement ceremony or
extra questions to the attendee experience.

1. Deploy a fresh isolated WT directly into Labs with simulated CT environment
   inputs and real Databricks resource bindings. Test real WT and generated apps.
2. Record the current-policy novice baseline, including failures. Remediate WT and
   rerun until the functional and UX acceptance gates pass.
3. Repeat the same scenario on a disposable unit deployed through existing CT,
   using the actual assigned labuser. Qualify integration and attendee governance
   separately before fleet rehearsal.

CT code, shared configuration, its working Labs deployment, and existing events
are outside the isolated test. `scripts/deploy_generated_app_test.py` does not
contact CT. It uses the Databricks SDK, provisions fresh resources, and blanks
`CONTROL_TOWER_URL`, ingest URL/token, `WORKSHOP_PAT`, and paired Omnigent URL.

The active isolated cell uses `attendee_mode: synthetic_attendee` with
`labuser+1@awsbricks.com` signing in normally. Its test app access and runtime
attendee binding were set directly within the isolated resources. This simulates
CT's attendee handoff; actual CT assignment/provisioning, event workspace isolation,
CT lifecycle, and fleet readiness remain unverified. `attendee_mode: operator_bound`
remains a valid alternative in which the Labs operator signs in normally and acts
as the novice, with assigned-labuser privileges also unverified. Operator CLI
access never substitutes for genuine browser authentication.

## Implemented foundation and limits

| Component | Available now | Still required for live acceptance |
|---|---|---|
| Deployment | Pure simulation plan, reviewed artifact pins, direct fresh-resource deployment, ownership receipts, narrow pending-identity resume; executable live app/deployment/SP/workspace and copied-source verification | Qualify actual toolchain, resource grants, attendee OBO, and model calls |
| Novice simulator | Consistent private bakery facts; answers only asked questions; no technical rescue; reply/deadline bounds | Actual complete-turn consultation through a pinned harness |
| Browser/harness | Authenticated WT entry/replies/reconnect, exact-session cleanup; executable native-turn consultation/build observation; read-only app inventory discovery; separate generated-app task, seeded-UC storage, reload/fresh-context, screenshot/ARIA command | Qualify the command against an actual app/UI and data; implement real restart proof and broader state/task/backend coverage |
| Observation | Default-off admin-only native-log bridge for one bound synthetic session | Native format qualification against real pinned Linux CLIs; mutation timing and worker coverage remain unverified |
| Evaluation/report | Failure-preserving evidence and native MLflow datasets/scorers/judges/evaluate adapter | Real sanitized traces and model evaluation; calibrated visual review with actual images |

The CT-oriented CLI `entry` command remains a UI-entry probe. The standalone
`scripts/run_generated_app_journey.py` command now drives complete native assistant
turns and novice UI replies in consultation/build modes. It verifies the exact
deployment receipt against live workspace metadata and copied runtime bytes before
browser entry, observes newly deployed apps, and closes its owned WT session.
Both commands report `accepted: false`. The separate generated-app verifier
collects task/storage/browser evidence; real restart and calibrated visual review
still prevent a full-app acceptance verdict.

## Prepare an isolated deployment

Run from the WT repository. The deployment script needs Python, `databricks-sdk`,
and PyYAML; the current repository `.venv` provides these. Journey execution also
needs HTTPX, Playwright, and an installed Chromium. Native evaluation targets MLflow
3.13.x; managed dataset dependencies need a reviewed compatible installation
before actual evaluation. No browser/model execution is needed for plan generation.
The optional controlled startup arm also needs local `pyte==0.8.2` and `wcwidth`.
These are evaluator dependencies; they are not added to the deployed app runtime.
The deployed requirements pin SDK `0.121.0`; the current operator `.venv` has
`0.116.0`. The journey client constructor is covered by a no-network regression
using the real installed SDK. That check does not qualify the deployed SDK or
native harness formats; record both versions in live evidence.

Create a private JSON spec, for example `/private/tmp/wt-r01-spec.json`. Replace the
marker and principal for each new test. The marker must start `wt-eval-`, fit the
planner's length/name rules, and identify resources exclusively owned by this run.
This is a shape example, not the receipt of an existing deployment:

```json
{
  "marker": "wt-eval-r01-1010-a1b2",
  "workspace_host": "https://dbc-e155652a-2cba.cloud.databricks.com",
  "workspace_id": "7474655950495189",
  "profile": "labs",
  "attendee_email": "al.thrussell+genai@databricks.com",
  "attendee_mode": "operator_bound",
  "disposable": true,
  "ttl_seconds": 7200,
  "cost_budget_usd": 10,
  "harnesses": ["claude", "codex"],
  "onboarding_wizard": true,
  "llm_wizard": true,
  "evaluation_observation": true,
  "release": {
    "source_kind": "runtime-snapshot",
    "source_digest": "0000000000000000000000000000000000000000000000000000000000000000",
    "parent_git_sha": "4d46461c6a0229892bdda5c153d8a6dd03d316c6",
    "instrumentation": true,
    "prompt_policy_unchanged_asserted": true
  }
}
```

Set the parent SHA to the reviewed source revision. The script replaces the
placeholder digest with the sorted runtime-file manifest digest. It records a
second digest after binding runtime environment/SP values into `app.yaml`.
`prompt_policy_unchanged_asserted` requires source review; it is not automatically
proven by the flag. An instrumented snapshot is a different source artifact from
the previous deployed package even when its prompt policy is unchanged.

Generate a local plan, inspect resource names, principal, scopes, release pins,
source manifest, observation settings, and bounds:

```sh
.venv/bin/python scripts/deploy_generated_app_test.py \
  --spec /private/tmp/wt-r01-spec.json \
  --output /private/tmp/wt-r01-plan.json
```

Deploy using a **different, fresh receipt path**:

```sh
.venv/bin/python scripts/deploy_generated_app_test.py \
  --spec /private/tmp/wt-r01-spec.json \
  --output /private/tmp/wt-r01-deploy.json --execute
```

The script verifies the profile host and operator identity, rejects existing
resource names, persists creation intent/results, waits boundedly for app identity,
then creates the isolated operator group/catalog, uploads runtime files, and
submits deployment asynchronously. `deployment_submitted` is not a ready or E2E
verdict. Poll only the exact receipt-owned app and deployment through the platform.
Read back readiness and bootstrap before launching. Genuine attendee browser
access must initialize and validate OBO; a readiness failure solely because no
attendee has visited is an incomplete authentication gate. Configured scopes and
a RUNNING app do not prove OBO or successful model calls.
Additional permissions must be scoped to the test resources/SP.

If the app was created but its SP/URL was unavailable, preserve the failed receipt.
Only that app-only partial stage can resume, after matching marker/spec and exact
app ID; all other resource names must still be fresh. Use a new output:

```sh
.venv/bin/python scripts/deploy_generated_app_test.py \
  --spec /private/tmp/wt-r01-spec.json \
  --resume-receipt /private/tmp/wt-r01-deploy.json \
  --output /private/tmp/wt-r01-resume.json --execute
```

An expired receipt or a partial deployment that already owns a group/catalog/source
cannot use this resume path. Preserve all receipts and reconcile their resources;
do not rerun fresh creation against them or overwrite their cleanup provenance.

## Protect the test boundary

The upload allowlist includes `server/`, `static/`, `assets/`, `content/`, and app
requirements/configuration only. It rejects links and excludes evaluation code,
private simulator facts, docs, tests, scripts, auth files, VCS metadata, and caches.
The builder must never receive the private fixture or scoring criteria.

Native transcript observation requires explicit evaluation enablement plus exact
marker, principal, run, and unit bindings. The endpoint is
`GET /api/admin/evaluation/sessions/{actual_wt_session_uuid}/messages`; it requires
admin authentication and reads only bounded complete authored user/assistant
messages from supported native formats. It excludes thinking, tools, and terminal
stdout. Keep operator observation auth separate from browser auth. Native logs
are attendee-writable, so the bridge alone cannot authenticate all events or prove
that requirements preceded code/resource mutation or delegated worker behavior.

Sign in to WT through the platform as the spec's browser principal. Keep exported
Playwright storage state private outside the repository/uploads/reports. Do not
inject an operator bearer into attendee browser requests. Console/network/trace
artifacts also require redaction and controlled retention.

TTL, $10 spend, and token ceilings are configured bounds. The standalone deployment
does not install an enforced gateway budget or automatic resource teardown.
Record actual usage and observed enforcement separately; stop owned sessions at
the run deadline, and mark missing usage/enforcement evidence unverified.

## Run the standalone consultation or build observation

Use the **full, current deployment receipt** emitted by
`deploy_generated_app_test.py`, including its plan, exact created resources,
deployment ID, and uploaded-source manifest. Use the new complete resume output
if deployment resumed. A readiness summary, local plan, app-only partial receipt,
or expired receipt is insufficient. Preserve the old receipt and use a fresh
marker/deployment after expiry; changing timestamps cannot renew ownership.

Create a private budget file, for example `/private/tmp/wt-r01-budget.json`:

```json
{
  "total_seconds": 1800,
  "consultation_seconds": 180,
  "token_ceiling": 100000,
  "spend_ceiling_usd": 5
}
```

The spend ceiling must fit the deployment receipt's cost bound. Deadlines are
enforced; token/spend enforcement remains unverified. The command reserves a
bounded cleanup period and recalculates the remaining receipt lifetime after
browser startup. It cannot launch an expired test or extend the original expiry.

Generate a local journey plan with a fresh output path. This reads local inputs
without creating an SDK client or calling the workspace:

```sh
.venv/bin/python scripts/run_generated_app_journey.py \
  --receipt /private/tmp/wt-r01-deploy.json \
  --budget /private/tmp/wt-r01-budget.json \
  --agent claude --mode consultation_probe --entry-path skip_wizard \
  --output /private/tmp/wt-r01-journey-plan.json
```

After signing in normally as the receipt's browser principal, export that
Playwright context's storage state to a private file outside the repository.
Operator API credentials are obtained separately from the receipt's SDK profile;
the command does not inject them into the attendee browser. Run the consultation
probe with another fresh evidence path:

```sh
.venv/bin/python scripts/run_generated_app_journey.py \
  --receipt /private/tmp/wt-r01-deploy.json \
  --budget /private/tmp/wt-r01-budget.json \
  --wt-browser-state /private/tmp/wt-r01-browser-auth.json \
  --agent claude --mode consultation_probe --entry-path skip_wizard \
  --execute --output /private/tmp/wt-r01-consultation.json
```

The probe can record novice answers and native-correlated scope agreement. It
closes only the session created through that run's normal UI and stops there.
`skip_wizard` uses the visible Skip control when onboarding is enabled. Use
`wizard_disabled` only with a disabled deployment. To assess onboarding itself,
choose `--entry-path wizard`; retain the initial industry-gate failure. A separate
compatible arm can add `--allow-industry-step --industry Retail`. That extra action
is recorded; it is not part of the attendee's opening sentence.

The isolated evaluation runtime also exposes an admin-only model invocation
canary at `POST /api/admin/evaluation/model-canary`. Send exactly
`{"role":"driver"}`, `{"role":"codex"}`, or `{"role":"wizard"}` to the receipt's
verified WT origin through the operator's ordinary authenticated API access. The
same default-off synthetic evaluation contract gates the endpoint. It requires
the platform OAuth M2M singleton and freshly verifies the same bearer against the
exact injected app application UUID and numeric SP ID; it never uses the attendee
token or emergency PAT. The provider host/path must belong to this runtime's exact
workspace. It invokes the current role policy once with fixed `Return exactly OK.`,
a 32-token output cap and a 30-second overall deadline, without following redirects.
Evidence contains only binding IDs, exact requested model/wire/URL, HTTP statuses,
fixed error classifications and validated token counts. `invocation_verified`
requires a valid native provider envelope and positive usage. A thinking-only
response can prove invocation while `ok_answer_verified` remains false. A safe
bounded `response_model` can diagnose an underlying alias; an unmatched alias
remains unverified. HTTP 200 alone never qualifies a call. The canary does not
establish harness startup, novice consultation, app quality, or acceptance.

For a separately labeled controlled startup cell, add `--qualify-startup` to the
`skip_wizard` command. This optional arm reconstructs the current startup screen
from the already observed sole owned session's PTY transport and exact terminal
resize, using `pyte`; it never infers assistant messages from terminal prose. The
only confirmation it accepts is the exact selected Claude folder-trust control
for the receipt-bound attendee's projects directory. Before its single normal
browser Enter key, it records a private terminal screenshot, current screen text,
hashes, session/path binding and friction. It then waits for the exact pinned CLI
banner and empty native prompt before typing the original simulator opening.
It rechecks the exact screen after browser focus, keeps startup observation active
through the first submission, and leaves readiness unverified when a new
confirmation interrupts delivery; any observed partial input remains recorded.
Unknown selections, other folders, permission dialogs, unsupported versions or
missing prompts receive no novice input and remain unqualified. It records no
inferred user or scope consent. Artifacts use a fresh `<output>.startup-browser`
directory. Native transcript correlation remains required after submission.

The controlled startup option rejects `wizard` and `wizard_disabled`. WT's wizard
auto-types its saved starter before the driver can qualify folder trust. Preserve
that strict wizard result separately; the controlled skip-wizard arm does not
repair or qualify the wizard delivery behavior.

Build observation uses a fresh session and evidence path:

```sh
.venv/bin/python scripts/run_generated_app_journey.py \
  --receipt /private/tmp/wt-r01-deploy.json \
  --budget /private/tmp/wt-r01-budget.json \
  --wt-browser-state /private/tmp/wt-r01-browser-auth.json \
  --agent claude --mode build --entry-path skip_wizard \
  --execute --output /private/tmp/wt-r01-build-observation.json
```

Use `--agent codex` for a separate Codex cell. `--show-browser` displays the browser;
it still requires genuine saved authentication. Do not repair the builder through
the operator UI or provide technical instructions beyond the novice simulator.

Before entry, the command verifies the live workspace-ID header and profile host,
operator identity where operator-bound, immutable app ID/URL/SP, active successful
deployment and source timestamps, and all copied snapshot file hashes. It checks
the deployed `app.yaml` bindings and that CT/PAT delivery remains blank. The recorded
SDK snapshot shape is `/Workspace/Users/<app-id>/src/<deployment-id>`; an unknown
shape remains unverified.

Discovery compares live app inventory with its private pre-entry baseline. A fresh
app created by the exact unique WT service principal can be attributed even with
a generic name; marker absence is recorded. A marker-bound fresh attendee-created
app can also be observed. Generic apps created by the operator require review,
because concurrent operator work is possible. Discovery rechecks actual deployment
source and active identity. It never establishes app usability, data persistence,
credentials, or authority to delete resources.

Every execution still returns a non-acceptance result. Exit `0` means local
planning completed, `1` means execution completed without full acceptance, and `2`
means execution was blocked or a runner operation could not complete. Read the
evidence's verdict and reasons. A missing readable assistant turn cannot establish
failed consultation: the operational deadline remains recorded, while the command
marks builder assessment unverified. Operator HTTP/SDK collection failures also
retain their specific unverified reason. Actual entry, deadline, or cleanup
failures with sufficient observation are retained as failures; none is an app
acceptance verdict. An individual operation timeout remains an operational failure
but its assessment is unverified, because a stalled collector can cause it even
after a valid assistant turn.

The journey also attempts a final WT screenshot and actual ARIA-text capture in
a fresh private `<output>.wt-browser` directory before closing the browser. It
requires the observed expected attendee identity and exact WT origin; an auth
redirect or missing identity skips capture. Partial capture failures retain their
paths and stable error type without replacing the journey outcome. This records
rendered industry-gate/startup failures as well as successful entry.

## Verify a generated seeded app independently

`scripts/verify_generated_app_result.py` is a separate external command. It needs
the exact original terminal deployment receipt, successful fixture seed receipt,
and actual build-journey output containing one correlated discovery candidate.
The fixture must be seeded **before app creation**. Adding data after the build
cannot qualify this cell. A changed receipt, expired run, unrelated creator,
ambiguous app inventory, replaced deployment/source, or already-packed target
blocks the command; it never repairs a precondition.

Start with an offline plan and fresh paths:

```sh
.venv/bin/python scripts/verify_generated_app_result.py \
  --terminal-receipt /private/tmp/wt-r01-deploy.json \
  --journey /private/tmp/wt-r01-build-observation.json \
  --seed-receipt /private/tmp/wt-r01-seed.json \
  --output /private/tmp/wt-r01-app-verification-plan.json \
  --artifacts /private/tmp/wt-r01-app-verification-plan-artifacts
```

This creates no SDK client/browser and does not mutate the app. Without a task
plan it records that actual UI calibration is still missing. Review the actual
generated app through a genuine attendee browser, retaining an actual screenshot
and accessibility tree privately. Derive a task plan from that UI; do not guess
labels from an example app or send evaluator instructions to the builder.

The task-plan object uses this contract:

| Field | Required content |
|---|---|
| `schema_version`, `operation` | Integer `1`, string `generated_app_packed_task` |
| `backend_kind` | `seeded_unity_catalog`; choose `unsupported` when this scoped UC oracle cannot establish the app's storage |
| `input_sha256` | Exact `terminal_receipt`, `journey`, and `seed_receipt` byte hashes from the local verification plan |
| `ui_calibration` | `source: actual_generated_app_ui`, `reviewed_by`, aware `observed_at`, exact `app_id`/`deployment_id`, and `artifacts` containing both an actual `screenshot` and `accessibility_snapshot`, each with absolute `path` and SHA-256 |
| `record_id` | One actual late, unpacked record from this seeded fixture; no expected result can substitute for its live read |
| `page_ready` | An exact observed heading/region locator proving the app view is loaded |
| `record_scope` | A `row`, `group`, `listitem`, or `article` role plus `anchor`: an exact cell/rowheader/heading whose name is that fixture record's order ID or customer name |
| `before_status` | The exact observed cell/status/heading/rowheader within that record before packing |
| `packed_action` | The exact observed button/checkbox/switch within that record; the only supplied mutation is one click |
| `after_condition` | Either `kind: visible_status` with a distinct exact scoped `target`, or `kind: record_absent` when packing removes the order from the attention queue |

Every locator object has exactly `role`, observed `name`, and `exact: true`.
Record filtering and child matching use separate exact anchors; a global “Packed”
label, “Unpacked,” or “Mark as Packed” substring cannot satisfy the status check.
Unknown fields, scripts, SQL, repair steps, and arbitrary action sequences are
rejected. The calibration ties the task to actual reviewed UI; it is not a
calibrated visual-quality score.

Recheck the plan with `--task-plan /private/tmp/wt-r01-app-task.json`, using another
fresh output/artifact path. Then execute with a genuine private browser-state
file outside the repository, readable only by its owner:

```sh
.venv/bin/python scripts/verify_generated_app_result.py \
  --terminal-receipt /private/tmp/wt-r01-deploy.json \
  --journey /private/tmp/wt-r01-build-observation.json \
  --seed-receipt /private/tmp/wt-r01-seed.json \
  --task-plan /private/tmp/wt-r01-app-task.json \
  --browser-state /private/tmp/wt-r01-generated-app-auth.json \
  --output /private/tmp/wt-r01-app-verification.json \
  --artifacts /private/tmp/wt-r01-app-verification-artifacts \
  --execute
```

Execution reverifies the terminal snapshot, workspace/operator binding, exact
fresh generated app/deployment/source/creator and inventory, seed table identity,
and actual grants. It reads the real target row before clicking, performs the
single reviewed UI action, then compares fresh SQL reads for `packed: false →
true`, preserving unchanged target fields. It checks the same scoped postcondition
after reload and in a fresh context with generated-origin local/IndexedDB state
removed. It retains actual first-preview/final screenshots, ARIA text, and overflow
observations at 1440, 768, and 390 pixels. Auth redirects and password fields skip
capture. A failed task or UI/backend contradiction remains a failed observation;
missing/auth/collector evidence remains unverified.

The oracle covers only this exact externally seeded UC table and target record.
It does not qualify another backend, app-created provisioning, other-row changes,
or a real app process restart. The command preserves `accepted: false` even when
its functional checks pass. Exit `0` means offline planning, `1` means observation
completed without full acceptance, and `2` means a blocked/incomplete operation.

`mlflow_browser_input` in the result supplies actual accessibility text, observed
tasks, and the storage outcome to the existing
`plan_quality_evaluation(..., app_evidence=...)` adapter. It performs no MLflow
registration/judge calls. Screenshot references remain file paths; text judges
cannot infer image contents, typography, color, or visual polish. Calibrated visual
review against the actual images, real restart evidence, usage, cleanup, and the
later CT deployment phase remain required.

## Judge the complete generated app

Use the fixture in
`evals/generated_apps/scenarios/novice-bakery-order-queue-v1.json`. Enter only:

> I run a small bakery. Can you make a page so I know which orders need attention?

First record the current wizard's mandatory-industry friction. If it prevents
progress, retain that failure. A separate compatible arm may choose Retail through
normal UI; skipped/disabled wizard arms are separate cells. Do not conceal the
extra step or supply missing business facts to rescue a build-first agent.

Qualify real native log envelopes against the receipt's exact reviewed pin:
current Claude `2.1.283` or Codex `0.157.1`; historical fixtures retain `2.1.237`
and `0.148.0`. The simulator receives complete assistant
messages; PTY screen fragments and tool output are not assistant turns. Unknown
formats/questions stop for review rather than inviting a technical rescue prompt.
Once the exact submitted opening is correlated to a bound native session, build
collection can continue to its total deadline while a tool-using assistant turn
is unfinished. Consultation remains unverified until complete authored text is
available; the simulator sends no replies after its consultation deadline. An
entry timeout before opening delivery is an evaluator entry failure, with no
agent-quality verdict.

Full acceptance still requires live app discovery tied to fresh deployment/source
and exact resource ownership; deterministic seed/backend truth; the visible packed
action; reload/second-context/process-restart persistence; first-preview and final
screenshots; responsive/accessibility/error-state checks; duration/usage evidence;
and exact cleanup. A URL, RUNNING app, imported observation JSON, successful local
test, or text-only MLflow judge cannot supply those missing proofs. Screenshot
paths are not images seen by a text judge. Keep missing evidence unverified.

In the isolated CT-compatible package build, pass the verified current seed receipt
with `--generated-access-seed`. Actual CT grants catalog/schema read access to
`account users` inside each contained attendee workspace, covering future app
principals. The Labs simulation instead provisions only the fresh, uniquely
attributed generated app's exact SP on the receipt-owned catalog and fixture
schema. The separate `.generated-access.json` ledger records direct and effective
readbacks and any partial grant. Include that ledger in evidence and generated-app
cleanup; remove only its recorded added privileges before deleting the app. Never
copy CT's account-group grant into shared Labs. This polling equivalence does not
qualify CT's static provisioning timing or allow extra write permissions to repair
a generated app.

Currently supported CLI syntax can be inspected locally without cloud mutations:

```sh
.venv/bin/python scripts/evaluate_generated_apps.py --help
.venv/bin/python scripts/evaluate_generated_apps.py entry --help
.venv/bin/python scripts/run_generated_app_journey.py --help
.venv/bin/python scripts/verify_generated_app_result.py --help
```

The CT-oriented `discover`, `prepare`, and `poll` commands are for the later
integrated phase. Do not fabricate CT inventory/admission observations to force
the isolated simulation through its CT gate. The standalone command supplies the
receipt-bound collector and conversation/build observation. Deterministic
seed/backend checks and independent app browser tasks are wired separately
through the verifier above for the seeded-UC target. Real restart persistence,
calibrated first/final visual review, usage measurement, actual native MLflow
scoring, and a single complete acceptance execution remain pending. These checks must use independently
collected evidence and preserve their limitations; importing expected observations
or passing local fixture tests cannot complete them.

## Preserve evidence and clean up

Retain spec, plan, every deployment/resume receipt, source identities, actual app
and deployment IDs, run/session IDs, private-auth-free conversation, independent
browser/backend evidence, scores, budgets/usage, and failure/cleanup verdicts.
Keep first-attempt and final outcomes separate. An actual CT receipt belongs to
the integrated phase; simulated `sim-...` IDs are not CT-created units.

Receipt-driven WT teardown is implemented in
`scripts/qualify_ct_compatible_wt_test.py cleanup`. Complete generated-app
observation and archive evidence before running it. Its scope is the WT resources
and exact SP permission deltas; it does not discover or delete generated apps or
arbitrary backends. Generated-resource teardown therefore requires a separately
reviewed, identity-bound inventory of the app, creator/time, bundle/snapshots, and
backend IDs/owner/time. The R01 closeout archives the actual scoped teardown
scripts and independent absence readbacks, including Lakebase tombstone handling.

Close only this run's native session. Delete only its exact owned resources through
ordinary platform APIs, and preserve attendee projects and every other principal.
Avoid name-prefix fleet sweeps and never delete existing event resources. Preserve
failed teardown receipts, confirm final absence, and report leftovers as cleanup
failures. TTL expiry and a soft-delete request alone are not absence proof.

Actual CT integration must use a new designated disposable unit after isolated
WT passes. Use CT's existing provision/cleanup interfaces and record the assigned
labuser, handoff, OBO/resource grants, deployed source identity, lifecycle, and the
same independent app-task/UX outcomes. Existing Labs CT code/deployment stays intact.
