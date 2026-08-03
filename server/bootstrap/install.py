"""Boot-time installer orchestration with per-agent readiness.

CLIs are installed once into the shared prefix (one copy on disk for all
users). The UI is served immediately; launch buttons enable as each binary
lands. Everything is idempotent so Control Tower redeploys are cheap.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import logging
import os
import platform
import re
import shutil
import subprocess
import tarfile
import tempfile
import threading
import time
import fcntl
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor

from .. import config
from .artifacts import (
    ArtifactManifest,
    ArtifactManifestError,
    directory_checksum as _directory_checksum,
)
from .codex_artifacts import install_native_alias, validate_codex_tarballs

logger = logging.getLogger(__name__)

# Pinned versions — bump deliberately per release.
CLAUDE_VERSION = os.environ.get("CLAUDE_CODE_VERSION", "2.1.216").strip()
CODEX_VERSION = os.environ.get("CODEX_CLI_VERSION", "0.144.6").strip()
DATABRICKS_CLI_VERSION = os.environ.get("DATABRICKS_CLI_VERSION", "1.8.0").strip()
OMNIGENT_VERSION = os.environ.get("OMNIGENT_VERSION", "0.7.0").strip()
OMNIGENT_PROTOCOL_VERSION = "0.7.0"
# Node 24 is the active LTS line; Node 22 is maintenance-only. Pi additionally
# declares ``engines.node >= 22.19.0``, so the old 22.14.0 pin could not have
# run it at all.
NODE_VERSION = os.environ.get("NODE_VERSION", "24.18.1").strip()
# Pi is Omnigent's any-gateway-model harness: unlike claude-sdk and
# codex-native, whose family guards reject a foreign model id, pi will drive
# whatever the gateway serves. That makes it the only way to put GLM, Luna or
# Qwen behind a Polly brain, and it gives those models a real terminal an
# attendee can take over. Omnigent's own floor is 0.79.0, for the
# non-interactive ``--approve`` flag (``onboarding/harness_install.py``).
PI_VERSION = os.environ.get("PI_CLI_VERSION", "0.83.0").strip()
PI_MIN_VERSION = "0.79.0"
# Binaries the prewarm proof inspects and reports, but does not let veto the
# aggregate. ``reusable`` hard-gates /readyz through the ``supply_chain`` check,
# and a harness we deliberately keep out of ``required_steps`` and out of the
# ``omnigent`` ready bit must not become fatal by that back door: an attendee
# without pi loses the gateway-only Polly variants, not the workshop. The per
# binary entry still says so, so a prewarm that silently stopped shipping pi is
# visible rather than implied.
ADVISORY_BINARIES = frozenset({"pi"})
CLAUDE_INSTALLER_URL = os.environ.get(
    "CLAUDE_INSTALLER_URL", "https://claude.ai/install.sh"
)
# Omnigent installs from the reviewed manifest only: a checksum-verified uv
# binary, a pinned Python 3.12 archive, and a fully hashed transitive lock.
# Omnigent's claude/codex wrappers hard-require tmux and the Apps runtime has
# no package manager — install a fully static musl build into the shared bin.
TMUX_STATIC_URL = os.environ.get("TMUX_STATIC_URL", "").strip() or (
    "https://github.com/mjakob-gh/build-static-tmux/releases/download/v3.6b/tmux.linux-amd64.stripped.gz"
)
TMUX_STATIC_SHA256 = os.environ.get("TMUX_STATIC_SHA256", "").strip() or (
    "a23e56e9913d610c31f2893a1c9c669a73cb8bb2b8ded1180f6572bb55e52ca5"
)
# Databricks agent skills are overlaid from databricks-agent-skills at boot; the
# vendored copy in assets/skills is the offline fallback (and carries the workflow
# skills that do not come from upstream). This replaced the deprecated ai-dev-kit,
# whose databricks-skills/ directory no longer exists upstream.
#
# The ref is pinned to a release tag rather than a branch, because a reviewed
# manifest binds the exact commit and content digest: an event installs a known
# skills version, not whatever happens to be on the tip at boot.
SKILLS_REPO = os.environ.get(
    "SKILLS_REPO", "https://github.com/databricks/databricks-agent-skills.git"
)
SKILLS_REF = os.environ.get("SKILLS_REF", "v0.2.10").strip() or "v0.2.10"
# The manifest and readiness key for the skills artifact.
SKILLS_ARTIFACT = "databricks_agent_skills"
# The directory inside the upstream repository that holds one subdirectory per
# skill. Each carries SKILL.md for Claude and agents/openai.yaml for Codex.
SKILLS_UPSTREAM_DIR = "skills"
# Where the upstream clone and its stamp live under the shared prefix.
SKILLS_CLONE_DIR = "databricks-agent-skills"
_ASSETS_SKILLS = os.path.normpath(
    os.path.join(os.path.dirname(__file__), "..", "..", "assets", "skills")
)

_state_lock = threading.Lock()
_state: dict[str, dict] = {}
# A step stops here. ``degraded`` means the step produced something usable
# without meeting its reviewed contract -- the skills vendored fallback -- so it
# ends the boot but never satisfies readiness.
TERMINAL_STATUSES = frozenset({"complete", "error", "degraded"})


def validate_remote_compatibility() -> None:
    """Fail fast when remote mode cannot guarantee the 0.7.0 protocol."""
    if not config.omnigent_app_url():
        return
    if not config.omnigent_enabled():
        raise ValueError("OMNIGENT_APP_URL requires OMNIGENT_ENABLED=true")
    if OMNIGENT_VERSION != OMNIGENT_PROTOCOL_VERSION:
        raise ValueError(
            "Remote Omnigent requires an exact protocol-compatible 0.7.0 "
            f"install, got {OMNIGENT_VERSION!r}"
        )


def _artifact_contract() -> ArtifactManifest:
    return ArtifactManifest.from_path(
        os.environ.get("ARTIFACT_MANIFEST_PATH", "").strip()
    )


def _verified_artifact(name: str) -> tuple[str, dict]:
    contract = _artifact_contract()
    entry = contract.entry(name)
    staging = os.path.join(config.shared_prefix(), "artifacts")
    return contract.verified_local_path(name, staging_dir=staging), entry


def _extracted_artifact_executable(name: str) -> tuple[str, dict]:
    """Verify an archive artifact, extract it once, return its executable.

    uv and the Python runtime are published only as archives, so the checksum
    covers the archive and extraction is content-addressed by it: a bumped
    version lands in a new directory and the old one is simply unused.
    """
    archive_path, entry = _verified_artifact(name)
    root = os.path.join(
        config.shared_prefix(), "artifacts", f"{name}-{entry['sha256'][:16]}"
    )
    executable = os.path.normpath(
        os.path.join(root, entry["executable_relative_path"])
    )
    if not executable.startswith(os.path.abspath(root) + os.sep):
        raise RuntimeError(f"{name} executable path escapes the archive root")
    if not os.path.isfile(executable):
        os.makedirs(os.path.dirname(root), exist_ok=True)
        staged = tempfile.mkdtemp(prefix=f".{name}-", dir=os.path.dirname(root))
        try:
            shutil.unpack_archive(archive_path, staged)
            if os.path.isdir(root):
                shutil.rmtree(root, ignore_errors=True)
            os.replace(staged, root)
        except Exception:
            shutil.rmtree(staged, ignore_errors=True)
            raise
        if not os.path.isfile(executable):
            raise RuntimeError(f"{name} archive has no {entry['executable_relative_path']}")
    os.chmod(executable, 0o755)
    return executable, entry


def _artifact_binary_stamp_path(name: str) -> str:
    return os.path.join(config.shared_prefix(), f"{name}.install.json")


def _write_artifact_binary_stamp(
    name: str, entry: dict, binary_path: str
) -> str:
    checksum = _file_checksum(binary_path)
    if not checksum:
        raise RuntimeError(f"{name} installed binary checksum unavailable")
    _write_json_atomic(
        _artifact_binary_stamp_path(name),
        {
            "artifact_sha256": entry["sha256"],
            "binary_sha256": checksum,
        },
    )
    return checksum


def _artifact_binary_reusable(name: str, entry: dict, binary_path: str) -> bool:
    stamp = _read_json(_artifact_binary_stamp_path(name))
    actual = _file_checksum(binary_path)
    return bool(
        actual
        and stamp.get("artifact_sha256") == entry["sha256"]
        and stamp.get("binary_sha256") == actual
    )


def _set(
    step: str,
    status: str,
    error: str | None = None,
    *,
    expected_version: str | None = None,
    actual_version: str | None = None,
    release_source: str | None = None,
    resolved_commit: str | None = None,
    source: str | None = None,
    expected_checksum: str | None = None,
    actual_checksum: str | None = None,
    clear_release: bool = False,
) -> None:
    now = time.time()
    with _state_lock:
        previous = _state.get(step, {})
        reset_run = status == "pending"
        started_at = (
            now
            if status == "running"
            else (None if reset_run else previous.get("started_at"))
        )
        completed_at = now if status in TERMINAL_STATUSES else None
        _state[step] = {
            "status": status,
            "error": error,
            "at": now,
            "source": (
                source
                if source is not None
                else (None if reset_run else previous.get("source"))
            ),
            "started_at": started_at,
            "completed_at": completed_at,
            "duration_ms": (
                max(0, round((completed_at - started_at) * 1000))
                if isinstance(started_at, (int, float))
                and isinstance(completed_at, (int, float))
                else None
            ),
            "expected_version": (
                expected_version
                if expected_version is not None
                else previous.get("expected_version")
            ),
            "actual_version": (
                actual_version
                if actual_version is not None
                else (None if clear_release else previous.get("actual_version"))
            ),
            "release_source": (
                release_source
                if release_source is not None
                else (None if clear_release else previous.get("release_source"))
            ),
            "resolved_commit": (
                resolved_commit
                if resolved_commit is not None
                else (None if clear_release else previous.get("resolved_commit"))
            ),
            "expected_checksum": (
                expected_checksum
                if expected_checksum is not None
                else previous.get("expected_checksum")
            ),
            "actual_checksum": (
                actual_checksum
                if actual_checksum is not None
                else (None if clear_release else previous.get("actual_checksum"))
            ),
        }


def _release_specs() -> dict[str, tuple[bool, str]]:
    return {
        "claude": (True, CLAUDE_VERSION),
        "codex": (True, CODEX_VERSION),
        "databricks": (True, DATABRICKS_CLI_VERSION),
        # Pi is only reachable as an Omnigent harness, so it follows Omnigent's
        # enablement rather than being installed unconditionally.
        "pi": (config.omnigent_enabled(), PI_VERSION),
        "omnigent": (config.omnigent_enabled(), OMNIGENT_VERSION),
    }


def status() -> dict:
    from .artifacts import status as artifact_manifest_status

    with _state_lock:
        steps = dict(_state)
    ready = {
        "bash": True,
        "claude": steps.get("claude", {}).get("status") == "complete",
        "codex": steps.get("codex", {}).get("status") == "complete",
        # Reported so an operator can see the harness landed, but deliberately
        # not folded into ``omnigent`` below: a missing pi costs an attendee the
        # cheap-model Polly variants, not the whole meta-harness.
        "pi": steps.get("pi", {}).get("status") == "complete",
        # Both the meta-harness and tmux (its terminal backend) must land
        # before any omnigent session type is launchable.
        "omnigent": (
            steps.get("omnigent", {}).get("status") == "complete"
            and steps.get("tmux", {}).get("status") == "complete"
        ),
    }
    installing = any(
        s.get("status") not in TERMINAL_STATUSES for s in steps.values()
    )
    release_manifest = {}
    for name, (enabled, expected) in _release_specs().items():
        step = steps.get(name, {})
        actual = step.get("actual_version")
        release_manifest[name] = {
            "enabled": enabled,
            "expected": expected,
            "actual": actual,
            "match": bool(
                enabled
                and steps.get(name, {}).get("status") == "complete"
                and expected
                and actual == expected
            ),
            "source": step.get("source"),
            "started_at": step.get("started_at"),
            "completed_at": step.get("completed_at"),
            "duration_ms": step.get("duration_ms"),
            "expected_checksum": step.get("expected_checksum"),
            "actual_checksum": step.get("actual_checksum"),
        }
    skills = steps.get("skills", {})
    skills_actual = skills.get("actual_version")
    skills_source = skills.get("release_source")
    resolved_commit = skills.get("resolved_commit")
    release_manifest[SKILLS_ARTIFACT] = {
        "enabled": True,
        "expected": SKILLS_REF,
        "actual": skills_actual,
        "match": bool(
            skills.get("status") == "complete"
            and skills_source in {"network", "prewarmed"}
            and skills_actual == SKILLS_REF
            and resolved_commit
        ),
        "source": skills_source,
        "resolved_commit": resolved_commit,
        "checksum": skills.get("actual_checksum"),
        "started_at": skills.get("started_at"),
        "completed_at": skills.get("completed_at"),
        "duration_ms": skills.get("duration_ms"),
        "expected_checksum": skills.get("expected_checksum"),
        "actual_checksum": skills.get("actual_checksum"),
    }
    artifact_status = artifact_manifest_status(
        os.environ.get("ARTIFACT_MANIFEST_PATH", "").strip()
    )
    artifact_proof = (
        _prewarm_status_unlocked()
        if artifact_status.get("ok") is True
        else {"reusable": False, "manifest": {}}
    )
    return {
        "steps": steps,
        "ready": ready,
        "installing": installing,
        "release_manifest": release_manifest,
        "artifact_manifest": artifact_status,
        "artifact_proof": artifact_proof,
    }


def prewarm_status() -> dict:
    with _install_file_lock(exclusive=False):
        return _prewarm_status_unlocked()


def _prewarm_status_unlocked() -> dict:
    """Verify persistent restart inputs directly from disk, not process state."""
    prefix = config.shared_prefix()
    try:
        contract = _artifact_contract()
    except ArtifactManifestError:
        contract = None
    bin_dir = os.path.join(prefix, "bin")
    expected_versions = {
        "node": NODE_VERSION,
        "claude": CLAUDE_VERSION,
        "codex": CODEX_VERSION,
        "databricks": DATABRICKS_CLI_VERSION,
    }
    if config.omnigent_enabled():
        expected_versions["omnigent"] = OMNIGENT_VERSION
        expected_versions["pi"] = PI_VERSION

    binaries: dict[str, dict] = {}
    artifact_names = {
        "node": (
            "node_linux_arm64"
            if platform.machine().lower() in {"aarch64", "arm64"}
            else "node_linux_x64"
        ),
        "claude": "claude_installer",
        "codex": "codex_npm_launcher_package",
        "databricks": "databricks_cli_archive_linux_x64",
        "pi": "pi_npm_package",
        "omnigent": "uv_binary",
    }
    for name, expected in expected_versions.items():
        path = os.path.join(bin_dir, name)
        actual = _read_cli_version(path) if os.path.isfile(path) else None
        binary_checksum = _file_checksum(path)
        stamp_ok = True
        artifact_entry = contract.entry(artifact_names[name]) if contract else None
        artifact_ok = bool(
            artifact_entry
            and _artifact_binary_reusable(name, artifact_entry, path)
        )
        if name == "codex" and contract:
            artifact_ok = _codex_install_reusable(
                prefix,
                contract.entry("codex_npm_launcher_package"),
                contract.entry("codex_native_package_linux_x64"),
            )
        if name == "pi" and contract:
            artifact_ok = _pi_install_reusable(
                prefix, contract.entry("pi_npm_package")
            )
        if name == "omnigent" and contract:
            artifact_ok = _omnigent_install_reusable(
                prefix,
                {
                    artifact_name: contract.entry(artifact_name)
                    for artifact_name in (
                        "uv_binary",
                        "python_3_12_runtime",
                        "omnigent_lock",
                    )
                },
            )
        if name == "claude" and contract:
            artifact_ok = (
                artifact_ok
                and binary_checksum == contract.entry("claude_binary")["sha256"]
            )
        binaries[name] = {
            "expected": expected,
            "actual": actual,
            "actual_checksum": binary_checksum or None,
            "source": "persistent",
            "reusable": bool(
                expected
                and actual == expected
                and binary_checksum
                and stamp_ok
                and artifact_ok
            ),
        }

    if config.omnigent_enabled():
        tmux_path = os.path.join(bin_dir, "tmux")
        tmux_stamp = _read_json(os.path.join(prefix, "tmux.install.json"))
        tmux_checksum = _file_checksum(tmux_path)
        stamped_binary_checksum = str(tmux_stamp.get("binary_sha256") or "")
        binaries["tmux"] = {
            "expected": stamped_binary_checksum or None,
            "actual": tmux_checksum or None,
            "expected_checksum": (
                contract.entry("tmux_linux_x64")["sha256"] if contract else None
            ),
            "actual_checksum": tmux_checksum or None,
            "source": "persistent",
            "reusable": bool(
                tmux_checksum
                and contract is not None
                and tmux_stamp.get("archive_sha256")
                == contract.entry("tmux_linux_x64")["sha256"]
                and stamped_binary_checksum == tmux_checksum
            ),
        }

    stamp = _read_json(_skills_stamp_path())
    try:
        persistent_skills = _persistent_skills_install(
            os.path.join(prefix, SKILLS_CLONE_DIR),
            os.path.join(prefix, "skills"),
        )
    except (OSError, RuntimeError, subprocess.TimeoutExpired):
        persistent_skills = None
    resolved_commit = str(stamp.get("resolved_commit") or "") or None
    expected_checksum = str(stamp.get("content_checksum") or "") or None
    skills_reusable = persistent_skills is not None
    actual_commit = persistent_skills[0] if persistent_skills else resolved_commit
    actual_checksum = persistent_skills[1] if persistent_skills else None
    skills_provenance = {
        "expected_ref": SKILLS_REF,
        "actual_ref": stamp.get("ref"),
        "resolved_commit": actual_commit,
        "expected_checksum": expected_checksum,
        "actual_checksum": actual_checksum,
        "source": "persistent",
        "reusable": skills_reusable,
    }
    reusable = (
        all(
            entry["reusable"]
            for name, entry in binaries.items()
            if name not in ADVISORY_BINARIES
        )
        and skills_reusable
    )
    return {
        "reusable": reusable,
        "manifest": {
            "expected_binaries": sorted(binaries),
            "binaries": binaries,
            SKILLS_ARTIFACT: skills_provenance,
        },
    }


def _parse_version(output: str) -> str | None:
    match = re.search(r"(?<!\d)(\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?)(?!\d)", output)
    return match.group(1) if match else None


def _read_cli_version(path: str) -> str | None:
    try:
        result = subprocess.run(
            [path, "--version"],
            capture_output=True,
            text=True,
            timeout=30,
            env=_install_env(),
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    if result.returncode != 0:
        return None
    return _parse_version(f"{result.stdout}\n{result.stderr}")


def _file_checksum(path: os.PathLike[str] | str) -> str:
    digest = hashlib.sha256()
    try:
        with open(path, "rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError:
        return ""
    return digest.hexdigest()


def _skills_stamp_path() -> str:
    return os.path.join(config.shared_prefix(), f"{SKILLS_CLONE_DIR}.install.json")


def skills_provenance() -> frozenset[str]:
    """Names of the skills installed from the reviewed upstream release.

    Empty when the shared tree is the vendored fallback (or skills have not
    landed yet) — callers must not assume the shared tree is all upstream.
    """
    stamp = _read_json(_skills_stamp_path())
    names = stamp.get("upstream_skills")
    if not isinstance(names, list):
        return frozenset()
    return frozenset(name for name in names if isinstance(name, str) and name)


def _read_json(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as handle:
            value = json.load(handle)
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _write_json_atomic(path: str, value: dict) -> None:
    _write_text_atomic(path, f"{json.dumps(value, sort_keys=True)}\n")


def _write_text_atomic(path: str, value: str) -> None:
    directory = os.path.dirname(os.path.abspath(path))
    basename = os.path.basename(path)
    fd, staging = tempfile.mkstemp(
        dir=directory,
        prefix=f".{basename}.",
        suffix=".tmp",
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(staging, path)
    finally:
        if os.path.exists(staging):
            os.unlink(staging)


@contextmanager
def _install_file_lock(*, exclusive: bool):
    """Cross-process lock for every shared-prefix read/mutation transaction."""
    prefix = config.shared_prefix()
    os.makedirs(prefix, exist_ok=True)
    lock_path = os.path.join(prefix, ".bootstrap.lock")
    with open(lock_path, "a+b") as lock_file:
        operation = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
        fcntl.flock(lock_file.fileno(), operation)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _persistent_skills_install(
    clone_dir: str,
    skills_dir: str,
) -> tuple[str, str] | None:
    """Return verified (commit, checksum), otherwise force a network refresh."""
    artifact = _artifact_contract().entry(SKILLS_ARTIFACT)
    stamp = _read_json(_skills_stamp_path())
    commit = str(stamp.get("resolved_commit") or "")
    expected_checksum = str(stamp.get("content_checksum") or "")
    if (
        stamp.get("repo") != artifact["source"]
        or stamp.get("ref") != SKILLS_REF
        or commit.lower() != str(artifact["commit"]).lower()
        or expected_checksum != artifact["content_sha256"]
        or not re.fullmatch(r"[0-9a-fA-F]{40}", commit)
        or not re.fullmatch(r"[0-9a-f]{64}", expected_checksum)
    ):
        return None
    result = subprocess.run(
        ["git", "-C", clone_dir, "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        timeout=30,
        env=_install_env(),
    )
    if result.returncode != 0 or (result.stdout or "").strip().lower() != commit.lower():
        return None
    upstream = os.path.join(clone_dir, SKILLS_UPSTREAM_DIR)
    names = {
        name
        for name in os.listdir(upstream)
        if os.path.isdir(os.path.join(upstream, name))
    } if os.path.isdir(upstream) else set()
    if not names:
        return None
    upstream_checksum = _directory_checksum(upstream, names)
    installed_checksum = _directory_checksum(skills_dir, names)
    if upstream_checksum != expected_checksum or installed_checksum != expected_checksum:
        return None
    return commit, expected_checksum


def _claude_install_argv() -> list[str]:
    # Execute only the checksum-verified staged installer with an exact version.
    return ["bash", "-s", CLAUDE_VERSION]


def _databricks_installer_url() -> str:
    # setup-cli documents pinning by replacing `main` with release tag `vX.Y.Z`.
    return (
        "https://raw.githubusercontent.com/databricks/setup-cli/"
        f"v{DATABRICKS_CLI_VERSION}/install.sh"
    )


def _install_env() -> dict:
    env = os.environ.copy()
    prefix = config.shared_prefix()
    env["PATH"] = f"{prefix}/bin:{env.get('PATH', '')}"
    # Installers must not see app SP credentials.
    env.pop("DATABRICKS_CLIENT_ID", None)
    env.pop("DATABRICKS_CLIENT_SECRET", None)
    return env


def _install_node() -> None:
    artifact_name = (
        "node_linux_arm64"
        if platform.machine().lower() in {"aarch64", "arm64"}
        else "node_linux_x64"
    )
    artifact_path, artifact = _verified_artifact(artifact_name)
    _set(
        "node",
        "running",
        expected_version=NODE_VERSION,
        source="network",
    )
    node_path = os.path.join(config.shared_prefix(), "bin", "node")
    actual = _read_cli_version(node_path) if os.path.isfile(node_path) else None
    if (
        actual == NODE_VERSION
        and _artifact_binary_reusable("node", artifact, node_path)
    ):
        _set(
            "node",
            "complete",
            actual_version=actual,
            source="prewarmed",
            expected_checksum=artifact["sha256"],
            actual_checksum=_file_checksum(node_path),
        )
        return
    script = os.path.join(os.path.dirname(__file__), "install_node.sh")
    env = _install_env()
    env["NODE_ARCHIVE_PATH"] = artifact_path
    env["NODE_ARCHIVE_SHA256"] = artifact["sha256"]
    result = subprocess.run(
        ["bash", script, config.shared_prefix()],
        capture_output=True, text=True, timeout=300, env=env,
    )
    if result.returncode == 0:
        actual = _read_cli_version(node_path)
        source = "staged"
        if actual != NODE_VERSION:
            _set(
                "node",
                "error",
                f"node version mismatch: expected {NODE_VERSION}, got {actual or 'unknown'}",
                actual_version=actual,
                source=source,
            )
            raise RuntimeError("node install failed")
        binary_checksum = _write_artifact_binary_stamp("node", artifact, node_path)
        _set(
            "node",
            "complete",
            actual_version=actual,
            source=source,
            expected_checksum=artifact["sha256"],
            actual_checksum=binary_checksum,
        )
    else:
        _set("node", "error", (result.stderr or result.stdout)[-500:])
        raise RuntimeError("node install failed")


def _install_claude() -> None:
    installer_path, installer_artifact = _verified_artifact("claude_installer")
    # Only the expected checksum is needed: the installer fetches the binary
    # itself, and pulling a quarter-gigabyte copy here to hash it would double
    # the claude step's boot cost for no added guarantee.
    binary_artifact = _artifact_contract().entry("claude_binary")
    _set("claude", "running", expected_version=CLAUDE_VERSION, source="network")
    prefix = config.shared_prefix()
    claude_bin = os.path.join(prefix, "bin", "claude")
    actual = _read_cli_version(claude_bin) if os.path.exists(claude_bin) else None
    if (
        actual == CLAUDE_VERSION
        and _artifact_binary_reusable("claude", installer_artifact, claude_bin)
        and _file_checksum(claude_bin) == binary_artifact["sha256"]
    ):
        _set(
            "claude",
            "complete",
            expected_version=CLAUDE_VERSION,
            actual_version=actual,
            source="prewarmed",
        )
        return
    env = _install_env()
    # The installer targets $HOME/.local — point HOME at the shared prefix's
    # parent so the binary lands in the shared tree, then link it into bin/.
    staging = os.path.join(prefix, "claude-home")
    os.makedirs(staging, exist_ok=True)
    env["HOME"] = staging
    try:
        with open(installer_path, "rb") as installer:
            result = subprocess.run(
                _claude_install_argv(),
                stdin=installer,
                capture_output=True,
                text=True,
                timeout=300,
                env=env,
            )
        installed = os.path.join(staging, ".local", "bin", "claude")
        if result.returncode == 0 and os.path.exists(installed):
            os.makedirs(os.path.dirname(claude_bin), exist_ok=True)
            if os.path.lexists(claude_bin):
                os.unlink(claude_bin)
            os.symlink(installed, claude_bin)
            actual = _read_cli_version(claude_bin)
            if actual != CLAUDE_VERSION:
                raise RuntimeError(
                    f"claude version mismatch: expected {CLAUDE_VERSION}, got {actual or 'unknown'}"
                )
            if _file_checksum(installed) != binary_artifact["sha256"]:
                raise RuntimeError("claude installed binary checksum mismatch")
            checksum = _write_artifact_binary_stamp(
                "claude", installer_artifact, installed
            )
            _set(
                "claude",
                "complete",
                expected_version=CLAUDE_VERSION,
                actual_version=actual,
                source="staged",
                expected_checksum=binary_artifact["sha256"],
                actual_checksum=checksum,
            )
        else:
            raise RuntimeError((result.stderr or result.stdout)[-500:])
    except (subprocess.TimeoutExpired, OSError, RuntimeError) as e:
        _set(
            "claude",
            "error",
            str(e),
            expected_version=CLAUDE_VERSION,
            actual_version=actual,
        )


def _find_codex_native_binary(prefix: str) -> str | None:
    candidates = []
    root = os.path.join(prefix, "lib", "node_modules")
    for current, _, files in os.walk(root):
        if "codex" in files and f"{os.sep}vendor{os.sep}" in current:
            candidates.append(os.path.join(current, "codex"))
    return candidates[0] if len(candidates) == 1 else None


def _codex_install_reusable(
    prefix: str, launcher_artifact: dict, native_artifact: dict
) -> bool:
    launcher = os.path.join(prefix, "bin", "codex")
    stamp = _read_json(os.path.join(prefix, "codex.install.json"))
    native_relative = str(stamp.get("native_relative_path") or "")
    native = os.path.normpath(os.path.join(prefix, native_relative))
    launcher_tree = os.path.normpath(
        os.path.join(
            prefix, str(stamp.get("launcher_tree_relative_path") or "")
        )
    )
    native_tree = os.path.normpath(
        os.path.join(
            prefix, str(stamp.get("native_tree_relative_path") or "")
        )
    )
    root_prefix = os.path.abspath(prefix) + os.sep
    if not all(
        path.startswith(root_prefix)
        for path in (native, launcher_tree, native_tree)
    ):
        return False
    launcher_checksum = _file_checksum(launcher)
    native_checksum = _file_checksum(native)
    launcher_tree_checksum = _directory_checksum(launcher_tree)
    native_tree_checksum = _directory_checksum(native_tree)
    return bool(
        launcher_checksum
        and native_checksum
        and launcher_tree_checksum
        and native_tree_checksum
        and stamp.get("launcher_package_sha256") == launcher_artifact["sha256"]
        and stamp.get("native_package_sha256") == native_artifact["sha256"]
        and stamp.get("launcher_sha256") == launcher_checksum
        and stamp.get("native_sha256") == native_checksum
        and stamp.get("launcher_tree_sha256") == launcher_tree_checksum
        and stamp.get("native_tree_sha256") == native_tree_checksum
        and native_checksum == native_artifact["executable_sha256"]
    )


def _install_codex() -> None:
    launcher_path, launcher_artifact = _verified_artifact(
        "codex_npm_launcher_package"
    )
    native_package_path, native_artifact = _verified_artifact(
        "codex_native_package_linux_x64"
    )
    validate_codex_tarballs(
        launcher_path, native_package_path, CODEX_VERSION
    )
    _set("codex", "running", expected_version=CODEX_VERSION, source="staged")
    prefix = config.shared_prefix()
    codex_bin = os.path.join(prefix, "bin", "codex")
    actual = _read_cli_version(codex_bin) if os.path.exists(codex_bin) else None
    if (
        actual == CODEX_VERSION
        and _codex_install_reusable(prefix, launcher_artifact, native_artifact)
    ):
        _set(
            "codex",
            "complete",
            expected_version=CODEX_VERSION,
            actual_version=actual,
            source="prewarmed",
        )
        return
    env = _install_env()
    npm_cache = os.path.join(prefix, "npm-offline-cache")
    shutil.rmtree(npm_cache, ignore_errors=True)
    os.makedirs(npm_cache, exist_ok=True)
    env["npm_config_offline"] = "true"
    env["npm_config_cache"] = npm_cache
    npm = os.path.join(prefix, "bin", "npm")
    if not os.path.exists(npm):
        npm = "npm"
    for attempt in range(1, 4):
        try:
            result = subprocess.run(
                [
                    npm,
                    "install",
                    "-g",
                    "--offline",
                    "--no-audit",
                    "--no-fund",
                    "--omit=optional",
                    f"--prefix={prefix}",
                    launcher_path,
                ],
                capture_output=True, text=True, timeout=300, env=env,
            )
        except (subprocess.TimeoutExpired, OSError) as e:
            result = None
            error = str(e)
        else:
            error = (result.stderr or result.stdout)[-500:]
        if result and result.returncode == 0 and os.path.exists(codex_bin):
            alias_directory = os.path.join(
                prefix,
                "lib",
                "node_modules",
                "@openai",
                "codex",
                "node_modules",
                "@openai",
                "codex-linux-x64",
            )
            try:
                install_native_alias(
                    launcher_path,
                    native_package_path,
                    alias_directory,
                    CODEX_VERSION,
                )
            except RuntimeError as extraction_error:
                error = str(extraction_error)
                continue
            actual = _read_cli_version(codex_bin)
            native_binary = _find_codex_native_binary(prefix)
            if (
                actual == CODEX_VERSION
                and native_binary
                and _file_checksum(native_binary)
                == native_artifact["executable_sha256"]
            ):
                launcher_checksum = _file_checksum(codex_bin)
                native_checksum = _file_checksum(native_binary)
                launcher_root = os.path.join(
                    prefix, "lib", "node_modules", "@openai", "codex"
                )
                launcher_tree_checksum = _directory_checksum(launcher_root)
                native_tree_checksum = _directory_checksum(alias_directory)
                _write_json_atomic(
                    os.path.join(prefix, "codex.install.json"),
                    {
                        "launcher_package_sha256": launcher_artifact["sha256"],
                        "native_package_sha256": native_artifact["sha256"],
                        "launcher_sha256": launcher_checksum,
                        "native_sha256": native_checksum,
                        "launcher_tree_sha256": launcher_tree_checksum,
                        "native_tree_sha256": native_tree_checksum,
                        "launcher_tree_relative_path": os.path.relpath(
                            launcher_root, prefix
                        ),
                        "native_tree_relative_path": os.path.relpath(
                            alias_directory, prefix
                        ),
                        "native_relative_path": os.path.relpath(
                            native_binary, prefix
                        ),
                    },
                )
                _set(
                    "codex",
                    "complete",
                    expected_version=CODEX_VERSION,
                    actual_version=actual,
                    source="staged",
                    expected_checksum=native_artifact["executable_sha256"],
                    actual_checksum=native_checksum,
                )
                return
            error = (
                f"codex version mismatch: expected {CODEX_VERSION}, "
                f"got {actual or 'unknown'}"
            )
        # Include the captured reason: without it the only record of *why*
        # codex failed is the terminal's attendee-gated status endpoint, so an
        # operator reading app logs saw three anonymous failures and no cause.
        logger.warning("codex install attempt %d/3 failed: %s", attempt, error)
        time.sleep(5)
    _set(
        "codex",
        "error",
        error,
        expected_version=CODEX_VERSION,
        actual_version=actual,
    )


_PI_PACKAGE = "@earendil-works/pi-coding-agent"


def _pi_package_root(prefix: str) -> str:
    return os.path.join(prefix, "lib", "node_modules", *_PI_PACKAGE.split("/"))


def _validate_pi_tarball(path: str, version: str) -> None:
    """Confirm the verified tarball is the pinned Pi and is fully hash-pinned.

    Pi is a pure-JS package with a large dependency tree, so unlike Codex it
    cannot be installed from one offline tarball. What makes that safe is
    ``npm-shrinkwrap.json``: Pi publishes it *inside* the tarball, pinning every
    transitive package to an exact version and ``integrity`` hash. Since the
    tarball itself is SHA-256 verified against the reviewed manifest, the
    shrinkwrap it carries is covered by that same checksum -- so the resolve npm
    performs is pinned end to end rather than floating on ``latest``.
    """
    with tarfile.open(path) as archive:
        try:
            manifest = archive.extractfile("package/package.json")
            shrinkwrap = archive.extractfile("package/npm-shrinkwrap.json")
            if manifest is None or shrinkwrap is None:
                raise KeyError("package/npm-shrinkwrap.json")
            declared = json.loads(manifest.read())
            locked = json.loads(shrinkwrap.read())
        except (KeyError, ValueError) as error:
            raise RuntimeError(f"Pi tarball layout is invalid: {error}") from error
    if declared.get("name") != _PI_PACKAGE or declared.get("version") != version:
        raise RuntimeError(
            f"Pi tarball is {declared.get('name')}@{declared.get('version')}, "
            f"expected {_PI_PACKAGE}@{version}"
        )
    packages = locked.get("packages")
    if not isinstance(packages, dict) or not packages:
        raise RuntimeError("Pi tarball carries no npm-shrinkwrap package set")
    floating = sorted(
        name
        for name, entry in packages.items()
        if name
        and isinstance(entry, dict)
        and entry.get("resolved")
        and not entry.get("integrity")
    )
    # Pi's three first-party siblings (pi-agent-core, pi-ai, pi-tui) ship
    # version-pinned but without an integrity hash. They are recorded rather
    # than rejected, so the gap is visible in logs instead of implied by a
    # blanket "fully pinned" claim.
    if floating:
        logger.info(
            "pi: %d shrinkwrap entries are version-pinned without integrity: %s",
            len(floating),
            ", ".join(floating),
        )


def _pi_install_reusable(prefix: str, artifact: dict) -> bool:
    launcher = os.path.join(prefix, "bin", "pi")
    stamp = _read_json(os.path.join(prefix, "pi.install.json"))
    tree = os.path.normpath(
        os.path.join(prefix, str(stamp.get("tree_relative_path") or ""))
    )
    if not tree.startswith(os.path.abspath(prefix) + os.sep):
        return False
    launcher_checksum = _file_checksum(launcher)
    tree_checksum = _directory_checksum(tree)
    return bool(
        launcher_checksum
        and tree_checksum
        and stamp.get("package_sha256") == artifact["sha256"]
        and stamp.get("launcher_sha256") == launcher_checksum
        and stamp.get("tree_sha256") == tree_checksum
    )


def _install_pi() -> None:
    """Install the Pi CLI from the reviewed npm tarball.

    Unlike Codex this install is *not* ``--offline``: Pi's dependency tree is
    fetched from the registry, pinned by the ``npm-shrinkwrap.json`` inside the
    checksum-verified tarball. ``--ignore-scripts`` is Pi's own documented
    install form and keeps third-party lifecycle scripts out of boot.
    """
    package_path, artifact = _verified_artifact("pi_npm_package")
    _validate_pi_tarball(package_path, PI_VERSION)
    _set("pi", "running", expected_version=PI_VERSION, source="network")
    prefix = config.shared_prefix()
    pi_bin = os.path.join(prefix, "bin", "pi")
    actual = _read_cli_version(pi_bin) if os.path.exists(pi_bin) else None
    if actual == PI_VERSION and _pi_install_reusable(prefix, artifact):
        _set(
            "pi",
            "complete",
            expected_version=PI_VERSION,
            actual_version=actual,
            source="prewarmed",
        )
        return
    env = _install_env()
    npm = os.path.join(prefix, "bin", "npm")
    if not os.path.exists(npm):
        npm = "npm"
    error = "pi install did not run"
    for attempt in range(1, 4):
        try:
            result = subprocess.run(
                [
                    npm,
                    "install",
                    "-g",
                    "--ignore-scripts",
                    "--no-audit",
                    "--no-fund",
                    f"--prefix={prefix}",
                    package_path,
                ],
                capture_output=True, text=True, timeout=600, env=env,
            )
        except (subprocess.TimeoutExpired, OSError) as e:
            error = str(e)
        else:
            error = (result.stderr or result.stdout)[-500:]
            if result.returncode == 0 and os.path.exists(pi_bin):
                actual = _read_cli_version(pi_bin)
                if actual == PI_VERSION:
                    launcher_checksum = _file_checksum(pi_bin)
                    tree = _pi_package_root(prefix)
                    _write_json_atomic(
                        os.path.join(prefix, "pi.install.json"),
                        {
                            "package_sha256": artifact["sha256"],
                            "launcher_sha256": launcher_checksum,
                            "tree_sha256": _directory_checksum(tree),
                            "tree_relative_path": os.path.relpath(tree, prefix),
                        },
                    )
                    _set(
                        "pi",
                        "complete",
                        expected_version=PI_VERSION,
                        actual_version=actual,
                        source="staged",
                        expected_checksum=artifact["sha256"],
                        actual_checksum=launcher_checksum,
                    )
                    return
                error = (
                    f"pi version mismatch: expected {PI_VERSION}, "
                    f"got {actual or 'unknown'}"
                )
        logger.warning("pi install attempt %d/3 failed: %s", attempt, error)
        time.sleep(5)
    _set(
        "pi",
        "error",
        error,
        expected_version=PI_VERSION,
        actual_version=actual,
    )


def _install_tmux() -> None:
    """Static tmux into the shared bin (omnigent's terminal backend)."""
    artifact_path, artifact = _verified_artifact("tmux_linux_x64")
    _set(
        "tmux",
        "running",
        source="network",
        expected_checksum=artifact["sha256"],
    )
    prefix = config.shared_prefix()
    tmux_bin = os.path.join(prefix, "bin", "tmux")
    stamp_path = os.path.join(prefix, "tmux.install.json")
    stamp = _read_json(stamp_path)
    binary_checksum = _file_checksum(tmux_bin)
    if (
        os.path.isfile(tmux_bin)
        and stamp.get("archive_sha256") == artifact["sha256"]
        and stamp.get("binary_sha256") == binary_checksum
        and binary_checksum
    ):
        smoke = subprocess.run(
            [tmux_bin, "-V"],
            capture_output=True,
            text=True,
            timeout=30,
            env=_install_env(),
        )
        if smoke.returncode == 0:
            _set(
                "tmux",
                "complete",
                source="prewarmed",
                expected_checksum=artifact["sha256"],
                actual_checksum=binary_checksum,
            )
            return
    staging = tmux_bin + ".download"
    try:
        shutil.copyfile(artifact_path, staging + ".gz")
        with open(staging + ".gz", "rb") as f:
            payload = f.read()
        digest = hashlib.sha256(payload).hexdigest()
        if digest != artifact["sha256"]:
            raise RuntimeError("tmux sha256 mismatch")
        with open(staging, "wb") as f:
            f.write(gzip.decompress(payload))
        os.chmod(staging, 0o755)
        os.replace(staging, tmux_bin)
        binary_checksum = _file_checksum(tmux_bin)
        smoke = subprocess.run(
            [tmux_bin, "-V"], capture_output=True, text=True,
            timeout=30, env=_install_env(),
        )
        if smoke.returncode != 0:
            raise RuntimeError(f"tmux -V failed: {(smoke.stderr or smoke.stdout)[-200:]}")
        _write_json_atomic(
            stamp_path,
            {
                "archive_sha256": artifact["sha256"],
                "binary_sha256": binary_checksum,
            },
        )
        _set(
            "tmux",
            "complete",
            source="staged",
            expected_checksum=artifact["sha256"],
            actual_checksum=binary_checksum,
        )
    except (subprocess.TimeoutExpired, OSError, RuntimeError) as e:
        _set("tmux", "error", str(e))
    finally:
        for leftover in (staging, staging + ".gz"):
            if os.path.exists(leftover):
                os.unlink(leftover)


def _omnigent_stamp_path() -> str:
    return os.path.join(config.shared_prefix(), "omnigent.install.json")


def _omnigent_install_reusable(prefix: str, entries: dict[str, dict]) -> bool:
    """Reuse an install only when the whole supply chain still matches.

    The committed lock is the integrity anchor now that there is no staged
    wheelhouse: it pins every direct and transitive wheel by hash, so a matching
    lock checksum plus a matching venv tree means the same bytes are installed.
    """
    stamp = _read_json(os.path.join(prefix, "omnigent.install.json"))
    omnigent_bin = os.path.join(prefix, "bin", "omnigent")
    checks = {
        "lock_sha256": _file_checksum(entries["omnigent_lock"]["source"]),
        "binary_sha256": _file_checksum(omnigent_bin),
        "venv_sha256": _directory_checksum(
            os.path.join(prefix, "omnigent-venv")
        ),
    }
    expected = {
        "uv_sha256": entries["uv_binary"]["sha256"],
        "python_runtime_sha256": entries["python_3_12_runtime"]["sha256"],
        "lock_sha256": entries["omnigent_lock"]["sha256"],
    }
    return bool(
        checks["binary_sha256"]
        and checks["venv_sha256"]
        and checks["lock_sha256"] == expected["lock_sha256"]
        and all(stamp.get(key) == value for key, value in expected.items())
        and all(stamp.get(key) == value for key, value in checks.items())
    )


def _install_omnigent() -> None:
    """Install Omnigent from the committed, fully hash-pinned lock.

    Every wheel is resolved from PyPI under ``--require-hashes`` against
    ``assets/artifacts/omnigent-<version>.lock``, so the install is reproducible
    without anyone staging a wheelhouse for the event.
    """
    _set("omnigent", "running", expected_version=OMNIGENT_VERSION, source="staged")
    paths: dict[str, str] = {}
    entries: dict[str, dict] = {}
    try:
        for name in ("uv_binary", "python_3_12_runtime"):
            paths[name], entries[name] = _extracted_artifact_executable(name)
        paths["omnigent_lock"], entries["omnigent_lock"] = _verified_artifact(
            "omnigent_lock"
        )
    except (OSError, RuntimeError, shutil.ReadError) as error:
        _set(
            "omnigent",
            "error",
            str(error),
            expected_version=OMNIGENT_VERSION,
        )
        return
    prefix = config.shared_prefix()
    omnigent_bin = os.path.join(prefix, "bin", "omnigent")
    stamp = _omnigent_stamp_path()
    actual = _read_cli_version(omnigent_bin) if os.path.exists(omnigent_bin) else None
    if (
        actual == OMNIGENT_VERSION
        and _omnigent_install_reusable(prefix, entries)
    ):
        _set(
            "omnigent",
            "complete",
            expected_version=OMNIGENT_VERSION,
            actual_version=actual,
            source="prewarmed",
        )
        return
    env = _install_env()
    # The runtime is the extracted reviewed build; uv must never fetch its own.
    env["UV_PYTHON_DOWNLOADS"] = "never"
    venv = os.path.join(prefix, "omnigent-venv")
    python_executable = paths["python_3_12_runtime"]
    try:
        lock_text = open(paths["omnigent_lock"], encoding="utf-8").read()
        if f"omnigent=={OMNIGENT_VERSION}" not in lock_text:
            raise RuntimeError("Omnigent lock does not pin the configured version")
        result = subprocess.run(
            [
                paths["uv_binary"],
                "venv",
                "--clear",
                "--python",
                python_executable,
                venv,
            ],
            capture_output=True,
            text=True,
            timeout=300,
            env=env,
        )
        if result.returncode != 0:
            raise RuntimeError(
                "offline Omnigent venv creation failed: "
                + (result.stderr or result.stdout)[-500:]
            )
        result = subprocess.run(
            [
                paths["uv_binary"],
                "pip",
                "install",
                "--python",
                os.path.join(venv, "bin", "python"),
                "--require-hashes",
                "-r",
                paths["omnigent_lock"],
            ],
            capture_output=True, text=True, timeout=900, env=env,
        )
        installed_bin = os.path.join(venv, "bin", "omnigent")
        if result.returncode != 0 or not os.path.exists(installed_bin):
            raise RuntimeError((result.stderr or result.stdout)[-500:])
        if os.path.lexists(omnigent_bin):
            os.unlink(omnigent_bin)
        os.symlink(installed_bin, omnigent_bin)
        actual = _read_cli_version(omnigent_bin)
        if actual != OMNIGENT_VERSION:
            raise RuntimeError(
                f"omnigent version mismatch: expected {OMNIGENT_VERSION}, "
                f"got {actual or 'unknown'}"
            )
        binary_checksum = _file_checksum(omnigent_bin)
        venv_checksum = _directory_checksum(venv)
        _write_json_atomic(stamp, {
            "uv_sha256": entries["uv_binary"]["sha256"],
            "python_runtime_sha256": entries["python_3_12_runtime"]["sha256"],
            "lock_sha256": entries["omnigent_lock"]["sha256"],
            "binary_sha256": binary_checksum,
            "venv_sha256": venv_checksum,
        })
        _set(
            "omnigent",
            "complete",
            expected_version=OMNIGENT_VERSION,
            actual_version=actual,
            source="staged",
            expected_checksum=entries["omnigent_lock"]["sha256"],
            actual_checksum=binary_checksum,
        )
    except (subprocess.TimeoutExpired, OSError, RuntimeError) as e:
        # Logged as well as recorded: the status endpoint that carries this is
        # attendee-gated, so app logs were an operator's only view and this
        # failure left no trace in them at all.
        logger.warning("omnigent install failed: %s", e)
        _set(
            "omnigent",
            "error",
            str(e),
            expected_version=OMNIGENT_VERSION,
            actual_version=actual,
        )


def _databricks_cli_current(path: str) -> bool:
    return _read_cli_version(path) == DATABRICKS_CLI_VERSION


def _install_databricks_cli() -> None:
    archive_path, archive_artifact = _verified_artifact(
        "databricks_cli_archive_linux_x64"
    )
    _verified_artifact("databricks_cli_installer")
    _set(
        "databricks",
        "running",
        expected_version=DATABRICKS_CLI_VERSION,
        source="network",
    )
    prefix = config.shared_prefix()
    bin_dir = os.path.join(prefix, "bin")
    target = os.path.join(bin_dir, "databricks")
    actual = _read_cli_version(target) if os.path.exists(target) else None
    if (
        actual == DATABRICKS_CLI_VERSION
        and _artifact_binary_reusable("databricks", archive_artifact, target)
    ):
        _set(
            "databricks",
            "complete",
            expected_version=DATABRICKS_CLI_VERSION,
            actual_version=actual,
            source="prewarmed",
        )
        return
    if os.path.lexists(target):
        os.unlink(target)  # stale or runtime-bundled old version
    try:
        with tempfile.TemporaryDirectory(prefix="databricks-cli-") as staging:
            shutil.unpack_archive(archive_path, staging, format="zip")
            candidates = [
                os.path.join(current, name)
                for current, _, files in os.walk(staging)
                for name in files
                if name == "databricks"
            ]
            if len(candidates) != 1:
                raise RuntimeError("Databricks CLI archive layout is invalid")
            shutil.copyfile(candidates[0], target)
            os.chmod(target, 0o755)
        actual = _read_cli_version(target) if os.path.exists(target) else None
        if (
            os.path.exists(target)
            and actual == DATABRICKS_CLI_VERSION
        ):
            checksum = _write_artifact_binary_stamp(
                "databricks", archive_artifact, target
            )
            _set(
                "databricks",
                "complete",
                expected_version=DATABRICKS_CLI_VERSION,
                actual_version=actual,
                source="staged",
                expected_checksum=archive_artifact["sha256"],
                actual_checksum=checksum,
            )
            return
        error = (
            f"version mismatch: expected {DATABRICKS_CLI_VERSION}, "
            f"got {actual or 'unknown'}"
        )
    except (subprocess.TimeoutExpired, OSError, RuntimeError, shutil.ReadError) as e:
        error = str(e)
    _set(
        "databricks",
        "error",
        error,
        expected_version=DATABRICKS_CLI_VERSION,
        actual_version=actual,
    )


def _publish_skills_tree(staged: str, target: str) -> None:
    """Swap one complete skills tree into place; never expose a mixed tree."""
    parent = os.path.dirname(target)
    backup = tempfile.mkdtemp(prefix=".skills-previous-", dir=parent)
    os.rmdir(backup)
    had_target = os.path.exists(target)
    try:
        if had_target:
            os.replace(target, backup)
        os.replace(staged, target)
    except Exception:
        if had_target and os.path.exists(backup) and not os.path.exists(target):
            os.replace(backup, target)
        raise
    finally:
        shutil.rmtree(backup, ignore_errors=True)


def _stage_vendored_skills(prefix: str) -> str:
    staged = tempfile.mkdtemp(prefix=".skills-stage-", dir=prefix)
    try:
        if os.path.isdir(_ASSETS_SKILLS):
            for name in os.listdir(_ASSETS_SKILLS):
                source = os.path.join(_ASSETS_SKILLS, name)
                if os.path.isdir(source):
                    shutil.copytree(source, os.path.join(staged, name))
        return staged
    except Exception:
        shutil.rmtree(staged, ignore_errors=True)
        raise


class SkillsContractError(RuntimeError):
    """The fetched skills violate the reviewed contract, so no fallback applies.

    A structural violation -- an empty upstream directory, a commit or content
    digest that differs from the manifest -- means the contract itself is wrong
    or the clone was tampered with. Serving the vendored copy there would hide
    the defect behind a working-looking terminal, which is how the stale
    ``databricks-skills/`` path survived a whole release. Only a transient
    failure (network, timeout, disk) earns the fallback.
    """


def _install_skills() -> None:
    """Build the shared skills library: vendored base + reviewed upstream skills.

    1. Copy assets/skills (the workshop's own skills + vendored Databricks
       skills as the offline fallback).
    2. Clone databricks-agent-skills at the reviewed ref and overlay ``skills/*``
       so attendees build on the canonical, AppKit-first Databricks skills.

    A successful overlay is ``complete``. A transient fetch failure serves the
    vendored copy and reports ``degraded`` -- usable, but never ``complete``,
    because the attendee is not running the reviewed skills.
    """
    artifact = _artifact_contract().entry(SKILLS_ARTIFACT)
    if artifact["version"] != SKILLS_REF:
        raise RuntimeError("skills manifest version does not match configured ref")
    _set(
        "skills",
        "running",
        expected_version=SKILLS_REF,
        source="staged",
        clear_release=True,
    )
    prefix = config.shared_prefix()
    skills_dir = os.path.join(prefix, "skills")
    os.makedirs(prefix, exist_ok=True)
    clone_dir = os.path.join(prefix, SKILLS_CLONE_DIR)
    staged: str | None = None
    try:
        persistent = _persistent_skills_install(clone_dir, skills_dir)
        if persistent is not None:
            resolved_commit, checksum = persistent
            _set(
                "skills",
                "complete",
                expected_version=SKILLS_REF,
                actual_version=SKILLS_REF,
                release_source="prewarmed",
                resolved_commit=resolved_commit,
                source="prewarmed",
                expected_checksum=checksum,
                actual_checksum=checksum,
            )
            return

        staged = _stage_vendored_skills(prefix)

        # Pinned-ref clone: --branch accepts a tag or branch; for a full-SHA
        # pin we clone then checkout. Invalid or tampered persistent content is
        # discarded and rebuilt from the reviewed ref.
        shutil.rmtree(clone_dir, ignore_errors=True)
        result = subprocess.run(
            ["git", "clone", "--depth", "1", "--branch", SKILLS_REF,
             artifact["source"], clone_dir],
            capture_output=True, text=True, timeout=300, env=_install_env(),
        )
        if result.returncode != 0:
            # --branch rejects raw commit SHAs; fall back to clone + checkout.
            shutil.rmtree(clone_dir, ignore_errors=True)
            subprocess.run(
                ["git", "clone", artifact["source"], clone_dir],
                capture_output=True, text=True, timeout=300, env=_install_env(),
            )
            result = subprocess.run(
                ["git", "-C", clone_dir, "checkout", SKILLS_REF],
                capture_output=True, text=True, timeout=120, env=_install_env(),
            )
        if result.returncode != 0:
            raise RuntimeError((result.stderr or result.stdout)[-300:])

        resolved = subprocess.run(
            ["git", "-C", clone_dir, "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=30,
            env=_install_env(),
        )
        resolved_commit = (resolved.stdout or "").strip()
        if resolved.returncode != 0 or not re.fullmatch(
            r"[0-9a-fA-F]{40}", resolved_commit
        ):
            raise RuntimeError("unable to verify fetched skills commit")
        if resolved_commit.lower() != str(artifact["commit"]).lower():
            raise SkillsContractError(
                "fetched skills commit differs from reviewed manifest"
            )

        upstream = os.path.join(clone_dir, SKILLS_UPSTREAM_DIR)
        updated = 0
        if os.path.isdir(upstream):
            for name in os.listdir(upstream):
                source = os.path.join(upstream, name)
                if not os.path.isdir(source):
                    continue
                target = os.path.join(staged, name)
                shutil.rmtree(target, ignore_errors=True)
                shutil.copytree(source, target)
                updated += 1
        if not updated:
            # The upstream layout moved (this is how the deprecated
            # databricks-skills/ path went unnoticed). Never fall back.
            raise SkillsContractError(
                f"{SKILLS_UPSTREAM_DIR}/ is empty in the fetched skills repository"
            )
        names = {
            name
            for name in os.listdir(upstream)
            if os.path.isdir(os.path.join(upstream, name))
        }
        checksum = _directory_checksum(upstream, names)
        if not checksum:
            raise SkillsContractError("unable to checksum fetched skills")
        if _directory_checksum(staged, names) != checksum:
            raise SkillsContractError("installed skills checksum mismatch")
        if checksum != artifact["content_sha256"]:
            raise SkillsContractError(
                "skills content differs from reviewed manifest"
            )
        _publish_skills_tree(staged, skills_dir)
        staged = None
        _write_json_atomic(
            _skills_stamp_path(),
            {
                "repo": artifact["source"],
                "ref": SKILLS_REF,
                "resolved_commit": resolved_commit,
                "content_checksum": checksum,
                # Which skills came from upstream, so per-user setup can declare
                # exactly those to the Databricks CLI's aitools state. The
                # shared tree also holds our vendored workflow skills, and no
                # name pattern separates them reliably (``databricks-app-apx``
                # is vendored, not upstream).
                "upstream_skills": sorted(names),
            },
        )
        logger.info("skills: %d refreshed from %s@%s", updated, SKILLS_REPO, SKILLS_REF)
        _set(
            "skills",
            "complete",
            expected_version=SKILLS_REF,
            actual_version=SKILLS_REF,
            release_source="network",
            resolved_commit=resolved_commit,
            source="network",
            expected_checksum=checksum,
            actual_checksum=checksum,
        )
    except SkillsContractError as e:
        logger.error("skills contract violated: %s", e)
        if staged is not None:
            shutil.rmtree(staged, ignore_errors=True)
            staged = None
        _set(
            "skills",
            "error",
            str(e),
            expected_version=SKILLS_REF,
            source="network",
        )
    except (subprocess.TimeoutExpired, OSError, RuntimeError) as e:
        logger.warning("skills fetch failed (%s) — using vendored skills", e)
        if staged is not None:
            shutil.rmtree(staged, ignore_errors=True)
            staged = None
        if not os.path.isdir(skills_dir):
            try:
                staged = _stage_vendored_skills(prefix)
                _publish_skills_tree(staged, skills_dir)
                staged = None
            except OSError as vendored_error:
                _set(
                    "skills",
                    "error",
                    f"vendored copy failed: {vendored_error}",
                    expected_version=SKILLS_REF,
                    release_source="vendored_fallback",
                    source="staged",
                )
                return
        # Usable but not the reviewed skill set: terminal, and never `complete`.
        _set(
            "skills",
            "degraded",
            error=f"vendored fallback: {e}",
            expected_version=SKILLS_REF,
            release_source="vendored_fallback",
            source="staged",
        )
    finally:
        if staged is not None:
            shutil.rmtree(staged, ignore_errors=True)


def _guard_installer(
    step: str,
    operation,
    *,
    expected_version: str | None = None,
    source: str = "staged",
):
    def guarded() -> None:
        _set(
            step,
            "running",
            expected_version=expected_version,
            source=source,
        )
        try:
            operation()
        except Exception as error:  # noqa: BLE001 - step must leave pending/running
            _set(
                step,
                "error",
                str(error),
                expected_version=expected_version,
                source=source,
            )

    return guarded


_install_node = _guard_installer(
    "node", _install_node, expected_version=NODE_VERSION
)
_install_claude = _guard_installer(
    "claude", _install_claude, expected_version=CLAUDE_VERSION
)
_install_codex = _guard_installer(
    "codex", _install_codex, expected_version=CODEX_VERSION
)
_install_pi = _guard_installer("pi", _install_pi, expected_version=PI_VERSION)
_install_tmux = _guard_installer("tmux", _install_tmux)
_install_omnigent = _guard_installer(
    "omnigent", _install_omnigent, expected_version=OMNIGENT_VERSION
)
_install_databricks_cli = _guard_installer(
    "databricks",
    _install_databricks_cli,
    expected_version=DATABRICKS_CLI_VERSION,
)
_install_skills = _guard_installer(
    "skills", _install_skills, expected_version=SKILLS_REF
)


def _run_parallel_installers(tasks, *, max_workers: int) -> None:
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {
            pool.submit(operation): step
            for step, operation in tasks
        }
        for future, step in futures.items():
            try:
                future.result()
            except Exception as error:  # noqa: BLE001 - consume every future
                _set(step, "error", str(error))


def run_in_background() -> None:
    omnigent = config.omnigent_enabled()
    steps = ["node", "claude", "codex", "databricks", "skills"]
    if omnigent:
        steps += ["tmux", "omnigent", "pi"]
    for step in steps:
        _set(step, "pending")

    def orchestrate_locked():
        os.makedirs(os.path.join(config.shared_prefix(), "bin"), exist_ok=True)
        try:
            _artifact_contract()
        except ArtifactManifestError as error:
            for step in steps:
                _set(step, "error", str(error))
            return
        try:
            _install_node()
        except Exception as error:  # noqa: BLE001 - monkeypatched/custom wrapper
            _set("node", "error", str(error))
        with _state_lock:
            node_ready = _state.get("node", {}).get("status") == "complete"
        if not node_ready:
            # Without node, codex and pi can't install; claude's installer is
            # self-contained, so still try it. tmux/omnigent don't need node.
            _set("codex", "error", "skipped: node install failed")
            if omnigent:
                _set("pi", "error", "skipped: node install failed")
            tasks = [
                ("claude", _install_claude),
                ("databricks", _install_databricks_cli),
                ("skills", _install_skills),
            ]
            if omnigent:
                tasks.extend([
                    ("tmux", _install_tmux),
                    ("omnigent", _install_omnigent),
                ])
            _run_parallel_installers(tasks, max_workers=5)
            return
        tasks = [
            ("claude", _install_claude),
            ("codex", _install_codex),
            ("databricks", _install_databricks_cli),
            ("skills", _install_skills),
        ]
        if omnigent:
            tasks.extend([
                ("tmux", _install_tmux),
                ("omnigent", _install_omnigent),
                ("pi", _install_pi),
            ])
        _run_parallel_installers(tasks, max_workers=7)

    def orchestrate():
        with _install_file_lock(exclusive=True):
            orchestrate_locked()

    threading.Thread(target=orchestrate, daemon=True, name="bootstrap").start()
