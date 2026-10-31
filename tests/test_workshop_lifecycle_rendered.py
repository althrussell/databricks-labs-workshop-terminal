"""Committed WT React UI against real local APIs and retained closeout state.

Only the browser event socket is a local transport fixture. No cloud workspace,
gateway model or real attendee fleet is allocated by these rendered regressions.
"""

import asyncio
import copy
import os
from pathlib import Path
import re

import pytest

from server import auth, closeout, config, main
from server import event_emitter as emitter_module
from server.capture_journal import CaptureJournal
from server.event_emitter import EventEmitter

from .test_closeout_api import ADMIN, BINDING, OWNER, request, retained  # noqa: F401

pytestmark = [pytest.mark.skipif(os.environ.get("WORKSHOP_E2E_BROWSER_TESTS") != "1",
                               reason="opt-in committed WT lifecycle browser/local API test"),
              pytest.mark.usefixtures("retained")]
ROOT = Path(__file__).resolve().parents[1]


class LocalLifecycleApp:
    def __init__(self, client):
        self.client = client
        self.faults = False
        self.requests = []

    async def start(self):
        web = pytest.importorskip("aiohttp.web")

        async def handler(browser_request):
            path = browser_request.path
            if path.startswith("/ws/"):
                socket = web.WebSocketResponse()
                await socket.prepare(browser_request)
                async for _message in socket:
                    await socket.send_json({"t": "pong"})
                return socket
            if not path.startswith("/api/"):
                return web.FileResponse(ROOT / "static" / (path.removeprefix("/") or "index.html"))
            self.requests.append((path, browser_request.method))
            if self.faults and path in {"/api/config", "/api/workshop-status"}:
                return web.json_response({"detail": "websocket_lost"}, status=503)
            body = await browser_request.read()
            response = await asyncio.to_thread(self.client.request, browser_request.method,
                browser_request.path_qs, content=body or None,
                headers={**OWNER, "Content-Type": "application/json"})
            return web.Response(body=response.content, status=response.status_code,
                                content_type="application/json")

        application = web.Application()
        application.router.add_route("*", "/{tail:.*}", handler)
        self.runner = web.AppRunner(application, shutdown_timeout=.1)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        self.url = "http://127.0.0.1:" + str(site._server.sockets[0].getsockname()[1])
        return self

    async def stop(self):
        await self.runner.cleanup()


async def assert_disabled_activities(page):
    activities = page.get_by_role("group", name="Workshop activities", exact=True)
    assert await activities.get_attribute("disabled") is not None
    assert await activities.get_by_role("button", name="New build", exact=True).is_disabled()
    assert await page.get_by_role("button", name="Agent preferences", exact=True).is_disabled()
    assert await page.get_by_role("button", name="Certificate", exact=True).is_disabled()
    assert await page.get_by_role("button", name=re.compile(r"^(?:Raise hand|Hand raised)$")).is_disabled()
    chat = page.get_by_role("complementary", name="Help chat with operators")
    assert await chat.get_by_role("textbox").is_disabled()
    assert await chat.get_by_role("button", name="Send", exact=True).is_disabled()
    await chat.get_by_text("Saved local help question", exact=True).wait_for()
    docs = page.get_by_role("link", name="AI Docs", exact=True)
    assert await docs.is_enabled()
    assert await docs.get_attribute("href") == "https://naveenb83.github.io/ai-dev-day/"
    assert not await page.evaluate("document.documentElement.scrollWidth > innerWidth")


def replace_emitter(source, monkeypatch):
    replacement = EventEmitter(run_id=BINDING["run_id"], workspace_id=BINDING["workspace_id"],
        ingest_url="", ingest_token="", journal=CaptureJournal(source.journal.storage, BINDING, {}))
    monkeypatch.setattr(emitter_module, "event_emitter", replacement)
    monkeypatch.setattr(main, "event_emitter", replacement)
    return replacement


