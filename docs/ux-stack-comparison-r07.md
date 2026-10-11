# UX stack comparison (R07)

The workshop owner selected **official APX + Impeccable** on 11 October after
reviewing the four independent builds below. R07 now standardises installation,
framework routing and ordinary project preparation. AppKit stays available for
explicit requests and existing projects. Streamlit requires an explicit request.

| Framework | Design authority |
| --- | --- |
| AppKit | Impeccable |
| AppKit | Taste (`design-taste-frontend`, experimental v2) |
| APX | Impeccable |
| APX | Taste (`design-taste-frontend`, experimental v2) |

The attendee-facing Repair Desk prompt and eight fictional sample repairs are
identical in every cell. Claude Code 2.1.296 uses
`system.ai.claude-opus-5-5` through the existing Labs WT gateway configuration.
Fresh HOME/project directories isolate each build from prior conversations and
from the other design skill. Framework setup differs; app requirements do not.
The selected default is an implementation stack, not a common visual template.

Prompt SHA-256:
`3bf862cbd6b7244e03fe65c9d1c85290dc8eed323496f381ee0408fb92ca3f8a`.
Dataset SHA-256:
`c867de11d91c2e4541875d209bb8a9b9b4b822ba0b22287faceca73658e7c54d`.

The same neutral projection of the AppKit platform guide is used for both
AppKit cells: its references to `impeccable` say "the selected UX skill". No
design rules are added to Taste. APX uses the official release's platform skill.
Both design skills retain their upstream rules. Taste v2 explicitly describes
landing pages, portfolios and redesigns as its scope and excludes dashboards,
data tables and multi-step product UI; this limits what this app trial can
establish about its intended uses.

## Version and runtime findings

- APX: official `databricks-solutions/apx` release `v0.3.8`, commit
  `50073e561a689ae5292f82e69ae0505417d75d3e`.
- Its standalone x86-64 Linux binary requires glibc 2.38/2.39; the tested
  Databricks Apps runtime is Ubuntu 22.04 with glibc 2.35. That binary fails.
- The same release's official `manylinux_2_28_x86_64` wheel successfully runs
  `apx --version` in WT. Wheel SHA-256:
  `46fc3ed450c7ea98392db98fdfb4b23e26ba9fea086b1f5bbbd1e1f1e377ba8a`.
  Installing `apx==0.3.8` by package name did not resolve in the public registry;
  the verified wheel was downloaded directly from the official GitHub release.
- Impeccable: compiled upstream skill `4.5.2`, engine `0.1.14`.
- Taste: upstream commit `717446e07a78d4e6d7918b1fde1a263378eebac9`.

## Review method

Builds run in the reused isolated Labs WT deployment; they are instructed to
compile their application without creating/deploying workspace resources.
Screenshots and initial interaction tests use the generated production frontends
on separate local origins. Every comparison build is also deployed as a distinct
Labs Databricks App with its own source path, runtime and URL. Generated UI code
is unchanged; deployment setup uses public package registries. These explicitly
browser-local demo apps need no custom Unity Catalog, SQL or model scopes. The
platform still requires its default identity/access-read and longer-running
consent for each new app identity. The test
attendee receives CAN_USE on each comparison app. Existing generated apps and
Control Tower resources are untouched. Final release testing must still qualify
the chosen stack's complete attendee deployment flow.

Capture the original supplied dataset at 1440 × 1000 and 390 × 844; comparison
screenshots exclude test fixtures. Exercise search, status filtering, repair details, intake, both status
transitions and reload persistence. Record observed behavior, setup reliability,
generation time, mobile usability and task-specific visual choices. Do not
manually redesign an output for the comparison. One build per cell is an
illustrative trial, not a statistical ranking.

The portable HTML comparison includes actual screenshots and comments side by
side. Source archives, screenshots and transcripts stay outside Git. Neither
Control Tower code nor its working Labs deployment is modified.

Streamlit is allowed only when explicitly requested by the attendee, including
after scaffold/deployment failures. The shared workshop contract and its memory
adapters now state this rule.

## Outcome

All four native builds succeeded using only `system.ai.claude-opus-5-5`. Each
standalone Labs deployment reports `SUCCEEDED` and `RUNNING`.

