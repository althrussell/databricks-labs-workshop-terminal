#!/usr/bin/env python3
"""Run integrity and mocked agent lifecycles inside the packaged PEX."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import stat
import sys
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
import zipfile


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _update_logical_digest(
    digest: "hashlib._Hash", relative: str, mode: int, data: bytes
) -> None:
    digest.update(relative.encode("utf-8"))
    digest.update(b"\0")
    digest.update(f"{mode:o}".encode("ascii"))
    digest.update(b"\0")
    digest.update(data)
    digest.update(b"\0")


def verify_artifact(artifact: Path, release_path: Path, source_root: Path) -> None:
    release = json.loads(release_path.read_text(encoding="utf-8"))
    if release["format_version"] != 1:
        raise AssertionError("unsupported release manifest")
    if release["artifact_name"] != artifact.name:
        raise AssertionError("release manifest names a different artifact")
    if release["size_bytes"] != artifact.stat().st_size:
        raise AssertionError("release manifest size mismatch")
    if release["sha256"] != _file_sha256(artifact):
        raise AssertionError("release artifact SHA-256 mismatch")
    if release["python_abi"] != "cp311" or release["platform"] != "linux_x86_64":
        raise AssertionError("release targets the wrong ABI or platform")
    if release["entry_point"] != "server.otel_bootstrap:main":
        raise AssertionError("release bypasses the early-OTel entry point")

    with zipfile.ZipFile(artifact) as archive:
        content = json.loads(archive.read("runtime-content-manifest.json"))
        digest = hashlib.sha256()
        for entry in content["files"]:
            relative = entry["path"]
            source = source_root / relative
            source_bytes = source.read_bytes()
            packaged_bytes = archive.read(relative)
            if packaged_bytes != source_bytes:
                raise AssertionError(f"packaged bytes differ from checkout: {relative}")
            if entry["size_bytes"] != len(source_bytes):
                raise AssertionError(f"content size mismatch: {relative}")
            if entry["sha256"] != _sha256(source_bytes):
                raise AssertionError(f"content hash mismatch: {relative}")
            mode = 0o755 if source.stat().st_mode & stat.S_IXUSR else 0o644
            if entry["mode"] != f"{mode:o}":
                raise AssertionError(f"content mode mismatch: {relative}")
            _update_logical_digest(digest, relative, mode, source_bytes)
        if content["file_count"] != len(content["files"]):
            raise AssertionError("content manifest file count mismatch")
        if content["logical_contents_sha256"] != digest.hexdigest():
            raise AssertionError("content manifest logical digest mismatch")
        if release["logical_contents_sha256"] != digest.hexdigest():
            raise AssertionError("external and embedded logical digests differ")

        pex_info = json.loads(archive.read("PEX-INFO"))
        distributions = {name.lower() for name in pex_info["distributions"]}
        for required in ("fastapi", "uvicorn", "grpcio", "cryptography", "pillow"):
            if not any(name.startswith(f"{required}-") for name in distributions):
                raise AssertionError(f"runtime dependency absent from PEX: {required}")
        for excluded in ("pytest", "httpx", "pex"):
            if any(name.startswith(f"{excluded}-") for name in distributions):
                raise AssertionError(f"non-runtime dependency present in PEX: {excluded}")

    catalog = json.loads((source_root / "content" / "agents.json").read_text())
    ids = {item["id"] for item in catalog}
    if ids != {"claude", "codex", "omnigent"}:
        raise AssertionError("packaged catalog differs from the supported agents")


def _request(
    method: str, url: str, payload: dict | None = None
) -> tuple[int, dict]:
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = Request(
        url,
        data=body,
        method=method,
        headers={
            "Content-Type": "application/json",
            "X-Forwarded-Email": "smoke@example.com",
        },
    )
    try:
        response = urlopen(request, timeout=3)
    except HTTPError as error:
        response = error
    with response:
        return response.status, json.loads(response.read())


def _wait(url: str) -> None:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            if _request("GET", f"{url}/healthz") == (200, {"status": "ok"}):
                return
        except (URLError, TimeoutError, json.JSONDecodeError):
            pass
        time.sleep(0.05)
    raise AssertionError("mock lifecycle server never became healthy")


def _lifecycle(url: str, agent_id: str) -> str:
    status, created = _request("POST", f"{url}/api/sessions", {"agent_id": agent_id})
    if status != 200:
        raise AssertionError(f"{agent_id} launch failed: {status} {created}")
    session = created["session"]
    if session["agent_id"] != agent_id:
        raise AssertionError(f"{agent_id} lifecycle returned another agent")
    status, sessions = _request("GET", f"{url}/api/sessions")
    if status != 200 or [item["id"] for item in sessions["sessions"]] != [session["id"]]:
        raise AssertionError(f"{agent_id} session did not remain active")
    status, typed = _request(
        "POST", f"{url}/api/sessions/{session['id']}/type", {"text": "smoke prompt"}
    )
    if status != 200 or typed != {"status": "ok"}:
        raise AssertionError(f"{agent_id} session rejected input")
    return session["id"]


def main() -> int:
    artifact = Path(os.environ["WT_RELEASE_ARTIFACT"])
    release = Path(os.environ["WT_RELEASE_MANIFEST"])
    source = Path(os.environ["WT_SOURCE_ROOT"])
    verify_artifact(artifact, release, source)

    os.environ.update(
        {
            "LOCAL_DEV": "1",
            "DATA_ROOT": "/tmp/wt-lifecycle-data",
            "DATABRICKS_HOST": "https://smoke.invalid",
            "WORKSHOP_AGENTS": "claude,codex,omnigent",
            "OMNIGENT_ENABLED": "true",
            "MAX_SESSIONS_PER_USER": "1",
            "MAX_SESSIONS_GLOBAL": "1",
        }
    )
    from server import main as application
    import uvicorn

    application.install.ready = lambda: {
        "claude": True,
        "codex": True,
        "omnigent": True,
    }
    application.install.failure_for = lambda _requires: ""
    application.ensure_user_credentials = lambda _user: None
    application.identity.observe = lambda _user: None
    application.agents.launch_command = lambda _agent: [
        sys.executable,
        "-c",
        "import time; time.sleep(60)",
    ]
    application.readiness.evaluate_runtime = lambda: {
        "ready": True,
        "problems": [],
        "smoke": True,
    }

    port = 8766
    url = f"http://127.0.0.1:{port}"
    server = uvicorn.Server(
        uvicorn.Config(application.app, host="127.0.0.1", port=port, workers=1)
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        _wait(url)
        status, agents = _request("GET", f"{url}/api/agents")
        ids = [item["id"] for item in agents["agents"]]
        if status != 200 or ids != ["omnigent", "claude", "codex"]:
            raise AssertionError(f"unexpected runtime agent catalog: {agents}")
        if _request("GET", f"{url}/readyz")[0] != 200:
            raise AssertionError("mocked packaged readiness is not green")

        claude = _lifecycle(url, "claude")
        status, conflict = _request(
            "POST", f"{url}/api/sessions", {"agent_id": "codex"}
        )
        if status != 409 or conflict.get("detail", {}).get("code") != "session_conflict":
            raise AssertionError("packaged runtime did not enforce one active session")
        if _request("DELETE", f"{url}/api/sessions/{claude}")[0] != 200:
            raise AssertionError("Claude session did not close")

        for agent_id in ("codex", "omnigent"):
            session_id = _lifecycle(url, agent_id)
            if _request("DELETE", f"{url}/api/sessions/{session_id}")[0] != 200:
                raise AssertionError(f"{agent_id} session did not close")
    finally:
        server.should_exit = True
        thread.join(timeout=5)
    if thread.is_alive():
        raise AssertionError("mock lifecycle server did not stop")
    print("packaged integrity and Claude/Codex/Omnigent lifecycles passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
