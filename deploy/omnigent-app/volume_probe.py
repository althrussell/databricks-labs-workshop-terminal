"""Startup invariant for the Omnigent artifact Volume.

Databricks Apps does not mount Unity Catalog volumes into the app container --
binding a volume resource injects its ``/Volumes/...`` path, but nothing answers
that path on the local filesystem. So the invariant is checked the way an app can
actually reach a volume: through the Files API, using the app's own identity.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4


def probe_artifact_volume(volume_path: str, client: Any | None = None) -> None:
    """Prove this app identity can durably write inside ``volume_path``.

    ``client`` defaults to an ambient ``WorkspaceClient``, which in Apps carries
    the app service principal's credentials.
    """
    import io

    path = str(volume_path).rstrip("/")
    if not path.startswith("/Volumes/"):
        raise RuntimeError(
            "artifact Volume path must be an absolute /Volumes path, "
            f"got {volume_path!r}"
        )

    if client is None:
        from databricks.sdk import WorkspaceClient

        client = WorkspaceClient()

    probe = f"{path}/.omnigent-readiness-{uuid4().hex}.probe"
    payload = b"omnigent-volume-readiness\n"
    try:
        client.files.upload(probe, io.BytesIO(payload), overwrite=True)
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(
            "artifact Volume is not writable by the app identity"
        ) from exc

    # A write that cannot be read back is not durable, and the artifact store
    # would fail later, further from the cause.
    try:
        response = client.files.download(probe)
        contents = response.contents.read()
    except Exception as exc:  # noqa: BLE001
        _delete_quietly(client, probe)
        raise RuntimeError("artifact Volume write is not readable back") from exc
    if contents != payload:
        _delete_quietly(client, probe)
        raise RuntimeError("artifact Volume returned different bytes than written")

    try:
        client.files.delete(probe)
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError("artifact Volume probe cannot be removed") from exc


def _delete_quietly(client: Any, path: str) -> None:
    try:
        client.files.delete(path)
    except Exception:  # noqa: BLE001 — the caller is already reporting a failure
        pass


__all__ = ["probe_artifact_volume"]
