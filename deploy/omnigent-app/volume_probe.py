"""Startup invariant for the Omnigent artifact Volume."""

from __future__ import annotations

import os
from pathlib import Path
from uuid import uuid4


def probe_artifact_volume(volume_path: str | Path) -> None:
    """Prove this process identity can durably write inside ``volume_path``."""
    root = Path(volume_path)
    if not root.is_absolute() or not root.is_dir():
        raise RuntimeError(
            "artifact Volume path must be an existing absolute directory"
        )
    root = root.resolve(strict=True)
    probe = root / f".omnigent-readiness-{uuid4().hex}.probe"
    try:
        with probe.open("xb") as handle:
            handle.write(b"omnigent-volume-readiness\n")
            handle.flush()
            os.fsync(handle.fileno())
    except OSError as exc:
        try:
            probe.unlink(missing_ok=True)
        except OSError:
            pass
        raise RuntimeError(
            "artifact Volume is not writable by the app identity"
        ) from exc
    try:
        probe.unlink()
    except OSError as exc:
        try:
            probe.unlink(missing_ok=True)
        except OSError:
            pass
        raise RuntimeError("artifact Volume probe cannot be removed") from exc


__all__ = ["probe_artifact_volume"]
