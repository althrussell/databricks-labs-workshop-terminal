# ADR 0002 — The agent builds as the app service principal, not as the attendee

- **Status:** accepted (the shipping half); the durable route is scoped, not built
- **Date:** 2026-08-10
- **Affects:** this repo; informs `databricks-labs-control-tower` admission

## Context

The stated intent was: app service principal for AI Gateway and model calls, the
**attendee's own identity** for everything the agent does against the workspace.
The first half shipped and is correct. The second half was never implemented,
and a day of measurement against a live deployment established that it cannot be
with anything currently reachable.

Four ceilings, each measured rather than inferred (method and evidence in
[../auth-identity-model.md](../auth-identity-model.md)):

1. **The OBO scope vocabulary is 17 scopes and none of them writes.** No `jobs`,
   `clusters`, `pipelines`, `permissions`, `serving-endpoints` or `apps` scope
   exists, and `all-apis` is rejected outright. The read-only ceiling is
   permanent, not a configuration gap.
2. **The attendee cannot mint their own PAT.** `POST /api/2.0/token/create` with
   the OBO returns *"Provided OAuth token does not have required scopes:
   authentication"*, and `authentication` is not in the vocabulary above.
3. **An admin cannot mint one for them.** `create-obo-token` issues tokens for
   service principals only.
4. **The Apps proxy rejects PATs.** So even a hand-pasted PAT cannot authenticate
   the Omnigent runner hop, which is where the harnesses live.

Two designs had been sketched against the gap before it was measured: a
**wrapper service principal** auth provider that would let the runner present a
per-attendee identity to the control plane, and a **credential broker** that
would vend attendee-scoped credentials on demand. Both assume an attendee-shaped
credential exists to wrap or vend. None does.

## Decision

**1. Ship App-SP-plus-reconciler.** The agent's CLI resolves the app service
principal on both planes — in Omnigent through a `databricks` wrapper that
redirects only when `DATABRICKS_CONFIG_FILE` holds exactly the Omnigent-plane
path. Resources are therefore created owned by the SP, and
`server/entitlements.py` grants and transfers them to the attendee afterwards.

**2. The runner's SDK stays on the attendee OBO.** The wrapper intercepts argv
only. This is deliberate and is the reason parity was not restored by merging
`[DEFAULT]` into the Omnigent config: the runner's token factory falls back to
SDK auth whenever the mirrored token is stale, with no way to disable it, so any
credential resolvable from that file is one the runner can silently assume.
Merging would have made stale-OBO sessions quietly service-principal-owned —
wrong identity in the audit log, discovered months later.

**3. Retire the wrapper-SP auth provider and the credential broker.** Neither
survives ceiling 1. Recording them as retired rather than deleting them
silently, because both are the natural thing to reach for and will be proposed
again otherwise.

**4. The durable route is a custom OAuth U2M app integration — post-event.** An
account admin registers a custom app integration with `all-apis` and
`offline_access`; WT hosts the redirect and holds the **refresh token**
server-side. It is the only surviving path to a full-rights, attendee-identity
credential with no browser-tab dependency. Not before the event: it needs an
account-admin change and three unknowns answered first.

## Consequences

**The entitlements reconciler is load-bearing, not a safety net.** It is the only
thing making an SP-created resource usable by the attendee who asked for it. Its
coverage is a correctness requirement — `entitlements.health` is emitted on
failure so a silent reconciler failure surfaces before an attendee finds it.
Known gaps stay documented in the identity model: Lakeview dashboards are not
handed off at all, and several resource types get permissions without ownership.

**Attendee-owned-at-creation is not achievable this cycle, and the audit log
carries the difference.** `identity.resolved` at session start records which
principal each CLI surface resolves to per plane while the instance is alive;
the workspace audit log answers "who created this?" after it is gone.

**The tab dependency is structural until the U2M route lands.** No refresh token
means freshness can only be pulled from a live browser tab. What changed is that
running out is now gated, explained and self-recovering rather than a mysterious
mid-lab failure — `credential_durability` in `/readyz` refuses admission to an
instance that cannot keep its credentials alive for the remaining event, and
[../verification-gate.md](../verification-gate.md) holds the evidence.

## The post-event spike, scoped

Answer these three before writing any code — each can kill the design:

1. **Does the account permit custom OAuth app integrations at all?** Account
   admin, `POST /api/2.0/accounts/{id}/oauth2/custom-app-integrations`. If the
   answer is no, everything below is moot and the manual-PAT fallback becomes
   the only option.
2. **Refresh-token lifetime and rotation behaviour.** A refresh token that
   expires inside a multi-day event, or one that rotates in a way WT cannot
   persist across an app restart, reintroduces the failure in a slower form.
3. **Can consent be pre-approved for a group?** If every attendee must click
   through a consent screen, the zero-friction start is gone and the design has
   to be judged against the manual-PAT path on equal terms.

If all three answer favourably: WT hosts the redirect, stores the refresh token
per attendee under `DATA_ROOT` at mode 0600, and the mirror written for the
Omnigent host becomes renewable server-side. The `databricks` wrapper then
points at the attendee profile instead of `[DEFAULT]`, the reconciler drops back
to being a safety net, and the App SP credential in the attendee's home
directory can be narrowed or removed.

**Fallback if the spike fails: an opt-in manual PAT entry box.** Validated at
entry, health-checked, degrading to the SP rather than hard-failing. It buys
correct ownership at creation with no reconciler, and it costs the
zero-friction start. It does **not** help Omnigent or Pi, because of ceiling 4 —
so it is an alternate CLI identity, never a fix for the harnesses.
