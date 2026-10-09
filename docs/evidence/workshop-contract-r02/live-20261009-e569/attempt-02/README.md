# R02 second live attempt, 9 October 2026

The genuine labuser ran the unchanged bakery opening through native Claude
2.1.283 (Sonnet 5). One short business question included a useful prioritization
recommendation; the actual reply supplied the attention rule. Native MLflow
scored only clarification (1.0), not complete requirements or generated UX.

The generated `bakery-orders` app reached a real AppKit deployment, which failed:
its new service principal lacked USE CATALOG on this test's catalog. Local CT
source review confirmed that actual CT grants read privileges to account users
in each contained attendee workspace. The isolated Labs simulation had granted
WT, but omitted equivalent access for future generated-app principals. This is
a simulation parity gap, not evidence of a WT permission defect.

The observer then treated Claude's native background task notification as an
attendee user reply and stopped with `native_user_delivery_mismatch` after 594.4
seconds. It interrupted the agent as recovery began, so autonomous recovery and
final app behavior are unverified. Source and original failed build logs are
preserved; seeded brief placeholders are interrupted-build evidence.

The corrected exact-SP access adapter was canaried against the failed app with
direct and effective grant readbacks. It did not repair/redeploy the app. Those
temporary deltas, the failed app and its exact source directories were removed
and independently verified absent. The collector now recognizes complete native
task notification envelopes. Both fixes are used by the third fresh attempt.

Control Tower code, deployment, events and permanent groups are unchanged.
Private browser state and raw native tool/worker logs are excluded. The manifest
records source and archived hashes, including any credential-form redactions.