async def lifecycle_journey(client, source, monkeypatch, tmp_path, width):
    browser_api = pytest.importorskip("playwright.async_api")
    monkeypatch.setattr(config, "onboarding_wizard_enabled", lambda: False)
    monkeypatch.setattr(auth, "get_groups", lambda principal:
        {config.admin_group()} if principal.name == ADMIN["X-Forwarded-Email"] else set())
    ready = False
    monkeypatch.setattr(closeout, "_drain", lambda _timeout: {
        "complete": ready, "issues": [] if ready else ["rendered_fixture_drain_pending"], "outcomes": []})
    application = await LocalLifecycleApp(client).start()
    try:
        async with browser_api.async_playwright() as engine:
            browser = await engine.chromium.launch(headless=True,
                executable_path=os.environ.get("WORKSHOP_BROWSER_EXECUTABLE"))
            try:
                page = await browser.new_page(viewport={"width": width, "height": 900})
                page.set_default_timeout(5000)
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                await page.clock.install()
                await page.goto(application.url)
                new_build = page.get_by_role("button", name="New build", exact=True)
                await new_build.wait_for()
                await new_build.click()
                await page.get_by_role("textbox", name="Build name", exact=True).fill("Unsent local build")
                await page.get_by_role("button", name="Ask for help", exact=True).click()
                chat = page.get_by_role("complementary", name="Help chat with operators")
                await chat.get_by_role("textbox").fill("Saved local help question")
                await chat.get_by_role("button", name="Send", exact=True).click()
                await chat.get_by_text("Saved local help question", exact=True).wait_for()
                await chat.get_by_role("textbox").fill("Unsent local help draft")
                await chat.get_by_role("button", name="Close help chat", exact=True).click()
                await page.get_by_role("button", name="Agent preferences", exact=True).click()
                await page.get_by_role("dialog", name="Agent preferences", exact=True).wait_for()

                # Drive the normal admin contract into a retained, pending
                # drain. Browser polling observes the same authoritative gate.
                response = client.post("/api/admin/capture-closeout/begin", headers=ADMIN, json=request(source))
                assert response.status_code == 200, response.text
                assert response.json()["phase"] in {"CLOSING", "INCOMPLETE"}, response.text
                await page.clock.fast_forward(10001)
                await page.get_by_role("status").filter(has_text="This workshop is closing.").wait_for()
                assert await page.get_by_role("dialog").count() == 0
                await page.get_by_role("button", name="Ask for help", exact=True).click()
                await assert_disabled_activities(page)
                assert await chat.get_by_role("textbox").input_value() == "Unsent local help draft"
                assert await page.get_by_role("textbox", name="Build name", exact=True).input_value() == "Unsent local build"
                await page.screenshot(path=str(tmp_path / f"workshop-closing-{width}.png"))

                # A failed refresh retains the barrier and drafts, rather than
                # presenting a relaunch/recovery action over closed input.
                application.faults = True
                before = copy.deepcopy(source.journal.read())
                await page.clock.fast_forward(10001)
                await assert_disabled_activities(page)
                assert source.journal.read() == before
                application.faults = False

                # Reopening the same durable source simulates the supported
                # replacement-container boundary; no OPEN response is invented.
                replacement = replace_emitter(source, monkeypatch)
                await page.reload()
                await page.get_by_role("status").filter(has_text="This workshop is closing.").wait_for()
                await page.get_by_role("button", name="Ask for help", exact=True).click()
                await assert_disabled_activities(page)
                ready = True
                response = client.get("/api/admin/capture-closeout/status", headers=ADMIN,
                                      params={"capture_id": "capture-api"})
                assert response.status_code == 200, response.text
                assert response.json()["phase"] == "SEALED", response.text
                assert response.json()["complete"] is True
                await page.clock.fast_forward(10001)
                await page.get_by_role("status").filter(has_text="This workshop has ended.").wait_for()
                await assert_disabled_activities(page)
                await page.reload()
                await page.get_by_role("status").filter(has_text="This workshop has ended.").wait_for()
                await page.get_by_role("button", name="Ask for help", exact=True).click()
                await assert_disabled_activities(page)
                assert replacement.journal.read()["closeout"]["phase"] == "SEALED"
                assert sum(path == "/api/help/messages" and method == "POST"
                           for path, method in application.requests) == 1
                assert not any(path == "/api/projects/select" or path == "/api/sessions" and method == "POST"
                               for path, method in application.requests)
                assert not errors, errors
                text = await page.locator("body").inner_text()
                assert "account team" not in text.lower() and "follow-up" not in text.lower()
            finally:
                await browser.close()
    finally:
        await application.stop()


@pytest.mark.parametrize("width", [390, 1440])
def test_rendered_closeout_read_only_help_link_and_retained_restart(client, monkeypatch, tmp_path, width):
    asyncio.run(lifecycle_journey(client, main.event_emitter, monkeypatch, tmp_path, width))


async def reload_journey(client):
    browser_api = pytest.importorskip("playwright.async_api")
    application = await LocalLifecycleApp(client).start()
    application.faults = True
    try:
        async with browser_api.async_playwright() as engine:
            browser = await engine.chromium.launch(headless=True,
                executable_path=os.environ.get("WORKSHOP_BROWSER_EXECUTABLE"))
            try:
                page = await browser.new_page()
                await page.goto(application.url)
                reload_button = page.get_by_role("button", name="Reload", exact=True)
                await reload_button.wait_for()
                before = sum(path == "/api/config" for path, _method in application.requests)
                application.faults = False
                await reload_button.click()
                await page.get_by_role("button", name="New build", exact=True).wait_for()
                assert sum(path == "/api/config" for path, _method in application.requests) > before
                assert await reload_button.count() == 0
            finally:
                await browser.close()
    finally:
        await application.stop()


def test_rendered_startup_status_failure_still_allows_read_only_reload(client, monkeypatch):
    monkeypatch.setattr(config, "onboarding_wizard_enabled", lambda: False)
    asyncio.run(reload_journey(client))
