# R03: goal-first onboarding and saved-task integrity

Status: acceptance remains pending; PR92 is unmerged. The held-out run completed
all 60 unique inputs once, returning 59 visible cards and one false rejection.
Native MLflow evaluation completed all 360 Boolean assessments: 354 passed and six
failed, with no errors, missing cells or model disagreements. All six failures
concern the same empty website-path result. The repair is in `673de90`; the original
outcome remains failed. Actual Claude/Codex app journeys remain unqualified.
The R03 branch includes
main through merged Agent Bricks PR91 (`2a19cde`); that feature remains opt-in.
This work does not change Control Tower.

## Current acceptance and workshop continuity

The exact `673de90` push and PR CI passed on 10 October. Its reused Labs WT
completed a genuine bakery suggestion, selection and saved-goal handoff as the
assigned attendee. Claude 2.1.295 received the actual offered starter unsent, and
the starter survived its first-run folder-trust prompt. Native Opus 5.5 transport
completed one small request. A concurrent main request was rejected before
upstream by the scratch evaluation proxy; a subsequent main request received
HTTP 400. The proxy retained only the error response hash, so its exact cause is
unproven. This is not an accepted app journey: no source or generated app was
created, and Codex 0.162.0 was not launched. Normal closure reaped Claude and
closed/drained admission. The $23.115204486528 conservative total includes the
full failed-request hold; it is not an invoice charge. Raw records stay outside
Git, and a continuation must preserve that hold within the approved $250 total.

The reviewed continuation reused the same app and compute and passed 102 offline
activation/transport checks. Its fresh genuine wizard save and unsent starter
delivery worked. Serialization removed the previous concurrent-request rejection;
the main Claude request still received HTTP 400. Its bounded diagnostic classifies
an unsupported request field and references `messages` and `output_config`.
The prior error's exact cause remains unproven. The scratch guard treated every
non-200 response as terminal, blocked a different follow-up request before
upstream, and was closed/drained;
normal UI closure reaped the child and confirmed no project sources. Codex's scored
app journey remains unstarted. Both native attempts retain their full failed-request holds, totalling
$46.2302752125984 conservatively. This is an evaluation-infrastructure failure,
not a verified CLI/gateway incompatibility. Genuine app journeys remain required
before acceptance or merge.

The subsequent offline investigation reproduced Claude 2.1.295's native recovery
from a synthetic `messages.1.output_config: Extra inputs are not permitted` HTTP
400. With Opus 5.5 selected, the CLI emitted `per_turn_effort_changed`, removed
the per-message effort field, and completed against a synthetic successful SSE
response. The unchanged v3 guard blocked that corrective request with HTTP 409.
A fresh v4 scratch guard permits one verified correction of the same conversation,
retains the full failed-request hold, and requires another whole-model reservation
within the existing cap, call limit and deadline. It does not translate the native
protocol or permit arbitrary retries. The real pinned CLI passed this repaired
offline path, and 109 targeted accounting/transport/recovery checks passed.
Seven older scratch activation tests also fail unchanged in v3 because their base
fixture omits the required continuation binding; this is not a fully qualified
live v4 package. No real credentials, external model traffic, tools or spending
were involved. Closed live ledgers, holds and expiry remain unchanged; raw fixture
records stay outside Git. The live error's exact text and follow-up payload delta
were not retained, so the reproduction establishes the guard defect rather than
proving that every live request would succeed. R03 does not change
`server/cli_config.py` relative to current `origin/main` (`88854d2`). No working WT
gateway or CT configuration repair is justified by this evidence.

Onboarding is optional for every offered harness. The closed scratch native
evaluation intercepted ordinary Home launches and returned “Native evaluation
is waiting for qualification or is closed.” The reused Labs app was restored to
the product entry point with evaluation disabled; its prior closed ledgers,
conservative holds and expiry were retained. Claude and Codex reached their
normal prompts after Skip without a saved goal or a submitted build prompt.
This startup check does not qualify their model transport or an app build.

