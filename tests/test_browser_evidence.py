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
    # initial + four non-wait actions, strictly desktop-only (phone viewport removed)
    assert len(shots) == 5
    for shot in shots:
        assert shot["viewport"] == "desktop"
        data = (project / shot["path"]).read_bytes()
        assert data[:8] == b"\x89PNG\r\n\x1a\n" and hashlib.sha256(data).hexdigest() == shot["sha256"]
        assert shot["title"] == "Outage log" and shot["revision"]["commit"]
    desktop = [step for step in result["actions"] if step["viewport"] == "desktop"]
    assert desktop[0]["result"] == "ok" and desktop[0]["visible_change"] is False   # typing changes no text
    assert desktop[1]["result"] == "ok" and desktop[1]["visible_change"] is True    # the live action works
    assert desktop[2]["result"] == "ok" and desktop[2]["visible_change"] is False   # a dead action, caught
    assert desktop[3]["result"] == "not_found"
    assert not [shot for shot in shots if shot["viewport"] == "phone"]

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
    monkeypatch.setattr(install.browser_runtime, "unpack",
                        lambda debs, runtime: (os.makedirs(runtime), Path(runtime, "fonts.conf").write_text("")))
    states = []
    monkeypatch.setattr(install, "_set", lambda step, status, *a, **k: states.append((step, status, a)))
    install._install_browser()
    assert states[-1][:2] == ("browser", "error") and "libnss3" in states[-1][2][0]
    stamp = json.loads((tmp_path / "shared" / "browser.install.json").read_text())
    assert stamp["binary"] == "" and "libnss3" in stamp["error"]
    assert "libnss3" in browser_evidence.status()["reason"] if os.environ.get("WORKSHOP_BROWSER") is None else True

    from server import readiness

    assert "browser" not in readiness.__dict__.get("REQUIRED_STEPS", ()), "readiness never waits on the browser"


def test_the_helper_is_installed_only_when_workshop_browser_enabled(client, monkeypatch):
    from server import user_content
    from server.users import user_manager
    from tests.test_user_content import _provisioned_home

    helper = Path(__file__).resolve().parents[1] / "assets" / "bin" / "workshop-browser"
    assert os.access(helper, os.X_OK)
    text = helper.read_text()
    assert "Open each screenshot with your image tool" in text and "VERIFICATION GAP" in text
    assert "--phone" not in text and "--both" not in text

    monkeypatch.delenv("WORKSHOP_BROWSER_ENABLED", raising=False)
    home_default = _provisioned_home(client, monkeypatch)
    assert not (Path(home_default) / ".local" / "bin" / "workshop-browser").exists()
    assert browser_evidence.status()["reason_code"] == "disabled"

    monkeypatch.setenv("WORKSHOP_BROWSER_ENABLED", "true")
    user_content.provision(user_manager.get("alice@example.com"))
    assert os.access(Path(home_default) / ".local" / "bin" / "workshop-browser", os.X_OK)


def test_only_the_official_chrome_for_testing_bucket_is_an_approved_publisher():
    import dependency_sources

    approved = "https://storage.googleapis.com/chrome-for-testing-public/156.0.8078.47/linux64/chrome-headless-shell-linux64.zip"
    assert dependency_sources.public_url(approved, kind="artifact")
    assert not dependency_sources.public_url("https://storage.googleapis.com/someone-else/chrome.zip", kind="artifact")
    assert not dependency_sources.public_url("https://storage.googleapis.com/chrome-for-testing-public/x?alt=1",
                                             kind="artifact")


_LOADED = "Mapoon ZS is worst, with 3,747,583 customer minutes lost across 308 outages."
_SLOW_PAGE = b"""<!doctype html><html><head><title>Substation Watch</title></head><body>
<h1>Substation Watch</h1><p id="status">Adding up 12 months of outages...</p>
<div id="rows"><div class="h-8 animate-pulse"></div><div class="h-8 animate-pulse"></div></div>
<script>fetch('/data').then((r) => r.text()).then((t) => {
  document.getElementById('status').textContent = t;
  document.getElementById('rows').remove(); });</script></body></html>"""


@pytest.mark.skipif(_chrome() is None, reason="no Chromium on this machine")
def test_a_capture_waits_for_the_pages_own_data_not_just_the_load_event(project, monkeypatch):
    # 28 Oct E2E: both deploy screenshots on seat 1 showed grey skeleton rows,
    # because the warehouse query had not returned 1.5 s after the load event.
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            if self.path == "/data":
                import time
                time.sleep(3)
                body, kind = _LOADED.encode(), "text/plain"
            else:
                body, kind = _SLOW_PAGE, "text/html"
            self.send_response(200)
            self.send_header("Content-Type", kind)
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setenv("DATABRICKS_APP_PORT", "8000")
    try:
        request = browser_evidence.InspectRequest(
            url=f"http://localhost:{server.server_address[1]}/", scope="local", viewports=["desktop"],
            project=project, actions=[], settle_seconds=0.5)
        shot, = browser_evidence.inspect(request, binary=_chrome())["artifacts"]
    finally:
        server.shutdown()
    assert shot["text_chars"] >= len("Substation Watch") + len(_LOADED)


def test_a_page_that_never_settles_is_captured_at_the_bound():
    clock = {"now": 0.0}

    class Devtools:
        def settle(self, seconds):
            clock["now"] += seconds

        def network_quiet(self, session, quiet):
            return False  # a request always in flight

        def call(self, method, params=None, session=None, timeout=20.0):
            clock["now"] += 0.05
            return {"result": {"value": {"busy": True, "text": "Loading"}}}

    ready = browser_evidence._wait_until_ready(Devtools(), "s", 1.5, 20.0, clock=lambda: clock["now"])
    assert ready is False
    assert 20.0 <= clock["now"] < 21.5
