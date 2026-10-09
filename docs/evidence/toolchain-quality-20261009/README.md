# Latest quality-test toolchain, 9 October 2026

New generated-app tests select `quality-20261009` from
`evals/generated_apps/test-baseline.json`. This is an immutable reviewed snapshot,
not a floating "latest" setting. Historical R01/R02 records keep their original
model, package, skill and harness identities. This branch depends on PR89.

| Component | Reviewed version |
|---|---|
| Claude model | `system.ai.claude-opus-5-5` |
| Codex model | `system.ai.gpt-6-1-sol` |
| Claude Code | `2.1.295` |
| Codex | `0.162.0` |
| Databricks SDK | `0.150.0` |
| Databricks CLI | `1.20.0` |
| Databricks agent skills | `v0.2.28` |
| AppKit / AppKit UI | `0.87.0` |
| Node active LTS | `24.21.0` |
| uv | `0.12.24` |
| Bundled harness Python 3.12 | `3.12.15` |
| MLflow / Databricks Agents / OpenAI evaluator | `3.17.0` / `1.12.0` / `3.23.0` |
| Playwright / aiohttp evaluator | `1.63.0` / `3.14.3` |

The WT Python and frontend direct dependencies also moved to audited current
releases. The committed resolution locks pin their compatible transitive sets.
FastAPI/Uvicorn are `0.142.2`/`0.54.0`; OTEL is the matched
`1.45.0`/`0.66b0` train; PEX is `2.103.3`; WT React/Vite are `19.3.0`/`8.3.2`.

## Evidence and limits

The Labs model catalogue lists both requested services and their native APIs.
Both tiny direct-inference checks passed with operator OAuth. Codex 0.162.0
completed a read-tool round trip through `/ai-gateway/codex/v1` on Sol 6.1;
Claude 2.1.295 completed a Read-tool round trip on Opus 5.5 using WT's normal
settings/apiKeyHelper configuration. Claude's provider response named
`claude-opus-5-5`. The direct Sol response named `gpt-6.1-sol`; only this exact
observed alias was added to the evaluation canary's model-identity check.
The Codex headless event stream does not report the provider model identity;
its success is a configured-route/tool check, not separate model-identity proof.

The first Claude `--bare` attempt returned `400 Invalid Token`, with no tools
or usage. It is retained in `native-qualification.json`; the normal launch then
passed. WT does not launch Claude with `--bare`. This comparison does not establish
a general upstream cause for the bare-mode authentication error.

The actual latest Claude TUI showed `No, exit` selected on the owned-folder trust
screen. Its shape matches the existing bounded driver; exact folder, version,
choices and stable-screen checks still apply. No trust confirmation was sent in
this local probe. Neither native smoke was an authenticated attendee browser run
or qualification of a fresh test app's service principal.

Databricks CLI 1.20.0 created a local AppKit 0.87.0 scaffold using explicit npm;
its server and client builds passed. npm used the managed laptop proxy. Generated
SQL type discovery was skipped because this scaffold has no warehouse resource.
The upstream template has a pnpm-specific npmrc warning and older vendor-pinned
subdependencies. Those template pins remain upstream-owned; this check does not
claim every package in a generated app is independently the latest release.

The refreshed skill tree matches its reviewed tag, commit and content digest.
Two skills were added (`databricks-serverless`, `databricks-unity-gateway`); none
were retired. Fork-owned instructions and design guidance were preserved.
All reviewed binary artifacts matched publisher checksums on re-verification.
Committed lockfiles and artifact URLs use public registries; laptop installs used
Databricks proxies. Global laptop tools and the repository's old `.venv` were not
replaced; validation used disposable environments.

The optional quality group installs separately from the attendee PEX. Actual
MLflow 3.17 APIs and OpenAI 3.23 client construction passed local contract checks;
no dataset registration, judge inference or tracking write was performed.
CI repeats this API check. Synthetic historical MLflow 3.13 records remain supported.

Regression runs exposed two test synchronization issues: the session-crash check
read its telemetry before delivery, and a fake deployment clock patched shared
stdlib time so background threads could advance its deadline. The tests now wait
for the observable exit delivery and scope their clock to the deployment module.
No product timeout or exit behavior changed. Initial failed runs are retained in
the private scratch logs; the final qualified suite is exported below.

## Explicit compatibility holds

- Omnigent remains `0.15.0`. `0.17.0` exists; its wheel was downloaded and verified
  (`ff282b15e40e9575e29555391f5cea75806a477615654a3f1e57c87ecc51fc84`). The WT
  client and paired server have an exact protocol/version contract. The newer
  wheel contains the control-plane import modules, but this does not qualify
  remote-host registration, routing, storage migration or paired-server behavior.
  This hold also retains the separate Omnigent bundle's locked dependencies,
  including SDK 0.136.0 and OpenAI 2.44.0; they are not the WT/evaluator environments.
  Updating the client alone would remove the reviewed matching pair. Qualify a
  disposable new pair before changing this pin; do not update working CT resources.
- Node 24.21.0 is the latest active LTS used here; Node 26.11.1 is the current line.
- The CT package contract stays Linux x86_64/CPython 3.11. Python 3.12.15 is the
  separate harness runtime and local validation interpreter. This is not a CT ABI upgrade.
- Parent constraints retain compatible transitive packages: for example,
  protobuf 6.33.6 and pydantic-core 2.46.5. A newer independent release is not proof
  that its parent supports it. The AppKit scaffold likewise retains its released
  template's dependency pins.
- The Databricks APX source release was audited separately; the npm package named
  `apx` is unrelated. Framework evaluation remains its own remediation workstream.
  Native CLIs bundle their provider SDKs; WT does not declare a separate Anthropic SDK.

## Before each new live run

1. Run `python scripts/audit_test_toolchain.py` with the configured laptop proxies.
   It is read-only; exit 1 reports newer releases, including held Omnigent, and
   exit 2 reports unverified metadata. Review updates and produce a new pinned
   baseline rather than letting an event self-update.
2. Use `"test_baseline": "quality-20261009"` in the CT-compatible package spec.
   The model-policy pool must contain exactly one Claude-capable service (Opus
   5.5) and one Codex-capable service (Sol 6.1). Chat services stay explicitly
   selected by the test's policy. The planner rejects older/fallback harness
   services or mismatched packaged toolchain versions before workspace mutation.
3. Run the existing isolated package deployment/qualification workflow. It must
   prove exact source, permissions, OAuth and canary invocation as that app's SP.
   Qualification rejects a successful canary on a different requested model.
4. Run actual labuser browser journeys on that immutable package and inspect
   the generated app. Local smoke results alone are not E2E acceptance.
5. Assemble reviewed workstream PRs, then perform final CT integration before
   merging/rollout. No CT code, deployment, events, configuration or permanent
   groups were changed for this refresh; no CT API requests were made.

## Final local validation

- Backend on Python 3.12.15 / SDK 0.150.0 with optional startup libraries: **2,753 passed, 2 skipped**, 9 deprecation warnings. The remaining skips require independently installed browser binaries.
- WT frontend on Node 24.21.0: **107 passed**; production build passed.
- AppKit 0.87.0 scaffold: server/client production build passed.
- Artifact publisher checks, skills fallback/provenance and public lock/export checks passed.
- Latest evaluator native API checks passed; tracking/model requests were zero.
- Linux CPython 3.11 PEX reproducibility/offline smoke is a CI check, not claimed as a laptop build.
- No fresh attendee browser app build, matching Omnigent 0.17 pair or final CT-integrated deployment was performed in this toolchain qualification.
