"""Rendered WT against its real local project/session APIs and harmless shells.

This proves project UI, source/context isolation and cwd handoff, not model or
Databricks app-build quality. Live qualification remains a release check.
"""
import asyncio
import os
from pathlib import Path
import re
import time

import pytest

from server import config, projects
from server.sessions import session_manager
from server.users import user_manager

pytestmark = pytest.mark.skipif(os.environ.get("WORKSHOP_E2E_BROWSER_TESTS") != "1",
                              reason="opt-in rendered WT project journey")
ROOT = Path(__file__).resolve().parents[1]
EMAIL = "alice@example.com"


async def exercise(client, tmp_path, monkeypatch, width):
    web = pytest.importorskip("aiohttp.web")
    browser_api = pytest.importorskip("playwright.async_api")
    monkeypatch.setattr(config, "users_root", lambda: str(tmp_path / "users"))
    monkeypatch.setattr(config, "onboarding_wizard_enabled", lambda: False)
    user = user_manager.get(EMAIL)
    a = projects.project_path(user, "repair-desk")
    a.mkdir()
    (a / "app.py").write_text("keep the repair desk source\n")
    projects.select(user, "repair-desk", goal="Track bicycle repairs")
    errors = []
    deliveries = []
    failed_requests = []

    async def handler(request):
        if request.path.startswith("/ws/"):
            ws = web.WebSocketResponse()
            await ws.prepare(request)
            async for _ in ws:
                pass
            return ws
        if not request.path.startswith("/api/"):
            return web.FileResponse(ROOT / "static" / (request.path.removeprefix("/") or "index.html"))
        payload = await request.json() if request.method == "POST" else None
        if request.path.endswith("/type"):
            deliveries.append(payload["text"])
            # Keep the controlled shell out of model-directed prompt typing.
            return web.json_response({"status": "ok"})
        response = client.request(request.method, request.path, json=payload,
                                  headers={"X-Forwarded-Email": EMAIL})
        if response.status_code >= 400:
            failed_requests.append((request.path, response.status_code, response.text))
        return web.Response(body=response.content, status=response.status_code,
                            content_type="application/json")

    application = web.Application()
    application.router.add_route("*", "/{tail:.*}", handler)
    runner = web.AppRunner(application, shutdown_timeout=.1)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    url = "http://127.0.0.1:" + str(site._server.sockets[0].getsockname()[1])
    try:
        async with browser_api.async_playwright() as engine:
            browser = await engine.chromium.launch(headless=True, executable_path=os.environ.get("WORKSHOP_BROWSER_EXECUTABLE"))
            try:
                page = await browser.new_page(viewport={"width": width, "height": 900})
                page.set_default_timeout(5000)
                page.on("pageerror", lambda error: errors.append(str(error)))
                await page.goto(url)
                controls = page.get_by_role("region", name="Workshop builds")
                await controls.get_by_text("repair-desk", exact=True).wait_for()
                await page.get_by_role("button", name="Claude Code", exact=False).first.click()
                await page.get_by_role("button", name="Close Claude Code", exact=True).wait_for()
                # Resize the live header through its crowded laptop width and back.
                # Every icon retains a name/tooltip and all controls remain reachable.
                nav = page.get_by_role("navigation", name="Workshop navigation")
                for nav_width, compact in [(2400, "false"), (1234, "true"), (390, "true"), (2400, "false")]:
                    await page.set_viewport_size({"width": nav_width, "height": 900})
                    await page.wait_for_function("([width, compact]) => window.innerWidth === width && document.querySelector('.header-links').dataset.compact === compact", arg=[nav_width, compact])
                    geometry = await nav.evaluate("""element => {
                        const nav = element.getBoundingClientRect();
                        const user = document.querySelector('.header-user').getBoundingClientRect();
                        return [...element.querySelectorAll('a,button')].map(control => {
                            const box = control.getBoundingClientRect();
                            return {name:control.getAttribute('aria-label'), tooltip:control.title,
                                inNav:box.left >= nav.left - 1 && box.right <= nav.right + 1,
                                inScreen:box.left >= 0 && box.right <= innerWidth,
                                clearOfUser:box.right <= user.left || box.left >= user.right || box.bottom <= user.top || box.top >= user.bottom};
                        });
                    }""")
                    assert all(c["name"] and c["tooltip"] and c["inNav"] and c["inScreen"] and c["clearOfUser"] for c in geometry), geometry
                    assert not await page.evaluate("document.documentElement.scrollWidth > innerWidth")
                await nav.get_by_role("button", name="Agent preferences", exact=True).click()
                await page.get_by_role("dialog").wait_for()
                await page.get_by_role("button", name="Done", exact=True).click()
                await page.set_viewport_size({"width": width, "height": 900})
                live = session_manager.active()
                assert live.project_name == "repair-desk"
                live.write_input("pwd\n")
                deadline = time.monotonic() + 2
                while str(a) not in live.scrollback.text() and time.monotonic() < deadline:
                    await asyncio.sleep(.02)
                assert str(a) in live.scrollback.text()
                await controls.get_by_role("button", name="Change goal", exact=True).click()
                await controls.get_by_role("textbox", name="What should change?").fill("Track repairs and pickup dates")
                await controls.get_by_role("button", name="Save goal", exact=True).click()
                await controls.get_by_text("Press Enter in the terminal", exact=False).wait_for()
                assert "Track repairs and pickup dates" in projects.instructions(user)
                assert "Track repairs and pickup dates" in deliveries[-1]
                assert (a / "app.py").read_text() == "keep the repair desk source\n"
                await controls.get_by_role("button", name="New build", exact=True).click()
                await controls.get_by_role("textbox", name="Build name", exact=True).fill("Garden plan")
                await controls.get_by_role("textbox", name="What would you like to build?", exact=True).fill("Plan a garden")
                await controls.get_by_role("button", name="Create build", exact=True).click()
                await controls.get_by_role("group", name="Confirm build switch").wait_for()
                assert projects.read(user)["active"] == "repair-desk"
                assert session_manager.active() is live
                await controls.get_by_role("button", name="Stop agent and switch", exact=True).click()
                await controls.get_by_text("garden-plan is selected", exact=False).wait_for()
                assert session_manager.active() is None
                b = projects.project_path(user, "garden-plan")
                assert not b.exists()  # official scaffolder requires an absent target
                b.mkdir()
                (b / "app.py").write_text("separate garden source\n")
                await page.reload()
                await controls.get_by_text("garden-plan", exact=True).wait_for()
                assert await page.get_by_role("dialog").count() == 0
                await controls.get_by_role("button", name="My builds", exact=True).click()
                await controls.locator("li").filter(has_text="repair-desk").get_by_role("button", name="Continue", exact=True).click()
                await controls.get_by_text("repair-desk is selected", exact=False).wait_for()
                await page.get_by_role("button", name=re.compile(r"^Codex\b")).first.click()
                await page.wait_for_timeout(200)
                assert not failed_requests, failed_requests
                try:
                    await page.get_by_role("button", name="Close Codex", exact=True).wait_for(timeout=5000)
                except browser_api.TimeoutError:
                    pytest.fail(f"Codex did not open: {await page.locator('body').inner_text()} API failures: {failed_requests}")
                assert session_manager.active().project_name == "repair-desk"
                assert projects.launch_directory(user) == str(a)
                assert "Track repairs and pickup dates" in projects.instructions(user)
                assert "Plan a garden" not in projects.instructions(user)
                assert (b / "app.py").read_text() == "separate garden source\n"
                await page.reload()
                await controls.get_by_text("repair-desk", exact=True).wait_for()
                assert await page.get_by_role("dialog").count() == 0
                overflow = await page.evaluate("document.documentElement.scrollWidth > window.innerWidth")
                assert not overflow
                work_width = await page.locator(".work-area").evaluate("element => element.clientWidth")
                assert work_width >= min(width - 80, 600)  # no sidebar squeezing the build into a narrow strip
                assert not errors, errors
            finally:
                await browser.close()
    finally:
        await runner.cleanup()


