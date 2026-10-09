# Generated-app E2E acceptance: a simulated nontechnical attendee

Status: **R01 is closed with a failed baseline**; the
[eligible current-release build and actual app evidence](remediation-validation.md#r01)
are archived and test resources cleaned. The simulator, browser/harness adapters,
bounded journeys, reports, CT-compatible package workflow and native MLflow
adapters exist. This contract's full passing acceptance remains outstanding:
independent Lakebase/restart/fault checks, calibrated UX scoring, wider route
coverage, product remediation and later CT integration are not supplied by R01.

Companion audit: [app-quality-audit-and-remediation-plan.md](app-quality-audit-and-remediation-plan.md).
Extended wizard audit and fault gates: [onboarding-wizard-audit.md](onboarding-wizard-audit.md).
Proposed scenario fixture: [novice-bakery-app.json](examples/novice-bakery-app.json).
Implementation status and commands:
[generated-app-evaluation-runbook.md](generated-app-evaluation-runbook.md).
Executable private simulator fixture:
`evals/generated_apps/scenarios/novice-bakery-order-queue-v1.json`.

## What the test must prove

Apply the [workshop pacing guidance](app-quality-audit-and-remediation-plan.md#workshop-pacing-and-scope).
The target is an attractive, usable demonstration of the art of the possible.
A clear request may proceed immediately; an ambiguous goal should usually need
one short exchange containing one or two consequential questions, a concise
recommendation, and transparent demo assumptions. Aim for about a minute of
idea shaping. Extra questions, formal scope approval or production planning are
quality defects when they add no material value to the demo.

During the attendee build, verify the main action, readable dates/labels,
appropriate screen layout and reload persistence where claimed. Detailed fault,
restart, backend, accessibility and calibrated visual checks below are operator
qualification of reusable foundations and releases. Run those outside the
attendee conversation; do not make every attendee wait through that whole suite
or supply a production specification. Preserve the original R01 receipts and
verdict; this guidance changes the remediation target, not historical evidence.

From a simple user goal, Workshop Terminal and an offered agent must:

1. Clarify consequential missing requirements and advise in plain language.
2. Preserve the attendee's answers and agree a useful first version.
3. Create, build, provision, and deploy a real Databricks App.
4. Produce a convincing first preview and a usable final app.
5. Demonstrate the main workflow through an independently controlled browser.

The test starts with the attendee opening Workshop Terminal. It finishes with an
authenticated browser completing the generated app's actual task and independent
evidence of persistence, presentation, and recovery. A model response, scaffold,
git commit, HTTP 200, or Apps RUNNING state cannot independently establish a pass.

## Golden scenario

First message, authored exactly as a novice might write it:

> I run a small bakery. Can you make a page so I know which orders need attention?

The attendee selects the business explanation option. They never say AppKit, APX,
React, SQL, Lakebase, persistence, Playwright, or a component name. They do not
provide a UI specification or tell the agent to improve its design.

The simulator has private facts that the building agent does not receive up front:

| Topic the agent might ask about | Novice answer |
|---|---|
| Who will use the page? | The people working in the shop. |
| What needs attention? | Orders that are late and haven't been packed yet. Show those first. |
| What should staff do? | Mark an order as packed when it's ready. |
| What happens when the page is reopened? | Yes, it should remember what we changed. |
| What data is available today? | Sample orders are fine for today. |
| Current workflow | We use a spreadsheet and keep asking each other what's ready. |
| What is out of scope? | We don't need payments or customers signing in today. |
| An unfamiliar technical choice | I don't know what that means. What would you recommend? |
| Colors or layout | Please choose something easy for the team to use. |

These are business facts, not a checklist handed to the builder. The simulator
answers only what the agent actually asks. Acknowledging a recommendation is
allowed; volunteering missing write-back or persistence requirements to rescue a
build-first agent is not. Facts must remain consistent across runs.

A good interaction might ask what needs attention, then recommend a small order
queue with a packed action, explain that demo orders and remembered changes will
let the attendee try it, and start building. A short "Yes, that sounds right" is
valid when confirmation helps, but it is not a mandatory additional turn. Safe
demo defaults need not each be elicited as private facts; label them as assumptions
rather than claiming the attendee requested them. The exact wording and screen
layout are open. A visually polished read-only chart dashboard that omits packed updates
does not meet this user's intended task.

Use one additional fully specified/trivial control case to ensure remediation
does not turn every request into a questionnaire. In that case, immediate
implementation is appropriate when there are no material unknowns. The golden
bakery request intentionally has material unknowns.

## Simulator rules and independence

Implement a constrained user simulator, preferably a scripted fact policy with an
optional model for natural phrasing. The simulator is a separate process/agent
from the builder and evaluator. Its private facts and evaluation criteria must
never be included in the build prompt, home instructions, or project memory.

The simulator may:

- Type the opening sentence through the wizard or terminal.
- Answer questions using its private facts in ordinary language.
- Say it does not understand a technical question and request a recommendation.
- Agree to a plainly described scope when it matches the intended task.
- Try the product and report a visible business problem, such as "It forgot the
  order I marked packed when I came back."

It may not:

- Choose a framework, package, database, API, command, file, or component.
- Request an audit, screenshot, accessibility check, or test on the builder's behalf.
- Reveal the scoring rubric or proactively supply all acceptance criteria.
- Debug authentication, logs, resource IDs, deployment, selectors, or TypeScript.
- Approve a scope that conflicts with its private business goal merely to keep
  the run moving.
- Access the builder's source/tools or independent evaluator reports.

Check simulator adherence as part of each run. A technically rescued run is invalid
as novice evidence and must remain visible in the report. Fail or time out agents
that never ask needed questions; do not teach the answer through a hidden retry.

## System boundary and setup

Use two qualification phases. First deploy a disposable WT instance directly into
Labs with fresh resources and the WT environment/resource contract that CT would
supply. The simulation planner makes no CT calls; the deployment wrapper uses the
Databricks SDK and blanks real CT destinations/tokens. This phase exercises real
WT, Unity AI Gateway, resolved models, pinned harness/toolchain, scaffold, resource
bindings, and Databricks Apps deployment. It does not qualify CT provisioning,
event-attendee isolation, or fleet behavior. Current R01 uses the genuine assigned
`labuser+1@awsbricks.com` browser identity with separate operator provisioning.
An operator-bound exploratory run must record `operator_bound` explicitly and
cannot replace assigned-attendee E2E evidence.

After isolated WT passes, repeat the same scenario on an explicitly designated
disposable one-attendee CT rehearsal unit with an actual assigned labuser and
event-equivalent access. Test the existing CT integration through its supported
interfaces. Do not change CT code, its shared settings, or the working Labs event
to unblock a WT test. Keep isolated and integrated verdicts separate.

Keep the runner outside the attendee container. Preinstall its browser and driver
dependencies, and authenticate the browser as the synthetic attendee. Verify the
chosen browser method reaches the private WT and generated-app URLs without
relaxing real platform access. Builder credentials and browser credentials have
their existing separate roles; never inject bearer tokens into prompts or evidence.

The setup manifest records workspace, WT revision/configuration, synthetic identity,
fixture IDs, allowed resource namespace, clock, model/harness versions, composed
policy/skills digests, template/dependency versions, and budgets. Begin with no
preexisting generated application that could be presented as a new build.
An instrumented runtime snapshot must record its parent Git SHA, runtime file
digest, final uploaded digest after environment/SP binding, and instrumentation
status. The read-only native transcript bridge is default-off and admin-only;
enable it only for the exact synthetic marker, principal, run, and unit. Native
logs are attendee-writable and omit tool/worker coverage, so complete authored
messages alone cannot prove mutation timing or all-harness authenticity.

Seed deterministic, representative bakery orders in the evaluation catalog through
the ordinary demo-data path. Make them discoverable through the same overlay/tools
as event data, not through technical user instructions. Include late unpacked,
future unpacked, and packed orders; long names; realistic dates and amounts; and
enough rows to exercise browsing behavior. Capture a per-run UTC epoch in the
setup manifest and materialize fixture date offsets from it: late orders are at
least 24 hours overdue and future orders at least 72 hours ahead, beyond the hard
run deadline. Record actual seed timestamps and independently recheck lateness
at verification time. This keeps classifications stable without freezing a cloud
host, injecting private acceptance facts, or patching generated app source. If a
separate test uses a configurable app clock, provide it through the ordinary
starter/runtime configuration and verify browser, server, and SQL agreement.

The agent remains responsible for discovering the source, choosing storage for
updates, creating/binding resources, and serving real data. If setup supplies a
reusable Lakebase resource for a separate warm-start scenario, disclose that in
the run manifest and do not count it as proof of fresh provisioning. Preflight has
its own fixed deadline; the app run has an overall deadline beginning with the
first user submission, including consultation, build, repairs, and retries.

The bakery scenario's dataset is synthetic and plainly labeled in the product.
External functional assertions compare the displayed records/updates with fixture
and backend truth. Displaying hardcoded fixture-like rows alone is insufficient.

## Runner components and remaining acceptance work

| Component | Responsibility |
|---|---|
| Scenario loader | Validate public prompt/private facts, seed manifest, expected tasks, budgets, and scenario version; import blueprints into a discovered/reused MLflow GenAI dataset. |
| Environment adapter | Provision/reuse an explicitly designated disposable evaluation unit, prepare identity/data, inventory resources, and validate operational readiness. |
| WT browser driver | Complete onboarding, choose the specified offered agent, launch through the normal UI, send novice replies, and test reconnect/switch behavior. |
| Harness observer | Capture synthetic conversation plus structured tool/worker events using supported harness interfaces; normalize events while retaining raw synthetic evidence. |
| User simulator | Answer only the asked business questions, without knowing the source or rubric. |
| App verifier | Independently identify the newly created app/resource set, confirm deployment, sign in, run tasks, inspect backend truth, and collect browser artifacts. |
| UX evaluator | Use registered MLflow scorers/judges and native GenAI evaluation for transcript/recommendation quality; combine with independently observed task/screenshot evidence and calibrated human review. |
| Report/cleanup | Produce per-cell verdicts/costs/versions/failures, redact credentials, and delete exactly the inventoried evaluation resources. |

Prefer structured event streams exposed by the shipped harness. PTY transport is
already exercised by `scripts/regress_mode2.py`, but terminal text alone is not a
reliable universal representation of messages/tool calls. Build and verify each
adapter against the event-pinned CLI. If required events cannot be observed for a
path, mark that behavioral check unverified; do not infer it from sparse prose.

Implemented foundation layout:

```text
evals/generated_apps/
  adapters/                 # browser, harness, CT client, pure simulated-CT plans
  scenarios/                # private novice simulator facts
  simulator.py
  journey.py                # bounded native-turn observation and UI replies
  report.py                 # missing evidence stays unverified
  mlflow_evaluation.py       # native datasets, scorers/judges, GenAI evaluation
scripts/evaluate_generated_apps.py
scripts/deploy_generated_app_test.py
server/evaluation.py         # optional read-only admin transcript bridge
```

The current evaluation CLI supports CT discovery/draft/poll, target planning,
preflight assessment, and a UI-entry probe. Its `entry` command closes its owned
session after probing; it does not run the novice conversation or accept a
generated app. Imported observation JSON is an assertion until bound to fresh,
independently authenticated collection. The journey and app-verifier libraries
still need complete executable wiring, live deployment discovery, backend/restart
proof, and visual review. Native Linux format qualification against the event pins
also remains required. See the runbook for currently supported command examples.

A full-run invocation must eventually select scenario, release, harness route,
framework arm, repetitions, and explicit budget, collect the evidence above, and
emit a nonzero failure status. Configured TTL/spend/token ceilings do not establish
enforcement or automatic teardown; retain those gates as unverified until observed.

Use MLflow native datasets, registered scorers/judges, and
`mlflow.genai.evaluate()` for the evaluation layer, with verified synthetic traces.
Discover existing experiment/dataset/scorers first and dry-run a small subset before
bulk evaluation. The external driver owns browser, provisioning, simulated-user,
and harness orchestration; it is not a second bespoke evaluation engine. Scenario
JSON remains an import blueprint. Keep simulator private facts and evaluator
criteria out of the builder's prompts and instruction files.

## Run sequence and assertions

### 1. Enter through Workshop Terminal

Open the deployed WT as the synthetic attendee. Choose business explanations.
On the proposed goal-first path, enter the single sentence, continue without a
mandatory industry choice, select the target agent, and submit the carried prompt
normally. Confirm original wording, any selected idea snapshot, and brief revision
are preserved. Do not treat selecting a suggestion as agreement on unstated scope.

The audited current wizard blocks Next until a free-text attendee confirms an
industry (`Wizard.tsx:402-407`). The baseline must record this gate/friction; the
test must not silently bypass it. If the novice cannot proceed, report the entry
failure. An additional current-flow-compatible arm may choose the closest offered
industry (Retail for the bakery) as an ordinary novice UI answer, recording the
extra step and choice. Use that arm to observe downstream baseline behavior; do
not report it as a pass for the proposed optional-industry invariant.

For the paired Omnigent UI-first variant, load WT only to authenticate and mirror
OBO; do not create a local terminal or call `POST /api/sessions`. Start a brand-new
chat directly in the paired Omnigent UI and supply the novice sentence there.
Assert complete preparation before the remote host/chat is usable. This is a
separate entry sequence from the normal wizard-to-local-terminal journey.

Run variants for skipped wizard and a server-generated dynamic idea. The dynamic
variant must preserve full prompt/table context and stated industry through save,
reload, process restart, and launch, including its immutable snapshot and source.
It is a distinct regression test, not a replacement for the plain novice sentence.
Also cover wizard disabled with an existing saved brief so stale context cannot
silently change the new user's task under the configured disable semantics.

Before expensive cloud builds, run the rendered wizard fault matrix from the
extended audit: stale responses while editing, selection/Next/Skip with pending
debounce, reordered industry GET/model POST, clear/unknown industry, delayed
Surprise, static-only matching, empty/fallback/error output, save/session/prompt
delivery failure and retry, draft reload, keyboard focus, and blocked agents.
Assert every applied candidate set belongs to the current input revision and every
launch belongs to the saved brief revision. Controlled mocks isolate those faults;
real generation/deployment is still required in the golden run.

Evaluate idea relevance separately on a held-out stratified novice dataset with
the actual configured wizard models/catalog. Check recommendation fit, plain-language
reason, achievable scope, meaningful assumptions, and truthful data readiness.
Then carry representative selected cards through actual app builds. A structurally
valid card or six visible cards does not establish a useful recommendation.

### 2. Observe consultation

Timestamp the first user goal, questions, recommendations, answers, agreed scope,
first scaffold/code/resource mutation, and any worker delegation. Preparatory reads,
data inspection, and brief documentation are allowed before scope is agreed.

For the bakery case, establish the meaning of attention and communicate a small
usable demo before building. A packed action and remembered demo changes can be
a clearly stated recommendation; the agent need not ask separate questions about
every sensible default. Fail when the result contradicts the attendee's answer,
material ambiguity is ignored, or excessive consultation delays a useful demo.
Ask only missing product questions; do not require needless
confirmation of facts already answered in the wizard. A recommendation must give
a concrete solution and a plain-language reason, and the attendee's response must
be reflected in the scope and resulting app.

Persist the brief revision supplied to delegated workers. An agent saying "I'll
check requirements" is not evidence that it did. Evaluate the actual exchange
and the build's alignment with the answers.

### 3. Observe build and first preview

Allow the agent to choose/adapt the selected framework arm's starter, discover
data/resources, and implement normally. The framework arm is an operator/test
configuration, not a phrase supplied by the nontechnical attendee.

Record package resolution, policy/reference access, resource creation and grants,
build results, deployment revision, first URL, and actual resolved harness/model
per brain/worker. Stop on budgets or a terminal failure and retain evidence.

At the first offered preview, immediately snapshot the main page and evaluate
initial usefulness. Capture a short task probe without changing the builder's
scope. This prevents hiding a poor first impression behind an eventual polished
final result. A transient startup error is recorded rather than recast as a useful
preview. Keep first-preview and final-quality scores separate.

### 4. Independently verify the deployed task

The app verifier authenticates as the attendee and uses rendered controls. It may
adapt to natural labels/layouts; it must not submit technical guidance to the
builder. Use role/label locators or a bounded browser agent. Record its actions
and prevent source access from turning a broken UI into a passing task.

Critical assertions:

- A recognizable order queue exists; late unpacked fixture orders are shown ahead
  of future work. Packed records are not misleadingly presented as needing action.
- Staff can inspect a specific order's details and understand what needs doing.
- Staff can locate a known order through an effective visible retrieval mechanism.
  If the app provides search/filter, verify it finds known records and offers a
  useful reset when nothing matches; the golden scope does not mandate that widget.
- Marking an order packed updates the list/detail and provides clear success feedback.
- Reload, a new browser page, and an app-process restart retain the update. Confirm
  backend state independently; a browser localStorage value does not prove shared
  team persistence. Also check a second authenticated browser context sees the update.
- Invalid submission, if a form is part of the agreed scope, preserves input and
  offers associated field errors; valid writes do not happen twice on repeated clicks.
- Navigation reaches real content, and every visible action is implemented or
  clearly disabled with a reason. The test does not require unrequested pages.
- Browser console/page exceptions and unexpected failed API requests are absent
  on the critical journey; known injected failures are classified separately.
- Resource ownership/grants and execution-identity disclosures match the deployed
  behavior. The generated app uses its own bound runtime resources, not credentials
  accidentally available only inside the WT container.

Purely API-level assertions supplement the browser. The critical action must be
completed from the UI. Real deployed data and persistence checks cannot be mocked.

### 5. Verify UX and states

Capture main queue, order detail, successful update, loading, empty, no-results,
and error/retry states at 1440, 768, and 390 CSS pixels. No-results coverage applies
when search/filter is present. Also test 200% zoom,
keyboard-only interaction, visible focus, understandable labels, and reduced motion.

Use controlled delayed/empty/failed backend requests or a dedicated test fixture
for state coverage. Label any browser network injection in the evidence; it proves
state handling, not a real backend integration. Verify retry recovers against the
unmodified real service. Check that errors do not invent a cause or falsely promise
data safety, and that sample-data/provenance text is truthful.

Automate detection of unexpected page-level horizontal overflow, clipped primary
controls, serious accessibility violations, and obvious runtime failures. An
accessible table may have intentional horizontal scrolling within its own region.
Do not fail that as page overflow or require a tiny unreadable table to fit.

Screenshots and task observation are judged independently against the rubric below.
Measure computed contrast/focus and keyboard behavior where possible; class-name
presence is not a visual/accessibility pass. Automated tools do not prove complete
accessibility; retain a calibrated human review sample.

### 6. Bounded repair and continuation

Keep both first-attempt and final verdicts. Allow at most two repair rounds in the
initial protocol, within the original run budget. The simulator reports visible
business failures in plain language. Internal verification feedback may reach the
builder only through the same shipped verification/repair capability available
to real attendees; record its origin and do not attribute it to the novice.
Technical feedback from the independent test oracle is permitted only in a
separate assisted diagnostic variant, excluded from the unassisted golden release
pass. The evaluator must not improve an otherwise failing attendee experience
through a capability the workshop does not supply.

Never allow the independent scoring judge to rewrite the app. The builder repairs
and redeploys; the verifier repeats failed checks and any affected critical journey.
Additional repairs do not erase original failures or make them independent passes.

In separate continuity variants, reconnect after scope agreement, resume after
credential renewal, and switch to a different offered agent before completion.
Workers must retain the confirmed brief and decisions without asking all questions
again or silently replacing the selected framework/product.

### 7. Evidence, verdict, and cleanup

Save a run manifest, synthetic transcript, normalized timeline, generated source
commit, deployed source revision and resource inventory, browser actions/trace,
screenshots, computed checks, rubric scores, repair messages/count, wall-clock and
active build time, token/spend totals, and final cleanup verdict.
For wizard entry, also retain request/input revisions, generation/selection IDs,
selected snapshot and provenance, saved/launch brief revisions, effective model,
capability/catalog version, generated/accepted/padded counts, fallback reason,
latency and stale-response discards. Correlate these with the independently observed
agent prompt and resulting task; redact secrets and real attendee data.

Do not retain gateway tokens, app credentials, browser session secrets, or raw real
attendee transcripts. Browser traces/network captures need redaction and controlled
retention too. Mark a claimed success without independently verified deployment
as failed/unverified, never accepted.

Clean up only resources owned by the run's inventory/namespace, using existing
rehearsal provenance rules. Preserve report artifacts first. A cleanup failure is
a distinct run failure; do not conceal it behind a functional pass.

## Scoring and gates

Use 1-5 scores with example anchors: 1 unusable/absent; 2 substantial defects;
3 usable but generic or needing help; 4 coherent and independently usable;
5 unusually clear, polished, and well matched to the task. Calibrate the rubric
with current and human-reviewed strong outputs before selecting the release.

| Dimension | Evidence for a strong result |
|---|---|
| Requirements | Identifies meaningful unknowns, reuses known facts, and confirms consequential scope. |
| Advice | Recommends a concrete MVP with a user-facing reason and relevant tradeoffs. |
| Language and agency | Understandable conversation; attendee answers affect the product; no technical quiz or design supervision required. |
| Information hierarchy | Obvious purpose, late-order priority, readable details, and clear next action. |
| Composition and density | Consistent spacing/type, deliberate grouping, reasonable amount of content, no unused template chrome. |
| Interaction | Primary task discoverable and efficient; actions/navigation work; useful validation and feedback. |
| Responsive/accessibility | Legible, operable controls and content across widths, zoom, keyboard, and required states. |
| Data and trust | Correct orders/state, truthful sample labels, identity/provenance and appropriate freshness where relevant. |

Proposed initial release criteria, finalized before comparing candidate releases:

- Every golden cell deploys a real app and passes all critical task/persistence checks.
- No critical/serious accessibility defects, blocking clipping, dead primary
  controls, unhandled journey errors, or false persistence/data-success claims.
- Requirements, advice, and overall UX average at least 4/5, with no scored
  dimension below 3. The first useful preview must meet the same UX floor, not
  only the repaired final app.
- Three fresh independent golden builds per offered cell pass initially. Report
  3/3 as limited evidence, not a statistical guarantee of all future builds. Expand
  samples and scenario breadth before a large event; show uncertainty and failures.
- Every failure, invalid simulator run, retry, and repair is visible. Do not average
  a failing harness into a passing fleet number.
- Set hard preflight, consultation, and overall run deadlines before execution.
  Proposed starting caps are 15 minutes preflight, 3 minutes consultation from the
  first user submission, and 30 minutes total after that submission. All repairs
  and retries consume the same original total budget. No scope agreement is a
  timed-out/failed run, not an opportunity to avoid starting the build clock.
- Initial timing targets: recommendation/demo framing in about a minute, usually
  one brief exchange; useful
  preview within 10 minutes of scope agreement; accepted golden app within 20
  minutes. Treat these as proposed targets to calibrate against baseline resource
  availability. Report cold setup, attendee waiting, active build, and repair time
  separately; changing a target requires a recorded decision before the comparison.
- Freeze an explicit per-run token/spend ceiling and resource allowlist before
  execution. A run exceeding it times out/fails and stays in the denominator.

Use objective task checks plus an independent visual/transcript judge and blinded
human calibration. A builder reviewing its own output or an LLM screenshot score
alone is insufficient. Record evaluator model/version/settings, disagreement, and
adjudication; comparisons should use the same evaluator conditions.

## Required coverage matrix

Derive exact offered harness/model routes from the deployed catalog and readiness.
The repository documents bare Claude/Codex plus local and paired self-hosted
Omnigent as supported. Do not silently include unsupported managed/sandbox modes.

| Suite | Cells and assertions | Cadence |
|---|---|---|
| Fast contracts | Common prompt policy; selected dynamic idea round trip; brief provenance/version; required preparation retries; cold/warm overlay integrity; noisy helper/adoption; actual exported UI props and rendered starter states | Each PR |
| Rendered WT journey | Wizard goal/card/skip/disabled → save → reload/restart → agent choice → exact carried brief; request races, clear/static mode, fault recovery, keyboard/readiness; paired UI-first preparation; switching/reconnect | Each relevant PR |
| Wizard recommendations | Held-out novice requests across sectors/no-sector/unknown-sector/intent/conflicting defaults; real model fit/feasibility and deterministic malformed/429/timeout fault coverage; selected cards continue to real apps | Nightly/qualification |
| Golden real builds | Bare Claude, bare Codex; local Omnigent coordinator and each offered native worker; paired remote via WT and paired UI-first; Auto with actual choice recorded | Release, three fresh runs per offered cell |
| Framework experiment | Current AppKit baseline, remediated AppKit, refreshed pinned APX; matched harness/model/data/budgets; both operational and analytics tasks | Default-selection release |
| Scenario breadth | Bakery write-back; read-only analytics; AI/Genie answer with SQL/identity/states; unfamiliar industry; vague idea; complete trivial ask; mid-build correction; explicit brand/accessibility need | Nightly/qualification |
| Experience and help preferences | Nontechnical user; developer new to Databricks; Databricks practitioner new to UI; experienced app developer; optional preference absent/explicit/changed mid-session; wizard disabled; worker/switch/reconnect continuity. Same functional/UX floor; relevant advice and appropriate detail without unnecessary questions | R02/R03 qualification, then release |
| Continuity/faults | Fresh and prewarmed; redeploy; worker worktree; reconnect after scope; agent switch; credential renewal; gateway fallback; failed deployment and retry; missing required content | Nightly/release |
| Workshop scale | 5, 15, 30, then 60 concurrent actual builds across the offered mix; gateway contention, model fallback, browser/service capacity, resource quotas, quality/cost/duration by path | Event rehearsal |

Run Auto repeatedly and require each *offered/reachable candidate route* to have
direct evidence too. A stochastic selector may never choose a weak route during
three runs. Record mid-run changes of worker/model and evaluate the actual route,
not only the coordinator card's label.

The 60-build rehearsal must reuse the novice scenarios, real app deployment, and
independent acceptance. Sixty green `/readyz` responses or arithmetic model probes
are complementary infrastructure evidence, not the generated-app quality test.
Use sixty independent WT attendee instances for wizard demand too. Include
synchronized cold arrival and realistic edits/industry changes/refresh/retry before
launch; measure actual requests/tokens, deadlines and quality by model. One-app
singleflight experiments do not establish fleet-wide gateway capacity. Apply the
extended audit's wizard load gates alongside the existing build-quality gates.

## Expected release report

Publish one row per cell/run: input/scenario version, WT release, framework/starter,
brain/worker/model, policy digest, consultation score, first-preview UX, critical
task verdict, final UX, repairs, duration, spend, cleanup, and evidence links.

Aggregate by harness/model/framework and show first-attempt and accepted-result
rates separately. Show baseline versus remediation without dropping failed runs.
Include representative first-preview and final screenshots, all hard failures,
the AppKit/APX decision, and any deliberately unoffered path.

Qualification is complete only when this report proves a novice-to-deployed-app
journey. The existing passing text, transport, build, readiness, and soak tests
remain useful inputs; none substitutes for that report.
