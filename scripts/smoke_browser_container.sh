#!/usr/bin/env bash
# Prove the prepared browser runs in clean Linux with no system packages (W3).
#
#   scripts/smoke_browser_container.sh [dist] [out-dir]
#
# Stages the pinned browser, its runtime packages and a standalone Python on
# the host, then runs the release PEX inside each image with the network
# disabled: the real bootstrap browser step, then a real page render. Ubuntu
# 22.04 is the Databricks Apps base (glibc 2.35); the Python image is the one
# the release smoke uses. Screenshots and a JSON report per image land in
# out-dir.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DIST="${1:-${ROOT}/dist}"
DIST="$(cd "${DIST}" && pwd)"
test -f "${DIST}/workshop-terminal.pex"
OUT="${2:-${DIST}/browser-smoke}"
mkdir -p "${OUT}"
OUT="$(cd "${OUT}" && pwd)"
WORK="$(mktemp -d)"
trap 'rm -rf "${WORK}"' EXIT

python3 "${ROOT}/scripts/smoke_browser.py" stage "${WORK}/staged"

pull() {
  local candidate
  for candidate in "$@"; do
    if docker image inspect "${candidate}" >/dev/null 2>&1 || docker pull --platform linux/amd64 "${candidate}" >/dev/null; then
      docker image inspect --format '{{.Id}}' "${candidate}"
      return 0
    fi
    echo "Unable to retrieve ${candidate}; trying the next configured registry." >&2
  done
  return 1
}

smoke() {
  local label="$1" python="$2"
  shift 2
  local image
  image="$(pull "$@")"
  rm -rf "${WORK}/${label}"
  cp -R "${WORK}/staged" "${WORK}/${label}"
  docker run --rm --pull never --platform linux/amd64 --network none \
    --volume "${ROOT}:/source:ro" \
    --volume "${DIST}:/release:ro" \
    --volume "${WORK}/${label}:/work" \
    --volume "${OUT}:/out" \
    --env SMOKE_IMAGE="$1" \
    --env PEX_ROOT=/tmp/pex \
    --env PEX_INTERPRETER=1 \
    "${image}" \
    "${python}" /release/workshop-terminal.pex /source/scripts/smoke_browser.py run /work "/out/${label}"
}

smoke ubuntu-22.04 /work/python/bin/python3.11 \
  public.ecr.aws/docker/library/ubuntu:22.04 docker.io/library/ubuntu:22.04 mirror.gcr.io/library/ubuntu:22.04
smoke python-3.11-slim-bookworm python \
  public.ecr.aws/docker/library/python:3.11-slim-bookworm docker.io/library/python:3.11-slim-bookworm \
  mirror.gcr.io/library/python:3.11-slim-bookworm
echo "browser smoke passed; screenshots in ${OUT}"
