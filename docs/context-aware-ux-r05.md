# R05: context-aware app UX

R05 replaces competing workshop design instructions with upstream Impeccable.
AppKit remains the default implementation; explicit framework and visual
requests are honored. The goal, audience, primary task and available data guide
each app's UX rather than a shared shell or palette.

## Delivery

- The reviewed manifest independently pins npm launcher 4.1.0, native engine
  0.1.14 and compiled skill bundle 4.5.2. Bootstrap invokes the supported
  installer with the local-bundle override and `--no-hooks`. All shipped
  artifact sources are public. Laptop refresh/check uses its configured npm
  proxy without recording that proxy in shipped configuration.
- The compiled upstream skill is the packaged fallback. The native binary
  stays outside Git and project copies; the skill launcher finds the verified
  shared engine on PATH. `scripts/refresh_impeccable.py --check` runs the real
  installer and native discovery smoke in an isolated temporary project.
- The Databricks clone retains raw commit/content verification. A deterministic
  projection removes the retired app-design skill and routes its callers to
  Impeccable. The delivered content has a separate manifest digest. Runtime
  bootstrap and vendored refresh apply the same projection.
- Packaged and shared trees retire both old UX skills. Warm fork refresh,
  managed home-link reconciliation and project refresh retire their old copies.
  Current Codex discovery uses `.agents/skills`; managed legacy links are
  removed while attendee-owned assets and notes are retained.
- One short workshop policy in `workshop_contract.md` integrates PRODUCT.md
  context with the existing README brief and optional wizard. It overrides
  mandatory interviews, concept choices and review machinery for ordinary
  workshop builds. Impeccable owns craft; platform skills own APIs/scaffolds.
- Claude 2.1.296 and Codex 0.162.1 were refreshed from the latest-release audit
  before qualification. The chosen live models remain Opus 5.5 and GPT Sol 6.1.

## Validation status

The pinned supported installer and native engine discovery passed on macOS.
Focused regressions cover raw/projected provenance, retired routes, warm trees,
home links, project copies, preserved notes and real Git worktree content.
CI adds the same supported-installer check on Linux.

Live acceptance is pending: reuse the isolated CT-compatible Labs WT app and
build contrasting apps from simple simulated attendee inputs. Verify actual
harness/worker discovery, useful first previews, working controls and narrow
layouts. Record observed results here before closing R05. Raw receipts and
screenshots remain outside Git. CT code and the working CT deployment stay
unchanged.

The first Claude/Opus 5.5 repair-desk preview exposed an AppKit theme collision:
generic `--card` was overridden for browser dark mode while custom text stayed
dark. Compilation and simulated DOM checks missed the rendered contrast defect.
The workshop integration now requires scoped product tokens or consistent use
of the framework theme. Attendee feedback repair and fresh-build qualification
of that correction are in progress; the failed first preview is retained.
