#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DIST="${1:-${ROOT}/dist}"
if [[ "${DIST}" != /* ]]; then
  DIST="${ROOT}/${DIST#./}"
fi
DIST="$(cd "${DIST}" && pwd)"
ARTIFACT="${DIST}/workshop-terminal.pex"
MANIFEST="${DIST}/release-manifest.json"

test -f "${ARTIFACT}"
test -f "${MANIFEST}"

docker run --rm --network none \
  --volume "${ROOT}:/source:ro" \
  --volume "${DIST}:/release" \
  python:3.11-slim-bookworm \
  python /source/scripts/benchmark_release.py \
    /release/workshop-terminal.pex \
    --output /release/release-benchmark.json

docker run --rm --network none \
  --volume "${ROOT}:/source:ro" \
  --volume "${DIST}:/release:ro" \
  --env PEX_ROOT=/tmp/pex \
  --env WT_RELEASE_ARTIFACT=/release/workshop-terminal.pex \
  --env WT_RELEASE_MANIFEST=/release/release-manifest.json \
  --env WT_SOURCE_ROOT=/source \
  python:3.11-slim-bookworm \
  /bin/sh -c 'PEX_INTERPRETER=1 /release/workshop-terminal.pex /source/scripts/smoke_release.py'
