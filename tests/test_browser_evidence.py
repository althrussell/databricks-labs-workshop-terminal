"""Browser evidence (W3): real screenshots, honest gaps, writes proven as the app.

The real-render test drives an actual Chromium when one is available
(``WORKSHOP_TEST_BROWSER``, Google Chrome, or a headless shell) and is skipped
otherwise; CI's Ubuntu runners ship Google Chrome.
"""
from __future__ import annotations

import glob
import hashlib
import http.server
import json
import os
from pathlib import Path
import shutil
import subprocess
import threading
import zipfile

import pytest

from server import browser_evidence, design_workflow


def _chrome() -> str | None:
    candidates = [os.environ.get("WORKSHOP_TEST_BROWSER", ""),
                  "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                  shutil.which("google-chrome") or "", shutil.which("chromium") or "",
                  shutil.which("chrome-headless-shell") or ""]
    candidates += sorted(glob.glob(os.path.expanduser(
        "~/Library/Caches/ms-playwright/chromium_headless_shell-*/chrome-headless-shell-*/chrome-headless-shell")))
    return next((c for c in candidates if c and os.path.isfile(c) and os.access(c, os.X_OK)), None)


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "projects" / "outages"
    root.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    (root / "app.py").write_text("print('hi')\n")
    subprocess.run(["git", "-C", str(root), "add", "."], check=True)
    subprocess.run(["git", "-C", str(root), "-c", "core.hooksPath=/dev/null", "-c", "user.email=t@x", "-c", "user.name=t", "commit", "-qm", "init"],
                   check=True)
    return root


def test_only_the_attendees_own_app_origins_can_be_inspected(monkeypatch):
    monkeypatch.setenv("DATABRICKS_APP_PORT", "8000")
    assert browser_evidence.check_target("http://localhost:8123/", "local")
    for url, scope, problem in [
        ("http://localhost:8000/", "local", "Workshop Terminal itself"),
        ("https://example.com/", "local", "dev server on this terminal"),
        ("http://user:pw@localhost:8123/", "local", "embedded credentials"),
        ("https://evil.example.com/", "deployed", "databricksapps.com"),
        ("http://outages-1.aws.databricksapps.com/", "deployed", "databricksapps.com"),
    ]:
        with pytest.raises(ValueError, match=problem):
            browser_evidence.check_target(url, scope)


def test_no_browser_is_an_explicit_gap_never_a_pass(project, monkeypatch, tmp_path):
    monkeypatch.setattr(browser_evidence.config, "shared_prefix", lambda: str(tmp_path / "shared"))
    monkeypatch.delenv("WORKSHOP_BROWSER", raising=False)
    assert browser_evidence.status()["available"] is False
    request = browser_evidence.InspectRequest(url="http://localhost:8123/", scope="local",
                                              viewports=["desktop"], actions=[], project=project)
    with pytest.raises(browser_evidence.Gap) as gap:
        browser_evidence.inspect(request)
    assert gap.value.code == "browser_unavailable"
    browser_evidence.record(project, [{**gap.value.as_dict(), "scope": "local"}])
    saved = design_workflow.read(project)
    assert saved["evidence"]["inspect"]["gaps"][0]["code"] == "browser_unavailable"
    assert design_workflow.verification(saved, project)["state"] == "unverified"


def test_deployed_inspection_needs_the_attendees_own_sign_in(project):
    request = browser_evidence.InspectRequest(url="https://outages-1.aws.databricksapps.com/", scope="deployed",
                                              viewports=["desktop"], actions=[], project=project, token="")
    with pytest.raises(browser_evidence.Gap) as gap:
        browser_evidence.inspect(request, binary="/bin/true")
    assert gap.value.code == "sign_in_unavailable"


_PAGE = b"""<!doctype html><html><head><title>Outage log</title></head><body>
<h1>Outages</h1><p id="count">0 open outages</p>
<input id="title" aria-label="Title"><button onclick="log()">Log an outage</button>
<button>Export</button>
<script>let n = 0; function log() { n += 1;
  document.getElementById('count').textContent = n + ' open outage' + (n === 1 ? '' : 's') + ': '
    + document.getElementById('title').value; }</script></body></html>"""


@pytest.mark.skipif(_chrome() is None, reason="no Chromium on this machine")
def test_a_real_render_captures_screenshots_and_catches_a_dead_action(project, monkeypatch):
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(_PAGE)

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setenv("DATABRICKS_APP_PORT", "8000")
    try:
        url = f"http://localhost:{server.server_address[1]}/"
        request = browser_evidence.InspectRequest(
            url=url, scope="local", viewports=["desktop", "phone"], project=project, settle_seconds=0.5,
            actions=["fill:#title=Fallen line", "click:Log an outage", "click:Export", "click:Delete everything"])
        result = browser_evidence.inspect(request, binary=_chrome())
    finally:
        server.shutdown()
    shots = result["artifacts"]
    # initial + four non-wait actions, at two viewports
    assert len(shots) == 10
    for shot in shots:
        data = (project / shot["path"]).read_bytes()
        assert data[:8] == b"\x89PNG\r\n\x1a\n" and hashlib.sha256(data).hexdigest() == shot["sha256"]
        assert shot["title"] == "Outage log" and shot["revision"]["commit"]
    desktop = [step for step in result["actions"] if step["viewport"] == "desktop"]
    assert desktop[0]["result"] == "ok" and desktop[0]["visible_change"] is False   # typing changes no text
    assert desktop[1]["result"] == "ok" and desktop[1]["visible_change"] is True    # the live action works
    assert desktop[2]["result"] == "ok" and desktop[2]["visible_change"] is False   # a dead action, caught
    assert desktop[3]["result"] == "not_found"
    phone = [shot for shot in shots if shot["viewport"] == "phone"]
    assert phone and phone[0]["label"] == "initial"

    browser_evidence.record(project, shots)
    saved = design_workflow.read(project)
    assert design_workflow.verification(saved, project)["state"] == "verified"
    # Editing the reviewed surface makes the review stale; tampering is flagged.
    (project / "app.py").write_text("print('changed')\n")
    subprocess.run(["git", "-C", str(project), "-c", "core.hooksPath=/dev/null", "-c", "user.email=t@x", "-c", "user.name=t", "commit", "-qam", "edit"],
                   check=True)
    assert design_workflow.verification(saved, project)["state"] == "stale"
    (project / shots[0]["path"]).write_bytes(b"not the screenshot")
    assert any("no longer matches" in p for p in design_workflow.check(saved, project))


