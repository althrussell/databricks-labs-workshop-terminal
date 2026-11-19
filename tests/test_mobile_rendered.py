"""Rendered WT on a phone and a laptop: prompt box, voice, touch keys, layout.

The browser talks to the real local APIs; the terminal socket is a recorder,
so the test sees exactly the frames a phone or laptop sends to the agent. The
browser's speech service is replaced by a fake that "hears" a fixed sentence.
Real speech and real phones remain a live check (docs/mobile-and-voice.md).
"""
import asyncio
import json
import os
from pathlib import Path

import pytest

from server import config, projects
from server.users import user_manager

pytestmark = pytest.mark.skipif(os.environ.get("WORKSHOP_E2E_BROWSER_TESTS") != "1",
                              reason="opt-in rendered WT phone and laptop journey")
ROOT = Path(__file__).resolve().parents[1]
EMAIL = "alice@example.com"
HEARD = "add a sales chart"

FAKE_SPEECH = """
window.__voiceStarts = 0;
class FakeRecognition {
  start() {
    // Browsers throw here when another recognizer is already running.
    if (window.__voiceFailStart) throw new Error("InvalidStateError");
    window.__voiceStarts += 1;
    setTimeout(() => {
      const alt = Object.assign([{ transcript: %s }], { isFinal: true });
      this.onresult && this.onresult({ results: Object.assign([alt], { length: 1 }) });
      this.onend && this.onend();
    }, 50);
  }
  stop() { this.onend && this.onend(); }
}
window.SpeechRecognition = FakeRecognition;
window.webkitSpeechRecognition = FakeRecognition;
""" % json.dumps(HEARD)

PHONE = dict(viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True, device_scale_factor=3)
LAPTOP = dict(viewport={"width": 1440, "height": 900})


async def _inputs_after(frames, start, expected, timeout=3.0):
    """Wait until the input frames sent since `start` equal `expected`."""
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        sent = [f["data"] for f in frames[start:] if f.get("t") == "input"]
        if sent == expected or asyncio.get_running_loop().time() > deadline:
            return sent
        await asyncio.sleep(0.02)


