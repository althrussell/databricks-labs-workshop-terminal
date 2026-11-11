# Model Serving: Calling ML Endpoints from Apps

Use Model Serving when your app needs **AI features** — chat, inference, embeddings, or predictions from a Databricks Model Serving endpoint. For analytics dashboards, use `config/queries/` instead. For persistent storage, use Lakebase.

## When to Use

| Pattern | Use Case | Data Source |
|---------|----------|-------------|
| Analytics | Read-only dashboards, charts, KPIs | SQL Warehouse |
| Lakebase | CRUD operations, persistent state, forms | PostgreSQL (Lakebase) |
| Model Serving | Chat, AI features, model inference | Serving Endpoint |
| Multiple | Dashboard with AI features or persistent state | Combine as needed |

## Scaffolding

Check if the `serving` plugin is available in the AppKit template:

```bash
databricks apps manifest --profile <PROFILE>
```

**If the manifest includes a `serving` plugin:**

```bash
databricks apps init --name <APP_NAME> --features serving \
  --set "serving.serving-endpoint.name=<ENDPOINT_NAME>" \
  --run none --profile <PROFILE>
```

**If adding to an existing app**, see *Adding Model Serving to an Existing App* below.

Use the `databricks-model-serving` skill to create a serving endpoint first if one doesn't exist yet.

## Adding Model Serving to an Existing App

**`databricks.yml`** — add serving endpoint resource and user_api_scopes:

```yaml
resources:
  apps:
    app:
      user_api_scopes:
        # ... existing scopes ...
        - model-serving
      resources:
        # ... existing resources ...
        - name: serving-endpoint
          serving_endpoint:
            name: <ENDPOINT_NAME>
            permission: CAN_QUERY
```

**`app.yaml`** — add env injection:

```yaml
env:
  # ... existing env vars ...
  - name: DATABRICKS_SERVING_ENDPOINT_NAME
    valueFrom: serving-endpoint
```

The injected value is the endpoint **name** (not a URL). Use it in server-side code to call the endpoint.

**`server/server.ts`** — register the plugin:

```typescript
import { createApp, server, analytics, serving } from "@databricks/appkit";

createApp({
  plugins: [server(), analytics(), serving()],
}).catch(console.error);
```

Preserve existing plugins and add `serving()` to the array.

**`server/.env`** — for local development:

```dotenv
DATABRICKS_SERVING_ENDPOINT_NAME=<your-endpoint-name>
```

Update smoke tests if headings or routes changed, then `databricks apps validate`.

## Serving Plugin API

Access model serving through the plugin handle returned by `createApp()`:

```typescript
import { createApp, server, serving } from "@databricks/appkit";

const appkit = await createApp({
  plugins: [server(), serving()],
});

// Non-streaming invocation
const result = await appkit.serving().invoke({
  messages: [{ role: "user", content: "Hello" }],
});

// Streaming invocation
for await (const chunk of appkit.serving().stream({
  messages: [{ role: "user", content: "Hello" }],
})) {
  console.log(chunk);
}

// On-behalf-of user (OBO) — uses the requesting user's identity
const result = await appkit.serving().asUser(req).invoke({
  messages: [{ role: "user", content: prompt }],
});
```

All serving routes execute on behalf of the authenticated user (OBO) by default. For programmatic access via `exports()`, use `.asUser(req)` to run in user context.

## Named Endpoints

Use endpoint aliases to reference multiple serving endpoints by name:

```typescript
serving({
  endpoints: {
    llm: { env: "DATABRICKS_SERVING_ENDPOINT_NAME" },
    classifier: { env: "DATABRICKS_SERVING_ENDPOINT_CLASSIFIER" },
  },
  timeout: 120000, // optional, default 2 min
})
```

Each alias maps to an environment variable holding the actual endpoint name. Access by alias:

```typescript
const result = await appkit.serving("llm").invoke({ messages });
const classification = await appkit.serving("classifier").invoke({ inputs: ["text"] });
```

If an endpoint serves multiple models, use `servedModel` to target a specific model directly:

```typescript
serving({
  endpoints: {
    llm: { env: "DATABRICKS_SERVING_ENDPOINT_NAME", servedModel: "llama-v2" },
  },
})
```

## HTTP Endpoints

The plugin auto-registers routes under `/api/serving`:

| Route | Method | Purpose |
|-------|--------|---------|
| `/api/serving/invoke` | `POST` | Non-streaming (default mode) |
| `/api/serving/stream` | `POST` | Streaming SSE (default mode) |
| `/api/serving/:alias/invoke` | `POST` | Non-streaming (named mode) |
| `/api/serving/:alias/stream` | `POST` | Streaming SSE (named mode) |

## Frontend

Use the built-in React hooks from `@databricks/appkit-ui/react` — do NOT call serving endpoints directly from the client.

**Streaming** (chat, real-time inference):

```tsx
import { useServingStream } from "@databricks/appkit-ui/react";

function ChatStream() {
  const { stream, chunks, streaming, error, reset } = useServingStream(
    { messages: [{ role: "user", content: "Hello" }] },
    {
      alias: "llm",
      onComplete: (finalChunks) => console.log("Done:", finalChunks.length, "chunks"),
    },
  );

  return (
    <>
      <button onClick={stream} disabled={streaming}>Send</button>
      <button onClick={reset}>Reset</button>
      {chunks.map((chunk, i) => <pre key={i}>{JSON.stringify(chunk)}</pre>)}
      {error && <p>{error}</p>}
    </>
  );
}
```