class _Response:
    def __init__(self, status: int, body: dict):
        self.status_code = status
        self._body = body

    def json(self):
        return self._body


class _Client:
    def __init__(self, app: _Response, history: _Response):
        self.app, self.history, self.calls = app, history, []

    def get(self, url, headers=None, params=None):
        self.calls.append((url, headers, params))
        return self.app if "/api/2.0/apps/" in url else self.history


def test_a_write_is_proven_only_when_the_app_itself_ran_it():
    app = _Response(200, {"name": "outages", "service_principal_id": 4242, "service_principal_name": "app-outages"})
    history = _Response(200, {"res": [
        {"query_id": "q1", "statement_type": "SELECT", "query_text": "SELECT * FROM main.s.outages"},
        {"query_id": "q2", "statement_type": "INSERT", "query_text": "INSERT INTO main.s.outages VALUES (1)",
         "status": "FINISHED", "query_start_time_ms": 1}]})
    client = _Client(app, history)
    proof = browser_evidence.verify_write(host="https://dbc.example", token="attendee-token", app_name="outages",
                                          table="main.s.outages", http=client)
    assert proof["proven"] is True and proof["principal"] == "app-outages"
    assert [s["query_id"] for s in proof["statements"]] == ["q2"]
    url, headers, params = client.calls[1]
    assert params["filter_by.user_ids"] == "4242" and headers["Authorization"] == "Bearer attendee-token"

    none = browser_evidence.verify_write(host="https://dbc.example", token="t", app_name="outages",
                                         table="main.s.other", http=client)
    assert none["proven"] is False


@pytest.mark.parametrize("app_status, history_status, code", [
    (404, 200, "app_not_found"), (200, 403, "query_history_unavailable"), (200, 500, "query_history_error")])
def test_an_unprovable_write_is_a_gap(app_status, history_status, code):
    client = _Client(_Response(app_status, {"service_principal_id": 1}), _Response(history_status, {}))
    with pytest.raises(browser_evidence.Gap) as gap:
        browser_evidence.verify_write(host="https://dbc.example", token="t", app_name="a", table="t", http=client)
    assert gap.value.code == code


def test_evidence_is_recorded_only_inside_the_attendees_own_projects(tmp_path):
    from fastapi import HTTPException

    from server.main import _owned_project

    class User:
        home = str(tmp_path)

    (tmp_path / "projects" / "mine").mkdir(parents=True)
    (tmp_path / "elsewhere").mkdir()
    assert _owned_project(User(), str(tmp_path / "projects" / "mine")) == (tmp_path / "projects" / "mine").resolve()
    with pytest.raises(HTTPException) as refused:
        _owned_project(User(), str(tmp_path / "elsewhere"))
    assert refused.value.status_code == 403


def test_the_browser_install_is_optional_and_says_why_it_cannot_run(monkeypatch, tmp_path):
    from server.bootstrap import install

    archive = tmp_path / "browser.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        info = zipfile.ZipInfo("chrome-headless-shell-linux64/chrome-headless-shell")
        info.external_attr = 0o755 << 16
        bundle.writestr(info, "#!/bin/sh\necho 'error while loading shared libraries: libnss3.so' >&2\nexit 127\n")
    monkeypatch.setattr(install.config, "shared_prefix", lambda: str(tmp_path / "shared"))
    monkeypatch.setattr(install, "_verified_artifact", lambda name: (str(archive), {"sha256": "x" * 64}))
    states = []
    monkeypatch.setattr(install, "_set", lambda step, status, *a, **k: states.append((step, status, a)))
    install._install_browser()
    assert states[-1][:2] == ("browser", "error") and "libnss3" in states[-1][2][0]
    stamp = json.loads((tmp_path / "shared" / "browser.install.json").read_text())
    assert stamp["binary"] == "" and "libnss3" in stamp["error"]
    assert "libnss3" in browser_evidence.status()["reason"] if os.environ.get("WORKSHOP_BROWSER") is None else True

    from server import readiness

    assert "browser" not in readiness.__dict__.get("REQUIRED_STEPS", ()), "readiness never waits on the browser"


def test_the_helper_is_installed_with_the_other_workshop_tools():
    helper = Path(__file__).resolve().parents[1] / "assets" / "bin" / "workshop-browser"
    assert os.access(helper, os.X_OK)
    text = helper.read_text()
    assert "Open each screenshot with your image tool" in text and "VERIFICATION GAP" in text


def test_only_the_official_chrome_for_testing_bucket_is_an_approved_publisher():
    import dependency_sources

    approved = "https://storage.googleapis.com/chrome-for-testing-public/156.0.8078.47/linux64/chrome-headless-shell-linux64.zip"
    assert dependency_sources.public_url(approved, kind="artifact")
    assert not dependency_sources.public_url("https://storage.googleapis.com/someone-else/chrome.zip", kind="artifact")
    assert not dependency_sources.public_url("https://storage.googleapis.com/chrome-for-testing-public/x?alt=1",
                                             kind="artifact")
