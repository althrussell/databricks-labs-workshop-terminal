# Reviewed bootstrap artifact manifest

The contract is owned by this repository. `assets/artifacts/manifest.json` pins
the version and SHA-256 of every artifact the boot path installs, so a Workshop
Terminal boots and installs its full toolchain with nothing staged for it. There
is no required environment variable and no external dependency on Control Tower.

`ARTIFACT_MANIFEST_PATH` remains supported as a **narrow, optional override** for
mirrored or air-gapped events. An override may only redirect `source` -- where an
artifact is fetched from. An override that changes `version`, `sha256`, or
`content_sha256` is rejected, so a stale or hostile mirror manifest cannot
silently downgrade an attendee's toolchain. `status()` and `/readyz` report
`source: "default"` or `source: "override"` so an operator can see which contract
is in force.

HTTPS sources are downloaded to staging and SHA-256 verified before an installer
runs or an archive is extracted. A non-HTTPS URL scheme is refused. Relative
sources resolve against the manifest's own directory, which is how the in-repo
Omnigent lock and the vendored installer scripts are referenced.

## Schema

Version 1, `reviewed: true`, plus an `artifacts` object. Every entry requires
`version`, `source`, and a 64-character lowercase `sha256`. Required keys:

- `node_linux_x64` and `node_linux_arm64`
- `tmux_linux_x64`
- `claude_installer` and `claude_binary`
- `codex_npm_launcher_package` and `codex_native_package_linux_x64`
- `databricks_cli_installer` and `databricks_cli_archive_linux_x64`
- `uv_binary` and `python_3_12_runtime`
- `omnigent_lock`
- `databricks_agent_skills`

`databricks_agent_skills` is cloned rather than fetched, so instead of a file
`sha256` it carries the exact 40-character `commit` and the `content_sha256` of
the copied skill directories. Its `version` must match `SKILLS_REF`.

`uv_binary` and `python_3_12_runtime` are published only as archives. They set
`kind: "archive"` and `executable_relative_path`; boot verifies the archive as a
file, extracts it into a content-addressed cache directory, and uses the named
executable inside it.

The Codex native package entry additionally carries `executable_sha256`. For
0.144.6 the launcher aliases `@openai/codex-linux-x64` to
`npm:@openai/codex@0.144.6-linux-x64`, while the native tarball's internal
package name remains `@openai/codex`. Bootstrap installs only the launcher via
offline npm, explicitly extracts the verified native tarball under the alias
directory, and validates `vendor/x86_64-unknown-linux-musl/bin/codex`.

`claude_installer` and `databricks_cli_installer` point at scripts vendored into
`assets/artifacts/`, not at their upstream URLs. Both upstreams publish from a
mutable path whose content is not version-addressed, so a checksum against the
live URL would drift under us. Each entry keeps the upstream it was copied from
in an `upstream` field for review.

## Omnigent

Omnigent installs from the manifest only. `assets/artifacts/omnigent-<version>.lock`
is the integrity anchor: every direct and transitive requirement is exact-pinned
and carries one or more `--hash=sha256:` values, so the resolve is reproducible
without anyone staging a wheelhouse. Bootstrap sets `UV_PYTHON_DOWNLOADS=never`,
passes the extracted reviewed Python 3.12 executable explicitly, and installs
with `--require-hashes`. Persistent reuse additionally hashes the entire
installed venv, including site-packages, transitive dependencies, and scripts.

## Regenerating

`scripts/build_artifact_manifest.py --write` rebuilds the manifest from the
versions pinned in `server/bootstrap/install.py`, deriving each checksum from the
upstream source rather than from a pasted value. Where a publisher attests its
own checksum (Node, the Databricks CLI, the Claude release manifest, uv,
python-build-standalone) the script cross-checks the download against it.
`--check` verifies the committed manifest without rewriting it, which is what CI
runs. Regeneration needs outbound access to nodejs.org, github.com,
downloads.claude.ai, and registry.npmjs.org; partial regeneration is not
accepted, because the manifest is what attendees install.

Node and tmux checksums are additionally asserted in code
(`NODE_LINUX_X64_SHA256`, `TMUX_LINUX_X64_SHA256`) so an independently verified
value guards the two artifacts every session depends on. No placeholder or
version-shaped checksum is accepted.

Persistent install stamps bind the reviewed artifact checksum to the installed
binary checksum. A version string alone never makes an install reusable.
