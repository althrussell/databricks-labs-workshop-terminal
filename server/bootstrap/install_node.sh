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

current_major() {
  command -v node >/dev/null 2>&1 || return 1
  node --version 2>/dev/null | sed 's/^v//' | cut -d. -f1
}

if [ -x "$INSTALL_DIR/bin/node" ]; then
  echo "Node $("$INSTALL_DIR/bin/node" --version) already installed at ${INSTALL_DIR}; skipping."
  exit 0
fi

if maj="$(current_major)"; then
  if [ "${maj:-0}" -ge "$NODE_MIN_MAJOR" ] 2>/dev/null; then
    echo "Node v$(node --version | sed 's/^v//') already satisfies >= v${NODE_MIN_MAJOR}; skipping install."
    exit 0
  fi
fi

arch="$(uname -m)"
case "$arch" in
  x86_64|amd64) node_arch="linux-x64" ;;
  aarch64|arm64) node_arch="linux-arm64" ;;
  *) echo "ERROR: unsupported architecture '${arch}'." >&2; exit 1 ;;
esac

url="${NODE_DIST_MIRROR}/v${NODE_VERSION}/node-v${NODE_VERSION}-${node_arch}.tar.xz"
echo "Downloading ${url}"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
curl -fsSL "$url" -o "$tmp/node.tar.xz"
tar -xJf "$tmp/node.tar.xz" -C "$INSTALL_DIR" --strip-components=1
echo "Installed Node $("$INSTALL_DIR/bin/node" --version) to ${INSTALL_DIR}"
