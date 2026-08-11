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

## The Polly tiers, and the roster gap

The in-Omnigent comparison (`polly-economy` / `polly-balanced` /
`polly-frontier`) still exists and still uses Pi for its third vendor. It is now
the *second* way to run the exercise rather than the only one.

Pi is advisory in the Workshop Terminal installer: an instance is ready without
it, because a missing Pi costs the cheap-model tiers rather than the workshop.
But every tier in `deploy/omnigent-app/polly_variants.py` names a Pi worker, and
the Omnigent App registers those tiers from its own container — it cannot see
whether the paired Workshop Terminal instance installed Pi. A brain dispatching
to a CLI that is not there spends the attendee's turn on an error they cannot
act on.

So the App is told. Set `WORKSHOP_HARNESSES` on the Omnigent App to the harness
list the paired instance reports at `/readyz` under
`checks.installers.harnesses`:

```
WORKSHOP_HARNESSES=claude,codex,pi
```

The builder then prunes worker slots whose CLI is absent, corrects the tier
description so the picker does not promise a model that is not in the roster,
and refuses to register a tier left with fewer than two vendors — cross-vendor
review is what a tier is for, and an attendee choosing one by name has no way to
tell it was quietly degraded. Unset means unmeasured and keeps the full roster.

Control Tower sets it from the measurement, and only when the measurement says
something is missing: the App is deployed before the terminal has finished
installing anything, so the value is not knowable at that point, and a full
roster is what unset already means. Once the terminal's `/readyz` reports a
short harness list, CT redeploys the paired App with `WORKSHOP_HARNESSES` set
(`_align_omnigent_roster` in its provisioning). A healthy instance therefore
pays nothing, and the degraded instance — the one that would otherwise dispatch
to a Pi that is not there — pays one redeploy.

## Run it before the event, not during

Re-run the matrix whenever the model set changes or a workshop moves region.
Endpoint availability is regional and the model roster turns over every few
weeks; the whole cost of this script is a few seconds per model, paid once,
against discovering it in front of a room.
