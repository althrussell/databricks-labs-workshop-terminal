"""Optional Unity Catalog Volume mirror for the pinned bootstrap toolchain.

Control Tower stages every manifest artifact into one central volume, keyed by
the artifact's repo-owned sha256, and points each app at it with
``WORKSHOP_TOOLCHAIN_MIRROR_PATH``. Boot then pulls ~430 MiB of toolchain from
workspace-local storage instead of the public internet.

Two properties make this safe to leave optional. Blobs are **content-addressed**,
so a release that bumps a pin simply misses on a volume nobody re-staged --
there is no stale-file case to coordinate around. And the checksum gate in
``artifacts.py`` is **identical on both paths**, so a miss costs a slow boot
rather than a broken or untrusted one.

Databricks Apps does not mount volumes into the container: ``/Volumes`` does not
exist on the app filesystem at all, so this reaches the volume the only way an
app can, through the Files API using the app's own service principal.
"""

from __future__ import annotations

import logging
import time

logger = logging.getLogger(__name__)

# Control Tower adds each new app SP to the mirror's reader group at deploy, and
# lifespan starts the bootstrap thread almost immediately after. A grant that
# has not propagated yet looks exactly like a missing one, so a permission error
# is worth a few short retries before concluding the mirror is unusable.
PERMISSION_RETRIES = 3
PERMISSION_BACKOFF = 2.0

_CHUNK = 1024 * 1024


class ToolchainMirrorError(RuntimeError):
    """The mirror was required and could not be used."""


def _error_classes(*names: str) -> tuple[type, ...]:
    try:
        from databricks.sdk import errors
    except ImportError:
        return ()
    found = tuple(
        cls
        for cls in (getattr(errors, name, None) for name in names)
        if isinstance(cls, type)
    )
    return found


def _is_absent(error: BaseException) -> bool:
    absent = _error_classes("NotFound", "ResourceDoesNotExist")
    if absent and isinstance(error, absent):
        return True
    text = str(error).lower()
    return "404" in text or "does not exist" in text or "not found" in text


def _is_permission(error: BaseException) -> bool:
    denied = _error_classes("PermissionDenied", "Unauthenticated")
    if denied and isinstance(error, denied):
        return True
    text = str(error).lower()
    return "403" in text or "401" in text or "permission denied" in text


class ToolchainMirror:
    """Read-only, content-addressed reader for the staged toolchain volume."""

    def __init__(
        self,
        volume_path: str,
        client,
        *,
        strict: bool = False,
        retries: int = PERMISSION_RETRIES,
        backoff: float = PERMISSION_BACKOFF,
        sleep=time.sleep,
    ):
        self.volume_path = str(volume_path).rstrip("/")
        self.strict = bool(strict)
        self._client = client
        self._retries = max(1, int(retries))
        self._backoff = float(backoff)
        self._sleep = sleep
        self.hits = 0
        self.misses = 0
        self.last_error: str | None = None

    def blob_path(self, sha256: str) -> str:
        return f"{self.volume_path}/{sha256}"

    def fetch(self, sha256: str, destination: str) -> bool:
        """Write the blob for ``sha256`` to ``destination``; ``False`` on a miss.

        Never raises for an absent or unreadable blob -- the caller decides
        whether that is a fallback or, in strict mode, a failure.
        """
        remote = self.blob_path(sha256)
        delay = self._backoff
        for attempt in range(1, self._retries + 1):
            try:
                self._download(remote, destination)
            except Exception as error:  # noqa: BLE001 - any failure is a miss
                if _is_absent(error):
                    return self._miss(f"not staged: {sha256}")
                if _is_permission(error) and attempt < self._retries:
                    logger.warning(
                        "toolchain mirror denied read of %s (attempt %d/%d), "
                        "retrying in case the reader grant is still propagating: %s",
                        remote,
                        attempt,
                        self._retries,
                        error,
                    )
                    self._sleep(delay)
                    delay *= 2
                    continue
                return self._miss(f"{type(error).__name__}: {error}")
            self.hits += 1
            return True
        return self._miss("retries exhausted")

    def _miss(self, reason: str) -> bool:
        self.misses += 1
        self.last_error = reason
        logger.info("toolchain mirror miss (%s) - falling back to source", reason)
        return False

    def _download(self, remote: str, destination: str) -> None:
        response = self._client.files.download(remote)
        contents = getattr(response, "contents", response)
        try:
            with open(destination, "wb") as handle:
                for chunk in iter(lambda: contents.read(_CHUNK), b""):
                    handle.write(chunk)
        finally:
            close = getattr(contents, "close", None)
            if callable(close):
                close()

    def status(self) -> dict:
        return {
            "path": self.volume_path,
            "strict": self.strict,
            "hits": self.hits,
            "misses": self.misses,
            "last_error": self.last_error,
        }


def from_environment() -> ToolchainMirror | None:
    """Build the mirror Control Tower configured, or ``None`` when there is none."""
    from .. import config, credentials

    raw = config.toolchain_mirror_raw()
    path = config.toolchain_mirror_path()
    strict = config.toolchain_mirror_strict()
    if not raw:
        return None
    if not path:
        message = (
            "WORKSHOP_TOOLCHAIN_MIRROR_PATH must be an absolute "
            f"/Volumes/<catalog>/<schema>/<volume> address, got {raw!r}"
        )
        if strict:
            raise ToolchainMirrorError(message)
        logger.error("%s - ignoring the mirror and downloading from source", message)
        return None
    client = credentials.workspace_client()
    if client is None:
        message = (
            f"toolchain mirror {path} is configured but the app identity is "
            "unavailable, so the Files API cannot be reached"
        )
        if strict:
            raise ToolchainMirrorError(message)
        logger.error("%s - downloading from source", message)
        return None
    return ToolchainMirror(path, client, strict=strict)


def configuration_status() -> dict:
    """Whether a mirror is configured and usable, independent of any fetch.

    Reported even when disabled: a mirror that was configured and rejected must
    never look the same as one that was never configured, because the whole
    failure mode this guards against is silently downloading from the internet
    while an operator believes the volume is in use.
    """
    from .. import config

    raw = config.toolchain_mirror_raw()
    path = config.toolchain_mirror_path()
    error = None
    if raw and not path:
        error = "path is not an absolute /Volumes/<catalog>/<schema>/<volume> address"
    return {
        "configured": bool(raw),
        "path": path,
        "strict": config.toolchain_mirror_strict(),
        "error": error,
    }


__all__ = [
    "PERMISSION_BACKOFF",
    "PERMISSION_RETRIES",
    "ToolchainMirror",
    "ToolchainMirrorError",
    "configuration_status",
    "from_environment",
]
