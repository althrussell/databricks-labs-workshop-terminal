"""Browser evidence for the attendee's own app (W3).

Agents need to see what they built. Until now nothing in the terminal could
render a page: Omnigent's browser tool needs its desktop renderer (test1025,
"no browser renderer is connected"), and agents verified apps by reading their
own source. This module drives a prepared, pinned headless Chromium over the
DevTools protocol, outside the terminal's input/output path, and writes real
screenshots into the project. The agent then opens each PNG with its own image
tool, so the picture reaches the model through the gateway; a filename, a DOM
dump or OCR text is never treated as proof that the model saw the page.

Local and deployed verification are separate. ``local`` renders a dev server on
this machine. ``deployed`` opens the attendee's Databricks App signed in as the
attendee (their own OBO token, sent only to the app's origin). Operator
credentials are never used. A write path is proven only when query history
shows the *app's* service principal ran the statement: seat 1 in test1025 could
only show that the agent itself had written the table.

Anything missing (no browser, sign-in blocked, history not visible) becomes an
explicit verification gap, never a pass.
"""
from __future__ import annotations

import base64
import configparser
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from typing import Any
from urllib.parse import quote, urlsplit

from . import config
from .bootstrap import browser_runtime

#: The pinned build bootstrap installs (``chrome_headless_shell_linux_x64``).
BROWSER_STAMP = "browser.install.json"
VIEWPORTS = {
    # name: (width, height, device scale, mobile)
    "desktop": (1280, 800, 1, False),
    "phone": (390, 844, 3, True),
}
EVIDENCE_DIR = ".workshop/evidence"
_START_SECONDS = 20.0
_LOAD_SECONDS = 30.0
_MAX_ACTIONS = 12
_inspect_lock = threading.Semaphore(2)


class Gap(Exception):
    """An explicit verification gap: something could not be checked, and why."""

    def __init__(self, code: str, detail: str):
        super().__init__(detail)
        self.code = code
        self.detail = detail

    def as_dict(self) -> dict:
        return {"kind": "gap", "code": self.code, "detail": self.detail[:500]}


# ---------------------------------------------------------------------------
# The prepared browser
# ---------------------------------------------------------------------------

def browser_binary() -> str | None:
    explicit = os.environ.get("WORKSHOP_BROWSER", "").strip()
    if explicit:
        return explicit if os.path.isfile(explicit) and os.access(explicit, os.X_OK) else None
    try:
        stamp = json.loads((Path(config.shared_prefix()) / BROWSER_STAMP).read_text())
    except (OSError, ValueError):
        return None
    binary = str(stamp.get("binary") or "")
    return binary if binary and os.path.isfile(binary) and os.access(binary, os.X_OK) else None


def browser_env(binary: str) -> dict[str, str]:
    """Environment for the browser process alone.

    The prepared browser's libraries and fonts are not on the system paths, so
    its process (and only its process) gets ``LD_LIBRARY_PATH`` and
    ``FONTCONFIG_FILE``. App credentials are not handed to it.
    """
    env = {key: value for key, value in os.environ.items()
           if key not in {"DATABRICKS_CLIENT_ID", "DATABRICKS_CLIENT_SECRET", "LD_LIBRARY_PATH", "FONTCONFIG_FILE"}}
    try:
        stamp = json.loads((Path(config.shared_prefix()) / BROWSER_STAMP).read_text())
    except (OSError, ValueError):
        return env
    runtime = str(stamp.get("runtime") or "")
    if stamp.get("binary") == binary and runtime and os.path.isdir(runtime):
        env.update(browser_runtime.runtime_env(runtime))
    return env


def status() -> dict:
    """Whether agents can inspect pages here, and if not, why not."""
    binary = browser_binary()
    if binary is None:
        reason, code = "the prepared browser is not installed on this terminal", "not_installed"
        try:
            stamp = json.loads((Path(config.shared_prefix()) / BROWSER_STAMP).read_text())
            reason = str(stamp.get("error") or reason)
            code = str(stamp.get("reason_code") or code)
        except (OSError, ValueError):
            pass
        return {"available": False, "reason": reason, "reason_code": code}
    try:
        stamp = json.loads((Path(config.shared_prefix()) / BROWSER_STAMP).read_text())
    except (OSError, ValueError):
        stamp = {}
    return {"available": True, "version": stamp.get("version", ""), "binary": binary}


