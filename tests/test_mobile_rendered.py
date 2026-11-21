"""Rendered WT on a phone and a laptop: prompt box, voice, touch keys, layout.

The phone runs Playwright's iPhone 15 Pro Max metrics (430x739 is the area Safari
leaves visible), so the tab-bar shell is measured against the real screen.

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

PHONE_DEVICE = "iPhone 15 Pro Max"
LAPTOP = dict(viewport={"width": 1440, "height": 900})


async def _inputs_after(frames, start, expected, timeout=3.0):
    """Wait until the input frames sent since `start` equal `expected`."""
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        sent = [f["data"] for f in frames[start:] if f.get("t") == "input"]
        if sent == expected or asyncio.get_running_loop().time() > deadline:
            return sent
        await asyncio.sleep(0.02)


async def _wait_for_socket(frames):
    deadline = asyncio.get_running_loop().time() + 5
    while not any(f.get("t") == "resize" for f in frames):
        assert asyncio.get_running_loop().time() < deadline, "terminal socket never opened"
        await asyncio.sleep(0.02)


async def _no_error_banner(page):
    """A caught failure shows as a dismissible banner, not a page error: fail on it."""
    texts = await page.locator(".app > .banner:has(.icon-btn)").all_inner_texts()
    assert not texts, texts


async def _box(locator):
    box = await locator.bounding_box()
    assert box is not None, locator
    return box


async def phone_journey(page, frames, session_sockets):
    tap = lambda loc: loc.tap()  # noqa: E731
    width, height = 430, 739
    tabs = page.get_by_role("navigation", name="Workshop sections")
    assert await page.locator(".header").count() == 0, "a phone has no header"
    assert await page.locator(".app").get_attribute("data-shell") == "phone"
    assert [await b.get_attribute("aria-label") for b in await tabs.get_by_role("button").all()] == [
        "Terminal", "Build", "Tips", "Help"]

    # No session yet: the Terminal tab shows the agent cards (scrollable in landscape too).
    await page.set_viewport_size({"width": 814, "height": 380})
    assert await page.locator(".hero").evaluate("el => getComputedStyle(el).overflowY") == "auto"
    await page.set_viewport_size({"width": width, "height": height})
    await tap(page.get_by_role("button", name="Claude Code", exact=False).first)
    await _wait_for_socket(frames)
    await page.locator(".phone-capsule").filter(has_text="Claude Code").wait_for()
    # Touch launch must not focus xterm's raw textarea, which pops open the OS keyboard.
    assert not await page.evaluate("document.activeElement?.classList?.contains('xterm-helper-textarea') ?? false")

    composer = page.get_by_role("form", name="Prompt for the agent")
    prompt = composer.get_by_role("textbox", name="Prompt")
    keys = page.get_by_role("toolbar", name="Terminal keys")
    await composer.wait_for()
    assert await page.get_by_role("button", name="Open on phone", exact=True).count() == 0

    await _no_error_banner(page)
    # The terminal gets the screen: below any banners (here the local-only "credential not
    # configured"), the status row, key row, prompt box and tab bar cost at most 200px together.
    # The old phone layout spent about 430px on header and bars. Every key fits without scrolling.
    banners = sum([(await _box(b))["height"] for b in await page.locator(".app > .banner").all()])
    terminal = await _box(page.locator(".terminal-container"))
    chrome = height - banners - terminal["height"]
    assert chrome <= 200, f"{chrome}px of controls around a {terminal['height']}px terminal"
    tab_box = await _box(tabs)
    assert tab_box["y"] + tab_box["height"] <= height + 1
    composer_box = await _box(composer)
    assert composer_box["y"] + composer_box["height"] <= tab_box["y"] + 1
    for key in await keys.get_by_role("button").all():
        key_box = await _box(key)
        assert key_box["x"] + key_box["width"] <= width + 1 and key_box["height"] >= 40, key_box
    assert await prompt.evaluate("el => getComputedStyle(el).fontSize") == "16px"
    assert await prompt.get_attribute("placeholder") == "Message Claude Code…"

    # Speak, read it back, Send: the paste, then Enter.
    await tap(composer.get_by_role("button", name="Speak a prompt"))
    await page.wait_for_function("() => window.__voiceStarts === 1")
    await page.wait_for_function("text => document.querySelector('.composer-input').value === text", arg=HEARD)
    start = len(frames)
    await tap(composer.get_by_role("button", name="Send to the agent"))
    assert await _inputs_after(frames, start, [HEARD, "\r"]) == [HEARD, "\r"]
    assert await prompt.input_value() == ""

    start = len(frames)
    await tap(keys.get_by_role("button", name="Escape"))
    await tap(keys.get_by_role("button", name="Up arrow"))
    await tap(keys.get_by_role("button", name="Shift+Tab"))
    assert await _inputs_after(frames, start, ["\x1b", "\x1b[A", "\x1b[Z"]) == ["\x1b", "\x1b[A", "\x1b[Z"]

    # The ⌨ toggle meets the 40px touch-target floor, hides the key row (terminal grows) and persists.
    keys_toggle = composer.get_by_role("button", name="Hide terminal keys")
    toggle_box = await _box(keys_toggle)
    assert toggle_box["width"] >= 40 and toggle_box["height"] >= 40, toggle_box
    await tap(keys_toggle)
    assert await keys.count() == 0
    assert (await _box(page.locator(".terminal-container")))["height"] > terminal["height"]
    assert await page.evaluate("localStorage.getItem('wt.phone.keys')") == "0"
    await tap(composer.get_by_role("button", name="Show terminal keys"))
    await keys.wait_for()

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

    # Build tab: the build, the next step, the agents, and everything the laptop header holds.
    await prompt.fill("draft across tabs")
    await tap(tabs.get_by_role("button", name="Build"))
    assert await page.locator(".terminal-container").is_hidden()
    await page.get_by_role("region", name="Workshop builds").wait_for()
    assert await page.locator(".your-build").is_visible()
    agents = page.get_by_role("region", name="Agents")
    assert await agents.get_by_role("button", name="Close Claude Code").is_visible()
    more = page.get_by_role("region", name="More")
    # More stays outside the workshop-inputs fieldset so Operator and Network remain usable during closeout.
    assert await more.evaluate("el => el.closest('fieldset') === null")
    for label in ("Agent preferences", "Certificate"):
        row = more.get_by_role("button", name=label, exact=True)
        await row.scroll_into_view_if_needed()
        assert await row.is_visible()
    # Network popover opens upward unclipped by the More card.
    net_btn = more.locator(".network-status-btn")
    await net_btn.scroll_into_view_if_needed()
    await tap(net_btn)
    net_dialog = more.get_by_role("dialog", name="Network check details")
    await net_dialog.wait_for()
    net_box = await _box(net_dialog)
    assert net_box["y"] >= 0 and net_box["height"] >= 60 and net_box["x"] + net_box["width"] <= width + 1, net_box
    await tap(net_btn)
    await tap(tabs.get_by_role("button", name="Terminal"))
    assert await prompt.input_value() == "draft across tabs", "drafts survive tab switches"
    await prompt.fill("")

    # Tips tab: the tips as a page. Help tab: raise hand and the chat, no close button.
    await tap(tabs.get_by_role("button", name="Tips"))
    await page.locator(".nuggets-page").wait_for()
    await tap(tabs.get_by_role("button", name="Help"))
    help_page = page.get_by_role("region", name="Help")
    await help_page.get_by_role("button", name="Raise hand", exact=True).wait_for()
    panel = page.locator(".help-chat-inline")
    await panel.wait_for()
    assert await page.get_by_role("button", name="Close help chat").count() == 0
    panel_box = await _box(panel)
    assert panel_box["x"] >= 0 and panel_box["x"] + panel_box["width"] <= width + 1
    await tap(tabs.get_by_role("button", name="Terminal"))

    # Keyboard up: the status row and tab bar step aside; the prompt box stays above the keyboard.
    await prompt.focus()
    await page.set_viewport_size({"width": width, "height": 420})
    await page.wait_for_function("() => document.documentElement.dataset.keyboard === 'open'")
    await page.wait_for_function("() => getComputedStyle(document.querySelector('.phone-tabs')).display === 'none'")
    assert await page.locator(".phone-status").is_hidden()
    composer_kb = await _box(composer)
    assert composer_kb["y"] + composer_kb["height"] <= 420 + 1
    await page.set_viewport_size({"width": width, "height": height})
    await page.evaluate("() => document.activeElement?.blur?.()")
    await page.wait_for_function("() => document.documentElement.dataset.keyboard === 'closed'")

    # A recognizer that refuses to start leaves the mic usable, not stuck listening.
    await page.evaluate("window.__voiceFailStart = true")
    mic = composer.get_by_role("button", name="Speak a prompt")
    await tap(mic)
    await page.get_by_role("status").filter(has_text="Voice input is busy").wait_for()
    assert await mic.get_attribute("aria-pressed") == "false"
    assert await prompt.get_attribute("placeholder") == "Message Claude Code…"
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

    # Sideways: the tabs become a left rail, toasts stay at top, and the terminal takes the height.
    await page.set_viewport_size({"width": 814, "height": 380})
    await page.wait_for_function("() => getComputedStyle(document.querySelector('.phone-tabs')).position === 'fixed'")
    rail = await _box(tabs)
    assert rail["x"] <= 1 and rail["width"] <= 80 and rail["height"] >= 300, rail
    landscape_terminal = await _box(page.locator(".terminal-container"))
    assert landscape_terminal["x"] >= rail["width"] - 1
    assert landscape_terminal["width"] > 600
    assert (await _box(composer))["y"] + (await _box(composer))["height"] <= 380 + 1
    assert await page.locator(".toast-host").evaluate("el => getComputedStyle(el).top") == "8px"
    # Even on a shorter phone in landscape (e.g. 320px), the status icons at the foot of the rail stay below Help.
    await page.set_viewport_size({"width": 814, "height": 320})
    help_tab_box = await _box(tabs.get_by_role("button", name="Help"))
    status_box = await _box(page.locator(".phone-status"))
    assert help_tab_box["y"] + help_tab_box["height"] <= status_box["y"] + 1, (help_tab_box, status_box)
    assert not await page.evaluate("document.documentElement.scrollWidth > innerWidth")
    await page.set_viewport_size({"width": width, "height": height})


async def laptop_journey(page, frames, url):
    await page.get_by_role("button", name="Claude Code", exact=False).first.click()
    await page.get_by_role("button", name="Close Claude Code", exact=True).wait_for()
    await _wait_for_socket(frames)
    await _no_error_banner(page)
    composer = page.get_by_role("form", name="Prompt for the agent")
    prompt = composer.get_by_role("textbox", name="Prompt")
    keys = page.get_by_role("toolbar", name="Terminal keys")
    assert await page.locator(".app").get_attribute("data-shell") == "desktop"
    assert await page.get_by_role("navigation", name="Workshop sections").count() == 0
    # Desktop focuses xterm so typing works immediately; the prompt box opens from the agent bar's mic.
    assert await page.evaluate("document.activeElement?.classList?.contains('xterm-helper-textarea') ?? false")
    assert not await composer.is_visible()
    assert await keys.count() == 0
    await page.locator(".toolbar").get_by_role("button", name="Speak a prompt").click()
    await composer.wait_for()
    await page.wait_for_function("() => window.__voiceStarts === 1")
    await page.wait_for_function("text => document.querySelector('.composer-input').value === text", arg=HEARD)

    # Send pastes the prompt, then presses Enter: nothing more, nothing less.
    start = len(frames)
    await prompt.press("Enter")
    assert await _inputs_after(frames, start, [HEARD, "\r"]) == [HEARD, "\r"]
    assert await prompt.input_value() == ""

    # Drafts survive Home -> Agent navigation without unmounting PromptComposer.
    await prompt.fill("draft across views")
    await page.get_by_role("button", name="Home", exact=True).click()
    await page.get_by_role("button", name="Claude Code", exact=True).click()
    assert await prompt.input_value() == "draft across views"
    await prompt.fill("")

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
    deadline = asyncio.get_running_loop().time() + 2
    while sum(1 for f in frames if f.get("t") == "resize") <= resize_before_click:
        assert asyncio.get_running_loop().time() < deadline, "clicking terminal did not reclaim size"
        await asyncio.sleep(0.02)

    phone_link = page.get_by_role("button", name="Open on phone", exact=True)
    await phone_link.click()
    dialog = page.get_by_role("dialog", name="Open on phone")
    await dialog.get_by_role("img", name=f"QR code for {url}/").wait_for()
    # Clicking dialog padding/whitespace inside the card must not dismiss the modal.
    await dialog.click(position={"x": 10, "y": 10})
    assert await dialog.is_visible()
    await dialog.get_by_role("button", name="Done").click()
    assert await dialog.count() == 0


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
                device_options = {k: v for k, v in engine.devices[PHONE_DEVICE].items() if k != "default_browser_type"} if phone else LAPTOP
                context = await browser.new_context(**device_options, reduced_motion="reduce")
                await context.add_init_script(FAKE_SPEECH)
                page = await context.new_page()
                page.set_default_timeout(5000)
                page.on("pageerror", lambda error: errors.append(str(error)))
                await page.goto(url)
                assert await page.evaluate("matchMedia('(pointer: coarse)').matches") is phone

                if phone:
                    await phone_journey(page, frames, session_sockets)
                else:
                    await laptop_journey(page, frames, url)
                assert not await page.evaluate("document.documentElement.scrollWidth > innerWidth")
                assert not errors, errors
            finally:
                await browser.close()
    finally:
        await runner.cleanup()


@pytest.mark.parametrize("device", ["phone", "laptop"])
def test_prompt_box_voice_and_touch_keys(client, launchable_agents, tmp_path, monkeypatch, device):
    asyncio.run(exercise(client, tmp_path, monkeypatch, device))