With onboarding disabled, Claude also reached its normal prompt. Codex's direct
launch exposed a separate lifecycle failure: its default shared app-server
daemon retained a socket pointing into the previous container-local HOME after
a same-app redeploy. The attendee saw an app-server socket error, not a wizard
requirement. The catalog now launches `codex --no-daemon`, supported by the pinned
0.162.0 CLI, so the interactive runtime belongs to the WT terminal process.
The failed startup screenshot is retained outside Git; local catalog tests and
the actual CLI option check passed. The live retest, with the wizard disabled,
reached the normal GPT Sol 6.1 prompt using the supported catalog override on
the unchanged package. This qualifies startup only. The exact `4bb3e48` CI
package, with all checks green and the same catalog bytes, was then staged to
the same app. Its empty wizard closed immediately on Skip, stayed dismissed
after reload and allowed direct Claude launch to the normal Opus 5.5 prompt.
The final Codex check on that package was unqualified: the owned-compute watchdog
had stopped the app at 04:24:52 UTC before a terminal opened. The app identity,
resources and earlier successful Codex screenshot remain available for reuse.
No model/build prompt was submitted in these startup checks.

Skip now dismisses immediately and saves dismissal in the background. A scoped
tab-local marker prevents a failed or pending skip write from reopening the
wizard on reload. Manual goal editing still opens it. Local rendered tests cover
Claude, Codex and enabled Omnigent after Skip, with onboarding disabled, and with
wizard loading/saving unavailable; a delayed skip write cannot block launch or
reload. A further review found Skip and Escape disabled while saving or launching.
Both now remain available throughout onboarding. Dismissal ignores late save
callbacks and cancels queued starter delivery/retries; an already requested
session remains owned and usable. An accepted server write may still finish and
is preserved. Local rendered coverage exercises these races for every supported
harness, as well as delayed loading, suggestions, prompt retries, launch failures
and conflicts. All 39 rendered cases, 111 frontend unit tests and the production
build passed. This additional hardening
requires a fresh live check; it was not part of the `4bb3e48` deployment.

Recommendation HTTP p95 was 9.822 seconds, maximum 11.747 seconds. This exceeds
the audit's proposed eight-second p95 target; the twelve-second caller bound
alone is not a latency acceptance verdict. The extra 120 repetitions and 720
assessment cells remain unexecuted. Same-app redeployment reused compute and
browser sign-in, but restarted the container and reset local attendee HOME,
brief and model policy. State survival across code deployment is unqualified.

Workshops also need evolving goals and several builds per attendee. The current
Home exposes **Change what I'm building**. **Start a different project** is hidden
inside the goal wizard's Optional context; it creates a new brief identity when
saved, but WT keeps one current brief rather than a navigable project history.
Existing source directories remain separate from that brief. An existing agent
can receive the new starter in its current conversation; editing the goal does
not clear that conversation or immediately update all project instructions.
When onboarding is disabled, the wizard's goal-edit entry is also hidden.

Before claiming clear support for multiple builds, expose **Change this goal**
and **Build something else** together, state that earlier files are retained,
and make the active task and conversation behavior explicit. Keep this a short
choice rather than another requirements exercise. Verify one attendee builds A,
changes A's direction, starts B in its own directory without overwriting A, and
returns to A with its correct brief. Check both harnesses and the wizard-disabled
path. R04/R08 must qualify project/worker propagation and returning to prior
work; a new discovery-record identity alone does not establish project switching.

The follow-up on `4c1825d` reused the original Labs app and its provisioned compute
under a new bounded receipt. CI passed, including the offline release smoke after
registry fallback was added. The live dry run attempted three of 13 regression
cases and stopped: two model cards were visible; the third incorrectly claimed
verified device values despite inspecting metadata only and was correctly rejected.
The other ten were not dispatched. Field-level generation guidance now explains
fit through the requested action and listed columns, states that the wizard read
zero rows, and requires consistency with unresolved row inspection. Honest metadata
wording and the actual false device-value claim have positive/negative controls.
This repair still needs a new live run; the failed run remains failed.

The next candidate, `2907e36`, completed all 13 regression attempts: 11 model
cards and two rejected/empty outcomes, with no missing or transport failures.
The device-value claim was corrected. Independent review found that a connection
requirement was incorrectly treated as a dataset-absence claim; an optional
wrong-object stand-in escaped validation in generated-data mode; and markup was
explicitly labelled as margin. Follow-up controls distinguish connection
requirements from absence, check stand-ins against the original task in either
data mode, and reject the observed margin/markup arithmetic contradiction.
Prompts request the simplest useful comparison and avoid unnecessary percentage
metrics. The bounded metadata rule still rejects unproven catalogue absence.
These repairs and all later live results require separate qualification.