def launch_args(binary: str, profile: str) -> list[str]:
    # --no-sandbox: Apps containers have no user namespaces for Chromium's
    # sandbox. The browser only opens the attendee's own app origins.
    return [binary, "--headless", "--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage",
            "--remote-debugging-address=127.0.0.1", "--remote-debugging-port=0",
            f"--user-data-dir={profile}", "--no-first-run", "--no-default-browser-check",
            "--hide-scrollbars", "--mute-audio", "--disable-extensions", "about:blank"]


def _devtools_url(process: subprocess.Popen) -> str:
    deadline = time.monotonic() + _START_SECONDS
    seen: list[str] = []
    assert process.stderr is not None
    while time.monotonic() < deadline:
        line = process.stderr.readline()
        if not line:
            if process.poll() is not None:
                break
            continue
        text = line.decode("utf-8", "replace").strip()
        seen.append(text)
        match = re.search(r"DevTools listening on (ws://\S+)", text)
        if match:
            return match.group(1)
    raise Gap("browser_failed_to_start", " | ".join(seen[-4:]) or "the browser exited before it was ready")


class _DevTools:
    """A minimal synchronous DevTools client over one browser websocket."""

    def __init__(self, url: str, *, auth_origin: str = "", token: str = ""):
        from websockets.sync.client import connect

        self._connection = connect(url, max_size=64 * 1024 * 1024, open_timeout=10)
        self._ws = self._connection.__enter__()
        self._next = 0
        self._events: list[dict] = []
        self._auth_origin = auth_origin
        self._token = token
        self.console_errors: list[str] = []
        self.document_status: int | None = None

    def close(self) -> None:
        try:
            self._connection.__exit__(None, None, None)
        except Exception:  # noqa: BLE001
            pass

    def _handle_event(self, message: dict) -> None:
        method = message.get("method", "")
        params = message.get("params") or {}
        session = message.get("sessionId")
        if method == "Fetch.requestPaused":
            request = params.get("request") or {}
            headers = [{"name": k, "value": v} for k, v in (request.get("headers") or {}).items()
                       if k.lower() != "authorization"]
            if self._token and _origin(request.get("url", "")) == self._auth_origin:
                # The attendee's own token, and only ever to their app's origin.
                headers.append({"name": "Authorization", "value": f"Bearer {self._token}"})
            self._send("Fetch.continueRequest", {"requestId": params.get("requestId"), "headers": headers},
                       session, expect=False)
            return
        if method == "Runtime.exceptionThrown":
            detail = (params.get("exceptionDetails") or {})
            self.console_errors.append(str((detail.get("exception") or {}).get("description")
                                           or detail.get("text") or "uncaught exception")[:300])
        elif method == "Runtime.consoleAPICalled" and params.get("type") == "error":
            args = params.get("args") or []
            self.console_errors.append(" ".join(str(a.get("value", a.get("description", ""))) for a in args)[:300])
        elif method == "Network.responseReceived" and params.get("type") == "Document":
            self.document_status = int((params.get("response") or {}).get("status") or 0)
        self._events.append(message)

    def _send(self, method: str, params: dict | None, session: str | None, *, expect: bool = True) -> int:
        self._next += 1
        message: dict[str, Any] = {"id": self._next, "method": method, "params": params or {}}
        if session:
            message["sessionId"] = session
        self._ws.send(json.dumps(message))
        return self._next

    def call(self, method: str, params: dict | None = None, session: str | None = None,
             timeout: float = 20.0) -> dict:
        wanted = self._send(method, params, session)
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise Gap("browser_timeout", f"{method} did not answer in {timeout:.0f}s")
            message = json.loads(self._ws.recv(timeout=remaining))
            if message.get("id") == wanted:
                if "error" in message:
                    raise Gap("browser_error", f"{method}: {message['error'].get('message', '')}")
                return message.get("result") or {}
            if "method" in message:
                self._handle_event(message)

    def wait_for(self, method: str, session: str, timeout: float) -> bool:
        for index, event in enumerate(self._events):
            if event.get("method") == method and event.get("sessionId") == session:
                del self._events[index]
                return True
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                message = json.loads(self._ws.recv(timeout=max(0.05, deadline - time.monotonic())))
            except TimeoutError:
                break
            if "method" in message:
                self._handle_event(message)
                if message.get("method") == method and message.get("sessionId") == session:
                    return True
        return False

    def settle(self, seconds: float) -> None:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            try:
                message = json.loads(self._ws.recv(timeout=max(0.05, deadline - time.monotonic())))
            except TimeoutError:
                return
            if "method" in message:
                self._handle_event(message)


