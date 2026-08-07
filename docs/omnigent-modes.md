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
| 4 | Managed server, WT host | `true` | `https://<workspace-host>/api/2.0/omnigent` | WT container, as a host registered to the workspace's managed Omnigent | Spiked; see below |
| 5 | Managed server, Sandbox host | `true` | managed, host delegated to Databricks Sandbox | Databricks-managed sandbox, not WT | Not available in `ap-southeast-2` |

Modes 3 and 4 differ only in *which server the host registers to*. The host
process, the token mirroring, and the readiness probe are identical.

Mode 5 is out of reach for the APJ fleet: Databricks Sandbox is offered only in
`us-east-1`, `us-west-2`, `eu-west-1`, `ap-southeast-1`, and `ap-south-1`. Our
workshops run in `ap-southeast-2`.

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

Verified against the labs workspace with Omnigent 0.8.2, using a token mirrored
the same way `obo.py` writes one. The host connected and the server agreed:

```
✓ Connected as 'workshop-spike-mode4' (7f51338c…), 0 live runner(s).

GET /api/2.0/omnigent/v1/hosts/7f51338c… → 200
{"host_id": "7f51338c…", "name": "workshop-spike-mode4",
 "owner": "al.thrussell+genai@databricks.com", "status": "online", …}
```

`status: "online"` is exactly the value `readiness()` requires before it reports
`connected: true`, and the `host_id` matched `stable_host_identity` byte for
byte. No WT change is needed to make mode 4 connect.

### Why the managed app looked empty

Opening `https://<workspace-host>/omnigent` shows no host and no working
directory because nothing has registered one. The managed server does not
create hosts; it waits for a host to connect to it. Mode 4 is precisely the act
of making WT that host.

### What mode 4 still needs before it is a supported mode

The spike proved the connection. It did not prove the workshop.

- **Control Tower.** A mode 4 run sets `pair_omnigent_app: false` and passes the
  managed URL through the terminal app's `env_overrides`. CT's readiness
  contract currently probes a paired Omnigent app for its version; with no app
  to probe, that path needs a decision rather than a default.
- **Workspace preview flag.** Omnigent is a workspace-level preview
  (`omnigents`). Provisioning has to enable it per attendee workspace or the
  managed endpoint will not exist.
- **Model access.** Mode 3 routes models through the paired app's configuration.
  What the managed server offers an attendee, and whether it honors the pinned
  model pool, is unverified.
- **Blast radius.** In mode 3 the Omnigent server is per-attendee and disposable
  with the workspace. In mode 4 it is workspace infrastructure shared with
  whatever else the attendee does there.
