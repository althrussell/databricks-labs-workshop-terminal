# Two-instance event-readiness runbook

Use this template for exactly two independently provisioned app instances. The
helper creates/closes test agent sessions but does not deploy, grant, restart, wait,
create real resources, or delete infrastructure.

## Evidence and credentials

Every report event is signed with the operator-owned
`CT_VALIDATION_HMAC_KEY` and linked by a timestamped previous-hash chain.
Control Tower attestations use Ed25519 with a private key held only by the
external CT signing process. The validator is public-key-only: configure the
base64 raw public key through `CT_ATTESTATION_PUBLIC_KEY`, or set
`CT_ATTESTATION_PUBLIC_KEY_FILE` to a file containing that base64 value, plus
`CT_ATTESTATION_KEY_ID`. The validator never receives a CT private key and
uses only the public key. The validator never generates or self-labels a CT
signature. No secret key or bearer value is serialized.

Automated invocations append a signed `probe_attempt` event. Failed attempts
remain in the chain and leave the phase open for a safe retry. A passing attempt
is followed by an immutable phase-completion event. Late-grant and teardown
attestations are one-time phase completions. Completed phases remain ordered:

`late_grant → baseline → restart → lunch_resume → teardown`

Automated observations use provenance `automated_collector`; lunch uses
`operator_attestation`. Failed attempts are never replaced or removed.

Use distinct environment-variable names and distinct resolved values for each
admin/attendee credential:

```json
{
  "apps": [
    {"name":"canary-a","url":"https://app-a.example","workspace_host":"https://workspace-a.example","token_env":"APP_A_ADMIN_TOKEN","attendee_token_env":"APP_A_ATTENDEE_TOKEN"},
    {"name":"canary-b","url":"https://app-b.example","workspace_host":"https://workspace-b.example","token_env":"APP_B_ADMIN_TOKEN","attendee_token_env":"APP_B_ATTENDEE_TOKEN"}
  ]
}
```

```bash
export CT_VALIDATION_HMAC_KEY='<strong report signing key>'
export CT_ATTESTATION_PUBLIC_KEY='<base64 Ed25519 raw public key>'
export CT_ATTESTATION_KEY_ID='ct-key-2026-07'
export APP_A_ADMIN_TOKEN='<external CT admin bearer>'
export APP_A_ATTENDEE_TOKEN='<app A attendee bearer>'
export APP_B_ADMIN_TOKEN='<external CT admin bearer>'
export APP_B_ATTENDEE_TOKEN='<app B attendee bearer>'
python scripts/ct_two_instance.py --report evidence/two-instance.json \
  init --inventory inventory.json --run-id event-run-1
```

The durable report may move between machines with the signing key supplied
separately. Do not put credentials in inventory, evidence, filenames, URLs, app
names, or run IDs.

## Staged procedure

1. **External Control Tower—deploy and prove direct OAuth.** Deploy both apps
   with no PAT/token grant, then observe recent `rotating` state, source
   `app_identity_oauth`, and no `WORKSHOP_PAT`. Control Tower signs a canonical
   message externally. It binds
   the report schema version, generated `report_id`, run ID, sorted two-app
   inventory (names + URLs), phase, UTC attestation timestamp, unique nonce,
   and SHA-256 `payload_hash`. This prevents replay across a report, run,
   inventory, or phase; duplicate nonce values are rejected.

   The canonical message is UTF-8 JSON with sorted keys and compact separators.
   In the CT signing process—which alone has the Ed25519 private key:

   ```python
   message = {
       "schema_version": report_header["schema_version"],
       "report_id": report_header["report_id"],
       "run_id": report_header["run_id"],
       "inventory": report_header["apps"],
       "phase": "late_grant",
       "attested_at": "2026-07-21T00:00:00Z",
       "nonce": "late-grant-001",
       "payload_hash": hashlib.sha256(canonical_json(unsigned_evidence)).hexdigest(),
   }
   signature = base64.b64encode(
       ct_private_key.sign(canonical_json(message))
   ).decode()
   evidence = {
       **unsigned_evidence,
       "ct_attestation": {
           "key_id": CT_KEY_ID,
           "algorithm": "Ed25519",
           "attested_at": message["attested_at"],
           "nonce": message["nonce"],
           "signature": signature,
       },
   }
   ```

   Put that externally signed, non-secret evidence in `late-grant.json`:

   ```json
   {"apps":[
     {"name":"canary-a","before_state":"unhealthy","after_state":"rotating","credential_source":"app_identity_oauth","workshop_pat_present":false},
     {"name":"canary-b","before_state":"unhealthy","after_state":"rotating","credential_source":"app_identity_oauth","workshop_pat_present":false}
   ],"ct_attestation":{"key_id":"ct-key-2026-07","algorithm":"Ed25519","attested_at":"2026-07-21T00:00:00Z","nonce":"late-grant-001","signature":"<base64 Ed25519 signature>"}}
   ```

   ```bash
   python scripts/ct_two_instance.py --report evidence/two-instance.json \
     record late_grant --evidence late-grant.json
   ```

