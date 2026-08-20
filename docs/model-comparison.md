# Model comparison

The workshop's headline exercise is: build something, read the session's real
token and cost figures, build it again on a different set of models, compare.

## The Codex profiles are gone, and cannot come back

`codex --profile glm|kimi|gemini` used to carry this. codex-cli 0.144.6 removed
the chat-completions wire — `responses` is now the only accepted `wire_api` —
and these models answer on chat completions and nowhere else. That is no longer
an error message we quote but a property we can read: a Unity Catalog model
service declares the wires it serves, and `system.ai.glm-5-2` reports

```
"supported_api_types": ["mlflow/v1/chat/completions", "mlflow/v1/responses"]
```

with no `openai/v1/responses` among them. So there is no wire that Codex speaks
and these models answer, and `models.resolve()` now enforces that mechanically
rather than by convention — see the wire filter in `server/models.py`. The
profiles were removed in full.

**This was not a degradation, it was a total outage.** An unknown `wire_api` does
not make Codex skip one provider — it invalidates the entire `config.toml`. Codex
then loads its own default config, which has no `databricks` provider, and every
session dies at startup:

```
Error: error loading default config after config error:
Model provider `databricks` not found
```

That took out bare `codex` **and** the Omnigent native Codex terminal together,
because Omnigent copies this same file into its per-session `CODEX_HOME`.
`tests/test_codex_model_profiles.py` now pins every generated provider to a wire
the shipped CLI accepts, and pins every `model_provider` reference to a table
that exists.

## Where the exercise stands

The set is still resolved and still smoke-tested, and `/api/config` still
publishes it under `model_comparison` — as `{profile, model, label, endpoint}`,
with a URL rather than a command. Nothing advertises an invocation we cannot
promise works.

That URL is now the same for every entry: Unity AI Gateway's provider-agnostic
`{host}/ai-gateway/mlflow/v1/chat/completions`, which takes the model in the
request body. One endpoint and a model name that changes is a better shape for
this exercise than a URL per model, because the thing the attendee varies is
exactly the thing the comparison is about.

What is missing is a harness that carries it. Omnigent still speaks the chat
wire (`wire_api: chat` is valid in an Omnigent `gateway` provider), so
re-homing it there is the open follow-up. Until that lands and is verified
against a live workspace, the models are reachable over plain HTTP at the
published endpoint.

Models are filtered against model-service discovery, so a region that is a
release behind advertises the models it has rather than the ones it does not.
The vendors are deliberately distinct — reading one task priced three ways is
the point — which is why Kimi K3 and Gemini 3.6 Flash were replaced by
Gemini 3.5 Flash Lite and Qwen 3.5 122B rather than by two more of the same
family when they left the catalogue.

## Publishing the set: the smoke matrix

Serving an endpoint is not the same as being usable by an agent. These models all
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

Set that in the deployment and the failing model disappears from `/api/config`.
Leaving it unset means *unmeasured*, and an unmeasured deployment offers
everything the workspace serves.

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
routing**, which picks harness + model from the live host catalog.

Auto is on by default from Omnigent 0.9.0. Where the account has AI Gateway
`routes:select` the external router decides; everywhere else — labs included —
the built-in judge does, calling the model pinned by
`WORKSHOP_ROUTING_JUDGE_MODEL`.

Note that the catalog Auto picks from is the workspace's live model list, which
includes the chat-only models above. A routed pick that lands on one of them
cannot be served on the Responses wire — the same constraint that removed the
Codex profiles. Excluding them from the candidate menu is tracked with the
routing work, not here.

## Run it before the event, not during

Re-run the matrix whenever the model set changes or a workshop moves region.
Endpoint availability is regional and the model roster turns over every few
weeks; the whole cost of this script is a few seconds per model, paid once,
against discovering it in front of a room.
