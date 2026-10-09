# Historical b53c run: grant follow-up

The original [summary](summary.json) records the initial model-access blocker and
pending grant approval. After subsequent user authorization, the executing
operator applied five direct EXECUTE grants for the isolated WT app's own service
principal and independently verified each direct and effective permission.

[model-grants.json](model-grants.json) is an exact copy of the operator receipt.
Its SHA-256 is `207819cb60304231f27c9b17fc611af7822cc54a22a512cefcdada4a1b9eac85`.
It binds to the preserved deployment receipt, app application ID
`6bff7044-f479-43bc-834b-4cf40b5ed654`, and numeric SP ID `70742253194359`.

| Service | Wire | Direct and effective EXECUTE |
|---|---|---|
| `system.ai.claude-sonnet-5` | Anthropic Messages | Independently verified |
| `system.ai.claude-opus-5` | Anthropic Messages | Independently verified |
| `system.ai.claude-haiku-4-5` | Anthropic Messages | Independently verified |
| `system.ai.gpt-5-6-terra` | OpenAI Responses | Independently verified |
| `system.ai.gpt-5-4-mini` | Chat Completions | Independently verified |

The recorded before/after readbacks retain other principals' permissions. CT
groups were unchanged, and the receipt records zero CT requests. This resolves
the grant blocker; it does not establish a model invocation.

The run expired at `2026-10-08T08:39:59Z` before an app-SP wire canary or generated
app was completed. No UX score or app acceptance was obtained for b53c.

The executing operator then completed cleanup. [cleanup.json](cleanup.json) is an
exact copy of its receipt, with SHA-256
`17cd3255b5f467e55774c2916c9c6af8561fdfac537a7a6db0e65ed2626d554a`.
The five exact test EXECUTE grants were removed and read back before deleting the
test app. Other principals' permissions remained unchanged. The app, catalog,
copied source, and test operator group were deleted and independently confirmed
absent. The receipt records no generated apps observed and zero CT requests.
The fresh `wt-eval-r01-1008-c72d` specification is prepared; a new deployment and
canary require new evidence.

[history-update.json](history-update.json) records this later status, the exact
grant-receipt hash, and local consistency checks. The original summary and all
original receipts remain byte-identical so their earlier observations and hashes
retain their provenance. [cleanup-followup.json](cleanup-followup.json) records the
subsequent cleanup without rewriting that intermediate history.