2. **External Control Tower—create real resources.** For each instance, create
   the dedicated catalog and exact resources through the app SP `[DEFAULT]`
   identity before any independent ownership verification. Do not pre-create
   them as the attendee or CT admin. Describe expected
   identifiers and required permission in `resources.json`; do not provide
   success booleans:

   ```json
   {"apps":[
     {"name":"canary-a","catalog":"event_a","catalog_owner":"attendee-a@example.com","attendee_principal":"attendee-a@example.com","tables":["event_a.default.acceptance_probe"],"sql_warehouse_id":"warehouse-a","resources":[{"type":"jobs","id":"123","required_permission":"IS_OWNER"}]},
     {"name":"canary-b","catalog":"event_b","catalog_owner":"attendee-b@example.com","attendee_principal":"attendee-b@example.com","tables":["event_b.default.acceptance_probe"],"sql_warehouse_id":"warehouse-b","resources":[{"type":"apps","id":"workshop-app","required_permission":"CAN_MANAGE"}]}
   ]}
   ```

3. **Automated baseline.** The probe calls attendee-authenticated
   `/readyz`, `/api/config`, `/api/entitlements/reconcile`, agent/session APIs,
   and admin setup/prewarm APIs. It does not trust app booleans or the
   entitlement handoff ledger as acceptance proof. Before any resource proof,
   each attendee bearer must return the same authoritative user ID and
   `userName` from workspace `current-user/me` and SCIM `/Me`; that identity
   must exactly match both `attendee_principal` and `catalog_owner`, with no
   service-principal `applicationId`. A mismatch or ambiguous SP token stops
   all permission/access proof. With each external admin credential the probe
   then reads UC catalog metadata/grants and each exact type/ID Permissions API
   directly from `workspace_host`. With each attendee credential it reads exact
   catalog/table metadata and executes the manifest SQL access probe. Catalog
   owner and `ALL_PRIVILEGES` are independent observations.

   ```bash
   python scripts/ct_two_instance.py --report evidence/two-instance.json \
     probe-baseline --inventory inventory.json --resource-manifest resources.json
   ```

   A transient failure appends a failed `probe_attempt`; rerun the same command
   to append another attempt without altering prior evidence. Session cleanup
   checks the DELETE response and polls GET `/api/sessions` with bounded retries
   to confirm absence. Any unconfirmed IDs are retained in
   `residual_session_ids`; the next retry must confirm those IDs are gone before
   creating any new session. Restart markers are created only after both probes
   pass. Partial marker creation uses the same confirmed cleanup rule, so a
   cleanup failure leaves the phase open and cannot create duplicates.

4. **External Control Tower—restart.** Restart both apps, wait for readiness,
   then prove each marker became a `server_restarted` ghost:

   ```bash
   python scripts/ct_two_instance.py --report evidence/two-instance.json \
     probe-recovery restart --inventory inventory.json
   ```

5. **External operator—90-minute closed-laptop pause.** Record real UTC start
   and completion timestamps, close the laptop for at least 90 minutes, reopen
   it, and perform the real Wi-Fi reconnect. Do not keep a helper alive or fake
   the wait.

   Use canonical UTC ISO timestamps. The measured interval must be at least
   5400 seconds and agree with `closed_laptop_pause_seconds` within five
   seconds. The signed `pause_started_at` must be at or after the signed restart
   phase-completion timestamp; negative, future, pre-restart, or contradictory
   timing fails but remains retryable.

   ```json
   {"pause_started_at":"2026-07-21T01:00:00Z","pause_completed_at":"2026-07-21T02:30:00Z","closed_laptop_pause_seconds":5400,"wifi_reconnected":true}
   ```

   ```bash
   python scripts/ct_two_instance.py --report evidence/two-instance.json \
     probe-recovery lunch_resume --inventory inventory.json \
     --attestation lunch.json
   ```

6. **External Control Tower—teardown.** Capture concrete deployment, app,
   workspace, catalog, and credential identifiers. Delete/revoke each item and
   retain one timestamped Control Tower deletion/revocation receipt per
   resource with `status: succeeded` and `provenance: control_tower`. The
   teardown checkpoint is an external CT attestation because deleted apps
   cannot self-report. Sign its canonical payload externally with the same
   private-key recipe, changing `phase`, timestamp, and nonce for `teardown`,
   then record it:

   ```bash
   python scripts/ct_two_instance.py --report evidence/two-instance.json \
     record teardown --evidence teardown.json
   ```

7. Verify the complete signatures, hash chain, phase order, and gates:

   ```bash
   python scripts/ct_two_instance.py --report evidence/two-instance.json evaluate
   ```

The final command fails for tampering, phase drift, red readiness, release
manifest drift, incorrect resource ownership/handoff, stale OBO, an
unsubstantiated pause, or incomplete teardown receipts.

## Deferred scale work

This run validates two instances only. The 10/100 instance tests and 4–8h fleet
soak remain deferred to the separate fleet-readiness phase.