Candidate `7f0e5e4` again completed all 13 attempts, with 11 visible model cards,
two rejected/empty results and no missing or transport failures. Its permit
stand-in for room bookings was rightly rejected, while honest uninspected-source
wording was wrongly rejected. A shown kiln card asserted unproven source absence;
another replaced version tracking with recall prioritisation. The follow-up
requires the source-mode relationship in the structured schema, distinguishes
bounded inspection limitations from absence claims, and places the attendee's
goal after source evidence so the requested action governs selection. Editable
workflow state needs an app. Local controls do not establish live quality; the
failed regression and full denominator remain retained outside Git.

Candidate `076cb43` stopped at its three-case gate: two cards were visible, one
diagnosis card was correctly rejected for an invented identifier, and ten cases
were unattempted. Independent review also found a shown discount comparison
missing its category dependency. Repeated Mini failures motivate qualification
of GPT Sol 6.1 as the preferred wizard service, with low reasoning and the same
12-second deadline. Explicit operator pins still win, and smaller available
services remain catalogue fallbacks. Required source declarations must include
the fields and joins needed for the primary comparison; visible copy uses human
field names. This model change requires fresh live quality and latency evidence.

The wizard now asks for a useful goal or offers a deliberate choice of ideas,
then opens a ready agent with the saved task. Industry and collaboration style
are optional. There is no model request on mount or typing. Removing Surprise
from this journey avoids an unrelated random choice; its compatibility endpoint
remains available. Disabling the wizard hides onboarding and its edit control,
while preserving completed product context. Agent preferences remain available
from the header when onboarding is disabled.

## What changed

- Curated and generated selections have namespaced IDs, attendee-scoped receipts,
  and immutable snapshots. Save, reload and process restart preserve the offered
  prompt, data declarations and provenance. Original words stay separate from the
  suggestion. Legacy selected-card wording has unverified authorship.
- Revisioned saves serialize the full transaction across threads/processes.
  Explicit clears, new-project identity, drafts, completed goals and skipped
  onboarding have separate semantics. Corrupt/future state is preserved and
  reports a recoverable error instead of being silently replaced.
- One request coordinator rejects obsolete candidates. Relevant edits clear old
  candidates/selection; a selection stays stable while another grid loads. A
  scoped 24-hour browser draft preserves unfinished words and context. Save
  conflict, load, suggestion, launch and delivery failures retain the useful goal.
- Goal and context controls pause while a save is pending. This prevents an
  attendee's later edits from appearing beside an older task queued for launch;
  failed saves restore the controls with the draft intact.
- Native named dialogs contain focus, restore it on close, and lock background
  scrolling. The header also fits narrow screens. Installing agents cannot launch.
- Prompt delivery accepts up to 12,000 characters, verifies the acknowledged
  digest and does not silently truncate. Delivery IDs make completed retries
  idempotent. Partial PTY writes are completed; interrupted delivery blocks replay
  and offers the agent view so the attendee can clear input and use a new session.
- Guided, concise, technical and adaptive preferences are versioned independently
  of the goal. Explicit choice has attendee provenance. A seeded business persona
  establishes neither expertise nor a preference. Profile changes refresh local
  home instructions without replacing product context.
- Discovery is an optional projection of a completed goal. Partial agent updates
  preserve omitted fields; wizard updates preserve agent enrichment. Enabling
  capture and resaving unchanged content can create the previously absent record.
  A discovery identifier is returned only when capture actually stored a record.

## Recommendation repairs

The selector ranks a stated task before shape variety and does not fill an app
request with other build types. Generated ideas declare fit, a small first
version, assumptions, material unknowns, data mode, tables and required columns.
Malformed output, duplicate IDs/labels, contradictory declarations and explicit
app-shape mismatches are rejected with separate offered/accepted/rejected counts.
Model errors show a failure notice and any task-matched curated alternatives;
unmatched goals remain usable without a card. Only an explicit schema-support rejection
permits a schema-less retry of the same selected service. A 12-second caller
deadline and bounded singleflight work cover discovery, generation and validation.
Generic page/tool wording leaves the output format open; an explicit refusal of
an app is respected rather than treating the word "app" as a positive request.

