"""Short lexical HOME for Codex's managed-daemon socket; no state relocation."""
import hashlib
import os
import stat

_APPS_SOURCE_ROOT = "/app/python/source_code"
_SOCKET_SUFFIX = "/.codex/app-server-control/app-server-control.sock"


def shell_home(attendee_home: str) -> str:
    """Work around Codex 0.157.1's unshortened client socket path.

    CODEX_HOME is canonicalized and cannot shorten an existing directory.
    Default HOME stays lexical, so an owned alias preserves all attendee files.
    Keep the socket below both Linux and macOS pathname limits (108/104 bytes).
    """
    original = os.path.abspath(attendee_home)
    if len(os.fsencode(original + _SOCKET_SUFFIX)) < 104:
        return attendee_home
    digest = hashlib.sha256(os.fsencode(original)).hexdigest()
    root = os.path.join(_APPS_SOURCE_ROOT, ".cx")
    apps_alias = os.path.join(root, digest[:24])
    if (original.startswith(_APPS_SOURCE_ROOT + os.sep)
            and len(os.fsencode(apps_alias + _SOCKET_SUFFIX)) < 104):
        # Apps source is writable. Keep the alias outside /tmp, where Codex
        # refuses to create its own helper binaries and prints a warning.
        try:
            os.mkdir(root, mode=0o700)
        except FileExistsError:
            pass
        metadata = os.lstat(root)
        if (not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.getuid()
                or metadata.st_mode & 0o077):
            raise RuntimeError("Codex HOME alias directory is not private to this runtime")
        alias = apps_alias
    else:
        alias = f"/tmp/wt-cx-{digest[:32]}"
    try:
        os.symlink(original, alias, target_is_directory=True)
    except FileExistsError:
        # Concurrent launches may reuse only the exact current-uid-owned link.
        # Never replace an unexpected entry, even inside the private directory.
        pass
    metadata = os.lstat(alias)
    if (not stat.S_ISLNK(metadata.st_mode) or metadata.st_uid != os.getuid()
            or os.readlink(alias) != original):
        raise RuntimeError("Codex HOME alias is not owned by this attendee runtime")
    return alias
