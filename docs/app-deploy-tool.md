# Workshop App deploy tool

Workshop Terminal registers one local MCP server named `workshop` for Claude
Code and Codex. It exposes exactly one tool: `deploy_databricks_app`.

The tool accepts `project_path`, an absolute project directory below
`~/projects`, an optional bundle target, and a 30–3600 second timeout. `project_dir`
and `app_path` are accepted as aliases of `project_path`: any mix of the three is
collapsed when every value names the same directory after normalisation, and a call
whose values disagree fails with `reason: invalid_request` naming the conflicting
fields. Agents resolve the current project path before calling MCP; relative paths
are rejected so the MCP server cannot deploy a tree selected by its own process
working directory.

**Target rule.** The tool never reads `databricks.yml` to choose a target.

- No target, or an empty one: the tool runs the CLI without `--target`, so the
  Databricks CLI picks the bundle's own default (the target marked `default: true`,
  the only declared target, or the CLI's implicit default). `include:` files and
  single-target bundles are the CLI's to resolve.
- A target: passed through unchanged. It is validated for characters only (letters,
  numbers, dot, underscore, hyphen); it is never remapped, so a name the bundle does
  not declare is reported by the CLI.

The tool runs the supported project deployment pipeline:

```text
databricks apps deploy [--target <target>] --auto-approve --no-wait
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
same project and target. An omitted target uses the lock and state key `default`,
the key the tool used before targets were optional, so pre-upgrade state files
still resume; recorded operations distinguish an omitted target from an explicit
`default` target so neither resumes or returns the other's result. The detached
submit worker receives the controller's recorded target (or no `--target` flag) and
never resolves it again. A concurrent duplicate waits for and returns the
active call's result instead of deploying again, while lock waits still honor
cancellation and timeout. There is no limit on sequential deployments.

## Model access scope

An App calls models and Genie as the signed-in attendee, so the bundle must give it the
`ai-gateway` and `genie` user authorization scopes (`user_api_scopes`). They are declared
always, not only when a model call is detected: a missing scope breaks the App, while an
extra consent line costs nothing in a disposable seat workspace. Before submitting, the
tool checks the bundle file and, when a scope is missing, adds it to the app resource's
list in `databricks.yml` (or a `resources/*.yml` file), or checks the resolved bundle
when the App is in no file it can edit. Existing scopes are kept. The edit is checked by a second
`bundle validate`, and the file is restored if any scope was lost or the edit
could not be made. Nothing is deployed in those cases.

- A first deploy creates the App with the scope, so the attendee's single Allow
  click covers it. The success message says so.
- An existing App without the scope gets it from this deploy. The success message
  tells the agent to tell the attendee to reopen the App and click Allow. If the
  live App still lacks the scope afterwards, the message says so and asks for a
  facilitator.
- `workshop-init-project` declares the scope in the scaffold the same way, through
  `workshop-app-deploy --declare-ai-gateway <project>` (text only, no deploy).

Open question: the CLI's `apps deploy` in a project directory is documented as
validate, deploy the project and run it. Whether it also applies a changed
`user_api_scopes` to an App that already exists is not verified. The success
message reports what the live App shows after the deploy.

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
workshop-app-deploy --project "$PWD"
```

`--target` is optional with the same rule as the MCP tool: omitted means the CLI's
default target, and a given name is passed through unchanged.

The CLI fallback resolves relative paths from the caller's shell working
directory; this differs intentionally from the MCP boundary.

Use `--json` for one machine-readable result. `SIGINT`/`SIGTERM` follows the same
cancellation contract as MCP cancellation.
