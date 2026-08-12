# Model comparison without Pi

The workshop's headline exercise is: build something, read the session's real
token and cost figures, build it again on a different set of models, compare.

That exercise used to require Pi, because Pi is the only harness that routes per
model across the Anthropic, Responses and chat-completions surfaces. It also
made the most fragile component in the room load-bearing for the thing the room
is there to see: every Omnigent harness — Pi included — dies together when the
attendee's tab-bound OBO token goes stale.

Bare Codex reaches the same models over one plain OpenAI-shaped surface and
touches no attendee credential on the way. The comparison therefore survives
every credential failure Phase 2 of the stability plan is about.

## What an attendee runs

```
codex --profile glm      # GLM 5.2
codex --profile kimi     # Kimi K3
codex --profile gemini   # Gemini 3.6 Flash
```

Plain `codex` stays on the everyday driver (a GPT-5.6 tier on the Responses
wire, which is what Codex is tuned for). The profiles above are the exception,
not the new default.

The published set is whatever this deployment will actually serve, and it is
readable from `/api/config` under `model_comparison` — resolved from the same
code that writes `~/.codex/config.toml`, so the UI and the CLI cannot disagree.

## How it is wired

`server/cli_config.configure_codex` writes a second Codex provider next to the
default one:

| | default | comparison |
|---|---|---|
| provider id | `databricks` | `databricks-chat` |
| wire | `responses` | `chat` |
| base URL | `<gateway>/codex/v1` | `<host>/serving-endpoints` |
| auth | rotating gateway-token file | the same file |

Both read the same rotating token file through a provider `auth` command, so a
long-running comparison survives token rotation exactly as an ordinary session
does.

Profiles are filtered against serving-endpoint discovery, so a region that is a
release behind advertises the models it has rather than the ones it does not.

## Publishing the set: the smoke matrix

Serving an endpoint is not the same as being usable from Codex. These models all
answer a question; tool-calling fidelity across vendors is where it stops
working, and an attendee's first file edit is where they would find out.

```
export DATABRICKS_HOST=https://ws.cloud.databricks.com
export DATABRICKS_TOKEN=...
scripts/smoke_models.py
```

Three checks per model, in increasing order of what an agent needs:

| check | passes when |
|---|---|
| `turn` | a plain question comes back with prose |
| `tool_call` | the supplied function is called, with arguments we can read |
| `file_edit` | an `apply_patch` call names the right file and carries the new line |

`file_edit` uses Codex's real patch format on purpose: a model that can call
`edit_file(path, contents)` but cannot produce a well-formed patch body is a
model that fails on the attendee's first real edit.

The script exits non-zero if anything failed and prints the line that drops it:

```
WORKSHOP_CODEX_COMPARE=glm,gemini
```

Set that in the deployment and the failing profile disappears from the generated
Codex config and from `/api/config`. Leaving it unset means *unmeasured*, and an
unmeasured deployment offers everything the workspace serves.

## Environment

| Var | Effect |
|---|---|
| `WORKSHOP_CODEX_COMPARE` | comma-separated profiles to offer; unset offers all that are served |
| `CODEX_COMPARE_GLM` / `_KIMI` / `_GEMINI` | re-point a profile at another endpoint when one is renamed or withdrawn |

Both exist so a dead endpoint mid-event is a values change rather than a
release. The endpoint names in `server/models.COMPARISON_MODELS` are the part of
this that goes stale fastest.

## Omnigent Auto (optional second path)

Workshop Polly economy/balanced/frontier agents were removed. Stock `polly`
remains the default Omnigent agent. The new-chat picker offers **Auto · smart
routing**, which picks harness + model from the live host catalog. That path is
independent of the Codex comparison profiles above; both can coexist.

Auto is on by default from Omnigent 0.9.0. Where the account has AI Gateway
`routes:select` the external router decides; everywhere else — labs included —
the built-in judge does, calling the model pinned by
`WORKSHOP_ROUTING_JUDGE_MODEL`. Use the Codex profiles when you want a fixed
cross-vendor comparison rather than a per-task pick.

## Run it before the event, not during

Re-run the matrix whenever the model set changes or a workshop moves region.
Endpoint availability is regional and the model roster turns over every few
weeks; the whole cost of this script is a few seconds per model, paid once,
against discovering it in front of a room.