The [first live sanity check](remediation-validation.md#r03)
failed: truncated output, invented dependencies, inferred-industry fallback that
changed the task, and a disguised app dependency. Follow-up generation supplies
actual verified columns, prefers one strong idea and requests low reasoning for
GPT-OSS within a larger output budget. Rejected ideas cannot establish an inferred
industry or steer fallback. Curated task matching can find prepared data across
sectors when no sector was stated; generic format words cannot justify unrelated
padding. These repairs require a fresh immutable package and live retest. Metadata
still does not prove SELECT, joins or task fit; lexical ranking is not a semantic
acceptance verdict.

The [second live check](remediation-validation.md#r03) was
faster and preserved task-matched fallback, but still failed on visible data
claims. Follow-up prompting requires sample/source/date limitations in visible
card wording, and prepared cards display a demo-data note. Comparing Sol exposed
an actual provider rejection of the forced sampling value; requests now use
provider defaults. Sol's low-reasoning setting is supported by the gateway. The
same-package runtime override, failed full requests, evaluator receipt corrections
and successful operator diagnostics are preserved separately. Diagnostics do not
establish app-SP recommendation quality or latency. The next 60 unexecuted requests
remain frozen while these regressions are resolved.

The [third live check](remediation-validation.md#r03) qualified
the full Sol wire fix but found a roughly ten-second read cutoff and empty
fallbacks. The unchanged GPT-OSS default was fast and both judges scored 4/5,
yet first-card device fit, 40% fallback and ambiguous live/today wording still
prevent acceptance. The next candidate requests one compact card without unrelated
examples, preserves unused connect time inside the existing total deadline,
weights explicit device needs in curated ranking and records bounded rejection
reasons. Those changes need a fresh package and live qualification; the larger
held-out set has not been executed or tuned from.

The [fourth live check](remediation-validation.md#r03) found
that the simulation's declared model policy excluded the first three services in
the existing chain. Its GPT-OSS last resort remained unqualified. A fresh
same-package simulation admitting the preferred Mini returned five useful cards;
both native judges scored 5/5, with independent concerns retained. One card still
fell back because its handoff exceeded 500 characters. The next candidate allows
1,000-character handoffs without truncation and supplies the complete JSON
contract only for explicitly unsupported structured-output services. An exact
observed Mini provider alias repairs qualification without accepting substitutes.
These are operator API results; the unseen60 and attendee app journeys remain
pending. Model-policy changes are recorded separately from code improvements.

The [fifth live check](remediation-validation.md#r03) passed
release/model qualification but returned an empty bakery card because the model
did not duplicate every declared table name in its handoff prose. WT now binds
validated source declarations into the offered handoff before its receipt/digest
is created, with read-only and metadata limitations. Unknown/undeclared sources,
unverified columns and incompatible data modes still fail. Local roundtrip checks
preserve the exact bound text through save/reload/launch; a fresh package still
needs live qualification. The frozen dataset remains unexecuted.

The [sixth live check](remediation-validation.md#r03) returned
five nonempty Mini cards with zero fallback in 3.520–4.861 seconds. Sol accepted
five and Opus four; the bakery card's visible today wording and undeclared source
status remain a retained disagreement. Code review separately reproduced a custom
industry/schema coupling. The follow-up preserves custom industries when adapting
prepared samples, binds a demo day for prepared-source today wording and includes
verified column declarations in the immutable handoff. New workflow state needs
an explicit owned-data default. Local checks pass; a new package and live retest
are required before the untouched frozen evaluation. Original attendee words and
all native reviewer verdicts remain preserved.

The [seventh live check](remediation-validation.md#r03)
returned five nonempty cards with zero fallback, but both judges rejected the
stock task's replacement with bank-account reporting. Opus also rejected an
undeclared source column in bakery-card prose. Local reproduction found that
prompt rendering discarded task-based table order by regrouping sectors, and
column validation covered declarations without checking literal identifiers in
the card text. The follow-up preserves source ranking and rejects undeclared
literal source fields throughout the card. Its prompt requires the business
object to stay intact and ambiguous interpretations to be proposed defaults.
Seven defect reproductions fail before the fix; 177 focused tests pass afterwards.
Native failures and disagreement stay in the evidence. Fresh live qualification
and the frozen evaluation remain required.

The [frozen 60-request evaluation](remediation-validation.md#r03)
completed all 180 scheduled generations on `6d7c946`. Nine were empty and 17 fell
back (9.44%, above the 5% gate). p95 was 4.945 seconds. The independent scoring run
timed out; only 18 of 1080 expected verdicts are available. The full denominator,
recorded errors and missing assessments are retained. No acceptance or attendee
app outcome is inferred from the successful dry run.

Local reproduction confirmed that the validator wrongly treated a known industry
chip as a source-schema restriction. The follow-up permits task-fitting verified
sources across industries while preserving confirmed attendee context. Named
tables, declared columns, single-schema dependencies and data-mode checks remain.
Task-ranked prompt inventory also considers other sectors for a stated goal.
Curated matching ignores generic workshop words and preserves an explicitly
requested summary rather than substituting data cleaning. A separate cold-read
reproduction found that valid metadata taking 350ms was rejected despite unused
request time. Generated dependency checks may now use up to one second of the
existing shared deadline. That local finding does not establish the reason for
the two live unverified-column rejections. The consumed corpus is regression
evidence; new live qualification and a new frozen set remain required.

The [targeted eight-case regression](remediation-validation.md#r03)
on `0fd1e2f` returned eight nonempty generated
cards, zero fallback and eight verified native HTTP traces in 3.507–5.721 seconds.
The hospital task used healthcare data despite the car-sales room context, and
the readings task proposed a useful summary. Independent review still rejected
acceptance: the first stock card claimed there was no prepared catalog despite
the deployment's configured, qualified catalog, and visible explanations were
cut mid-sentence. Bakery pickup-time suitability also remains unverified.
Serialized native scoring completed all 16 cells without infrastructure errors:
Opus accepted seven and Sol eight. Their disagreement on the broad automotive
team task is retained. Both accepted the false catalog-absence claim because the
fixed inputs lacked independent catalog facts; future evaluation must include
that evidence. Those verdicts do not override primary review.

Seven local reproductions confirmed that cold/unavailable inventory was rendered
as catalog absence and oversized card fields were silently shortened. The
follow-up describes configured but unverified metadata honestly, bounds visible
copy in the JSON schema and rejects oversized schema-free replies rather than
cutting their task or limitations. New source attributes must be explicit proposed
working defaults or remain unresolved. All 181 focused wizard tests pass. These
repairs require a fresh immutable package and live retest; neither these eight
consumed requests nor the original 180 generations establish acceptance.

The [8a3e617 regression](remediation-validation.md#r03)
also failed. Eight consumed requests were scheduled, but the first-three gate
stopped after two nonempty results and one empty bakery fallback; five remain
missing. Primary review found an unfinished stock explanation at exactly the
240-character string cap. The bakery runtime rejected an undeclared table
reference; the raw rejected card was not captured, so its exact reference is
unknown. Three native roots and HTTP tool spans independently match the retained
inputs and outputs. No model judge scored these actual outputs.

The next repair asks for shorter complete copy without provider string caps,
retains local size validation and rejects incomplete explanations at the cap.
Deliberate suggestions allow a bounded one-second cold inventory read inside the
existing deadline. Generated-sample cards cannot name undeclared source tables.
Both local reproductions failed before repair; all 183 focused wizard tests pass
after it. Separate synthetic judge controls completed 18 native boolean cells,
but only 12 matched frozen expectations: the rubric crossed dimension boundaries
and one task-feasibility expectation was too permissive. Versioned replacement
controls must pass before fresh acceptance scoring; earlier failures remain intact.

The [0077edc regression](remediation-validation.md#r03)
returned eight nonempty responses with one selector fallback. Qualified dimension
judges completed all 48 native cells: 42 true, six false, zero errors or missing.
Both rejected the bakery's current-date sample assumption and the hospital card's
unsupported staffing-data absence claim. Two data-truth disagreements remain,
and primary review rejects the unspecified team's unlabelled vehicle-service
interpretation despite both task-fit judges accepting it. All native verdicts
and the separate adjudication are preserved.

The follow-up preserves attributed attendee words in build prompts, binds daily
previews to dates actually found in inspected rows, rejects unqualified literal
current-period assumptions and retains useful warnings. Generated-sample offers
always carry exploration-before-invention instructions. Prompt guidance requires
any proposed domain to be visible and missing-source facts to remain unverified.
All 187 focused wizard checks pass. Earlier full local runs passed 2,860 and 2,861
checks; their scope and the final warning refinement are recorded separately.
Exact final-commit CI and new live qualification remain required.

[Evaluator controls](remediation-validation.md#r03)
now pass a prefrozen combined 36-cell gate using 30 exact retained v2 cells and
six new v3 cells. V1 and v2 failures remain unchanged. This qualifies the frozen
dimension scorers; it does not establish recommendation or app acceptance.

Read-only Labs metadata inspection found actual curated catalog errors. The
repairs cover 47 table-dependent cards with declared columns and consistent table
references, including these task corrections:

| Card | Correction |
|---|---|
| Retail store app | Include order items for sales totals; use/disclose the seeded period. |
| Customer service drift | Filter by available state; remove the absent dealer filter. |
| Recall progress | Add current-owner state data and explain that location's meaning. |
| Manufacturing defects dashboard | Use plant, machine and shift; remove absent suppliers. |
| Manufacturing prediction | Predict work-order defect risk; defects are not machine-failure labels. |
| Media churn | Include earlier viewing events and exclude later/cancellation predictors. |
| Outage dashboard | Summarise regional context separately to avoid multiplying outage totals. |
| Telematics pipeline | Use the prepared table instead of assuming an unverified raw-file volume. |
| Web funnel | Use session events; remove unsupported reconciliation to sales. |

Curated checks share a metadata budget and warm only relevant dependencies in
bounded background work. The badge says **Prepared tables available**. Metadata
checks establish named tables/columns under the WT app identity; they do not prove
SELECT access, join semantics, SQL/serverless resources, or generated-app grants.
The agent still checks those under the execution identity. Synthetic/sample data
and proposed assumptions remain explicit.

## Verification and remaining acceptance

The [a1e182b regression](remediation-validation.md#r03)
completed eight nonempty outputs with zero fallback, eight native roots and HTTP
spans, and no missing generations. Primary review still rejected its bakery date
assumption, astronomy source-absence claim and unproposed vehicle-service domain.
No model judges were run for this rejected candidate; the untouched new 60-request
set remains unexecuted. The follow-up binds proposal status into visible cards and
saved handoffs, applies the demo-day correction to assumptions, and rejects clear
source-absence assertions while retaining exploration warnings, unverified-source
statements and prepared-table write restrictions. Six reproductions failed before
repair, and 210 focused wizard tests pass after the final refinement. Raw attendee and
build-prompt wording stays intact. The new commit/package needs CI and live
qualification before it can supply acceptance evidence.

The [356bd0e regression](remediation-validation.md#r03)
qualified the exact CI package and completed all eight consumed requests with
nonempty HTTP 200 responses. One selector fallback remains. Independent native
verification found eight roots and eight HTTP spans across two FINISHED runs.
Primary review of every retained card and bound handoff found the previously
reproduced sample-date, source-absence and unproposed-domain defects corrected.
The maximum response was 11.595 seconds; this small consumed set establishes
neither the held-out latency/fallback gates nor R03 acceptance. No model judges
scored these outputs, and the new frozen 60-input set remains unexecuted.

A real synthetic-attendee Claude journey on the same candidate preserved the
plain bakery goal, explored prepared retail data, asked two material questions,
and built the requested late/unpacked prioritization and Mark packed action from
the answers. Claude Code 2.1.295 visibly used Opus 5.5. The generated app reached
RUNNING with shared Lakebase state. Its native prose, source, ownership and
cleanup records are retained in the
[attendee bakery evidence](remediation-validation.md#r03).
Operator API checks verified packing persistence and restored the sample record.
Attendee browser consent was pending when the disposable resources were removed,
so rendered, responsive, keyboard and attendee reload acceptance remain missing.
The agent explicitly reported missing browser tooling. Tool/worker attribution,
implementation timing and transcript authenticity remain unqualified by the
read-only observer. This older candidate does not accept the current package.

The user approved a maximum **$250 total** for a new four-hour disposable
acceptance run. Existing receipts keep their original expiries. The
[4442597 acceptance attempt](remediation-validation.md#r03)
qualified the exact CI package and a 24-cell native judge transport dry run, then
failed its recommendation generation on a ReadTimeout. Of 180 scheduled outputs,
143 were attempted: 129 nonempty model cards and 14 empty selector responses;
37 remain missing. All 1,080 recommendation reviewer cells remain unexecuted and
missing. No reviewer scored this incomplete run. Native stopped roots, attempted
HTTP responses and reviewer outcomes remain distinct in the retained ledgers.

Independent review inspected every retained output. Declared source columns
matched the catalog snapshot, but cards still substituted mobile plans for a
streaming offer or citizen requests for meeting-room requests, overlooked prepared
metadata candidates, and offered filters missing from their declared source plan.
The complete review distinguishes those defects from unverified row/access
suitability, reasonable proposed defaults and optional feature ambiguities.

Local repairs correct app/website source metrics being mistaken for app output
requests, reserve room for the server's proposal prefix, enumerate structured data
modes and truthfully badge unverified data. Inventory ranking now puts direct
source-name hints and relevant curated dependencies ahead of unrelated curated
examples; omitted table names remain visible without implying verified columns or
task fit. Prompt guidance preserves the requested business object, binds every
promised control to source fields or explicit working defaults, and avoids adding
unrequested date scope. Four source-displacement reproductions exercise a full
24-table budget; all 222 focused wizard checks pass. These repairs require a fresh
exact-commit package and live tests; they do not establish that the reviewed
semantic defects are resolved.

Receipt-owned cleanup verified deletion of the disposable app, source, catalog
and group and removal of only its shared access deltas before the original expiry,
with zero CT calls. Conservative retained model consumption/reservations are
$47.8338787583888 for dry judging and $8.3610644424384 for generation, plus the
existing infrastructure bound. Those amounts are not invoice costs. Further test
allocations must carry them forward under the same $250 total cap. The failed
corpus is consumed regression evidence, and neither budget approval nor the
qualified judge transport establishes recommendation or attendee acceptance.

The [bb74124 consumed regression](remediation-validation.md#r03)
qualified the exact seven-check CI package and completed all 13 scheduled requests
with no missing, transport or native-trace errors. Ten cards were shown and three
were empty/rejected. Independent review found website visits replaced by viewing
sessions, streaming pricing replaced by mobile allowances, a false derived-field
rejection with a separate margin/markup error, an unsupported source-absence
assertion, and invented optional facility fields. Ward exists; city, address and
status do not belong to the inspected facilities table. Raw rejected cards and
all native records, spans, validation codes and review limits remain visible.

The follow-up includes actual source comments from the existing metadata read,
improves plural/domain hints within the unchanged column budget, and recognizes
explicit arithmetic definitions over declared fields without executing them.
Unknown source identifiers and unsupported absence claims still fail. Five local
reproductions failed before repair and 237 focused wizard checks pass after it.
The broader local run passed 2,899 and skipped 35; eight environment failures pass
in an isolated recheck with identical changed source bytes, committed public locks
and loopback access. Exact-commit CI subsequently passed all seven checks on
`332d03a`. The guard
settled $0.1652814750048; individual provider usage envelopes were not retained,
so independent token recount is unavailable. Verified receipt-owned cleanup has
zero CT calls. Conservative carried consumption and new infrastructure/canary
reserves total $77.3602246758320 under the $250 cap, not an invoice. No acceptance
judges or attendee app builds ran in this phase; PR92 remains unaccepted.

The [332d03a consumed regression](remediation-validation.md#r03)
independently verified all 471 committed runtime files and modes, then returned
13/13 nonempty model cards in 54.508 seconds with no missing or transport errors.
The earlier five affected outcomes now preserve their tasks and source semantics;
the price card uses the correct selling-price denominator for margin. Independent
native database reconciliation verified all records, runs and root/HTTP spans.
Known numeric provider usage is retained and independently reconciles each of the
13 settlements and the $0.1960988697360 conservative generation total.

The review still found a booking request replaced by citizen-service/SLA controls,
uninspected genre values called verified, and unnecessary parts quality/risk
analytics added to a version-lookup example. The follow-up strengthens task/source
guidance, rejects explicit cross-object stand-ins and unsupported verified-row
claims, and distinguishes metadata analysis examples from attendee requirements.
Same-object samples, aliases, warnings and future inspection remain usable.
Six harmful examples failed before their respective repairs; 260 focused checks
now pass. Offline validation of all retained raw cards rejects the two source/copy
problem cards and preserves the other 11. This is no new generation or live proof.
Fresh CI and another relevant live regression are required before acceptance.

Cleanup verified all 11 receipt-owned operations before the unchanged expiry.
Zero CT calls or changes occurred. The $32 allocation was released after cleanup,
retaining model charges and $11 infrastructure/canary reserves. Conservative
carried consumption/reservations now total $88.5563235455680 of the approved $250,
leaving $161.4436764544320; these are not invoice costs. No acceptance judges,
harness builds or attendee browser journey ran in this phase. PR92 stays unmerged.

The [9822afc consumed regression](remediation-validation.md#r03)
executed all 13 requests once in 39.283 seconds: 11 visible cards, two empty/rejected
results, and zero transport/native errors or missing outputs. Independent review
found that booking requests and version lookup now preserve their task, but the
streaming card again uses mobile data/minute allowances and loses the selected
fun intent. A valid equipment age calculation is rejected because the prior
calculation grammar excludes `floor` and a calendar anchor. The genre card's
unsupported claim of verified row values is correctly blocked; that empty outcome
still counts. All 49 declared fields match the independent metadata snapshot.

The follow-up binds selected intent independently of model synonyms, restricts
structured intent tags to known choices, and rejects the observed streaming/mobile
allowance contradiction using original task context. This bounded check is not
general semantic verification or an industry-source restriction. The calculation
validator recognizes source-bound date differences and pure rounding operations
without executing expressions, preserves their syntax, and keeps unknown operands
and arbitrary calls invalid. Three reproductions failed before repair; all 274
focused checks now pass, including negative controls. Offline validation of all
13 actual raw cards accepts the equipment card, rejects the streaming and unsupported
row-claim cards, and preserves the remaining ten. It makes zero network/model calls
and supplies no new live acceptance evidence.

Cleanup independently verified all 11 owned operations before original expiry
`2026-10-09T23:29:28Z`; a sandboxed authentication failure before any operation is
preserved separately. The watchdog subsequently verified the early cleanup. Zero
CT requests or changes occurred. Numeric provider usage reconciles the conservative
$0.1964474823696 generation settlement. After releasing the unused $32 allocation
and retaining $11 infrastructure/canary reserves, cumulative consumption/reservations
are $99.7527710279376 of the approved $250, leaving $150.2472289720624. These are not
invoice costs. Exact CI/package qualification, a fresh relevant regression, held-out
evaluation and genuine attendee app journeys remain required. PR92 stays unmerged.

The local browser fixture serves the committed React/static WT, uses real brief
storage, and simulates API/session faults. It does not replace the wizard with a
handwritten test UI. It covers 390/768/1440px, keyboard focus, exact selected-task
delivery, obsolete responses, save conflicts, launch/delivery recovery, partial
delivery replay prevention, malformed drafts, new-project reload, and preferences
with the wizard disabled. CI runs these cases and builds the Linux CP311 package.

Dated logs, screenshots and the prepared schema fixture's provenance are retained
under [local evidence](remediation-validation.md#r03). Passing local
checks is not an R03 acceptance verdict. Required remaining evidence includes the
held-out model relevance/feasibility evaluation and actual wizard-to-harness-to-app
journeys on a fresh WT-owned CT-compatible Labs deployment, with genuine attendee
authentication. Claude uses Opus 5.5; Codex uses GPT Sol 6.1. The final assembled
CT integration remains separate.

Warm-install, remote-first, project/worker preference delivery and migration remain
R04/R08 gates. A 60-instance arrival/build rehearsal belongs to R10. Keep the event
wizard disabled until its live outcome gates pass.

The current `54c73ba` consumed regression qualified all 471 CI runtime files and
completed 13 requests once with 12 visible cards, one empty result, no missing or
transport errors and 107.299 seconds of native evaluation. The equipment-age
card's valid plain-language calendar anchor was falsely treated as a source
column. The bounded follow-up exempts the standard `current_date` intrinsic while
retaining unknown-identifier checks. The exact retained card passes offline
validation against its declared metadata; 290 focused wizard checks pass. This
repair still requires fresh exact-package held-out and actual harness acceptance.
The held-out scope preserves all 60 unique inputs, once each, and all 360 unchanged
judge cells. The original additional 120 repetitions/720 cells remain unexecuted;
no repeat-stability claim is made.

The `d811500` held-out run returned HTTP 200 for all 60 inputs in 483.399 seconds.
Independent native-trace and numeric-usage reconciliation verified every result
and the closed, drained generation allowance. One valid path-frequency dashboard
was rejected because `website paths` forced an app output; the bounded intent
parser now recognises paths as source metrics while preserving explicit app
requests and refusals. Three reproductions failed before repair; 293 focused
wizard checks pass afterwards, with 12 environment-dependent checks skipped.
The original 59/60 result remains retained. One clinician-experience suggestion
also missed suitable prepared provider data, but preserves conditional discovery
before sample generation; this remains a source-ranking improvement opportunity.
The unchanged six reviewers will score all 60 actual outcomes, including the empty
result. This code repair alone does not establish live acceptance.
