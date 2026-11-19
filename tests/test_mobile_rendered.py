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
window.__voiceAborts = 0;
class FakeRecognition {
  start() {
    // Browsers throw here when another recognizer is already running.
    if (window.__voiceFailStart) throw new Error("InvalidStateError");
    window.__voiceStarts += 1;
    window.__lastRecognition = this;
    if (window.__voiceHoldOpen) return;
    setTimeout(() => {
      const alt = Object.assign([{ transcript: %s }], { isFinal: true });
      this.onresult && this.onresult({ results: Object.assign([alt], { length: 1 }) });
      this.onend && this.onend();
    }, 50);
  }
  stop() {
    if (window.__voiceStallStop) return;
    this.onend && this.onend();
  }
  abort() {
    window.__voiceAborts += 1;
    this.onend && this.onend();
  }
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
    session_sockets = []
    errors = []

    async def handler(request):
        if request.path.startswith("/ws/"):
            ws = web.WebSocketResponse()
            await ws.prepare(request)
            if request.path.startswith("/ws/sessions/"):
                session_sockets.append(ws)
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
                    # Touch launch must not auto-focus xterm's raw textarea (which pops open the OS keyboard).
                    assert not await page.evaluate("document.activeElement?.classList?.contains('xterm-helper-textarea') ?? false")
                    # Each work-area bar above the terminal is a single row, header actions stay in view, and tap targets meet 40px.
                    for selector in (".project-bar", ".your-build-bar", ".toolbar"):
                        bar_box = await page.locator(selector).bounding_box()
                        assert bar_box is not None and bar_box["height"] <= 58, f"{selector} wrapped to {bar_box}"
                    for nav_name in ("Home", "Raise hand", "Ask for help"):
                        nav_box = await page.get_by_role("button", name=nav_name, exact=False).first.bounding_box()
                        assert nav_box is not None and nav_box["height"] >= 40 and nav_box["x"] + nav_box["width"] <= 390 + 1
                    # Expanded Your build panel keeps readable labels on text-only buttons ("Explain this", "Get help").
                    await tap(page.locator(".your-build-toggle"))
                    for label in ("Explain this", "Get help"):
                        btn = page.locator(".your-build-panel").get_by_role("button", name=label, exact=True)
                        assert float((await btn.evaluate("el => getComputedStyle(el).fontSize")).removesuffix("px")) >= 13
                    await tap(page.locator(".your-build-toggle"))
                    # Docked under the terminal, inside the screen, with essential keys visible without scrolling.
                    await composer.wait_for()
                    assert await keys.is_visible()
                    for key_name in ("Escape", "Up arrow", "Down arrow", "Enter", "Ctrl+C (interrupt)"):
                        key_box = await keys.get_by_role("button", name=key_name, exact=True).bounding_box()
                        assert key_box is not None and key_box["x"] + key_box["width"] <= 390 + 1, f"{key_name} off-screen: {key_box}"
                    assert await phone_link.count() == 0
                    box = await composer.bounding_box()
                    assert box["y"] + box["height"] <= 844 + 1
                    assert await prompt.evaluate("el => getComputedStyle(el).fontSize") == "16px"
                    await tap(composer.get_by_role("button", name="Speak a prompt"))
                else:
                    # Desktop focuses xterm so typing works immediately; the prompt box opens from the agent bar's mic.
                    assert await page.evaluate("document.activeElement?.classList?.contains('xterm-helper-textarea') ?? false")
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

                # Drafts survive Home -> Agent navigation without unmounting PromptComposer.
                await prompt.fill("draft across views")
                await tap(page.get_by_role("button", name="Home", exact=True))
                await tap(page.get_by_role("button", name="Claude Code", exact=True))
                assert await prompt.input_value() == "draft across views"
                await prompt.fill("")

                if phone:
                    start = len(frames)
                    await tap(keys.get_by_role("button", name="Escape"))
                    await tap(keys.get_by_role("button", name="Up arrow"))
                    await tap(keys.get_by_role("button", name="Shift+Tab"))
                    assert await _inputs_after(frames, start, ["\x1b", "\x1b[A", "\x1b[Z"]) == ["\x1b", "\x1b[A", "\x1b[Z"]

                    # Finger-dragging over terminal output scrolls xterm 6's scrollback upward.
                    bulk = "".join(f"scroll-line-{i:02d}\r\n" for i in range(80))
                    await session_sockets[-1].send_str(json.dumps({"t": "output", "data": bulk}))
                    await page.wait_for_function("() => document.querySelector('.xterm-rows')?.textContent?.includes('scroll-line-79')")
                    await page.evaluate("""() => {
                      const el = document.querySelector('.terminal-container');
                      const mk = (y) => [new Touch({ identifier: 1, target: el, clientX: 180, clientY: y })];
                      el.dispatchEvent(new TouchEvent('touchstart', { touches: mk(120), changedTouches: mk(120), bubbles: true, cancelable: true }));
                      el.dispatchEvent(new TouchEvent('touchmove', { touches: mk(360), changedTouches: mk(360), bubbles: true, cancelable: true }));
                      el.dispatchEvent(new TouchEvent('touchend', { touches: [], changedTouches: mk(360), bubbles: true, cancelable: true }));
                    }""")
                    await page.wait_for_function("() => !document.querySelector('.xterm-rows')?.textContent?.includes('scroll-line-79')")

