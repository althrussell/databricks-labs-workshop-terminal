# Omnigent dedicated App acceptance checklist

Use a disposable attendee workspace. Record timestamps, deployment/resource
IDs, HTTP status, and redacted logs for every step.

## Provision

- [ ] Pin the Workshop Terminal source revision and confirm
      `source_subdir=deploy/omnigent-app`.
- [ ] Confirm `upstream-lock.json`, `pyproject.toml`, and `uv.lock` agree on
      Omnigent `0.10.0`, and `pyproject.toml` requires Python
      `>=3.12,<3.13`.
- [ ] Create a dedicated Lakebase project, production branch, endpoint, and
      default database; verify the endpoint becomes active.
- [ ] Create the attendee UC schema and artifact Volume.
- [ ] Create the dedicated App and capture its service-principal identity.
- [ ] Grant Lakebase connect/create plus migration privileges.
- [ ] Grant `WRITE_VOLUME`, `USE_CATALOG`, and `USE_SCHEMA`.
- [ ] Bind App resource keys `postgres` and `artifact_volume`.
- [ ] Verify `OMNIGENT_AUTH_PROVIDER=header`.
- [ ] Confirm `WORKSHOP_SMART_ROUTING` is `true` and
      `WORKSHOP_ROUTING_JUDGE_MODEL` names a serving endpoint the App service
      principal can query. Pay-per-token foundation models need no grant; a
      custom endpoint needs `CAN_QUERY`. Without query access Auto still appears
      but every decision falls back to the session default.
- [ ] Confirm workshop Polly economy/balanced/frontier agents are absent; stock
      `polly` remains.
- [ ] Deploy the App and confirm the process binds the runtime port.

## App validation

- [ ] An authenticated `GET /health` returns HTTP 200 and `{"status":"ok"}`.
- [ ] `GET /api/version` returns `{"version":"0.10.0"}`.
- [ ] Deny Volume writes to the App SP and verify startup/health fails. Restore
      `WRITE_VOLUME`; verify startup writes+fsyncs a unique probe beneath
      `AP_ARTIFACT_VOLUME_PATH` and removes it successfully. Deny deletion and
      verify startup fails rather than declaring the Volume healthy.
- [ ] The upstream web UI loads through the Apps proxy.
- [ ] New-chat agent picker does not list `polly-economy` / `polly-balanced` /
      `polly-frontier`.
- [ ] With `WORKSHOP_SMART_ROUTING=false`, Auto · smart routing is hidden.
- [ ] With `WORKSHOP_SMART_ROUTING=true`, Auto appears and a first message
      returns a decision. On an account without `routes:select` the App logs one
      `ENDPOINT_NOT_FOUND` and latches the external router off; `GET /v1/info`
      then reports the `oss` routing source only, and the decision chip is
      sourced `oss-llm`.
- [ ] Confirm harness inference still uses the Workshop Terminal gateway token:
      the judge call is the only inference the App service principal makes.
- [ ] A request without proxy authentication is rejected.
- [ ] A client-supplied `X-Forwarded-Email` cannot bypass the Apps proxy.
- [ ] Create/read a test conversation and verify it survives an App restart.
- [ ] Write/read a test artifact and verify it lands in the bound UC Volume.
- [ ] Verify this wrapper starts no local host or runner process. Do not infer
      that the upstream package omits its normal process-manager code.
- [ ] Inspect logs and process environments: no model key, static access token,
      database token, or host bootstrap secret is exposed.

## Remote host and attendee auth

- [ ] Set the attendee-specific HTTPS `OMNIGENT_APP_URL` on Workshop Terminal;
      verify an empty value preserves bare/local Omnigent behavior.
- [ ] Set `WORKSHOP_ATTENDEE_EMAIL` to the exact normalized unit email. Verify
      remote startup rejects missing/invalid values and a different attendee is
      denied before HOME or OBO writes, including after local marker/data loss.
- [ ] Configure one-attendee topology
      (`ALLOW_SHARED_TOPOLOGY` unset/false and
      `MAX_SESSIONS_GLOBAL <= MAX_SESSIONS_PER_USER`). Verify remote startup
      fails closed if either shared-topology opt-in or multi-attendee session
      caps are configured; this is enforced because HOMEs share one Unix uid.
- [ ] Enable Workshop Terminal App user authorization so authenticated browser
      requests carry the attendee OBO bearer. `ENABLE_OBO` may remain false.
