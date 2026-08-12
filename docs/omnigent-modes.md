# Omnigent operating modes

Workshop Terminal can reach Omnigent five different ways. All five are selected
by configuration; there is no build flag and no code branch to enable one.

This document names them so that "mode 4" means one thing in a design
discussion, a test plan, and a Control Tower payload.

## The matrix

| # | Mode | `OMNIGENT_ENABLED` | `OMNIGENT_APP_URL` | Where the session runs | Status |
|---|------|--------------------|--------------------|------------------------|--------|
| 1 | Off | `false` | — | Nowhere; bare `claude`/`codex` only | Supported |
| 2 | Local | `true` | empty | Per-attendee loopback server auto-spawned in the WT container | Supported (default) |
| 3 | Self-hosted app | `true` | WT's own Omnigent App | WT container, as a host registered to the paired app | Supported (production today) |
| 4 | Managed server, WT host | `true` | `https://<workspace-host>/api/2.0/omnigent` | WT container, as a host registered to the workspace's managed Omnigent | Does not connect from the app; see below |
| 5 | Managed server, Sandbox host | `true` | managed, host delegated to Databricks Sandbox | Databricks-managed sandbox, not WT | Not available in `ap-southeast-2` |

Modes 3 and 4 differ only in *which server the host registers to*. The host
process, the token mirroring, and the readiness probe are identical.

Mode 5 is out of reach for the APJ fleet: Databricks Sandbox is offered only in
`us-east-1`, `us-west-2`, `eu-west-1`, `ap-southeast-1`, and `ap-south-1`. Our
workshops run in `ap-southeast-2`.

That is not just a documented region list. On a workspace where the managed
preview *is* enabled in `ap-southeast-2`, the sandbox surface is absent from the
API entirely, while the host surface answers normally:

```
GET /api/2.0/omnigent/v1/sandboxes → 404 {"detail":"Not Found"}
GET /api/2.0/omnigent/v1/hosts     → 200 {"hosts":[…]}
GET /api/2.0/omnigent/v1/runners   → 200 {"data":[]}
```

## Enabling the managed preview

Modes 4 and 5 need the workspace-level Omnigent preview turned on. **There is no
working public API for this.** Admins toggle it by hand at
`https://<workspace-host>/settings/workspace/previews`.

The obvious candidate does not work. `workspace-settings-v2` exposes an
`omnigents` setting, but it is metadata only: `PATCH` fails even against a
workspace where the preview is already active, and the value it reports does not
track the toggle. Do not build provisioning on it.

Probe the real thing functionally instead — the host endpoint's status code is
the flag:

```
GET /api/2.0/omnigent/v1/hosts → 404   preview off
GET /api/2.0/omnigent/v1/hosts → 200   preview on
```

This is what Control Tower has to gate a mode 4 run on, and it is why mode 4
cannot be fully self-service until the toggle grows an API.

## Mode 4: what the spike established

**The managed Omnigent host API is served at `/api/2.0/omnigent`, not at
`/omnigent`.** The latter is the single-page app; it returns HTML for every
path, including API paths, which is what makes a wrong base URL look like a
silently broken server rather than a 404.

Set `OMNIGENT_APP_URL` to the `/api/2.0/omnigent` base and every existing code
path lines up without modification:

- `config.normalize_omnigent_app_url` preserves the path segment, so the base
  survives normalization intact.
- `RemoteHostManager.readiness` probes `{server_url}/v1/hosts/{host_id}`, which
  resolves to the real managed endpoint.
- `obo.py` mirrors the attendee's forwarded token into
  `~/.omnigent/auth_tokens.json` keyed on `config.omnigent_app_url()`, so the
  managed URL is keyed automatically.
- `install.validate_remote_compatibility` only compares the *locally installed*
  version against the pinned protocol version. It never probes the remote, so
  it does not care which server the host registers to.

Verified against the labs workspace with Omnigent 0.9.0, using a token mirrored
the same way `obo.py` writes one. The host connected and the server agreed:

```
✓ Connected as 'workshop-spike-mode4' (7f51338c…), 0 live runner(s).

GET /api/2.0/omnigent/v1/hosts/7f51338c… → 200
{"host_id": "7f51338c…", "name": "workshop-spike-mode4",
 "owner": "al.thrussell+genai@databricks.com", "status": "online", …}
```

`status: "online"` is exactly the value `readiness()` requires before it reports
`connected: true`, and the `host_id` matched `stable_host_identity` byte for
byte.

That spike was run by hand, and it concluded too much. See below.

### The spike was not the workshop: mode 4 does not connect from the app

Running the same configuration from a real deployed Workshop Terminal against
the same managed server does *not* connect. The host worker cycles
`waiting_for_token → running → backoff` indefinitely and the server keeps
reporting the host `offline`.

`stable_host_identity` is not the problem: the app derived
`7f51338c6f548b1f64e4e48849bdc2ee`, the same id the hand-run spike registered.
Nor is OBO plumbing — the failing run has OBO fully healthy
(`enabled: true`, `validation_state: "verified"`, `fresh: true`, no missing
scopes) and still backs off.

The difference between the two runs is the *credential*, not the code. The
spike authenticated with a full user OAuth token from `databricks auth token`.
WT does not have one and never will: it mirrors the attendee's forwarded OBO
token, which carries only the scopes on the app resource —
`catalog.catalogs:read`, `catalog.schemas:read`, `catalog.tables:read`, `sql`.
There is no Omnigent scope to add to that list.

So the working assumption is that host registration needs authority the OBO
token cannot carry. Until that is resolved with the Omnigent team, **mode 4 is
not reachable from a Workshop Terminal app**, whatever the preview flag says.
Do not plan a workshop on it, and do not read the hand-run spike above as
evidence that it works — it only proves the URL and the host identity are
right.

### Why the managed app looked empty

Opening `https://<workspace-host>/omnigent` shows no host and no working
directory because nothing has registered one. The managed server does not
create hosts; it waits for a host to connect to it. Mode 4 is precisely the act
of making WT that host.

### What mode 4 still needs before it is a supported mode

Two of these are blocking today, in this order.

- **Host registration from the app.** Nothing else matters until this works —
  see the section above. A hand-run host connects; a deployed Workshop Terminal
  does not.
- **Control Tower.** A mode 4 run sets `pair_omnigent_app: false` and passes the
  managed URL through the terminal app's `env_overrides`. CT's readiness
  contract currently probes a paired Omnigent app for its version; with no app
  to probe, that path needs a decision rather than a default.
- **Workspace preview flag.** This is the hard blocker, not a task. The preview
  has no working public API (see "Enabling the managed preview" above), so a
  freshly provisioned attendee workspace has no managed endpoint and no way for
  Control Tower to give it one. Until that changes, every mode 4 workshop needs
  a human to toggle each workspace by hand — which does not scale past a demo.
- **Model access.** Mode 3 routes models through the paired app's configuration.
  What the managed server offers an attendee, and whether it honors the pinned
  model pool, is unverified.
- **Blast radius.** In mode 3 the Omnigent server is per-attendee and disposable
  with the workspace. In mode 4 it is workspace infrastructure shared with
  whatever else the attendee does there.
