# R02: workshop interaction and quality contract

Implemented in the working tree on 9 October 2026. No WT release deployment,
Control Tower changes, or wizard re-enablement were performed. R01's recorded
baseline and verdict are unchanged.

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

## Limits and remaining qualification

These are five synthetic scenarios per model, not a statistically meaningful
quality rate. Earlier iterations show that instruction following is not
deterministic; a passing final sanity subset does not establish repeatability.
Direct model probes do not qualify native Claude/Codex/Omnigent
execution, delegation behavior, model fleet consistency, generated-app UX, or
persistence. Policy delivery tests prove composition and propagation, not that
every third-party CLI will obey every instruction. R01's date/mobile findings
are not fixed by this policy change.

Existing warm installations and already seeded project adoption still need R04's
versioned reconciliation. R03 still owns wizard flow, card/typed-goal integrity,
idea relevance, persistence, and request coordination. R05/R06 own starter quality
and practical browser/repair infrastructure. Run a fresh CT-compatible isolated
WT-to-app journey and then CT integration when qualifying the resulting release.
This report does not claim that either has been rerun for R02.

To repeat the limited probe with an explicitly configured tracking store:

```bash
uv run --with mlflow==3.13.0 --with openai==2.40.0 \
  python scripts/evaluate_workshop_contract.py \
  --profile labs --tracking-uri "$MLFLOW_TRACKING_URI" \
  --output /tmp/wt-r02-sanity.json
```

Use `--model databricks-gpt-5-6-terra` for the second route. MLflow/OpenAI are
external evaluator dependencies and are not added to the attendee runtime.