**Non-streaming** (one-shot inference, classification):

```tsx
import { useServingInvoke } from "@databricks/appkit-ui/react";

function Classify() {
  const { invoke, data, loading, error } = useServingInvoke(
    { inputs: ["sample text"] },
    { alias: "classifier" },
  );

  return (
    <>
      <button onClick={() => invoke()} disabled={loading}>Classify</button>
      {data && <pre>{JSON.stringify(data)}</pre>}
      {error && <p>{error}</p>}
    </>
  );
}
```

Both hooks accept `autoStart: true` to invoke automatically on mount.

For the full hook API and type generation details, see `npx @databricks/appkit docs ./docs/plugins/model-serving.md`.

For off-platform streaming (AI SDK v6 with Databricks AI Gateway), see the **`databricks-model-serving`** skill.

AppKit integrates with **Model Serving endpoints**. AI Gateway (beta) endpoints are not directly supported — use the underlying Model Serving endpoint name instead. AI Gateway features (rate limits, usage tracking) can be configured on Model Serving endpoints via the `databricks-model-serving` skill.

## Unity Catalog model services

Built-in pay-per-token `databricks-*` foundation-model endpoints are being retired in favor of **Unity Catalog model services**, addressed by UC full name (`catalog.schema.name`; foundation models are `system.ai.<model>`). After the workspace enables **Enforce Unity Gateway**, an app that calls a retired endpoint on `/serving-endpoints/<name>/invocations` gets **HTTP 403 PERMISSION_DENIED** from the model-serving proxy: `"Querying pay-per-token foundation model endpoint '<name>' is disabled for this workspace. Please use Unity Gateway."` (the console UI phrases it as `"...is no longer available. Use Unity Catalog model services."`). Custom, external-model, and MPS-backed serving endpoints keep using `/serving-endpoints/<name>/invocations`.

Two related 403s look similar:

- **Provisioned-throughput (PT) foundation-model endpoints** also reject direct queries under enforcement: `"Querying provisioned throughput foundation model endpoint '<name>' directly is disabled for this workspace. Please use Unity Gateway."` Create a model service that references the PT endpoint, and call that model service as the signed-in user (below).
- `"Endpoint '<name>' is no longer available. Please use Unity Gateway."` is returned for AI Gateway v2 endpoints of every type, custom and external included. On its own it doesn't mean a built-in endpoint was retired; check which endpoint the app is calling.

Call the model service as the signed-in user, never as the app's service principal, and do not declare it as a `uc_securable` resource. The Apps proxy forwards the attendee's token in `X-Forwarded-Access-Token` when the app declares the `ai-gateway` user scope (`user_api_scopes`). Send that token on `/ai-gateway/mlflow/v1`:

```typescript
// Server route. `req` is the Express request.
const token = req.header('x-forwarded-access-token');
if (!token) throw new Error('No forwarded user token: the app needs the ai-gateway user scope');
const host = process.env.DATABRICKS_HOST!.replace(/^(?!https?:\/\/)/, 'https://').replace(/\/$/, '');
const response = await fetch(`${host}/ai-gateway/mlflow/v1/chat/completions`, {
  method: 'POST',
  headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' },
  body: JSON.stringify({
    model: process.env.DATABRICKS_MODEL, // the workshop default for App features: system.ai.claude-sonnet-5-5
    messages: [{ role: 'user', content: 'Summarize this support case.' }],
    max_tokens: 256,
  }),
});
if (!response.ok) throw new Error(`Model call failed: HTTP ${response.status}`);
const data = await response.json();
```

`serving()` and its endpoint aliases take Model Serving endpoint names only. Do not pass a model-service name to `serving()`. Background work with no signed-in user runs as the app; the Agent Bricks exception is in the workshop contract.

## Troubleshooting

| Error | Cause | Solution |
|-------|-------|---------|
| `PERMISSION_DENIED` on query | SP missing CAN_QUERY | Declare `serving_endpoint` resource in `databricks.yml` with `permission: CAN_QUERY` |
| `DATABRICKS_SERVING_ENDPOINT_NAME` env var empty | Missing env injection | Add `valueFrom: serving-endpoint` to `app.yaml` env section |
| 502 / 504 Gateway Timeout | Inference exceeds the proxy timeout (60s without response headers on Spaces apps, 300s total) | Stream the response or reduce `max_tokens` — see [Platform Guide](../platform-guide.md) |
| Unknown serving endpoint alias | Alias not configured or env var not set | Check `serving()` config in `server.ts` and `DATABRICKS_SERVING_ENDPOINT_*` in `app.yaml` / `.env` |
| `403` / `PERMISSION_DENIED` on a foundation model: `"...is disabled for this workspace. Please use Unity Gateway."` (console UI: `"...is no longer available. Use Unity Catalog model services."`) | The workspace enabled Enforce Unity Gateway, so a built-in `databricks-*` pay-per-token endpoint is retired, or a provisioned-throughput foundation-model endpoint can no longer be queried directly (`"...provisioned throughput ... directly is disabled..."`); the UC model service needs `EXECUTE` (+ `USE CATALOG`/`USE SCHEMA`), not `CAN_QUERY` | For an app, call the model service as the signed-in user with their forwarded token (`X-Forwarded-Access-Token`) and the `ai-gateway` user scope, with no `uc_securable` resource — see *Unity Catalog model services* above. For a PT endpoint, first create a model service that references it. For non-app callers, use the **`databricks-unity-gateway`** skill. |