def _origin(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}" if parts.scheme and parts.netloc else ""


def check_target(url: str, scope: str) -> str:
    """Only the attendee's own app origins: a local dev server, or a Databricks App."""
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"} or not parts.hostname or parts.username or parts.password:
        raise ValueError("inspect an http(s) URL without embedded credentials")
    if scope == "local":
        if parts.hostname not in {"localhost", "127.0.0.1"}:
            raise ValueError("local inspection is for a dev server on this terminal (localhost)")
        from .otel_bootstrap import serving_port

        if str(parts.port or 80) == serving_port(os.environ):
            raise ValueError("that port is the Workshop Terminal itself; inspect your app on $DATABRICKS_APP_PORT")
    elif scope == "deployed":
        if parts.scheme != "https" or not parts.hostname.endswith(".databricksapps.com"):
            raise ValueError("deployed inspection is for your Databricks App (https://…databricksapps.com)")
    else:
        raise ValueError("scope is local or deployed")
    return url


# Finds the element an attendee would act on by what it says, not by selector.
_FIND_JS = """
(text) => {
  const want = text.trim().toLowerCase();
  const nodes = Array.from(document.querySelectorAll(
    'button, a[href], [role=button], [role=link], [role=tab], input[type=submit], input[type=button], summary, label'));
  const label = (n) => (n.innerText || n.value || n.getAttribute('aria-label') || '').trim().toLowerCase();
  const exact = nodes.find((n) => label(n) === want);
  const node = exact || nodes.find((n) => label(n).includes(want));
  if (!node) return {found: false};
  node.scrollIntoView({block: 'center'});
  const r = node.getBoundingClientRect();
  return {found: true, tag: node.tagName.toLowerCase(), text: label(node).slice(0, 80),
          visible: r.width > 0 && r.height > 0, x: r.left + r.width / 2, y: r.top + r.height / 2};
}
"""
_FILL_JS = """
(selector, value) => {
  const node = document.querySelector(selector);
  if (!node) return {found: false};
  node.focus();
  const proto = node.tagName === 'TEXTAREA' ? HTMLTextAreaElement.prototype
    : node.tagName === 'SELECT' ? HTMLSelectElement.prototype : HTMLInputElement.prototype;
  Object.getOwnPropertyDescriptor(proto, 'value').set.call(node, value);
  node.dispatchEvent(new Event('input', {bubbles: true}));
  node.dispatchEvent(new Event('change', {bubbles: true}));
  return {found: true};
}
"""
_FACTS_JS = """
() => {
  const body = document.body;
  const text = body ? body.innerText : '';
  const controls = document.querySelectorAll('button, a[href], input, select, textarea, [role=button]');
  const unlabelled = Array.from(controls).filter((n) =>
    !(n.innerText || n.value || n.getAttribute('aria-label') || n.getAttribute('title') || n.labels?.length)).length;
  return {title: document.title, url: location.href, text_chars: text.length,
          text_sha256_input: text.slice(0, 20000), controls: controls.length, unlabelled_controls: unlabelled};
}
"""


def _evaluate(devtools: _DevTools, session: str, function: str, *args: Any) -> Any:
    expression = f"({function})({', '.join(json.dumps(a) for a in args)})"
    result = devtools.call("Runtime.evaluate", {"expression": expression, "returnByValue": True,
                                                "awaitPromise": True}, session)
    return (result.get("result") or {}).get("value")


def parse_actions(actions: list[str]) -> list[tuple[str, str]]:
    parsed = []
    for raw in actions[:_MAX_ACTIONS]:
        kind, sep, value = raw.partition(":")
        if not sep or kind not in {"click", "fill", "press", "wait"}:
            raise ValueError(f"action must be click:TEXT, fill:SELECTOR=VALUE, press:KEY or wait:SECONDS, got {raw!r}")
        parsed.append((kind, value))
    return parsed


