# Immutable Workshop Terminal runtime

Workshop Terminal releases are a single self-contained PEX plus a small JSON
manifest. The artifact is built on Linux x86_64 with CPython 3.11 and executes
on the Python 3.11 interpreter supplied by Databricks Apps. Official workspace
files currently allow individual files up to 500 MB; WT additionally enforces a
200 MiB release budget.

## Build contract

`pyproject.toml` and `uv.lock` are the dependency source of truth. The release
builder runs from `uv sync --frozen --no-group dev --group release`; PEX resolves
only the exact runtime requirements already present in that environment, with
package indexes and source builds disabled. No dependency is installed when the
artifact starts.

The PEX contains only tracked files under:

- `server/`
- `static/`
- `content/`
- `assets/`
- generated `runtime-content-manifest.json`

The assets include attendee instructions, the vendored skills fallback, helper
scripts, brand assets, and the reviewed toolchain manifest. Node, tmux, Claude,
Codex, the Databricks CLI, uv, Python 3.12, and Omnigent distributions remain
external toolchain artifacts. Their checksums and mirror path are still governed
by `assets/artifacts/manifest.json`.

The entry point is `server.otel_bootstrap:main`. PEX venv mode prepends its own
console-script directory to `PATH`, so the launcher's `uvicorn` and
`opentelemetry-instrument` processes resolve from the PEX. When telemetry is
enabled, the launcher enters Uvicorn through the packaged Python interpreter
instead of its PEX-generated console script. This preserves OpenTelemetry's
`PYTHONPATH` bootstrap, which the console script's `-E` shebang would otherwise
discard. OTel environment and
fleet identity are merged before `server.main` imports. Uvicorn always receives
`--workers 1`.

On Linux the launcher then stays resident as Uvicorn's supervisor
(`server/supervisor.py`) instead of exec'ing it, because attendee agents run as
the same Unix user and can signal the server:

- If Uvicorn exits when the platform didn't ask, it restarts after 1 s, backing
  off to 16 s for repeated quick deaths. After 5 restarts in 10 minutes the
  launcher exits non-zero so the platform marks the app crashed.
- Stop signals are read with `sigtimedwait`. One sent from inside the app's own
  process tree is ignored and recorded as `server.kill_blocked`; any other sender
  is the platform, and is forwarded to Uvicorn as SIGTERM within the 15 s Apps
  shutdown window.
- The supervisor holds the platform's OAuth secret to restart the server, so it
  runs only when it can make itself non-dumpable. Otherwise, off Linux, or with
  `WORKSHOP_SUPERVISOR=0`, the launcher execs Uvicorn as before.

The new server process records `server.restarted` and `server.kill_blocked` in
the WT OTel logs.

## Release manifest

`release-manifest.json` is deliberately flat so Control Tower can validate it
before downloading the artifact. It records:

- format and artifact type/name
- immutable 40-character WT Git SHA and release tag
- CPython ABI and target platform
- entry point
- artifact byte size and SHA-256
- logical-content SHA-256 and file count
- minimum Control Tower package-contract version

The logical digest covers each tracked path, normalized Git executable mode,
and exact bytes in sorted order. Normalizing to `0644` or `0755` keeps checkout
umasks from changing a release. The same digest and per-file hashes are embedded
in the PEX.
Control Tower can vendor `tests/fixtures/wt-release-manifest-v1.json` as the
versioned cross-repository parser contract.

## CI and release gate

Every pull request:

1. validates the uv lock and source-deployment requirements exports;
2. runs the full source suite on Python 3.11 and 3.12;
3. builds the PEX twice and compares both artifacts and manifests byte-for-byte;
4. starts the real packaged entry point in a clean Python 3.11 container with
   networking disabled;
5. checks `/healthz`, `/readyz`, and `/api/agents`;
6. verifies every bundled file against the checkout and embedded manifest;
7. runs mocked Claude and Codex lifecycles, verifies the one-session conflict,
   and runs the retained Omnigent lifecycle;
8. records cold startup and artifact size in `release-benchmark.json`.

A `v*` tag repeats those gates before publishing the PEX and manifest as GitHub
release assets. Control Tower must verify the pinned manifest digest, artifact
size, artifact SHA-256, ABI/platform, and minimum contract version before
staging. Source deployment remains the rollback path for one release window.

Reference: [Databricks workspace file limits](https://docs.databricks.com/aws/en/files/workspace#file-size-limit).
