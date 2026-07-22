#!/bin/bash
# Install a pinned Node 22 LTS into the shared prefix when the runtime's
# node is missing or older than NODE_MIN_MAJOR. Idempotent.
#
#   $1                 install prefix (e.g. /app/python/source_code/data/shared)
#   NODE_VERSION       exact version to install (default pinned below)
#   NODE_MIN_MAJOR     minimum acceptable major already on PATH (default 22)
#   NODE_DIST_MIRROR   replacement for https://nodejs.org/dist (same path tail)

set -euo pipefail

INSTALL_DIR="${1:?usage: install_node.sh <prefix>}"
mkdir -p "$INSTALL_DIR/bin"

NODE_MIN_MAJOR="${NODE_MIN_MAJOR:-22}"
NODE_VERSION="${NODE_VERSION:-22.14.0}"
NODE_DIST_MIRROR="${NODE_DIST_MIRROR:-https://nodejs.org/dist}"
NODE_DIST_MIRROR="${NODE_DIST_MIRROR%/}"
NODE_ARCHIVE_PATH="${NODE_ARCHIVE_PATH:-}"
NODE_ARCHIVE_SHA256="${NODE_ARCHIVE_SHA256:-}"

if [ -z "$NODE_ARCHIVE_PATH" ] || [ -z "$NODE_ARCHIVE_SHA256" ]; then
  echo "ERROR: reviewed Node artifact path and SHA-256 are required." >&2
  exit 1
fi

if [ -x "$INSTALL_DIR/bin/node" ]; then
  rm -f "$INSTALL_DIR/bin/node" "$INSTALL_DIR/bin/npm" "$INSTALL_DIR/bin/npx"
fi

arch="$(uname -m)"
case "$arch" in
  x86_64|amd64) node_arch="linux-x64" ;;
  aarch64|arm64) node_arch="linux-arm64" ;;
  *) echo "ERROR: unsupported architecture '${arch}'." >&2; exit 1 ;;
esac

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
cp "$NODE_ARCHIVE_PATH" "$tmp/node.tar.xz"
actual_sha="$(sha256sum "$tmp/node.tar.xz" | awk '{print $1}')"
if [ "$actual_sha" != "$NODE_ARCHIVE_SHA256" ]; then
  echo "ERROR: Node archive SHA-256 mismatch." >&2
  exit 1
fi
tar -xJf "$tmp/node.tar.xz" -C "$INSTALL_DIR" --strip-components=1
echo "Installed Node $("$INSTALL_DIR/bin/node" --version) to ${INSTALL_DIR}"
