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
0.148.0 the launcher aliases `@openai/codex-linux-x64` to
`npm:@openai/codex@0.148.0-linux-x64`, while the native tarball's internal
package name remains `@openai/codex`. Bootstrap installs only the launcher via
offline npm, explicitly extracts the verified native tarball under the alias
directory, and validates `vendor/x86_64-unknown-linux-musl/bin/codex`.

`NODE_VERSION` tracks the active LTS line used by Codex. The frontend CI test
derives its Node pin from `install.NODE_VERSION` so build and deployed runtime
cannot drift.

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

## Toolchain mirror

An event may stage every fetched artifact into one Unity Catalog Volume and have
each app read from there instead of the public internet. It is **optional and off
by default**: with no mirror configured a Workshop Terminal boots exactly as
described above, and that remains a fully supported configuration.

The motivation is cold boot. A fresh app instance with an empty volume installs
roughly 430 MiB of pinned toolchain before `/readyz` goes green, and on event
morning many instances do that at once against the same handful of public hosts.
Workspace-local storage is faster and, unlike npmjs.org or GitHub, is not a
third party that can rate-limit or go down during a workshop.

### Contract

Two environment variables, both patched in by Control Tower at deploy:

| Variable | Default | Meaning |
| --- | --- | --- |
| `WORKSHOP_TOOLCHAIN_MIRROR_PATH` | `""` | Absolute `/Volumes/<catalog>/<schema>/<volume>` holding the staged toolchain. Empty means download from source. |
| `WORKSHOP_TOOLCHAIN_MIRROR_STRICT` | `"false"` | When true, an artifact the mirror cannot serve fails the install instead of falling back to the internet. For air-gapped events only. |

A path that is not a well-formed volume address is rejected rather than used, and
the rejection is reported. "Configured and rejected" must never be
indistinguishable from "never configured", because the failure this feature
guards against is an operator believing a volume is in use while every app
quietly downloads from the internet.

### Why the Files API, not a filesystem read

Databricks Apps does **not** mount volumes into the app container — `/Volumes`
does not exist on the filesystem at all. This was confirmed empirically by
deploying a probe app that inspected `/proc/mounts` and attempted both access
paths. Boot therefore reads the volume through the Files API using the app's own
service principal, via the singleton client `credentials.workspace_client()`
returns. An ambient `WorkspaceClient()` constructed inside the app cannot work:
initialization scrubs the client secret from the environment.

### Content addressing

Blobs are stored flat at `{volume}/{sha256}`, keyed by the artifact's
repo-owned checksum. Two properties follow, and together they are what make the
mirror safe to leave optional:

- **A release that bumps a pin cannot read a stale blob.** The new checksum is
  simply a key that is not there yet, so that one artifact misses and falls back.
  There is no stale-file case, and no need to coordinate WT releases with a
  re-stage.
- **The checksum gate is identical on both paths.** A mis-staged, truncated or
  tampered blob fails the same verification an internet download would, so the
  worst a bad mirror can do is cost a slow boot — never a broken or untrusted
  toolchain.

Flat rather than sharded into `{sha256[:2]}/` subdirectories: at roughly a dozen
objects, sharding would turn verification from one list call into a walk over up
to 256 prefixes.

### Staging and verification

`scripts/ct_mirror.py` lives here rather than in Control Tower because
`assets/artifacts/manifest.json` is repo-owned — whatever stages the volume has
to be version-locked to whatever verifies it. Control Tower runs it as the
deployer service principal from the checkout it is about to deploy. Every run
prints one JSON object and exits `0` on success, `1` on drift or failure, `2` on
invalid input.

| Command | Use |
| --- | --- |
| `verify` | Is the volume able to serve this release? Backs Control Tower's **Validate** button. |
| `stage` | Upload anything missing. Idempotent — keys are checksums, so a pin bump uploads one file. |
| `resync` | Re-fetch and rewrite everything. Backs the **Force resync** button, for a blob suspected corrupt. |
| `prune` | Delete blobs the current manifest no longer names. Storage hygiene only, worth ~430 MiB per superseded release. |