                    # Soft keyboard opening on a header/bar input must keep that bar visible; focusing prompt hides top bars.
                    await tap(page.get_by_role("button", name="Raise hand", exact=True))
                    note_input = page.get_by_placeholder("Optional note for the operator")
                    await note_input.wait_for()
                    await page.set_viewport_size({"width": 390, "height": 420})
                    await page.wait_for_function("() => document.documentElement.dataset.keyboard === 'open'")
                    assert await page.locator(".header").is_visible()
                    assert await note_input.is_visible()
                    await tap(page.get_by_role("button", name="Cancel", exact=True))
                    await prompt.focus()
                    await page.wait_for_function("() => getComputedStyle(document.querySelector('.header')).display === 'none'")
                    composer_kb_box = await composer.bounding_box()
                    assert composer_kb_box is not None and composer_kb_box["y"] + composer_kb_box["height"] <= 420 + 1
                    await page.set_viewport_size({"width": 390, "height": 844})
                    await page.evaluate("() => document.activeElement?.blur?.()")

                    # A recognizer that refuses to start leaves the mic usable, not stuck listening.
                    await page.evaluate("window.__voiceFailStart = true")
                    mic = composer.get_by_role("button", name="Speak a prompt")
                    await tap(mic)
                    await page.get_by_role("status").filter(has_text="Voice input is busy").wait_for()
                    assert await mic.get_attribute("aria-pressed") == "false"
                    assert await prompt.get_attribute("placeholder") == "Prompt the agent"
                    await page.evaluate("window.__voiceFailStart = false")

                    # Manual edits while listening stop dictation cleanly instead of letting late speech clobber edits;
                    # and a stalled stop() recovers on a second tap via abort().
                    await page.evaluate("window.__voiceHoldOpen = true; window.__voiceStallStop = true;")
                    await tap(mic)
                    await page.wait_for_function("() => window.__voiceStarts === 2")
                    stop_btn = composer.get_by_role("button", name="Stop listening")
                    await tap(stop_btn)
                    await tap(stop_btn)
                    assert await mic.get_attribute("aria-pressed") == "false"
                    await page.evaluate("window.__voiceStallStop = false")
                    await tap(mic)
                    await page.wait_for_function("() => window.__voiceStarts === 3")
                    await prompt.fill("manual edit wins")
                    await page.evaluate("""() => {
                      const rec = window.__lastRecognition;
                      const alt = Object.assign([{ transcript: 'late room noise' }], { isFinal: true });
                      rec.onresult && rec.onresult({ results: Object.assign([alt], { length: 1 }) });
                    }""")
                    assert await prompt.input_value() == "manual edit wins"
                    assert await mic.get_attribute("aria-pressed") == "false"
                    await prompt.fill("")

                    await tap(page.get_by_role("button", name="Ask for help", exact=False).first)
                    panel = page.locator(".help-chat-panel")
                    await panel.wait_for()
                    panel_box = await panel.bounding_box()
                    assert panel_box["x"] >= 0 and panel_box["x"] + panel_box["width"] <= 390 + 1
                else:
                    await composer.get_by_role("button", name="Close the prompt box").click()
                    assert not await composer.is_visible()

                    # Hidden TerminalView on Home must not steal PTY size; clicking back into the terminal reclaims size.
                    await page.get_by_role("button", name="Home", exact=True).click()
                    resize_before_home_focus = sum(1 for f in frames if f.get("t") == "resize")
                    await page.evaluate("() => window.dispatchEvent(new Event('focus'))")
                    await asyncio.sleep(0.05)
                    assert sum(1 for f in frames if f.get("t") == "resize") == resize_before_home_focus
                    await page.get_by_role("button", name="Claude Code", exact=True).click()
                    resize_before_click = sum(1 for f in frames if f.get("t") == "resize")
                    await page.locator(".terminal-container").dispatch_event("pointerdown")
                    await page.wait_for_function("() => true")
                    deadline = asyncio.get_running_loop().time() + 2
                    while sum(1 for f in frames if f.get("t") == "resize") <= resize_before_click:
                        assert asyncio.get_running_loop().time() < deadline, "clicking terminal did not reclaim size"
                        await asyncio.sleep(0.02)

                    await phone_link.click()
                    dialog = page.get_by_role("dialog", name="Open on phone")
                    await dialog.get_by_role("img", name=f"QR code for {url}/").wait_for()
                    # Clicking dialog padding/whitespace inside the card must not dismiss the modal.
                    await dialog.click(position={"x": 10, "y": 10})
                    assert await dialog.is_visible()
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
