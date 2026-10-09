# R02: workshop interaction and quality contract

R02's interaction-contract implementation in PR #89 was accepted by the project
owner on 9 October, with the remaining quality findings carried into subsequent
workstreams. Policy delivery tests and bounded model probes pass. Genuine
labuser Browser tests qualified Codex question/reply delivery and a clear static
page, plus Claude's consequential bakery question, recommendation, deployed app,
packing action and reload. The fresh corrected package also passed first-preview
phone status/actions without horizontal scrolling. It explored shared retail
data but missed the suitable working-catalog bakery fixture, and the initial
page omitted sample/reset disclosure. A simple user request fixed the disclosure;
the original failures remain recorded. Working-data context belongs to R04,
truthful starter defaults to R05, and observed completion checks to R06. This
workstream closure does not mark every generated-app criterion or the release
qualified. CT was unchanged and the wizard remains disabled. R01 is unchanged.

## What changed

One fork-owned source, `assets/instructions/workshop_contract.md`, is inlined
into Claude home instructions, Codex home instructions, and the project template
committed for worktree workers. Its interaction/readiness rules take precedence
over coach, wizard, and upstream skill workflow advice; platform/API and identity
rules remain in force. Missing or duplicate insertion slots fail composition.

Clear requests go straight to building. An undefined business rule such as
“which orders need attention?” gets a short consequential question. Usually one
exchange containing one or two questions suffices. Agents give a visible useful
recommendation with a reason, state material demo assumptions, then start. There
is no routine scope approval, technology quiz, or experience questionnaire.
Explicit requests for more or less explanation adapt the help, without lowering
the functional or visual quality floor.

Coach, wizard handoff, and design-studio prose now follow this contract. Typed
and selected-card starters use the same framing and preserve their task text.
Known wizard facts are reused; they no longer forbid clarifying missing facts.
AppKit remains the app baseline. A read-only analytics request can be recommended
as managed AI/BI; design silence concerns visual choices, not useful product advice.

The project helper seeds a five-bullet versioned brief in `README.md`. The agent
maintains facts, first version, assumptions, and observed/unverified checks from
the conversation. Helper reruns preserve it and attendee notes; commits propagate
it and the policy into a real Git worktree. These fields are not another form.
This is an instruction to maintain continuity, not runtime enforcement that an
arbitrary third-party CLI has filled every field.

Deployment is a first preview. Before claiming the useful task works, agents
use available prepared tooling to inspect rendering, primary actions, dates,
reload when saving is promised, and reachable controls at a narrow width. Cold
browser installation and production acceptance suites are not workshop gates.
If a check cannot run, the preview carries the exact limitation. Stronger
starters, browser infrastructure, bounded repair, and full release qualification
remain later remediation work.

The policy lives outside the refreshed upstream skill tree; the design adapter
remains protected by the existing `FORK_ONLY` allowlist. Upstream skills and their
reviewed digest were not edited. Release packaging now requires the shared
policy asset explicitly, so a missing asset fails the inventory check.

PR review found stale README advice in both the project and home instructions
that reduced it to a purpose line and URL. Both adapters now explicitly retain
the versioned continuity brief and attendee-authored notes; the helper comment
was aligned too. A policy-delivery regression reproduced the contradictory
advice before the correction and covers every composed instruction channel.
The original direct-model evidence is preserved; that probe preceded this
README wording correction, which leaves the shared contract unchanged.

## Validation and evidence

The [R02 evidence directory](evidence/workshop-contract-r02/) contains test logs,
native MLflow summaries/traces, prompt snapshots, intermediate probe results,
and a hash manifest. The final combined local suite covers 340 tests, with one
existing skipped test, plus nine passing release-packaging tests. Final focused
checks were rerun after the consistency edits. It exercises actual per-user provisioning with coach and
discovery independently enabled/disabled, wizard context present/absent, all
instruction channels, committed project-only delivery, brief preservation,
skill refresh, persona changes, discovery, and the existing remote-session probes.

`scripts/evaluate_workshop_contract.py` uses native MLflow datasets, registered
`make_judge` scorers, tracing, and `mlflow.genai.evaluate`. It discovers existing
resources and reuses the dataset/scorer. A traced prediction and stored spans are
read back before evaluation, then three scenarios run before the remaining two.
Tracking is local SQLite; Labs provides model inference under the operator
profile. No app or resource creation tool is exposed to the tested model.

The scenarios are the ambiguous bakery opening, a complete hello-world request,
known wizard facts, a guided project worker, and an experienced project worker.
Claude Sonnet 4.6 uses Chat Completions; GPT 5.6 Terra uses Responses, the supported
wire for its function tools. The only implementation tool records build intent.
The bakery simulator discloses only facts requested in the observed questions.