def _act(devtools: _DevTools, session: str, kind: str, value: str) -> dict:
    if kind == "wait":
        devtools.settle(min(float(value or 1), 15.0))
        return {"action": f"wait:{value}", "result": "ok"}
    if kind == "press":
        key = value or "Enter"
        for event in ("keyDown", "keyUp"):
            devtools.call("Input.dispatchKeyEvent", {"type": event, "key": key, "code": key,
                                                     "windowsVirtualKeyCode": {"Enter": 13, "Tab": 9,
                                                                               "Escape": 27}.get(key, 0)}, session)
        return {"action": f"press:{key}", "result": "ok"}
    if kind == "fill":
        selector, _, text = value.partition("=")
        found = _evaluate(devtools, session, _FILL_JS, selector, text) or {}
        return {"action": f"fill:{selector}", "result": "ok" if found.get("found") else "not_found"}
    found = _evaluate(devtools, session, _FIND_JS, value) or {}
    if not found.get("found"):
        return {"action": f"click:{value}", "result": "not_found"}
    if not found.get("visible"):
        return {"action": f"click:{value}", "result": "not_visible", "element": found.get("tag")}
    for event in ("mousePressed", "mouseReleased"):
        devtools.call("Input.dispatchMouseEvent", {"type": event, "x": found["x"], "y": found["y"],
                                                   "button": "left", "clickCount": 1}, session)
    return {"action": f"click:{value}", "result": "ok", "element": found.get("tag"), "label": found.get("text")}


def _revision(project: Path) -> dict:
    """The source revision the evidence describes, so later edits make it stale."""
    def git(*args: str) -> str:
        try:
            done = subprocess.run(["git", "-C", str(project), *args], capture_output=True, text=True, timeout=10)
            return done.stdout.strip() if done.returncode == 0 else ""
        except (OSError, subprocess.TimeoutExpired):
            return ""
    head = git("rev-parse", "HEAD")
    dirty = git("status", "--porcelain", "--untracked-files=normal", "--", ".", ":(exclude).workshop")
    return {"commit": head, "dirty": hashlib.sha256(dirty.encode()).hexdigest()[:16] if dirty else ""}


@dataclass
class InspectRequest:
    url: str
    scope: str
    viewports: list[str]
    actions: list[str]
    project: Path
    token: str = ""
    settle_seconds: float = 2.0


def inspect(request: InspectRequest, *, binary: str | None = None) -> dict:
    """Render the app, run the actions, and write real screenshots into the project."""
    check_target(request.url, request.scope)
    actions = parse_actions(request.actions)
    viewports = [v for v in request.viewports if v in VIEWPORTS] or ["desktop"]
    binary = binary or browser_binary()
    if binary is None:
        raise Gap("browser_unavailable", status().get("reason", "no prepared browser"))
    if request.scope == "deployed" and not request.token:
        raise Gap("sign_in_unavailable", "your Databricks sign-in for this terminal is not ready yet")
    out = request.project / EVIDENCE_DIR
    out.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    revision = _revision(request.project)
    with _inspect_lock:
        profile = tempfile.mkdtemp(prefix="workshop-browser-")
        process = subprocess.Popen(launch_args(binary, profile), stdout=subprocess.DEVNULL,
                                   stderr=subprocess.PIPE, start_new_session=True, env=browser_env(binary))
        devtools = None
        try:
            devtools = _DevTools(_devtools_url(process), auth_origin=_origin(request.url),
                                 token=request.token if request.scope == "deployed" else "")
            artifacts, steps = [], []
            for viewport in viewports:
                target = devtools.call("Target.createTarget", {"url": "about:blank"})["targetId"]
                session = devtools.call("Target.attachToTarget", {"targetId": target, "flatten": True})["sessionId"]
                for method in ("Page.enable", "Runtime.enable", "Network.enable"):
                    devtools.call(method, None, session)
                if devtools._token:
                    devtools.call("Fetch.enable", {"patterns": [{"urlPattern": "*", "requestStage": "Request"}]},
                                  session)
                width, height, scale, mobile = VIEWPORTS[viewport]
                devtools.call("Emulation.setDeviceMetricsOverride", {"width": width, "height": height,
                                                                     "deviceScaleFactor": scale, "mobile": mobile},
                              session)
                devtools.call("Page.navigate", {"url": request.url}, session)
                if not devtools.wait_for("Page.loadEventFired", session, _LOAD_SECONDS):
                    raise Gap("page_did_not_load", f"{request.url} did not finish loading in {_LOAD_SECONDS:.0f}s")
                devtools.settle(request.settle_seconds)
                facts = _evaluate(devtools, session, _FACTS_JS) or {}
                if request.scope == "deployed" and _origin(str(facts.get("url", ""))) != _origin(request.url):
                    raise Gap("sign_in_blocked", "the app redirected away from itself (a sign-in page); "
                              "nothing about the app itself was observed")
                artifacts.append(_capture(devtools, session, out, request, stamp, viewport, "initial",
                                          facts, revision))
                for index, (kind, value) in enumerate(actions, start=1):
                    before = facts.get("text_sha256_input", "")
                    step = _act(devtools, session, kind, value)
                    devtools.settle(1.5)
                    facts = _evaluate(devtools, session, _FACTS_JS) or {}
                    step["visible_change"] = facts.get("text_sha256_input", "") != before
                    step["viewport"] = viewport
                    steps.append(step)
                    if kind != "wait":
                        artifacts.append(_capture(devtools, session, out, request, stamp, viewport,
                                                  f"after-{index}", facts, revision, action=step))
                devtools.call("Target.closeTarget", {"targetId": target})
            return {"artifacts": artifacts, "actions": steps,
                    "console_errors": devtools.console_errors[:10],
                    "document_status": devtools.document_status}
        finally:
            if devtools is not None:
                devtools.close()
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
            process.wait(timeout=10)
            shutil.rmtree(profile, ignore_errors=True)


