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
# Resolve the official Python image before the network-disabled checks. Public
# registries can rate-limit shared runners, so try each independent mirror once.
if [[ -n "${WT_RELEASE_SMOKE_IMAGE:-}" ]]; then
  SMOKE_IMAGES=("${WT_RELEASE_SMOKE_IMAGE}")
else
  SMOKE_IMAGES=(
    "public.ecr.aws/docker/library/python:3.11-slim-bookworm"
    "docker.io/library/python:3.11-slim-bookworm"
    "mirror.gcr.io/library/python:3.11-slim-bookworm"
  )
fi

test -f "${ARTIFACT}"
test -f "${MANIFEST}"

SMOKE_IMAGE_ID=""
for candidate in "${SMOKE_IMAGES[@]}"; do
  if docker image inspect "${candidate}" >/dev/null 2>&1 || docker pull "${candidate}"; then
    SMOKE_IMAGE_ID="$(docker image inspect --format '{{.Id}}' "${candidate}")"
    break
  fi
  echo "Unable to retrieve ${candidate}; trying the next configured registry." >&2
done
if [[ -z "${SMOKE_IMAGE_ID}" ]]; then
  echo "Unable to retrieve the Python smoke image from any configured registry." >&2
  exit 1
fi

docker run --rm --pull never --network none \
  --volume "${ROOT}:/source:ro" \
  --volume "${DIST}:/release" \
  "${SMOKE_IMAGE_ID}" \
  python /source/scripts/benchmark_release.py \
    /release/workshop-terminal.pex \
    --output /release/release-benchmark.json

docker run --rm --pull never --network none \
  --volume "${ROOT}:/source:ro" \
  --volume "${DIST}:/release:ro" \
  --env PEX_ROOT=/tmp/pex \
  --env WT_RELEASE_ARTIFACT=/release/workshop-terminal.pex \
  --env WT_RELEASE_MANIFEST=/release/release-manifest.json \
  --env WT_SOURCE_ROOT=/source \
  "${SMOKE_IMAGE_ID}" \
  /bin/sh -c 'PEX_INTERPRETER=1 /release/workshop-terminal.pex /source/scripts/smoke_release.py'

docker run --rm --pull never --network none \
  --volume "${ROOT}:/source:ro" \
  --volume "${DIST}:/release:ro" \
  --env WT_RELEASE_ARTIFACT=/release/workshop-terminal.pex \
  "${SMOKE_IMAGE_ID}" \
  python /source/scripts/smoke_otel_entrypoint.py
