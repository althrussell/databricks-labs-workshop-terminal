# Reviewed bootstrap artifact manifest

Event admission requires `ARTIFACT_MANIFEST_PATH` to point to a Control
Tower-supplied, reviewed JSON document. Prefer files staged on the app volume or
a controlled mirror. HTTPS sources are downloaded to staging and SHA-256
verified before an installer is executed or an archive is extracted.

The schema is version 1 with `reviewed: true` and an `artifacts` object. Every
entry requires `version`, `source`, and a 64-character lowercase `sha256`.
Required keys are:

- `node_linux_x64` and `node_linux_arm64`
- `tmux_linux_x64`
- `claude_installer` and `claude_binary`
- `codex_npm_launcher_package` and `codex_native_package_linux_x64`
- `databricks_cli_installer` and `databricks_cli_archive_linux_x64`
- `uv_binary` and `python_3_12_runtime`
- `omnigent_wheelhouse` and `omnigent_lock`
- `ai_dev_kit`

`ai_dev_kit` additionally requires the exact 40-character `commit` and the
`content_sha256` of the copied `databricks-skills` directories. Its `version`
must match `AI_DEV_KIT_REF`.

The Codex native package entry additionally carries `executable_sha256`.
For 0.144.6 the launcher aliases `@openai/codex-linux-x64` to
`npm:@openai/codex@0.144.6-linux-x64`, while the native tarball's internal
package name remains `@openai/codex`. Bootstrap installs only the launcher via
offline npm, explicitly extracts the verified native tarball under the alias
directory, and validates `vendor/x86_64-unknown-linux-musl/bin/codex`. Run
`scripts/validate_codex_packages.py` against CT's production tarballs before
staging them.

Omnigent event mode has no installer or package-index fallback. CT must stage a
checksum-verified uv executable, complete Python 3.12 runtime tree
(`kind: directory`, `content_sha256`, and `executable_relative_path`), complete
wheelhouse directory (`kind: directory` plus `content_sha256`), and a lock file
where every direct and transitive requirement is exact-pinned and carries one
or more `--hash=sha256:` values. Bootstrap sets `UV_OFFLINE=1`,
`UV_NO_INDEX=1`, and `UV_PYTHON_DOWNLOADS=never`, passes the exact Python path,
and installs with `--require-hashes`.
Persistent reuse additionally hashes the entire installed uv venv, including
site-packages, transitive dependencies, and scripts.

Control Tower owns and reviews production values. This repository hardcodes
only the independently verified official Node 22.14.0 linux-x64 archive SHA-256
`69b09dba5c8dcb05c4e4273a4340db1005abeafe3927efda2bc5b249e80437ec`
and tmux linux-x64 SHA-256
`a23e56e9913d610c31f2893a1c9c669a73cb8bb2b8ded1180f6572bb55e52ca5`.
No placeholder or version-shaped checksum is accepted for event readiness.

Persistent install stamps bind the reviewed artifact checksum to the installed
binary checksum. A version string alone never makes an install reusable.