def _capture(devtools: _DevTools, session: str, out: Path, request: InspectRequest, stamp: str,
             viewport: str, label: str, facts: dict, revision: dict, action: dict | None = None) -> dict:
    shot = devtools.call("Page.captureScreenshot", {"format": "png"}, session, timeout=30)
    data = base64.b64decode(shot.get("data") or "")
    if not data:
        raise Gap("screenshot_failed", "the browser returned an empty screenshot")
    path = out / f"{stamp}-{request.scope}-{viewport}-{label}.png"
    path.write_bytes(data)
    return {
        "kind": "screenshot", "path": str(path.relative_to(request.project)),
        "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data), "scope": request.scope,
        "url": request.url, "viewport": viewport, "label": label, "title": facts.get("title", ""),
        "text_chars": facts.get("text_chars", 0), "controls": facts.get("controls", 0),
        "unlabelled_controls": facts.get("unlabelled_controls", 0), "action": action,
        "revision": revision, "captured_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


# ---------------------------------------------------------------------------
# Deployed writes, proven as the app's own identity
# ---------------------------------------------------------------------------

_WRITE = re.compile(r"\b(insert|update|merge|delete)\b", re.I)
# Unity Catalog formats whose rows live in Lakebase Postgres. An app writes them
# over the Postgres wire protocol, which SQL query history never records.
_POSTGRES_FORMATS = {"POSTGRESQL_FORMAT", "DATABRICKS_ROW_STORE_FORMAT"}
_LAKEBASE_NEXT = (
    "Lakebase Postgres writes never appear in SQL query history, so this check cannot prove them. "
    "Write through the deployed app instead (workshop-browser inspect <app url> --deployed --do click:\"Save\" ...), "
    "read the row back (reload the page, or query the table), and tell the attendee the app-identity check is unverified."
)


def _uses_lakebase(app: dict) -> bool:
    return any(isinstance(resource, dict) and ({"database", "postgres", "lakebase"} & set(resource))
               for resource in app.get("resources") or [])


def _require_query_history_coverage(client, base: str, headers: dict, table: str, app: dict) -> None:
    """Raise a Lakebase gap when query history cannot show who wrote ``table``."""
    parts = [part.strip("`") for part in table.strip().split(".")]
    via = " and the app uses a Lakebase database" if _uses_lakebase(app) else ""
    if len(parts) != 3 or not all(parts):
        raise Gap("lakebase_not_in_query_history",
                  f"{table!r} is not a Unity Catalog name (catalog.schema.table){via}. " + _LAKEBASE_NEXT)
    response = client.get(f"{base}/api/2.1/unity-catalog/tables/{quote('.'.join(parts), safe='.')}",
                          headers=headers, timeout=30)
    if response.status_code == 404:
        raise Gap("lakebase_not_in_query_history", f"{table} is not a Unity Catalog table{via}. " + _LAKEBASE_NEXT)
    if response.status_code >= 400:
        return  # cannot tell from here; query history still decides
    if str(response.json().get("data_source_format") or "").upper() in _POSTGRES_FORMATS:
        raise Gap("lakebase_not_in_query_history", f"{table} is a Lakebase Postgres table. " + _LAKEBASE_NEXT)


def verify_write(*, host: str, token: str, app_name: str, table: str, since_minutes: int = 30,
                 http=None) -> dict:
    """Did the deployed app's service principal write ``table`` recently?

    Reads query history as the attendee. The proof is a write statement whose
    executing principal is the app, not the agent or the attendee. Only Unity
    Catalog tables written through SQL appear there; a Lakebase table is an
    explicit gap, never a silent "not proven".
    """
    import requests  # a runtime dependency; httpx ships only with the test client

    client = http or requests.Session()
    headers = {"Authorization": f"Bearer {token}"}
    base = host.rstrip("/")
    app = client.get(f"{base}/api/2.0/apps/{app_name}", headers=headers, timeout=30)
    if app.status_code == 404:
        raise Gap("app_not_found", f"no app named {app_name!r} in this workspace")
    if app.status_code >= 400:
        raise Gap("app_unreadable", f"the app could not be read (HTTP {app.status_code})")
    details = app.json()
    principal_id = str(details.get("service_principal_id") or "")
    principal_name = str(details.get("service_principal_name") or details.get("service_principal_client_id") or "")
    if not principal_id:
        raise Gap("app_identity_unknown", "the app reports no service principal")
    _require_query_history_coverage(client, base, headers, table, details)
    since_ms = int((time.time() - since_minutes * 60) * 1000)
    history = client.get(f"{base}/api/2.0/sql/history/queries", headers=headers, timeout=30, params={
        "filter_by.user_ids": principal_id,
        "filter_by.query_start_time_range.start_time_ms": since_ms,
        "max_results": 100,
    })
    if history.status_code in {401, 403}:
        raise Gap("query_history_unavailable", "query history is not visible to you, so a write by the app "
                  "cannot be proven; check the table in the app instead and say it is unverified")
    if history.status_code >= 400:
        raise Gap("query_history_error", f"query history returned HTTP {history.status_code}")
    wanted = table.split(".")[-1].strip("`").lower()
    statements = []
    for query in history.json().get("res") or []:
        text = str(query.get("query_text") or "")
        kind = str(query.get("statement_type") or "")
        if (kind.upper() in {"INSERT", "UPDATE", "MERGE", "DELETE"} or _WRITE.search(text)) and wanted in text.lower():
            statements.append({"query_id": query.get("query_id"), "statement_type": kind or _WRITE.search(text).group(1).upper(),
                               "status": query.get("status"), "at_ms": query.get("query_start_time_ms")})
    return {"kind": "write_proof", "proven": bool(statements), "table": table, "app": app_name,
            "principal": principal_name, "statements": statements[:10],
            "checked_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "since_minutes": since_minutes}


def attendee_token(user) -> tuple[str, str]:
    """The attendee's own Databricks host and OBO token from their CLI profile."""
    parser = configparser.ConfigParser(interpolation=None)
    parser.read(Path(user.home) / ".databrickscfg")
    profile = config.obo_profile_name()
    if not parser.has_section(profile):
        return "", ""
    return parser.get(profile, "host", fallback="").rstrip("/"), parser.get(profile, "token", fallback="")


# ---------------------------------------------------------------------------
# The workflow record
# ---------------------------------------------------------------------------

def record(project: Path, entries: list[dict]) -> int:
    """Append evidence to ``evidence.inspect``; returns the record's new revision."""
    from .design_workflow import workflow

    current = workflow.load(project)
    base = current["revision"]
    inspect_evidence = current["evidence"].setdefault("inspect", {})
    for entry in entries:
        bucket = {"screenshot": "artifacts", "write_proof": "write_proofs"}.get(entry.get("kind"), "gaps")
        inspect_evidence.setdefault(bucket, []).append(entry)
    current["history"].append({"at": workflow.now(), "event": "evidence",
                               "kinds": sorted({e.get("kind", "gap") for e in entries})})
    saved = workflow.save(project, current, expected_revision=None, base_revision=base)
    return saved["revision"]


__all__ = ["Gap", "InspectRequest", "attendee_token", "browser_binary", "check_target", "inspect",
           "parse_actions", "record", "status", "verify_write"]
