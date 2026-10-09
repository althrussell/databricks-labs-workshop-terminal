# Preliminary R02 package qualification, 9 October 2026

PR #89 CI passed for branch commit `9f0cab97c53f674aa0d739f4c68d127a187f841d`.
The Linux package records CI merge commit `6d8868cbe5930ea4dc42ea983f2d58eec51514ba`;
all 444 runtime files were independently byte-compared to the branch commit.
Both Python matrices passed 2,665 tests with 22 skips. Frontend, reproducibility
and clean Linux smoke checks passed.

Standalone CT-compatible setup passed actual own-SP driver, Codex and wizard
model invocation. No attendee journey ran. PR review then found contradictory
README advice, so this setup was cleaned before rebuilding the correction.
Cleanup verified exact owned-resource removal and shared test-SP delta removal.
Post-cleanup readback verifies CT deployment/source and the empty attendee
projects directory were unchanged. No browser authentication state was created
or archived. The PEX is available in the linked CI artifact rather than Git.

`readme-alignment-tests.log` records 60 passing local checks after correcting
both README adapters and the helper comment. Earlier model probe snapshots
remain historical and unchanged.

CI: https://github.com/althrussell/databricks-labs-workshop-terminal/actions/runs/37873087440
