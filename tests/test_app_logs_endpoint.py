"""`databricks apps logs` inside the terminal (28 Oct E2E: every call was a 404).

The endpoint read the app with ``json.load`` in a module that never imported
``json``. The broad ``except`` turned that NameError into "Could not read app",
and the wrapper printed a Python traceback, so the agent reported a log-access
problem it could not act on.
"""
from __future__ import annotations

import io
import json
import os
import subprocess
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest


class _AppResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _LogSocket:
    def __init__(self, frames):
        self._frames = list(frames)
        self.sent = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def send(self, value):
        self.sent.append(value)

    def recv(self, timeout=None):
        if not self._frames:
            raise TimeoutError
        return self._frames.pop(0)


def _capability(email: str) -> str:
    from server import user_content
    from server.users import user_manager

    user = user_manager.get(email)
    user.bootstrap_home()
    user_content._write_callback_capability(user)
    return Path(user_content.callback_capability_path(user)).read_text().strip()


@pytest.fixture()
def apps_api(monkeypatch):
    from server import main

    monkeypatch.setattr(main.credential_manager, "token", lambda: "app-token")
    calls = {}

    def respond(app_url="https://life-support-7.aws.databricksapps.com", error=None):
        def urlopen(request, timeout=None):
            calls["app_request"] = request.full_url
            if error is not None:
                raise error
            return _AppResponse(json.dumps({"name": "life-support", "url": app_url}).encode())

        monkeypatch.setattr(urllib.request, "urlopen", urlopen)

    frames = [
        json.dumps({"timestamp": 1793163000, "source": "APP", "message": "Uvicorn running\n"}),
        json.dumps({"timestamp": 1793163001, "source": "APP", "message": "GET /api/calls 200"}),
        "\x00",
    ]

    def connect(url, **kwargs):
        calls["ws_url"] = url
        calls["ws_origin"] = kwargs.get("origin")
        return _LogSocket(frames)

    import websockets.sync.client

    monkeypatch.setattr(websockets.sync.client, "connect", connect)
    return respond, calls


def test_logs_endpoint_returns_the_apps_recent_lines(client, apps_api):
    respond, calls = apps_api
    respond()
    email = "logs@example.com"

    response = client.post(
        "/api/apps/logs",
        headers={"X-Workshop-Capability": _capability(email)},
        json={"email": email, "app_name": "life-support", "tail_lines": 1},
    )

    assert response.status_code == 200, response.text
    assert response.json() == {
        "app_name": "life-support",
        "lines": ["2026-10-28T04:50:01Z [APP] GET /api/calls 200"],
    }
    assert calls["app_request"].endswith("/api/2.0/apps/life-support")
    assert calls["ws_url"] == "wss://life-support-7.aws.databricksapps.com/logz/stream"
    assert calls["ws_origin"] == "https://life-support-7.aws.databricksapps.com"


@pytest.mark.parametrize("code,expected", [(404, 404), (403, 502), (500, 502)])
def test_only_a_missing_app_is_a_404(client, apps_api, code, expected):
    respond, _ = apps_api
    respond(error=urllib.error.HTTPError("https://x", code, "nope", {}, None))
    email = "logs-missing@example.com"

    response = client.post(
        "/api/apps/logs",
        headers={"X-Workshop-Capability": _capability(email)},
        json={"email": email, "app_name": "life-support"},
    )

    assert response.status_code == expected
    assert "Could not read app 'life-support'" in response.json()["detail"]


class _Refuse(BaseHTTPRequestHandler):
    def do_POST(self):  # noqa: N802 — http.server's handler name
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        body = json.dumps({"detail": "Could not read app 'gone': HTTP Error 404: Not Found"}).encode()
        self.send_response(404)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def test_the_cli_wrapper_prints_the_reason_not_a_traceback(tmp_path, monkeypatch):
    from server import config
    from server.users import User

    prefix = tmp_path / "shared"
    (prefix / "bin").mkdir(parents=True)
    real = prefix / "bin" / "databricks"
    real.write_text("#!/bin/sh\necho real-cli \"$@\"\n")
    real.chmod(0o755)
    monkeypatch.setattr(config, "shared_prefix", lambda: str(prefix))
    monkeypatch.setattr(config, "users_root", lambda: str(tmp_path / "users"))
    user = User("wrapper@example.com")
    user._write_databricks_cli_wrapper()
    wrapper = Path(user.home) / ".local" / "bin" / "databricks"
    capability = Path(user.home) / ".config" / "workshop" / "callback-capability"
    capability.parent.mkdir(parents=True, exist_ok=True)
    capability.write_text("cap\n")

    server = HTTPServer(("127.0.0.1", 0), _Refuse)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        env = {
            **os.environ,
            "HOME": user.home,
            "WORKSHOP_USER_EMAIL": user.email,
            "WORKSHOP_APP_URL": f"http://127.0.0.1:{server.server_port}",
        }
        result = subprocess.run(
            [str(wrapper), "apps", "logs", "gone", "--tail-lines", "20"],
            env=env, capture_output=True, text=True, timeout=30,
        )
    finally:
        server.shutdown()

    assert result.returncode == 1
    assert "Traceback" not in result.stderr
    assert "databricks apps logs: Could not read app 'gone'" in result.stderr