async def exercise(client, tmp_path, monkeypatch, device):
    web = pytest.importorskip("aiohttp.web")
    browser_api = pytest.importorskip("playwright.async_api")
    monkeypatch.setattr(config, "users_root", lambda: str(tmp_path / "users"))
    monkeypatch.setattr(config, "onboarding_wizard_enabled", lambda: False)
    user = user_manager.get(EMAIL)
    projects.project_path(user, "phone-check").mkdir()
    projects.select(user, "phone-check", goal="Check WT on a phone")
    frames = []
    errors = []

    async def handler(request):
        if request.path.startswith("/ws/"):
            ws = web.WebSocketResponse()
            await ws.prepare(request)
            async for message in ws:
                if request.path.startswith("/ws/sessions/"):
                    frames.append(json.loads(message.data))
            return ws
        if not request.path.startswith("/api/"):
            return web.FileResponse(ROOT / "static" / (request.path.removeprefix("/") or "index.html"))
        payload = await request.json() if request.method == "POST" else None
        response = client.request(request.method, request.path, json=payload,
                                  headers={"X-Forwarded-Email": EMAIL})
        return web.Response(body=response.content, status=response.status_code,
                            content_type="application/json")

    application = web.Application()
    application.router.add_route("*", "/{tail:.*}", handler)
    runner = web.AppRunner(application, shutdown_timeout=.1)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    url = "http://127.0.0.1:" + str(site._server.sockets[0].getsockname()[1])
    phone = device == "phone"
    try:
        async with browser_api.async_playwright() as engine:
            browser = await engine.chromium.launch(headless=True, executable_path=os.environ.get("WORKSHOP_BROWSER_EXECUTABLE"))
            try:
                context = await browser.new_context(**(PHONE if phone else LAPTOP), reduced_motion="reduce")
                await context.add_init_script(FAKE_SPEECH)
                page = await context.new_page()
                page.set_default_timeout(5000)
                page.on("pageerror", lambda error: errors.append(str(error)))
                await page.goto(url)
                assert await page.evaluate("matchMedia('(pointer: coarse)').matches") is phone

                tap = (lambda loc: loc.tap()) if phone else (lambda loc: loc.click())
                await tap(page.get_by_role("button", name="Claude Code", exact=False).first)
                await page.get_by_role("button", name="Close Claude Code", exact=True).wait_for()
                deadline = asyncio.get_running_loop().time() + 5
                while not any(f.get("t") == "resize" for f in frames):
                    assert asyncio.get_running_loop().time() < deadline, "terminal socket never opened"
                    await asyncio.sleep(0.02)

                composer = page.get_by_role("form", name="Prompt for the agent")
                prompt = composer.get_by_role("textbox", name="Prompt")
                keys = page.get_by_role("toolbar", name="Terminal keys")
                phone_link = page.get_by_role("button", name="Open on phone", exact=True)
                if phone:
                    # Docked under the terminal, inside the screen, with the keys a phone lacks.
                    await composer.wait_for()
                    assert await keys.is_visible()
                    assert await phone_link.count() == 0
                    box = await composer.bounding_box()
                    assert box["y"] + box["height"] <= 844 + 1
                    assert await prompt.evaluate("el => getComputedStyle(el).fontSize") == "16px"
                    await tap(composer.get_by_role("button", name="Speak a prompt"))
                else:
                    # Terminal first: the prompt box opens from the agent bar's mic.
                    assert not await composer.is_visible()
                    assert await keys.count() == 0
                    await page.locator(".toolbar").get_by_role("button", name="Speak a prompt").click()
                    await composer.wait_for()
                await page.wait_for_function("() => window.__voiceStarts === 1")
                await page.wait_for_function(
                    "text => document.querySelector('.composer-input').value === text", arg=HEARD)

                # Send pastes the prompt, then presses Enter: nothing more, nothing less.
                start = len(frames)
                if phone:
                    await tap(composer.get_by_role("button", name="Send to the agent"))
                else:
                    await prompt.press("Enter")
                assert await _inputs_after(frames, start, [HEARD, "\r"]) == [HEARD, "\r"]
                assert await prompt.input_value() == ""

                if phone:
                    start = len(frames)
                    await tap(keys.get_by_role("button", name="Escape"))
                    await tap(keys.get_by_role("button", name="Up arrow"))
                    await tap(keys.get_by_role("button", name="Shift+Tab"))
                    assert await _inputs_after(frames, start, ["\x1b", "\x1b[A", "\x1b[Z"]) == ["\x1b", "\x1b[A", "\x1b[Z"]
                    # A recognizer that refuses to start leaves the mic usable, not stuck listening.
                    await page.evaluate("window.__voiceFailStart = true")
                    mic = composer.get_by_role("button", name="Speak a prompt")
                    await tap(mic)
                    await page.get_by_role("status").filter(has_text="Voice input is busy").wait_for()
                    assert await mic.get_attribute("aria-pressed") == "false"
                    assert await prompt.get_attribute("placeholder") == "Prompt the agent"
                    await page.evaluate("window.__voiceFailStart = false")
                    await tap(mic)
                    await page.wait_for_function("() => window.__voiceStarts === 2")
                    await tap(page.get_by_role("button", name="Ask for help", exact=False).first)
                    panel = page.locator(".help-chat-panel")
                    await panel.wait_for()
                    panel_box = await panel.bounding_box()
                    assert panel_box["x"] >= 0 and panel_box["x"] + panel_box["width"] <= 390 + 1
                else:
                    await composer.get_by_role("button", name="Close the prompt box").click()
                    assert not await composer.is_visible()
                    await phone_link.click()
                    dialog = page.get_by_role("dialog", name="Open on phone")
                    await dialog.get_by_role("img", name=f"QR code for {url}/").wait_for()
                    await dialog.get_by_role("button", name="Done").click()
                    assert await dialog.count() == 0

                assert not await page.evaluate("document.documentElement.scrollWidth > innerWidth")
                assert not errors, errors
            finally:
                await browser.close()
    finally:
        await runner.cleanup()


@pytest.mark.parametrize("device", ["phone", "laptop"])
def test_prompt_box_voice_and_touch_keys(client, launchable_agents, tmp_path, monkeypatch, device):
    asyncio.run(exercise(client, tmp_path, monkeypatch, device))
