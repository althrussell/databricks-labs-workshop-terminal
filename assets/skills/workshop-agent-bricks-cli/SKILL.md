---
name: workshop-agent-bricks-cli
description: Build, migrate, run and deploy code-first custom Databricks agents with the agentbricks CLI and agent.toml. Use for LangGraph or OpenAI Agents SDK backends, managed tools, memory and sessions; managed Knowledge Assistants and Supervisors use databricks-agent-bricks.
metadata:
  version: "0.1.0"
---

# Custom agents with Agent Bricks CLI

Use the installed **`agentbricks`** command from `databricks-agentbricks`, not
invented `databricks agentbricks` commands. This workshop integration is reviewed
against CLI **0.4.0**. Check `agentbricks --version`, `uv --version`, and the
relevant `--help` before relying on a flag. A missing binary means the optional
installer is disabled, pending or failed: ask the facilitator to check setup
status; do not upgrade the shared toolchain mid-workshop.

Choose this path when the attendee needs custom agent code. The separate
`databricks-agent-bricks` skill covers managed Knowledge Assistants and
Supervisors. An ordinary data app still follows `databricks-apps`.
Apply the workshop interaction contract: clarify the useful task briefly,
recommend a small first version with a reason, then build. Explore existing
workshop data and tools before inventing datasets.

## Start a project

```bash
cd "$(workshop-init-project my-agent --agentbricks -- --framework langgraph)"
agentbricks doctor .
```

Use `openai` when the attendee chose the OpenAI Agents SDK; otherwise LangGraph
is a practical default. The helper scaffolds into the project root **before**
seeding and committing both `AGENTS.md` and `CLAUDE.md`, retains scaffold notes,
and creates the continuity brief. Do not create an empty project first and then
call `agentbricks init` into it: the CLI refuses an existing directory.
The helper also commits this skill under `.agents/skills` with a Claude project
link, so Codex and Omnigent workers retain it in an isolated home/worktree.

The helper disables the template's demo chat UI. The generated Python agent
backend and `DurableAgentServer` are supported; they are an exception to the
workshop's UI framework rule. If a visible UI is needed, build a separate AppKit
frontend with the workshop design skills and the existing app deployment tool.
Do not present an API-only URL as a finished attendee-facing app.

## Authentication, models and resources

- WT already provisions the build identity in `~/.databrickscfg` as `DEFAULT`.
  Use **`agentbricks --profile "${DATABRICKS_CONFIG_PROFILE:-DEFAULT}" ...`** to
  preserve the invoking harness's supplied profile; do not run browser login or copy
  tokens into `.env`, project files or command arguments. The scaffold's `.env`
  contains a profile name and stays ignored by git.
- Omnigent workers inherit their own supplied profile/environment; do not copy
  another harness's credentials into an isolated worker. If that identity lacks
  a capability, use the workshop's normal provisioning handoff.
- The released templates currently set `MODEL` in `agent/agent.py` to
  `system.ai.claude-sonnet-4-5`. Select a model from the event's approved policy
  and accessible Unity Gateway services before running. A harness model choice
  is not proof that the **generated agent's service principal** can invoke it.
  After deployment, verify a real model/tool turn as that deployed identity.
- `agent.toml` declares framework, stores, tracing and tool bindings. Keep it
  authoritative; avoid separate ad hoc resource provisioning. CLI init is local;
  bind commands and deployment can create or change workspace resources.
- Reuse workshop-provided data and tools. Create UC objects only inside
  `$WORKSHOP_CATALOG` / `$WORKSHOP_SCHEMA`. Keep store names and agent names
  specific to this project. Do not bind a working workshop/CT resource simply
  because a workspace-wide list returns it.
- Managed MCP tools default to **request-user** permissions. `--auth app` uses
  the deployed app identity and is a deliberate permission choice. UC function
  tools require app auth in 0.4.0. Do not switch identity to hide an access error.
  See [references/workflow.md](references/workflow.md) for supported commands.

## Run, deploy and verify

`agentbricks doctor .` checks files offline; it does not execute the agent or
prove model, store, tool, browser or workspace access. Run a real representative
turn with `agentbricks --profile "${DATABRICKS_CONFIG_PROFILE:-DEFAULT}" dev --app-port 8800` before deploying. Stop the local
server after testing; do not collide with the Workshop Terminal's listening port.
Choose an unused port and check `dev --help` before running.

For the **agent backend**, deploy with:

```bash
agentbricks --profile "${DATABRICKS_CONFIG_PROFILE:-DEFAULT}" deploy my-agent --source "$PWD" --pip-index-url https://pypi.org/simple/
agentbricks --profile "${DATABRICKS_CONFIG_PROFILE:-DEFAULT}" deployments get agent-bricks-my-agent
workshop-grant-me
```

Use a unique name within the Apps 30-character limit (including `agent-bricks-`).
This CLI provisions resources from `agent.toml`; do not substitute the AppKit
deployment helper for an Agent Bricks backend. Use that helper for its separate
AppKit UI. Runtime/deploy dependencies resolve from **public PyPI**; a
facilitator's laptop uses its Databricks package proxies for local installs.
Do not copy laptop proxy addresses into a deployed app or its lockfile.

Verify deployment status, one useful model/tool turn, and attendee access. The
CLI grants its new app SP access to declared resources, while WT's existing
entitlement handoff shares the app with the assigned attendee. These are separate
checks. User-authorized tools may also need browser consent for the new agent app.
Record observed checks and the live URL in the brief. If a beta API is unavailable
or access is rejected, report the concrete blocker; don't call deployment complete.

## Existing agents

`agentbricks init --framework langgraph --existing .` (or `openai`) prepares
migration instructions and a reference project without changing application code
or creating resources. Read the generated `agent-bricks-migrate/` instructions,
convert the code and run `agentbricks doctor .` before run/deploy checks.
The CLI writes `.claude/skills` and `.agent/skills`; **Codex does not use
`.agent/skills` here**. Give Codex the generated prompt/instructions explicitly.
Remove only this migration's temporary files/skills after verification and keep
them out of commits. Do not discard other project skills or earlier conversations;
switching session stores does not migrate history.

Authority: [Databricks Agent Bricks CLI](https://docs.databricks.com/aws/en/agents/custom-agents/agent-bricks-cli)
and the installed release's `--help` / templates. Upstream `main` can be ahead
of the released wheel: install from PyPI, not GitHub source.
