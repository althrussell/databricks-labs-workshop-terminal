# Final release toolchain refresh

The 11 October audit checks 49 direct WT, evaluator, harness and UX-tool components.
It includes official APX, Bun, and Impeccable launcher/engine/skill release channels.
Forty-eight pins match their current published releases. Omnigent remains on its
existing 0.15.0 compatibility hold pending a matching paired server; it is disabled
on the isolated Labs deployment. No live worker model execution is claimed.

The candidate updates Databricks SDK to 0.151.0, Agent Bricks CLI to 0.5.0, uv
to 0.13.0, Bun to 1.4.3, lucide-react to 1.51.0, OpenAI to 3.24.0 and PEX to
2.103.4. Claude Code 2.1.296, Codex 0.162.1, Databricks CLI 1.20.0, agent skills
v0.2.28, official APX 0.3.8 and the three Impeccable pins remain current.
The dated test-toolchain contract is now `quality-20261011`.

Laptop installs use configured package proxies in disposable environments.
Committed locks retain public registry URLs and verified package digests.
The artifact manifest cross-checks official binary checksums; no proxy address
or credential belongs in the runtime, locks or manifest.

Local qualification passed 118 readiness/artifact/framework checks and 111
frontend checks. The frontend builds; the latest SDK/OpenAI evaluator API
contract passes without cloud calls. Actual Agent Bricks 0.5.0 passes offline
LangGraph/OpenAI scaffold, doctor, repeat setup and source-preserving migration
with a network-denial guard. These are not model/deployment qualification.

All nine Linux CI jobs passed, including the actual Agent Bricks installer, APX
with the refreshed Bun/uv pair, Impeccable delivery, public-lock consistency and
reproducible offline PEX. The candidate package passed readiness and an actual
native Codex analytical APX build on the reused isolated WT deployment. The
preview and concise runtime-filter iteration passed browser interactions,
shortlist persistence and 390px controls; prepared-source and missing-rating
limits were disclosed. See [final qualification](final-release-qualification.md)
for runtime identity and the retained limits. Publication remains the ship step.
Evidence remains outside Git.
No Control Tower code, configuration or working deployment is changed.
