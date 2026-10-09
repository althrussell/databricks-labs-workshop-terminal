# Agent Bricks CLI 0.4.0 qualification

This record qualifies local scaffolding and installation contracts for WT's
opt-in custom-agent backend support. It is not Labs deployment or attendee
browser evidence. No workspace requests or Control Tower changes were made.

- The released PyPI CLI was installed in a disposable laptop environment through
  the Databricks proxy. The real command scaffolded both LangGraph and OpenAI
  projects, passed every offline doctor check, preserved both harness memories,
  tracked the custom skill, ignored `.env`, avoided nested projects, preserved
  commits on rerun and retained agent source through migration preparation.
  A subprocess socket guard prohibited connections during these checks.
- The reviewed artifact manifest matches the publishers' artifact checksums.
  The CLI lock SHA-256 is
  `3aa33fa614f105c7eaeed2c8777ae753395c029d3ea64f782dfb1c1c8700f661`.
- Local full backend testing passed 2,768 tests, skipped two, and failed the
  public-source check because an incidental laptop uv invocation had rewritten
  the existing release lock to proxy URLs. The lock was restored byte-for-byte
  from HEAD; the failed check passed on rerun, including the new Agent Bricks
  lock. `uv.lock` is unchanged by this PR. Logs retain the original result.
- The added prewarm tests exercise new/changed fork skill refresh without an
  upstream clone and preservation of the previous tree on copy failure. Planner
  tests cover opt-in enablement, historical manifests, missing locks and rejection
  of unknown artifacts or mismatched hashes.

The Linux `agentbricks-cli-contract` CI job additionally exercises WT's actual
hashed wheel installer, persistent reuse and tampered-library rejection.
Production Apps installs use public PyPI; laptop proxy URLs are excluded from
committed configuration and locks.

Pending acceptance: a fresh isolated Labs WT, a real generated agent model/tool
turn as its service principal, any declared store access, assigned-attendee
browser access, and final CT integration. Templates default to Sonnet 4.5 and
need event-approved model selection. The feature remains disabled by default.