| Build | Native generation | Local preview workflows | Live attendee browser |
| --- | --- | --- | --- |
| AppKit + Impeccable | 18m11s | Passed | Pending consent |
| AppKit + Taste | 9m00s | Passed | Pending consent |
| APX + Impeccable | 9m12s | Passed | Registration, status changes and reload passed |
| APX + Taste | 9m20s | Passed | Pending consent |

The same Travel toaster / Pat Fixture intake, both status transitions, refresh,
search, status filter, urgent ordering and repair details passed in every local
production preview. Phone intake and transitions were exercised at 390 × 844.
Initial desktop and phone screenshots plus workflow screenshots are embedded
in a portable comparison HTML held outside Git.

The recommendation is APX + Impeccable for the repair-specific visual language
and direct queue actions. AppKit + Impeccable is a credible alternative for
Genie builder continuity. Taste produced usable, calmer interfaces; APX + Taste
uses desktop width well. The design-skill difference was more apparent than
any inevitable framework theme. One run does not prove a speed advantage or
coverage across unrelated app ideas.

AppKit + Impeccable added helper review/documentation overhead; APX + Impeccable
explicitly skipped interactive variant selection and screenshot review. A
workshop default should keep the user context and practical preview check
without imposing layered reviewer loops.

Both APX projects resolved the public PyPI SDK 0.151.0, FastAPI 0.143.0,
Uvicorn 0.54.0 and pydantic-settings 2.15.0. Their ordinary `apx build` wheel and
configuration were deployed unchanged apart from an explicit public index.
Both AppKit projects use scaffolded AppKit/appkit-ui 0.84.0. Deployment sources
exclude credentials and dependency directories.

The four new app identities are `r07-ak-impeccable-1011`, `r07-ak-taste-1011`,
`r07-apx-impeccable-1011` and `r07-apx-taste-1011`. Existing attendee app names
are preserved. Live attendee checks remain pending the requested four-app
consent handoff as labuser+1; local qualification must not be described as live
browser acceptance.

The Streamlit instruction changes pass all 13 existing workshop contract tests;
`git diff --check` passes. The owner selected APX + Impeccable. Repository
routing and installer changes are being validated; a production release has
not yet been published.


## Standardised WT delivery

- New app requests use official APX + Impeccable across Claude, Codex and the
  committed project policy used by Omnigent workers. Existing AppKit projects
  and explicit framework requests retain their implementation.
- Normal bootstrap installs APX 0.3.8 from its checksum-verified official
  manylinux wheel, with Bun 1.3.8 and uv 0.12.24. The CLI has its own isolated
  environment; WT's PEX dependencies are unchanged. Warm reuse verifies the
  installed contents. Readiness reports installed versions, including when
  Agent Bricks is disabled.
- The current official APX skill replaces the stale fork. Upstream refresh and
  warm installs cannot restore its old routes or the retired UX skills. The
  wheel and skill have reviewed source, executable and content digests.
- `workshop-init-project --apx` avoids nested directories, interactive profile
  selection and a mandatory sidebar. It commits the framework skill, sole UX
  skill and workshop memory for project-only workers, preserves notes on
  reruns and reports scaffold failures. Existing non-APX projects are preserved.
- WT registers the official local APX MCP for both harnesses and uses the
  existing app deployment helper with the project's target (`dev` in the
  standard APX scaffold). The helper excludes APX assistant addons that install
  another `@latest` browser MCP and hooks.
- Release acceptance remains a thorough single CT-compatible deployment with
  simple attendee inputs and hands-on preview checks. The existing Labs app
  and compute are reused. No CT code or working CT deployment changes occur.
  Screenshots, generated code, archives and transcripts remain outside Git.

The selected live app opened after the owner completed sign-in. Registration of
Travel toaster / Pat Fixture, Waiting → In progress → Ready for collection,
visible confirmation and reload persistence passed on the actual deployed app.
The new live capture is desktop; the narrow-layout results above remain the
prior generated-production-preview tests. The browser viewport override did
not apply to these existing live tabs, so this check does not add phone evidence.
