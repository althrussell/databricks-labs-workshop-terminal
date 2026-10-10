# Control Tower provisioning and R01 test parity

Reviewed on 8 October 2026. Control Tower was read only. This review changes no
CT code, configuration, group membership, event, or deployment.

The earlier isolated test reproduced a subset of CT's environment, not its full
provisioning contract. Its setup failures cannot be treated as evidence that CT's
working attendee deployment is broken, or as a generated-app quality score.

## Source versions

The primary CT checkout at `/Users/a/code/databricks-labs-control-tower` is an
August branch at `34f4d108a66b91c71ff1b5390995ff0f412b888a`. The local Labs
deployment checkout is
`/Users/a/.codex/worktrees/ct-labs-latest-deploy/databricks-labs-control-tower`,
at `3d61017`; it includes permanent model-pool permissions and package deployment.
Its bundle pins WT's `v2026.09.28.1` release manifest. Live CT app metadata reports
a 29 September deployment. The
[source-review receipt](remediation-validation.md#r01)
records the exact full CT revision and reviewed file hashes. Live CT source byte
equivalence remains unverified; the local checkout is not silently promoted into
proof of the deployed source.

The original audit used the 27 August commit
`4d46461c6a0229892bdda5c153d8a6dd03d316c6`. On 9 October the working branch was
aligned with latest remote `main`, `440232b953a5050c965838b42a594e1592ee3d33`.
The verified CT-pinned PEX contains that same revision. Its model-policy endpoint
is exercised by the new package deployment runner. August evidence remains
historical and is not attributed to the current CT attendee release.

The package manifest root of trust is SHA256
`345509ef449a910f7e901a2ab7aaf8580aa4a7ade976ee9c4c4281d5652f9f63`;
PEX SHA256 is `bd10e2ab166232f5fe3f6848cdb93c180a3790530096a0f29448ee155b67e8fc`.
The runner verifies the package and every logical file, keeps the PEX unchanged,
and records the separately hashed external observer and bootstrap.

## What CT provisions

| Stage | Actual CT behavior | Source in the reviewed CT checkout |
|---|---|---|
| Attendee workspace | Creates or uses the unit's workspace, assigns the attendee, applies workspace entitlements; does not federate the shared lab group into every attendee workspace | `orchestration/provisioning.py`, `_apply_lab_user_workspace_permissions` |
| Catalog and schema | Creates the unit catalog/schema with platform ownership retained; attendee receives `ALL_PRIVILEGES`, and `MANAGE` when entitlements are enabled | `orchestration/provisioning.py`, catalog creation; `databricks/real.py`, `grant_lab_privileges` |
| Generated app read access | Grants `account users` read access only after verifying that the catalog is isolated and bound to that unit workspace alone | `databricks/real.py`, `grant_workspace_read_privileges` |
| WT app and browser access | Creates the app/SP; grants the attendee `CAN_MANAGE` as soon as the app exists, before deployment completes | `databricks/real.py`, `deploy_app`, `prepare_app`, `_grant_app_manage` |
| Attendee OBO | Declares user API scopes on the app resource; preserves them on redeploy; browser consent is a separate prerequisite | `orchestration/provisioning.py`; `databricks/real.py`, app create/update paths |
| App credentials | Binds the app's numeric SP ID after creation; terminal agents use the app's own OAuth identity. Legacy token-create/PAT setup remains separate | `orchestration/provisioning.py`; `databricks/real.py`, `ensure_app_can_mint_tokens` |
| WT build rights | Grants WT's SP `USE_CATALOG`, `CREATE_SCHEMA`, and `MANAGE` on the unit catalog, with readback | `databricks/real.py`, `grant_sp_catalog_privileges` |
| Model access | Permanent lab-user group gets its approved model pool; `control_tower_wt_callers` gets the WT-SP pool. Both receive `USE_SCHEMA` on `system.ai`; enabled model services receive exact `EXECUTE` grants | `orchestration/static_model_permissions.py`, `process_static_sync`; `databricks/real.py`, `apply_event_permissions` |
| New terminal registration | Adds the terminal's application/client UUID to the account caller group. Package mode verifies caller/package-reader membership before WT starts | `orchestration/wt_caller.py`, `register_wt_app_sp`; `orchestration/provisioning.py`, `_entitle_packaged_terminal_sp` |
| Model selection policy | Sets `WORKSHOP_MODEL_POLICY_REQUIRED`; pushes the revisioned WT-SP pool and denied models through `PUT /api/admin/model-policy`; verifies returned policy revision/sets | `orchestration/static_model_permissions.py`, `sync_terminal_policies_for_run`; `databricks/real.py`, `push_terminal_model_policy` |
| Gateway | Explicitly supplies the workspace-hosted `https://<workspace>/ai-gateway` in both persisted source config and deployment config | `orchestration/provisioning.py`, `_terminal_gateway_environment` |
| Durable work | Gives WT's SP write access to the attendee's `projects` home subdirectory; registers the release/toolchain reader; supplies immutable release/pin identities | `orchestration/wt_caller.py`; package deployment paths |

CT's policy acknowledgement checks the applied revision and allowed/denied sets.
The WT September admin implementation returns these from its applied policy.
That acknowledgement is not itself an inference canary. An actual app-SP request
is still needed before model-dependent test execution.

## Differences in the isolated d94e test

The test uses one app in the existing Labs workspace, not a new isolated attendee
workspace. CT's workspace-wide `account users` grants must not be copied into
Labs; use grants restricted to this test's SP and owned catalog instead.

Generated apps need this equivalence as well as WT. In the isolated runner,
`--generated-access-seed` watches the fresh app inventory and grants only the
new app's verified service principal `USE_CATALOG`/`SELECT` on the receipt-owned
catalog and `USE_SCHEMA`/`SELECT` on the verified fixture schema. Direct and
effective readbacks are required before it records qualification. It checks the
app creator/time/SP, catalog owner/creation/metastore and live fixture table UUID;
ambiguous inventory receives no grants. The permission ledger is a separate
artifact. It changes no account group or shared catalog. This polling adapter is
specific to the one-app Labs simulation; CT's contained grant exists before app
creation, so actual CT integration and fleet timing still need separate testing.

The simulator initially omitted model access setup, selected the dedicated gateway
instead of CT's explicit workspace-hosted URL, transferred catalog ownership to
the attendee, and gave the attendee `CAN_USE` rather than CT's `CAN_MANAGE`. It also
omits CT's model-policy-required flag and policy push, release-volume bootstrap,
package/toolchain group registration, and work-sync grant. These are evaluator
differences, not newly proven WT product defects.

The subsequent d94e readback verifies the exact app SP identity and effective
`USE_CATALOG`, `USE_SCHEMA`, and `EXECUTE` for the tested model services. Its SP has
workspace and SQL entitlements. Therefore missing caller-group membership alone
does not establish the cause of the remaining provider HTTP 404: the equivalent
direct UC privileges are already present. Preserve the response and diagnose the
actual caller/request contract rather than keep adding grants speculatively.

## Corrected R01 execution order

1. Select and record the intended WT baseline release. Retain August evidence as
   an explicitly older-checkout run; use the CT-pinned September release for a
   claim about today's CT attendee experience. Compare audit findings across the
   two versions before changing product policy.
2. Generate a provisioning contract from the reviewed CT paths above. Apply it
   only to fresh test resources. Preserve operator catalog ownership/cleanup
   authority; grant attendee management and app-SP build rights separately.
3. Simulate permanent caller-group/model policy using a disposable test group and
   the recorded approved pool, or explicitly document direct-grant equivalence.
   Never add test principals to CT's working groups. Exercise the baseline's real
   policy endpoint when that release requires it; an older release cannot be
   certified by skipping a missing endpoint.
4. Verify genuine browser identity/OBO, app SP identity, exact source/release,
   effective permissions, selected models, and a successful bounded app-SP model
   call. Stop setup at a provider failure; do not count it as an app UX failure.
5. Finish fixture setup with independent data/grant readbacks. Launch a fresh
   native harness session with only the novice's plain-language input, collect
   actual consultation/build turns, and independently test any resulting app.
6. Record functional outcomes and actual screenshots, or the independently
   classified journey failure. Clean up only receipt-owned resources and grants.
   CT deployment/integration follows after the isolated WT test passes.

R01 is now closed with the
[eligible current-release failed baseline](remediation-validation.md#r01).
It includes native-correlated simple inputs, an actual generated app, independent
UI tasks/screenshots, and exact cleanup before the original expiry. The generated
app failed scope agreement and due-date presentation; full acceptance remains
false. CT integration has not been exercised. Final readback verifies CT remained
on its same RUNNING/ACTIVE deployment, and the reviewed clean checkout/file hashes
are unchanged. Passing infrastructure checks alone did not close this baseline.
