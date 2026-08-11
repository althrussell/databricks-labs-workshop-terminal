#!/usr/bin/env python3
"""Regenerate assets/artifacts/manifest.json from the pinned versions in install.py.

The manifest is repo-owned: it fixes the version *and* the SHA-256 of every
artifact the boot path installs, so a Workshop Terminal deployed by any means
installs the same reviewed software. This script derives the checksums from the
upstream sources rather than trusting whatever a reviewer pasted in.

    python3 scripts/build_artifact_manifest.py --check     # verify, change nothing
    python3 scripts/build_artifact_manifest.py --write     # rewrite the manifest

Needs outbound access to nodejs.org, github.com, downloads.claude.ai, and an npm
registry. Run it from a network that reaches all four; the manifest it produces
is what attendees install, so partial regeneration is not accepted. npm is read
through whatever registry npm is configured with -- on a Databricks laptop that
is the managed proxy, since registry.npmjs.org does not resolve there -- while
the recorded source URL stays canonical for boot to fetch.

Sources that publish a checksum of their own (Node, the Databricks CLI, the
Claude release manifest) are cross-checked against it instead of being taken on
trust from a local download.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import subprocess
import sys
import tarfile
import tempfile
import urllib.request

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

from server.bootstrap import install  # noqa: E402
from server.bootstrap.artifacts import DEFAULT_MANIFEST_PATH  # noqa: E402

NODE_DIST = f"https://nodejs.org/dist/v{install.NODE_VERSION}"
CLAUDE_RELEASES = "https://downloads.claude.ai/claude-code-releases"
CLI_RELEASE = (
    "https://github.com/databricks/cli/releases/download/"
    f"v{install.DATABRICKS_CLI_VERSION}"
)
NPM_PUBLIC_REGISTRY = "https://registry.npmjs.org"
CODEX_PACKAGE = "@openai/codex"
PI_PACKAGE = "@earendil-works/pi-coding-agent"
UV_VERSION = "0.12.0"
PYTHON_RELEASE = "20260728"
PYTHON_VERSION = "3.12.13"
CODEX_NATIVE_EXECUTABLE = "vendor/x86_64-unknown-linux-musl/bin/codex"


def _fetch(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=300) as response:
        return response.read()


def _fetch_text(url: str) -> str:
    return _fetch(url).decode("utf-8")


def _npm_base() -> str:
    """The registry to download from, which is not the registry we record.

    Databricks laptops reach npm through a managed proxy and cannot resolve
    ``registry.npmjs.org`` at all, so this script fetches from whatever npm is
    configured to use. The manifest still records the canonical public URL,
    because boot resolves ``source`` from a container with no proxy access.
    """
    # npm treats npm_config_* as case-insensitive, and CI sets the lower form.
    configured = (
        os.environ.get("NPM_CONFIG_REGISTRY")
        or os.environ.get("npm_config_registry")
        or ""
    ).strip()
    if not configured:
        try:
            configured = subprocess.run(
                ["npm", "config", "get", "registry"],
                capture_output=True,
                text=True,
                timeout=60,
                check=True,
            ).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            configured = ""
    if configured in {"", "null", "undefined"}:
        configured = NPM_PUBLIC_REGISTRY
    return configured.rstrip("/")


def _download_sha256(url: str) -> tuple[str, str]:
    """Return (sha256, local path) for a URL, streaming to a temp file."""
    digest = hashlib.sha256()
    fd, path = tempfile.mkstemp(prefix="manifest-artifact-")
    with os.fdopen(fd, "wb") as output, urllib.request.urlopen(
        url, timeout=600
    ) as response:
        for chunk in iter(lambda: response.read(1024 * 1024), b""):
            digest.update(chunk)
            output.write(chunk)
    return digest.hexdigest(), path


def _checksum_from_sums(text: str, filename: str) -> str:
    for line in text.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1].lstrip("*") == filename:
            return parts[0]
    raise SystemExit(f"{filename} is absent from the published checksum list")


def _verified(url: str, expected: str, label: str) -> str:
    actual, path = _download_sha256(url)
    os.unlink(path)
    if actual != expected:
        raise SystemExit(f"{label}: published {expected}, downloaded {actual}")
    return actual


def _npm_entry(
    package: str,
    packument: dict,
    version: str,
    basename: str,
    executable: str | None = None,
) -> dict:
    """Checksum an npm tarball, cross-checked against npm's own attestation.

    npm publishes ``dist.integrity`` (SHA-512) for every version. Verifying the
    download against it means a stale mirror cannot slip different bytes past
    this script, which is the only reason a mirror is usable at all. When the
    mirror serves the packument too it is attesting its own bytes, so this
    narrows to an internal-consistency check; the standing guarantee is that
    ``source`` stays canonical and boot re-verifies sha256 against a manifest a
    human reviewed.
    """
    dist = packument["versions"][version]["dist"]
    url = f"{NPM_PUBLIC_REGISTRY}/{package}/-/{basename}-{version}.tgz"
    download = str(dist["tarball"])
    checksum, path = _download_sha256(download)
    try:
        algorithm, encoded = str(dist["integrity"]).split("-", 1)
        expected = base64.b64decode(encoded).hex()
        digest = hashlib.new(algorithm)
        with open(path, "rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        if digest.hexdigest() != expected:
            raise SystemExit(f"{download} does not match its published {algorithm}")
        entry = {"version": version, "source": url, "sha256": checksum}
        if executable:
            with tarfile.open(path) as archive:
                member = archive.extractfile(f"package/{executable}")
                if member is None:
                    raise SystemExit(f"{download} has no {executable}")
                entry["executable_sha256"] = hashlib.sha256(member.read()).hexdigest()
    finally:
        os.unlink(path)
    return entry


def _assert_pi_node_floor(packument: dict) -> None:
    """Fail when the pinned Node cannot run the pinned Pi.

    Pi declares ``engines.node``, and it has moved: 0.79.0 raised the floor to
    22.19.0, which the previously pinned Node 22.14.0 did not meet. npm only
    *warns* on EBADENGINE, so without this check a Pi bump would install
    cleanly at boot and then fail in front of an attendee.
    """
    version = install.PI_VERSION
    try:
        release = packument["versions"][version]
    except KeyError:
        raise SystemExit(f"pi {version} is not published on npm") from None
    if version < install.PI_MIN_VERSION:
        raise SystemExit(
            f"pi {version} is below Omnigent's {install.PI_MIN_VERSION} floor"
        )
    required = str((release.get("engines") or {}).get("node") or "").strip()
    floor = required.lstrip(">=v ").split(" ")[0].split("||")[0].strip()
    if not floor:
        return
    parse = lambda value: tuple(  # noqa: E731 - local, single use
        int(part) for part in re.findall(r"\d+", value)[:3]
    )
    if parse(install.NODE_VERSION) < parse(floor):
        raise SystemExit(
            f"pi {version} requires node {required}, but NODE_VERSION is "
            f"{install.NODE_VERSION}"
        )


def build() -> dict:
    node_sums = _fetch_text(f"{NODE_DIST}/SHASUMS256.txt")
    cli_sums = _fetch_text(
        f"{CLI_RELEASE}/databricks_cli_{install.DATABRICKS_CLI_VERSION}_SHA256SUMS"
    )
    claude_manifest = json.loads(
        _fetch_text(f"{CLAUDE_RELEASES}/{install.CLAUDE_VERSION}/manifest.json")
    )
    claude_linux = claude_manifest["platforms"]["linux-x64"]["checksum"]
    npm = _npm_base()
    codex_packument = json.loads(_fetch_text(f"{npm}/{CODEX_PACKAGE}"))
    pi_packument = json.loads(_fetch_text(f"{npm}/{PI_PACKAGE}"))
    _assert_pi_node_floor(pi_packument)

    node_x64 = f"node-v{install.NODE_VERSION}-linux-x64.tar.xz"
    node_arm64 = f"node-v{install.NODE_VERSION}-linux-arm64.tar.xz"
    cli_archive = (
        f"databricks_cli_{install.DATABRICKS_CLI_VERSION}_linux_amd64.zip"
    )
    python_archive = (
        f"cpython-{PYTHON_VERSION}+{PYTHON_RELEASE}"
        "-x86_64-unknown-linux-gnu-install_only.tar.gz"
    )
    uv_archive = "uv-x86_64-unknown-linux-gnu.tar.gz"

    artifacts = {
        "node_linux_x64": {
            "version": install.NODE_VERSION,
            "source": f"{NODE_DIST}/{node_x64}",
            "sha256": _checksum_from_sums(node_sums, node_x64),
        },
        "node_linux_arm64": {
            "version": install.NODE_VERSION,
            "source": f"{NODE_DIST}/{node_arm64}",
            "sha256": _checksum_from_sums(node_sums, node_arm64),
        },
        "tmux_linux_x64": {
            "version": "3.6b",
            "source": install.TMUX_STATIC_URL,
            "sha256": install.TMUX_STATIC_SHA256,
        },
        "claude_installer": {
            # _vendored_script_entry swaps source for the vendored filename and
            # keeps this URL as `upstream`, so it must be the URL, not the file.
            "version": install.CLAUDE_VERSION,
            "source": install.CLAUDE_INSTALLER_URL,
            "sha256": "",
        },
        "claude_binary": {
            "version": install.CLAUDE_VERSION,
            "source": (
                f"{CLAUDE_RELEASES}/{install.CLAUDE_VERSION}/linux-x64/claude"
            ),
            "sha256": claude_linux,
        },
        "codex_npm_launcher_package": _npm_entry(
            CODEX_PACKAGE, codex_packument, install.CODEX_VERSION, "codex"
        ),
        "codex_native_package_linux_x64": _npm_entry(
            CODEX_PACKAGE,
            codex_packument,
            f"{install.CODEX_VERSION}-linux-x64",
            "codex",
            CODEX_NATIVE_EXECUTABLE,
        ),
        "pi_npm_package": _npm_entry(
            PI_PACKAGE, pi_packument, install.PI_VERSION, "pi-coding-agent"
        ),
        "databricks_cli_installer": {
            "version": install.DATABRICKS_CLI_VERSION,
            "source": install._databricks_installer_url(),
            "sha256": "",
        },
        "databricks_cli_archive_linux_x64": {
            "version": install.DATABRICKS_CLI_VERSION,
            "source": f"{CLI_RELEASE}/{cli_archive}",
            "sha256": _checksum_from_sums(cli_sums, cli_archive),
        },
        "uv_binary": {
            "version": UV_VERSION,
            "source": (
                f"https://github.com/astral-sh/uv/releases/download/"
                f"{UV_VERSION}/{uv_archive}"
            ),
            "sha256": _checksum_from_sums(
                _fetch_text(
                    f"https://github.com/astral-sh/uv/releases/download/"
                    f"{UV_VERSION}/{uv_archive}.sha256"
                ),
                uv_archive,
            ),
            "kind": "archive",
            "executable_relative_path": "uv-x86_64-unknown-linux-gnu/uv",
        },
        "python_3_12_runtime": {
            "version": PYTHON_VERSION,
            "source": (
                "https://github.com/astral-sh/python-build-standalone/releases/"
                f"download/{PYTHON_RELEASE}/{python_archive}"
            ),
            "sha256": _checksum_from_sums(
                _fetch_text(
                    "https://github.com/astral-sh/python-build-standalone/"
                    f"releases/download/{PYTHON_RELEASE}/SHA256SUMS"
                ),
                python_archive,
            ),
            "kind": "archive",
            "executable_relative_path": "python/bin/python3.12",
        },
        "omnigent_lock": _lock_entry(),
        "databricks_agent_skills": _skills_entry(),
    }

    # Vendored scripts: checksum what is committed rather than the live URL,
    # whose content is not version-addressed and would drift under us.
    for name in ("claude_installer", "databricks_cli_installer"):
        artifacts[name] = _vendored_script_entry(name, artifacts[name])

    # Cross-check the two large binaries whose publisher checksum we trust.
    _verified(
        artifacts["node_linux_x64"]["source"],
        artifacts["node_linux_x64"]["sha256"],
        "node linux-x64",
    )
    _verified(
        artifacts["databricks_cli_archive_linux_x64"]["source"],
        artifacts["databricks_cli_archive_linux_x64"]["sha256"],
        "databricks cli linux-amd64",
    )
    return {"schema_version": 1, "reviewed": True, "artifacts": artifacts}


def _vendored_script_entry(name: str, entry: dict) -> dict:
    filename = {
        "claude_installer": "claude-code-bootstrap.sh",
        "databricks_cli_installer": "databricks-cli-install.sh",
    }[name]
    path = os.path.join(REPO_ROOT, "assets", "artifacts", filename)
    if not os.path.isfile(path):
        raise SystemExit(f"vendored {filename} is missing; stage it first")
    return {
        "version": entry["version"],
        "source": filename,
        "sha256": hashlib.sha256(open(path, "rb").read()).hexdigest(),
        "upstream": entry["source"],
    }


def _lock_entry() -> dict:
    filename = f"omnigent-{install.OMNIGENT_VERSION}.lock"
    path = os.path.join(REPO_ROOT, "assets", "artifacts", filename)
    if not os.path.isfile(path):
        raise SystemExit(f"{filename} is missing; export it from deploy/omnigent-app")
    checksum = hashlib.sha256(open(path, "rb").read()).hexdigest()
    return {
        "version": install.OMNIGENT_VERSION,
        "source": filename,
        "sha256": checksum,
        "lock_sha256": checksum,
    }


def _skills_entry() -> dict:
    """Resolve the pinned skills ref to its commit and installed content digest.

    The digest must be computed exactly the way bootstrap computes it, or the
    install would reject its own reviewed manifest, so this calls the same
    ``directory_checksum`` over the same subdirectory selection.
    """
    from server.bootstrap.artifacts import directory_checksum

    with tempfile.TemporaryDirectory(prefix="manifest-skills-") as clone:
        subprocess.run(
            [
                "git", "clone", "--quiet", "--depth", "1",
                "--branch", install.SKILLS_REF, install.SKILLS_REPO, clone,
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=600,
        )
        commit = subprocess.run(
            ["git", "-C", clone, "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
            timeout=60,
        ).stdout.strip()
        upstream = os.path.join(clone, install.SKILLS_UPSTREAM_DIR)
        names = {
            name
            for name in os.listdir(upstream)
            if os.path.isdir(os.path.join(upstream, name))
        }
        if not names:
            raise SystemExit(
                f"{install.SKILLS_UPSTREAM_DIR}/ is empty at {install.SKILLS_REF}"
            )
        return {
            "version": install.SKILLS_REF,
            "source": install.SKILLS_REPO,
            "commit": commit,
            "content_sha256": directory_checksum(upstream, names),
        }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true", help="rewrite the manifest")
    parser.add_argument(
        "--check",
        action="store_true",
        help="fail when the committed manifest differs from upstream",
    )
    args = parser.parse_args()
    if not args.write and not args.check:
        parser.error("pass --write or --check")

    built = build()
    built["//"] = (
        "GENERATED FILE. Run `python3 scripts/build_artifact_manifest.py --write` "
        "to regenerate. See docs/artifact-manifest.md."
    )
    rendered = json.dumps(built, indent=2, sort_keys=True) + "\n"
    if args.write:
        with open(DEFAULT_MANIFEST_PATH, "w", encoding="utf-8") as handle:
            handle.write(rendered)
        print(f"wrote {DEFAULT_MANIFEST_PATH}")
        return 0
    with open(DEFAULT_MANIFEST_PATH, encoding="utf-8") as handle:
        current = handle.read()
    if current != rendered:
        print("committed manifest differs from upstream; run --write", file=sys.stderr)
        return 1
    print("manifest matches upstream")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
