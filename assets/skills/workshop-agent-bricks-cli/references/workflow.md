# CLI 0.8.0 command pointers

Global `--profile` and `--output` precede the command. `DEFAULT` is the WT build
profile, not the attendee's optional `me` OBO profile. Check the installed command
help when choosing arguments; store identifiers are not interchangeable with
display names.

```bash
# Local validation; no workspace calls.
agentbricks doctor .

# Discover available managed MCP services before declaring a binding.
agentbricks --profile "${DATABRICKS_CONFIG_PROFILE:-DEFAULT}" tools list --kind mcp
agentbricks --profile "${DATABRICKS_CONFIG_PROFILE:-DEFAULT}" tools add mcp system.ai.dbsql
# Inspect agent.toml for configured bindings: tools list is discovery,
# not a listing of this project's configured tools.

# Bind only when needed; these may create stores in the selected workspace.
agentbricks --profile "${DATABRICKS_CONFIG_PROFILE:-DEFAULT}" sessions bind my-agent-sessions
agentbricks --profile "${DATABRICKS_CONFIG_PROFILE:-DEFAULT}" memory bind my-agent-memory

# Trace inspection after a real turn; init's tracing declaration is not evidence.
agentbricks --profile "${DATABRICKS_CONFIG_PROFILE:-DEFAULT}" tracing list
agentbricks --profile "${DATABRICKS_CONFIG_PROFILE:-DEFAULT}" tracing bind --experiment-id <owned-experiment-id>

# Inspect your agent's deployment. Lists can contain other attendees' resources.
# The app is agent-bricks-<deployment_name>, from this project's agent.toml.
agentbricks --profile "${DATABRICKS_CONFIG_PROFILE:-DEFAULT}" deployments get agent-bricks-<deployment_name>
agentbricks --profile "${DATABRICKS_CONFIG_PROFILE:-DEFAULT}" deployments logs agent-bricks-<deployment_name>
agentbricks --profile "${DATABRICKS_CONFIG_PROFILE:-DEFAULT}" endpoint invoke --help
```

Keep bindings in `agent.toml`; tools and binding commands update it. Managed
memory is long-term information, sessions are conversation history, and tracing
is diagnostic evidence: add what the small first version needs.

For sandbox or UC-function bindings, inspect `agentbricks tools add --help` and
its subcommand help. Use the assigned workshop catalog/schema. Don't invent a
tool/source schema. In 0.8.0 `tools add sandbox` has no scope: sandbox code can
read and write all Unity Catalog data the selected identity can reach, and it
receives that identity's Databricks token by default. Prefer a UC function or a
managed MCP tool for data access. Add a sandbox only when the attendee needs code
execution, and tell them first what it can reach: with `--auth user` it acts with
the full access of whoever uses the agent; `--auth app` limits it to the app
service principal's grants, which is narrower but applies to every user. Add
`--no-databricks-access-token-included` unless the code must call Databricks APIs.

`deployments delete` retains memory/session stores, tracing and source files;
`--delete-source` also removes the uploaded source folder.
Clean up only resources owned by this disposable project after checking their
identifiers and references; never use broad workspace list results as a deletion
list. Follow the workshop's normal isolated-run cleanup process.