@pytest.mark.parametrize("width", [390, 1440])
def test_build_goal_new_build_return_and_harness_switch(client, launchable_agents, tmp_path, monkeypatch, width):
    asyncio.run(exercise(client, tmp_path, monkeypatch, width))


async def exercise_lost_recovery(client, tmp_path, monkeypatch):
    web = pytest.importorskip("aiohttp.web")
    browser_api = pytest.importorskip("playwright.async_api")
    monkeypatch.setattr(config, "users_root", lambda: str(tmp_path / "users"))
    monkeypatch.setattr(config, "onboarding_wizard_enabled", lambda: False)
    user = user_manager.get(EMAIL)
    attempts = []

    def saved(_user):
        value = projects.inventory(user)
        if not value["projects"]:
            value["projects"].append(dict(name="film-club", goal="", available=False, saved=True, missing=True))
        return value

    def recover(_user, name):
        attempts.append(name)
        path = projects.project_path(user, name)
        path.mkdir()
        (path / "app.py").write_text("recovered film source\n")
        return projects.select(user, name, goal="Choose our next film")

    monkeypatch.setattr(projects, "saved_inventory", saved)
    monkeypatch.setattr(projects, "recover", recover)

    async def handler(request):
        if not request.path.startswith("/api/"):
            return web.FileResponse(ROOT / "static" / (request.path.removeprefix("/") or "index.html"))
        payload = await request.json() if request.can_read_body else None
        response = client.request(request.method, request.path, json=payload,
                                  headers={"X-Forwarded-Email": EMAIL})
        if request.path == "/api/projects/film-club/recover":
            assert response.status_code == 200
            # Send headers before losing the completed route's body. This
            # prevents Chromium from retrying the POST on a stale connection.
            lost = web.StreamResponse(status=200, headers={"Content-Type": "application/json", "Content-Length": str(len(response.content))})
            await lost.prepare(request)
            await lost.write(response.content[:16])
            request.transport.close()
            return lost
        return web.Response(body=response.content, status=response.status_code,
                            content_type="application/json")

    application = web.Application()
    application.router.add_route("*", "/{tail:.*}", handler)
    runner = web.AppRunner(application, shutdown_timeout=.1)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    url = "http://127.0.0.1:" + str(site._server.sockets[0].getsockname()[1])
    try:
        async with browser_api.async_playwright() as engine:
            browser = await engine.chromium.launch(headless=True, executable_path=os.environ.get("WORKSHOP_BROWSER_EXECUTABLE"))
            page = await browser.new_page()
            await page.goto(url)
            controls = page.get_by_role("region", name="Workshop builds")
            await controls.get_by_role("button", name="My builds", exact=True).click()
            await controls.get_by_role("button", name="Check saved builds", exact=True).click()
            await controls.get_by_role("button", name="Recover source", exact=True).click()
            await controls.get_by_text("Source recovered and selected", exact=False).wait_for()
            await controls.get_by_text("film-club", exact=True).wait_for()
            assert await controls.get_by_role("alert").count() == 0
            assert attempts == ["film-club"]
            assert projects.read(user)["active"] == "film-club"
            assert "Choose our next film" in projects.instructions(user)
            assert (projects.project_path(user, "film-club") / "app.py").read_text() == "recovered film source\n"
            await browser.close()
    finally:
        await runner.cleanup()


def test_lost_recovery_response_reconciles_confirmed_source(client, launchable_agents, tmp_path, monkeypatch):
    asyncio.run(exercise_lost_recovery(client, tmp_path, monkeypatch))