- [ ] Verify the first request creates the attendee HOME and atomically writes
      `~/.omnigent/auth_tokens.json` mode 0600, keyed by the normalized URL,
      with lowercased email and JWT expiry. Existing server records survive.
- [ ] Verify no token appears in API responses, logs, host argv, or process
      environment; specifically exclude `DATABRICKS_CLIENT_ID`,
      `DATABRICKS_CLIENT_SECRET`, `DATABRICKS_TOKEN`, and the OBO bearer.
- [ ] Populate the attendee `[DEFAULT]` profile with app-SP credentials, expire
      the mirrored OBO token, and verify remote host/TUI auth cannot fall back
      to it. Confirm the isolated empty config/profile is selected remotely and
      the supervisor waits for a fresh attendee token.
- [ ] Verify exactly one attendee-owned foreground host runs as
      `omnigent host --server <OMNIGENT_APP_URL> --non-interactive`.
- [ ] Verify the supervisor alone receives paired `OMNIGENT_HOST_ID` and
      `OMNIGENT_HOST_NAME`; the ID is stable across HOME loss/restart, exactly
      32 lowercase hex, and differs across attendee/server pairs. Confirm
      attendee PTYs/TUI receive neither override and `OMNIGENT_HOST_TOKEN`
      remains unset.
- [ ] Verify token rotation updates the JSON record without restarting the
      process. An expired token reports/waits for user auth without busy-looping;
      a fresh browser request wakes it.
- [ ] Close the browser tab and confirm no server-side refresh is claimed.
      Active WebSockets may remain; reopening/reconnecting must refresh the
      token file through a fresh authenticated request.
- [ ] Launch the Omnigent catalog card against a fresh control plane and verify
      the generated helper executes `omnigent polly --server <OMNIGENT_APP_URL>`
      with no second local server.
- [ ] With a session already open, launch the card again and verify it executes
      `omnigent polly --server <OMNIGENT_APP_URL> -c` and continues that
      conversation, so the App's UI and the terminal show the same one rather
      than forking a second. Then send a prompt: an agent-less
      `run --server` client reaches this point and dies with `Sessions API fresh
      session creation requires a local agent bundle`, so the prompt — not the
      launch — is what proves the branch.
- [ ] Verify supervisor shutdown sends TERM to process groups, waits within the
      Apps shutdown window, then KILLs/reaps stragglers without restart.
- [ ] Verify authenticated `GET /v1/hosts/{expected_host_id}` reports `online`
      before `connected=true`; offline, mismatch, network, and auth failures
      retain the exact local lifecycle state and return no token.
- [ ] Confirm `last_seen_at` is the UTC time Workshop Terminal successfully
      verified the host, not an upstream field (v0.10.0 returns none).
- [ ] Run normal stats collection and verify persisted readiness transitions
      pending → ready → disconnected. Repeat in final pre-teardown collection;
      an admin fetch error or malformed/mismatched report must invalidate stale
      connectivity with retryable `CONTROL_TOWER_HOST_READINESS_UNAVAILABLE`,
      and the next valid online report must recover to ready.
- [ ] Confirm no Control Tower static bootstrap secret exists or is required.

## Retry and rollback

- [ ] Repeat provisioning with the same attendee/run key; no duplicate App,
      Lakebase project, branch, database, or Volume is created.
- [ ] Simulate a transient deployment/health failure and verify bounded
      backoff plus stable failure fields.
- [ ] Verify missing/expired OBO remains `pending_user_auth`; process exits use
      capped full-jitter exponential backoff.
- [ ] Redeploy the pinned source revision and confirm health/state recovery.
- [ ] Verify a required App failure fails attendee provisioning; verify an
      optional failure is explicitly degraded and returns no App URL.

## Teardown

- [ ] Stop/delete Workshop Terminal references before the Omnigent App.
- [ ] Stop/delete the Omnigent App and wait for termination.
- [ ] Revoke App resource permissions.
- [ ] Delete the event-owned Volume/schema.
- [ ] Delete the Lakebase branch/project.
- [ ] Delete Control Tower correlation/deployment rows.
- [ ] Run teardown again; not-found results are treated as success.
- [ ] Verify repeated teardown correlates the same deterministic host ID.
- [ ] Confirm no App, grant, Volume, Lakebase resource, mirrored token, secret,
      host process, or deployment record remains.