`verify` is fast by default and downloads no artifact bytes: one list call
establishes presence and size together, and size is checked against an
`index.json` sidecar written at stage time. That catches the realistic corruption
mode, an interrupted upload leaving a partial object under the right key. It
cannot catch a blob of exactly the right length with wrong contents, and does not
need to — boot re-hashes every artifact regardless. Measured against a real
volume: 6 seconds fast, 77 seconds for `--deep`, which re-downloads and re-hashes
everything and is a manual diagnostic rather than what the button calls.

After a partial stage the index is still written, marked `complete: false`. The
alternative — withholding it — would leave the artifacts that *did* upload with
no recorded size, silently costing `verify` its truncation check for as long as
the upstream problem lasted. Where a blob has no recorded size, `verify` passes
it but names it in `unsized`, so a green result never implies more assurance than
was actually bought.

### Reader access is granted to a group

The app service principal reads the volume as a member of a reader group that
holds `READ_VOLUME`, plus `USE_CATALOG` and `USE_SCHEMA` on the parents. Granting
to the SP directly would race: the SP is minted seconds before deploy, and the
bootstrap thread starts almost immediately after the container comes up, so a
grant that has not propagated yet is indistinguishable from a missing one. Boot
still retries a permission error a few times with backoff to absorb the residual
lag.

Control Tower stages the volume, issues the grants and adds the app SP to the
group **before** deploying, for the same reason.

### Three-layer pre-flight

Before a workshop, three checks answer different questions. None substitutes for
another:

1. **Is the volume right?**
   `ct_mirror.py verify --volume ... --reader-group ...` — every artifact staged,
   sizes intact, reader group authorised. Seconds.
2. **Is one app using it?** Deploy a canary and read
   `/api/admin/setup-status`. `toolchain_mirror.served` counts artifacts that came
   off the volume; `from_network` names the ones that did not.
3. **Is the fleet using it?** `ct_verify.py --require-mirror`. Layers 1 and 2 can
   both pass while most of the fleet still downloads from the internet, because
   group membership propagates per principal and a late-minted app SP can miss a
   grant every earlier app already had.

Layer 3 exists because a bypassed mirror is otherwise invisible. The app is
healthy, every checksum matches, the installed bytes are identical, and the only
symptom is that boot is slow again on the morning of the event. `ct_verify.py`
reports `mirror_bypassed` distinctly from `not_ready` because the remedy differs:
a resync or a grant, not a redeploy. It only reaches that verdict once the apps
are otherwise healthy — mid-bootstrap an app has served nothing yet and looks
identical to a bypass, and sending an operator to rebuild a volume that was fine
is worse than telling them to wait.

What layer 3 attests to is narrower than "the fleet is using the volume": it is
"nothing came from the internet on this boot". An app redeployed onto its
existing shared prefix installs from prewarmed binaries and fetches nothing at
all, which passes — correctly, since it took nothing from the internet — but
proves nothing about the volume, and a cache filled by an earlier bypassed boot
is indistinguishable from one filled off the volume. For positive proof, use
layer 2 on a genuinely cold instance and check that `served` is non-zero.

Mirror provenance is deliberately **not** part of the cross-instance manifest
comparison. One app serving from the volume while another reuses a prewarmed
disk is a legitimate fleet, and folding provenance into manifest equality would
make two healthy apps look divergent.

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

Both Codex npm artifacts go through `_npm_entry`, which downloads each tarball
and verifies it against the `dist.integrity` SHA-512 npm publishes for that
exact version, so a stale or hostile mirror cannot substitute bytes.

Node and tmux checksums are additionally asserted in code
(`NODE_LINUX_X64_SHA256`, `TMUX_LINUX_X64_SHA256`) so an independently verified
value guards the two artifacts every session depends on. No placeholder or
version-shaped checksum is accepted.

Persistent install stamps bind the reviewed artifact checksum to the installed
binary checksum. A version string alone never makes an install reusable.