Both models met the five pre-build criteria with the final policy and corrected
scorer (10/10 scored cases). The two routes used identical home/project prompt
hashes. Model-response time was 2.1–9.2 seconds per scored scenario, excluding
human reading/reply time and any build. Each route exported six prediction traces
with thirteen spans, including the separate trace verification call. Five traces
per route carry the native scored assessments.

Initial probes exposed recommendations hidden in tool arguments, a session-only
storage assumption, and Claude still guessing an undefined business rule. These
results are retained in `iteration-01/` through `iteration-04/`. The policy now requires
visible framing before the first implementation tool call, defines remembered
changes as surviving reload, and explicitly treats undefined attention rules as
ambiguous. A subsequent consistency probe caught a contradictory Lakebase/browser
storage description and missed requested first-action guidance. The final contract
requires one actual storage mode, shared storage for shared staff records, and a
plain-language first action when requested. The first judge also sometimes
demanded actual code from a pre-build
stub probe; its corrected native scorer evaluates only interaction and build
intent. Scores from those different scorer/policy versions are not a quantitative
before/after comparison.

## Isolated Labs native journeys

The [live closeout](evidence/workshop-contract-r02/live-20261009-e569/CLOSEOUT.md)
preserves all three attempts, exact package/observer identities, native MLflow
traces, the failed generated source, cleanup receipts, and CT readbacks. The
runtime used R02 instruction commit `28c80f1`, independently compared across all
444 runtime files to its CI package. External observer refreshes retained the
PEX bytes and original four-hour expiry. They do not imply that later evaluator
commits were built into that immutable PEX.

The attendee was genuinely signed in as `labuser+1@awsbricks.com`. Native Claude
`2.1.283` selected Sonnet 5; this is a different route from the direct Sonnet 4.6
and GPT 5.6 Terra probes above. Wizard and wizard LLM were disabled; these runs
do not qualify onboarding or other harnesses. Every run used the same bakery
opening and disclosed only facts requested by an observed question.

| Attempt | Observed result | Interpretation |
| --- | --- | --- |
| 1, 695.3 seconds | One consequential question/reply; collector stopped with `native_message_size_budget`; no app observed | Compaction handling reproduced the collector failure; the original flag was not retained at observer refresh. Marked compaction summaries are now excluded; real oversized user replies still fail. Generated UX unverified. |
| 2, 594.4 seconds | Short attention question and visible prioritization advice; actual reply; generated AppKit deployment failed for missing catalog access; collector then stopped on a task notification | CT's contained workspace grant covers future app SPs. The Labs simulation omitted that equivalence. Exact generated-SP read grants now pass direct/effective canary readbacks. Native task notifications are excluded from attendee replies. Recovery/final UX unverified because the collector interrupted it. |
| 3, 688.5 seconds | Terminal showed a question set; primary native log exported only the opening; evaluator stopped the exact stalled session | The simulator never received the question and froze replies at its original deadline. Separate metadata inspection found no assistant records in that primary log. Native persistence/projection qualification remains unresolved; this is not an agent-quality verdict. |

Native MLflow scored only the correlated clarification criterion in attempts 1
and 2, at 1.0 each. That does not score complete consultation, assumptions,
implementation ordering, UX or persistence. Attempt 3 was not scored as a
policy failure from missing observer text. The legacy R01 scope-agreement result
is retained in raw journeys but is not an R02 requirement.

The final focused evaluator suite passed 287 checks, including the prepared
browser/pyte checks. CI passed on `53ec03c`. Generated app/source/backend cleanup
and WT teardown were independently verified; the attendee's existing projects
directory was preserved. The CT app stayed on deployment
`01f1bbc1de2911cb93356caa765f77a3`, and all seven reviewed CT source hashes matched.
Authentication state was excluded from evidence and removed after teardown.

## Data-first and first-preview follow-up

The native run exposed two concrete defects. `workshop-init-project --appkit`
mixed scaffold progress with its stdout path, so the documented command
substitution passed that prose to `cd`. The helper now sends progress to stderr
and emits only its final directory on stdout; the regression runs the actual
documented `cd` expression with noisy and warning-only scaffold outputs.

Prepared-data inventory used `schema_name_pattern="*"`, although the API accepts
SQL LIKE patterns. A read-only Labs comparison returned zero tables with `*`
and 110 without that filter. The lookup now lists all schemas, and the observed
manifest is included in committed project instructions as well as home
instructions. The workshop contract requires bounded exploration of supplied
data, prepared demo data and relevant attendee data before inventing samples;
reuse suitable sources, keep shared sources read-only and explain a specific
fallback when none fits. Static pages skip discovery.

