"""Read and validate working-tree uploads before opening a remote client."""

from __future__ import annotations

import fnmatch
import os
import stat
from pathlib import Path

from dependency_sources import validate_release_inputs, validate_source_files


def read_upload_sources(root: Path, *, exclude=()) -> dict[str, bytes]:
    """Freeze the actual upload bytes after clean/public-source preflight.

    The ordinary dev exclusions still apply. Validate the selected bytes too:
    ignored local files are not covered by Git's clean-tree check.
    """
    root = Path(root).resolve()
    validate_release_inputs(root, require_clean=True)
    files = {}

    def excluded(relative):
        return any(fnmatch.fnmatch(relative, pattern)
                   or ("/" not in pattern and any(fnmatch.fnmatch(part, pattern)
                                                  for part in Path(relative).parts))
                   for pattern in exclude)

    for directory, dirs, names in os.walk(root):
        relative_root = os.path.relpath(directory, root)
        dirs[:] = [name for name in dirs if not excluded(os.path.normpath(os.path.join(relative_root, name)))]
        if any((Path(directory) / name).is_symlink() for name in dirs):
            raise ValueError("Source upload directory links are not deployable")
        for name in names:
            relative = os.path.normpath(os.path.join(relative_root, name))
            if excluded(relative):
                continue
            path = Path(directory) / name
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise ValueError("Source upload links are not deployable")
            files[Path(relative).as_posix()] = path.read_bytes()
    validate_source_files(files)
    # A concurrent edit during the snapshot must not turn a clean preflight into
    # an unreviewed upload. The returned bytes are the ones validated above.
    validate_release_inputs(root, require_clean=True)
    return files


def read_selected_upload_sources(root: Path, names) -> dict[str, bytes]:
    """Freeze selected helper sources without following links outside checkout."""
    root = Path(root).resolve()
    validate_release_inputs(root, require_clean=True)
    result = {}
    for name in names:
        relative = Path(name)
        if relative.is_absolute() or ".." in relative.parts or relative.as_posix() != name:
            raise ValueError("Selected upload paths must stay inside the release checkout")
        path = root / relative
        for parent in path.parents:
            if parent == root:
                break
            if parent.is_symlink():
                raise ValueError("Selected upload source ancestors cannot be links")
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ValueError("Selected upload source links are not deployable")
        result[name] = path.read_bytes()
    validate_source_files(result)
    validate_release_inputs(root, require_clean=True)
    return result
