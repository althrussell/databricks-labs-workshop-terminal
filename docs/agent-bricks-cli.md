# Agent Bricks CLI in Workshop Terminal

WT supports the separate `agentbricks` command from
`databricks-agentbricks==0.4.0` for custom LangGraph and OpenAI Agents SDK
backends. The managed Knowledge Assistant and Supervisor workflow remains in
the upstream `databricks-agent-bricks` skill.

This beta integration is **opt-in**. Set `AGENTBRICKS_ENABLED=true` in WT's
deployment environment; the checked-in `app.yaml` leaves it disabled. The
reviewed installer pins SDK 0.150.0, uv 0.12.24 and Python 3.12.15, with a
fully hashed Linux x86_64 wheel lock. It installs in its own shared-prefix venv
and exposes `agentbricks` and `uv` on attendee PATH. Coding harnesses can launch
independently; enabled CLI installation is checked by deep readiness.

For an isolated WT test, add `"agentbricks": true` to the specification in the
[CT-compatible evaluation runbook](generated-app-evaluation-runbook.md). The
WT-owned planner requires the reviewed CLI lock and emits the enable flag and
package pin. It makes no Control Tower requests. This change does not configure
or update the working Control Tower deployment.

Start an API-only project from an attendee harness:

```bash
cd "$(workshop-init-project my-agent --agentbricks -- --framework langgraph)"
agentbricks doctor .
```

Choose `--framework openai` for the OpenAI Agents SDK. The helper preserves the
invoking Databricks profile, scaffolds before creating files, retains template
notes, commits both harness memories and the workshop brief, and includes the
custom skill in isolated Codex/Omnigent worktrees. The skill is refreshed on WT
redeploy even when the upstream Databricks skills tag is unchanged.

Follow [the CLI skill](../assets/skills/workshop-agent-bricks-cli/SKILL.md) for
tool/store binding, migration, development and deployment. The released CLI's
`tools list` discovers available integrations; project bindings live in
`agent.toml`. Its development port flag is `--app-port`. Templates currently
default to Sonnet 4.5: select an event-approved accessible model before running,
then verify it again as the generated app's service principal. An API-only
backend needs a separate AppKit frontend if the attendee expects a visible app.

Package installation in Databricks Apps uses public PyPI; laptop qualification
uses disposable environments with Databricks proxies. Proxy addresses and
credentials must not ship in app configuration or locks. The optional toolchain
volume can mirror the reviewed Python/uv artifacts and the lock, but the wheel
install still requires public PyPI access.

Qualification uses the actual released CLI:

```bash
# Laptop: select a disposable CLI environment installed through the proxy.
python scripts/check_agentbricks_toolchain.py --bin-dir /path/to/cli/bin

# Linux x86_64: exercise the WT installer and reuse/tamper proof.
python scripts/check_agentbricks_toolchain.py --bootstrap
```

These checks scaffold and run offline doctor for both frameworks, verify
committed instructions, ignored `.env`, repeatability and source-preserving
migration. They do not deploy or invoke a model. Local evidence is in
[the qualification record](remediation-validation.md#agent-bricks).
Before enabling this for a workshop, qualify a fresh isolated Labs deployment:
one useful model/tool turn, declared stores if used, generated-app identity
permissions, and browser access as the assigned attendee. Final CT integration
remains a separate acceptance step.