The isolated CT-compatible runner now optionally declares the existing demo
catalog and grants CT-equivalent read access only to exact temporary WT and
generated-app SPs. Shared identity/effective access and exact privilege removal
are verified; CT code, configuration and permanent groups are untouched. The
earlier cell had an empty demo-catalog setting and therefore cannot qualify
prepared-data parity.

The phone requirement now explicitly keeps status and the primary action
visible without horizontal scrolling, using stacked rows/cards where needed.
Sample data and any reset-on-restart limitation must be visible on the page.
The original bakery held shared state in server memory: reload preserved it,
but its repair redeploy reset it. A successful repair does not turn the original
first preview into a pass. Native Codex UI question transport passed separately;
its collector still reported `native_message_size_budget`, so that collector
is unqualified. Claude's final repair collector was ready and correlated the
actual user messages.

The combined local suite for helper/policy/inventory and deployment/access/
cleanup passed 180 tests. A wizard API test reused an attendee whose brief an
earlier policy test had saved; it now uses its own attendee identity. The fresh
[packaged run and closeout](evidence/workshop-contract-r02/live-20261009-fdae/CLOSEOUT.md)
independently matched all 445 runtime files to commit `4c8b8d7`. A genuine labuser
answered the consequential question in about 47 seconds. Desktop and phone
packing survived reload, with valid relative due dates and no horizontal action
scroll. Shared retail metadata was inspected and rejected for missing bakery
fields, but the existing 20-row bakery fixture was not inspected or reused.
The first page had no sample/reset notice; the ordinary disclosure request
produced a verified repair. Its redeploy reset packed state, consistent with
the disclosed shared server-memory implementation. No restart/fault suite was
run. The collector correlated the exact native question, answer and repair.

## Limits and remaining qualification

These are five synthetic scenarios per model, not a statistically meaningful
quality rate. Earlier iterations show that instruction following is not
deterministic; a passing final sanity subset does not establish repeatability.
Direct model probes do not qualify native Claude/Codex/Omnigent
execution, delegation behavior, model fleet consistency, generated-app UX, or
persistence. Policy delivery tests prove composition and propagation, not that
every third-party CLI will obey every instruction. Those earlier model probes
do not establish a fix for R01's date/mobile findings.

Existing warm installations and already seeded project adoption still need R04's
versioned reconciliation. R03 still owns wizard flow, card/typed-goal integrity,
idea relevance, persistence, and request coordination. R05/R06 own starter quality
and practical browser/repair infrastructure. The subsequent
[direct browser question-delivery probe](evidence/workshop-contract-r02/ui-question-capture-20261009-6b90/README.md)
qualified Claude's plain-text question, inline custom answer and two-question
form with multiple selections on the pinned Linux CLI. Both UI acknowledgements
and collector correlation passed in those controlled cells. The earlier missing
assistant records during a tool-using build remain unexplained. Codex exited at
startup in both controlled probes; the subsequent c6d6 Browser run qualified
its visible question/reply delivery, with its collector still unqualified.
The subsequent [startup and coach-hint repair](evidence/workshop-contract-r02/codex-startup-and-coach-hint-20261009/README.md)
reproduced Codex's long client socket pathname failure in a disposable Linux
App and verified the exact WT short-HOME helper: native daemon start passed in
1.699 seconds, with the original state preserved. The banner now offers a quick
idea choice without claiming CLI readiness, and ended terminals retain their
output with Relaunch. Local Browser checks and 76 focused backend / 107 frontend
tests passed. The subsequent c6d6 native test and fdae corrected-package test
exercised these changes through real CT-compatible WT UI instances. Daemon
startup alone does not qualify model reply delivery or generated UX; the
separately recorded Browser observations provide those bounded checks.
Actual CT integration follows qualification of the assembled remediation release.
R02 is accepted as a completed interaction workstream, with the above failures
retained for follow-up. These tests do not establish full generated-app acceptance
or actual CT integration.

To repeat the limited probe with an explicitly configured tracking store:

```bash
uv run --with mlflow==3.13.0 --with openai==2.40.0 \
  python scripts/evaluate_workshop_contract.py \
  --profile labs --tracking-uri "$MLFLOW_TRACKING_URI" \
  --output /tmp/wt-r02-sanity.json
```

Use `--model databricks-gpt-5-6-terra` for the second route. MLflow/OpenAI are
external evaluator dependencies and are not added to the attendee runtime.
