# Workshop App deploy tool

Workshop Terminal registers one local MCP server named `workshop` for Claude
Code and Codex. It exposes exactly one tool: `deploy_databricks_app`.

The tool accepts an absolute project directory below `~/projects`, a bundle
target, and a 30–3600 second timeout. Agents resolve the current project path
before calling MCP; relative paths are rejected so the MCP server cannot deploy
a tree selected by its own process working directory. It runs the supported
project deployment pipeline:

```text
databricks apps deploy --target <target> --auto-approve --no-wait
```

It then follows the exact deployment returned by the Apps API until the
deployment is `SUCCEEDED`, application state is `RUNNING`, and compute state is
`ACTIVE`. The result includes the app name, URL, deployment ID, terminal states,
duration, poll count, and whether an interrupted operation was resumed.

## Credentials and scope

Every CLI subprocess removes ambient Databricks host/token/client-secret values
and re-reads the attendee home's rotating `[DEFAULT]` profile. Credentials never
enter MCP arguments, process arguments, progress, state, or telemetry. MCP
project paths must be absolute and all project paths are constrained below
`~/projects`; target names are validated and subprocesses never use a shell.

State and bounded CLI progress are stored with mode `0600` below
`~/.config/workshop/app-deploy/`. One file lock serializes deployment of the
same project and target. A concurrent duplicate waits for and returns the
active call's result instead of deploying again, while lock waits still honor
cancellation and timeout. There is no limit on sequential deployments.

## Terminal outcomes

| Status | Meaning | Follow-up |
|---|---|---|
| `succeeded` | Exact deployment succeeded and the URL is live | Open `app_url` |
| `failed` | CLI validation/submission, deployment, or app startup failed | Fix the reported reason and run again |
| `timed_out` | Local wait deadline elapsed | Run the tool again; accepted remote work is resumed |
| `cancelled` | The MCP/CLI caller cancelled its local wait | Run again to resume if Databricks already accepted it |

Databricks Apps has no deployment-cancel API. Cancellation terminates a local
submission process when it is still running, but never stops an already accepted
remote deployment or submits a duplicate. MCP process restarts use the same
state record and resume the detached submission or remote deployment.

On success the helper requests the existing `apps` entitlement reconciliation
so the attendee can open the app immediately. That callback is best effort and
does not rewrite the deployment result. Each completion also emits bounded OTel
duration/outcome/reason/attempt/resume attributes; app output, URL, project path,
email, and credentials are excluded.

## CLI fallback

The same controller is available when an MCP client cannot start:

```bash
workshop-app-deploy --project "$PWD" --target default
```

The CLI fallback resolves relative paths from the caller's shell working
directory; this differs intentionally from the MCP boundary.

Use `--json` for one machine-readable result. `SIGINT`/`SIGTERM` follows the same
cancellation contract as MCP cancellation.
