"""Drive the committed WT React app against isolated local API fixtures.

These checks qualify UI state and local brief integrity. They do not qualify
gateway models, a live coding harness, or a generated Databricks app.
"""

import asyncio
import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from server import attendee_profile, config, demo_data, wizard, wizard_selections
from .test_wizard_recommendations import idea

pytestmark = pytest.mark.skipif(os.environ.get("WORKSHOP_E2E_BROWSER_TESTS") != "1",
                              reason="opt-in actual WT browser/localhost test")
ROOT = Path(__file__).resolve().parents[1]


async def exercise(tmp_path, monkeypatch, *, width=1440, faults=False, races=False,
                   interrupted=False, malformed_draft=False, new_project=False,
                   unavailable_suggestions=False, prepared_suggestions=False,
                   save_pending=False, direct_launch=None, wizard_mode="skipped", skip_failure=False,
                   skip_pending=False):
    web = pytest.importorskip("aiohttp.web")
    browser_api = pytest.importorskip("playwright.async_api")
    monkeypatch.setattr(config, "discovery_enabled", lambda: False)
    monkeypatch.setattr(config, "onboarding_wizard_enabled", lambda: True)
    monkeypatch.setattr(config, "llm_wizard_enabled", lambda: True)
    monkeypatch.setattr(demo_data.config, "workshop_demo_catalog", lambda: "")
    offered_idea = idea() | {"industries": [], "required_columns": {}}
    if prepared_suggestions:
        # Synthetic API metadata, not live data or task-feasibility evidence.
        monkeypatch.setattr(demo_data, "verify", lambda _tables: True)
        monkeypatch.setattr(demo_data, "supports", lambda *_args, **_kwargs: True)
        monkeypatch.setattr(demo_data, "data_ready", lambda _tables: True)
        offered_idea.update(demo_tables=["retail.orders"], data_mode="demo",
            required_columns={"retail.orders": ["order_id"]},
            prompt="Review clearly labelled sample bakery orders from retail.orders.")
    user = SimpleNamespace(email="simulated-attendee@example.invalid", home=str(tmp_path / "attendee"))
    switches = {"enabled": wizard_mode != "disabled", "load_errors": 2 if (faults or skip_failure) else 0, "save_errors": int(skip_failure),
                "launch_errors": 0, "delivery_errors": 0}
    sessions = []
    deliveries = []
    interrupted_ids = []
    suggestion_entered = asyncio.Event()
    suggestion_release = asyncio.Event()
    save_entered = asyncio.Event()
    save_release = asyncio.Event()
    requests = []

    async def handler(request):
        path = request.path
        if path.startswith("/ws/"):
            ws = web.WebSocketResponse()
            await ws.prepare(request)
            if path.startswith("/ws/sessions/"):
                await ws.send_json({"t": "output", "data": "Simulated harness prompt > "})
            async for _ in ws:
                pass
            return ws
        if not path.startswith("/api/"):
            file = ROOT / "static" / (path.removeprefix("/") or "index.html")
            return web.FileResponse(file)
        payload = await request.json() if request.method == "POST" else {}
        requests.append((path, request.method, payload))
        if path == "/api/config":
            body = {"user": {"email": user.email, "is_admin": False},
                "branding": {"brand_name": "Databricks", "brand_logo_url": "", "event_name": "Workshop UI qualification", "brand_primary_color": "", "cobranded": False},
                "workspace_url": "https://workspace.invalid", "shell": {"links": [], "workspace_links": [], "features": {}},
                "phase": "welcome", "broadcast": None, "limits": {"max_sessions_per_user": 1},
                "credential": {"configured": True, "healthy": True, "degraded": False},
                "obo": {"enabled": False, "present": True, "fresh": True}, "help": {"raised": False},
                "omnigent_remote": {"enabled": False, "url": ""}, "onboarding_wizard": {"enabled": switches["enabled"]}}
        elif path == "/api/agents":
            body = {"agents": [{"id": "claude", "label": "Claude Code", "description": "Simulated ready harness", "icon": "terminal", "order": 1, "ready": True, "needs_credentials": False},
                {"id": "codex", "label": "Codex", "description": "Simulated installing harness", "icon": "terminal", "order": 2, "ready": False, "needs_credentials": False}]}
            if direct_launch:
                body["agents"][1]["ready"] = True
                body["agents"].append({"id": "omnigent", "label": "Omnigent", "description": "Simulated ready harness", "icon": "terminal", "order": 3, "ready": True, "needs_credentials": False})
        elif path == "/api/setup/status":
            body = {"steps": {"claude": {"status": "done"}, "codex": {"status": "installing"}}, "installing": True, "ready": {"claude": True}}
        elif path == "/api/nuggets":
            body = {"phase": "welcome", "prompts": [], "nuggets": []}
        elif path == "/api/help/thread":
            body = {"raised": False, "messages": []}
        elif path == "/api/wizard/suggest":
            suggestion_entered.set()
            if races:
                await suggestion_release.wait()
            if unavailable_suggestions:
                body = {"industry": "", "ideas": [], "source": "selector",
                    "fallback_reason": "Generated ideas could not be verified. Continue with your goal or try suggestions again."}
            else:
                offered = wizard_selections.offer(user, offered_idea, source="generated", model="synthetic-model")
                body = {"industry": "", "ideas": [offered], "source": "llm"}
        elif path == "/api/wizard":
            if request.method == "GET":
                if switches["load_errors"]:
                    switches["load_errors"] -= 1
                    return web.json_response({"detail": "Saved goal is temporarily unavailable"}, status=503)
                body = wizard.state(user, industry=request.query.get("industry"), query=request.query.get("q", ""))
                body["enabled"] = switches["enabled"]
                body["should_show"] = switches["enabled"] and not body["brief"]["seen"]
            else:
                if skip_pending and payload.get("operation") == "skip":
                    save_entered.set()
                    await save_release.wait()
                if save_pending and payload.get("operation") == "complete":
                    save_entered.set()
                    await save_release.wait()
                if switches["save_errors"]:
                    switches["save_errors"] -= 1
                    return web.json_response({"detail": "Saving failed; retry with your draft"}, status=503)
                try:
                    brief = wizard.save(user, payload)
                except wizard.BriefConflict as exc:
                    return web.json_response({"detail": {"message": str(exc), "brief": exc.brief.to_json()}}, status=409)
                body = {"brief": brief.to_json(), "starter_prompt": wizard.starter_prompt(brief)}
        elif path == "/api/profile":
            if request.method == "GET":
                body = attendee_profile.read(user).to_json()
            else:
                body = attendee_profile.save(user, payload["help_preference"], expected_revision=payload["expected_revision"]).to_json()
        elif path == "/api/sessions":
            if request.method == "POST":
                if switches["launch_errors"]:
                    switches["launch_errors"] -= 1
                    return web.json_response({"detail": "Simulated launch failure"}, status=503)
                created = {"id": "local-session", "agent_id": payload["agent_id"], "label": {"claude": "Claude Code", "codex": "Codex", "omnigent": "Omnigent"}[payload["agent_id"]], "created_at": 0, "last_activity": 0, "exited": False}
                sessions[:] = [created]
                body = {"session": created}
            else:
                body = {"sessions": sessions, "prior_sessions": []}
        elif path.endswith("/type"):
            if interrupted:
                interrupted_ids.append(payload["delivery_id"])
                return web.json_response({"detail": "Prompt delivery was interrupted. Open the agent to clear its input, then start a new session to load your saved goal."}, status=409)
            if switches["delivery_errors"]:
                switches["delivery_errors"] -= 1
                return web.json_response({"detail": "Simulated prompt delivery failure"}, status=409)
            deliveries.append(payload["text"])
            flattened = payload["text"].replace("\n", " ").replace("\r", " ")
            body = {"status": "ok", "typed_sha256": hashlib.sha256(flattened.encode()).hexdigest()}
        else:
            body = {}
        return web.json_response(body)

    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", handler)
    runner = web.AppRunner(app, shutdown_timeout=.1)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    url = "http://127.0.0.1:" + str(site._server.sockets[0].getsockname()[1])
    try:
        async with browser_api.async_playwright() as engine:
            browser = await engine.chromium.launch(headless=True)
            context = await browser.new_context(viewport={"width": width, "height": 900}, reduced_motion="reduce")
            page = await context.new_page()
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            await page.goto(url)
            dialog = page.get_by_role("dialog", name="What would you like to build?")
            if direct_launch:
                if wizard_mode == "skipped":
                    await dialog.get_by_role("button", name="Skip onboarding", exact=True).click()
                    await dialog.wait_for(state="hidden")
                    if skip_pending:
                        await save_entered.wait()
                await page.get_by_role("heading", name="What will you build today?").wait_for()
                await page.reload()
                label = {"claude": "Claude Code", "codex": "Codex", "omnigent": "Omnigent"}[direct_launch]
                await page.get_by_role("button", name=label, exact=False).click()
                await page.get_by_role("button", name=f"Close {label}", exact=True).wait_for()
                assert sessions[0]["agent_id"] == direct_launch
                assert await dialog.count() == 0
                brief = wizard.read_brief(user)
                assert brief.what_building == "" and brief.selected_idea is None
                assert not deliveries
                assert not any(path == "/api/wizard/suggest" for path, _, _ in requests)
                assert not errors, errors
                await context.close()
                await browser.close()
                return
            await dialog.wait_for()
            if faults:
                await dialog.get_by_role("alert").wait_for()
                await dialog.get_by_role("button", name="Retry loading").click()
            goal = dialog.get_by_role("textbox", name="Your goal", exact=True)
            await goal.fill("Help my bakery staff keep track of today's orders")
            assert await dialog.get_by_role("button", name="Continue", exact=True).is_enabled()
            for _ in range(14):
                await page.keyboard.press("Tab")
                assert await page.evaluate("document.activeElement.closest('dialog') !== null")
            dimensions = await dialog.evaluate("element => ({left: element.getBoundingClientRect().left,right:element.getBoundingClientRect().right,overflow:element.scrollWidth>element.clientWidth})")
            assert dimensions["left"] >= 0 and dimensions["right"] <= width and not dimensions["overflow"]
            assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            assert await page.evaluate("document.body.style.overflow") == "hidden"
            await page.screenshot(path=str(tmp_path / f"wizard-{width}.png"), full_page=True)
            if races:
                await dialog.get_by_role("button", name="Suggest ideas for my goal").click()
                await suggestion_entered.wait()
                await goal.fill("Help staff plan their rota")
                suggestion_release.set()
                await page.wait_for_timeout(100)
                assert await dialog.get_by_role("button", name="Pack bakery orders", exact=False).count() == 0
                assert await goal.input_value() == "Help staff plan their rota"
                # Explicit action can request new candidates after the obsolete result.
                await goal.fill("Help my bakery staff keep track of today's orders")
            if malformed_draft:
                await page.evaluate("""() => {
                    const key = Object.keys(localStorage).find(k => k.startsWith('wt-wizard-draft:'));
                    const draft = JSON.parse(localStorage.getItem(key));
                    draft.selected = {id:'broken',label:'Malformed selection',outcome:'Broken',prompt:'Broken',assumptions:{invalid:true}};
                    localStorage.setItem(key, JSON.stringify(draft));
                }""")
            await page.reload()
            dialog = page.get_by_role("dialog", name="What would you like to build?")
            await dialog.get_by_role("textbox", name="Your goal", exact=True).wait_for()
            assert "bakery" in await dialog.get_by_role("textbox", name="Your goal", exact=True).input_value()
            assert await dialog.get_by_text("Malformed selection", exact=True).count() == 0
            await dialog.get_by_role("button", name="Suggest ideas for my goal").click()
            if unavailable_suggestions:
                await dialog.get_by_text("Generated ideas could not be verified. Continue with your goal or try suggestions again.", exact=True).wait_for()
                assert await dialog.get_by_role("button", name="Pack bakery orders", exact=False).count() == 0
                assert await dialog.get_by_role("button", name="Continue", exact=True).is_enabled()
                await page.screenshot(path=str(tmp_path / f"wizard-fallback-{width}.png"), full_page=True)
            else:
                if prepared_suggestions:
                    await dialog.get_by_role("button", name="Pack bakery orders", exact=False).get_by_text(
                        "Workshop demo data; your agent will check its source, dates and access", exact=True).wait_for()
                await dialog.get_by_role("button", name="Pack bakery orders", exact=False).click()
            assert "bakery" in await dialog.get_by_role("textbox", name="Your goal", exact=True).input_value()
            if faults:
                switches["save_errors"] = 1
                await dialog.get_by_role("button", name="Continue", exact=True).click()
                await dialog.get_by_role("alert").wait_for()
                assert await dialog.get_by_role("textbox", name="Your goal", exact=True).is_visible()
                # Another tab saved a different goal. The UI must recover visibly.
                wizard.save(user, {"what_building": "A different saved task", "expected_revision": 0})
                await dialog.get_by_role("button", name="Continue", exact=True).click()
                await dialog.get_by_role("button", name="Save my draft instead").click()
            else:
                await dialog.get_by_role("button", name="Continue", exact=True).click()
                if save_pending:
                    await save_entered.wait()
                    # A save captures a particular goal and selection. Do not
                    # show later edits while launching the previously saved text.
                    assert not await dialog.get_by_role("textbox", name="Your goal", exact=True).is_enabled()
                    assert not await dialog.get_by_role("button", name="Remove idea and keep my words").is_enabled()
                    assert not await dialog.get_by_role("button", name="Suggest ideas for my goal").is_enabled()
                    save_release.set()
            saved_dialog = page.get_by_role("dialog", name="Your goal is saved")
            await saved_dialog.wait_for()
            assert not await saved_dialog.get_by_role("button", name="Codex", exact=False).is_enabled()
            saved = wizard.read_brief(user, strict=True)
            assert saved.what_building == "Help my bakery staff keep track of today's orders"
            if unavailable_suggestions:
                assert saved.selected_idea is None
            else:
                assert saved.selected_idea["prompt"] == offered_idea["prompt"]
            if new_project:
                previous_id = saved.record_id
                await saved_dialog.get_by_role("button", name="Edit goal", exact=True).click()
                dialog = page.get_by_role("dialog", name="What would you like to build?")
                await dialog.get_by_role("button", name="Optional context", exact=True).click()
                await dialog.get_by_role("button", name="Start a different project", exact=True).click()
                assert await dialog.get_by_role("textbox", name="Your goal", exact=True).input_value() == ""
                await page.reload()
                await page.get_by_role("button", name="Change what I'm building", exact=True).click()
                dialog = page.get_by_role("dialog", name="What would you like to build?")
                await dialog.get_by_role("textbox", name="Your goal", exact=True).fill("Help bakery staff plan tomorrow's rota")
                await dialog.get_by_role("button", name="Continue", exact=True).click()
                saved_dialog = page.get_by_role("dialog", name="Your goal is saved")
                await saved_dialog.wait_for()
                saved = wizard.read_brief(user, strict=True)
                assert saved.record_id != previous_id and saved.selected_idea is None
                assert "rota" in saved.what_building
            if faults:
                switches["launch_errors"] = 1
                await saved_dialog.get_by_role("button", name="Claude Code", exact=False).click()
                await saved_dialog.get_by_role("alert").wait_for()
                assert await saved_dialog.is_visible()
                switches["delivery_errors"] = 2
                await saved_dialog.get_by_role("button", name="Claude Code", exact=False).click()
                await saved_dialog.get_by_role("alert").filter(has_text="delivery failure").wait_for()
                assert await saved_dialog.is_visible()
            if interrupted:
                for _ in range(2):
                    await saved_dialog.get_by_role("button", name="Claude Code", exact=False).click()
                    await saved_dialog.get_by_role("button", name="Open agent", exact=True).wait_for()
                assert len(interrupted_ids) == 2 and len(set(interrupted_ids)) == 1
                assert not deliveries
                await saved_dialog.get_by_role("button", name="Open agent", exact=True).click()
                await saved_dialog.wait_for(state="hidden")
                # A page reload must not turn the same interrupted delivery into
                # a fresh ID and append the task onto partial visible input.
                await page.reload()
                await page.get_by_role("button", name="Home", exact=True).click()
                await page.get_by_role("button", name="Change what I'm building", exact=True).click()
                dialog = page.get_by_role("dialog", name="What would you like to build?")
                await dialog.get_by_role("button", name="Continue", exact=True).click()
                saved_dialog = page.get_by_role("dialog", name="Your goal is saved")
                await saved_dialog.get_by_role("button", name="Claude Code", exact=False).click()
                await saved_dialog.get_by_role("button", name="Open agent", exact=True).wait_for()
                assert len(interrupted_ids) == 3 and len(set(interrupted_ids)) == 1
                assert wizard.read_brief(user).to_json() == saved.to_json()
                assert not errors, errors
                await context.close()
                await browser.close()
                return
            await saved_dialog.get_by_role("button", name="Claude Code", exact=False).click()
            await saved_dialog.wait_for(state="hidden")
            assert deliveries == [wizard.starter_prompt(saved)]
            # Preferences remain reachable when onboarding is disabled.
            switches["enabled"] = False
            await page.reload()
            await page.get_by_role("button", name="Agent preferences", exact=True).click()
            preferences = page.get_by_role("dialog", name="Agent preferences")
            await preferences.get_by_role("combobox").select_option("concise")
            await preferences.get_by_role("button", name="Save preference", exact=True).click()
            await preferences.get_by_role("status").wait_for()
            assert attendee_profile.read(user).help_preference == "concise"
            assert wizard.read_brief(user).to_json() == saved.to_json()
            await preferences.get_by_role("button", name="Done", exact=True).click()
            assert await page.get_by_role("button", name="Agent preferences", exact=True).evaluate("e => e === document.activeElement")
            assert not errors, errors
            await context.close()
            await browser.close()
    finally:
        suggestion_release.set()
        save_release.set()
        await runner.cleanup()


@pytest.mark.parametrize("width", [390, 768, 1440])
def test_rendered_goal_selection_reload_keyboard_launch_and_preferences(tmp_path, monkeypatch, width):
    asyncio.run(exercise(tmp_path, monkeypatch, width=width))


def test_rendered_load_save_conflict_launch_and_delivery_recovery(tmp_path, monkeypatch):
    asyncio.run(exercise(tmp_path, monkeypatch, faults=True))


def test_rendered_partial_delivery_requires_opening_agent_without_retyping(tmp_path, monkeypatch):
    asyncio.run(exercise(tmp_path, monkeypatch, interrupted=True))


def test_rendered_malformed_draft_preserves_words_and_recovers_selection(tmp_path, monkeypatch):
    asyncio.run(exercise(tmp_path, monkeypatch, malformed_draft=True))


def test_rendered_new_project_draft_survives_reload_with_a_new_identity(tmp_path, monkeypatch):
    asyncio.run(exercise(tmp_path, monkeypatch, new_project=True))


def test_rendered_obsolete_suggestion_cannot_replace_new_words(tmp_path, monkeypatch):
    asyncio.run(exercise(tmp_path, monkeypatch, races=True))


@pytest.mark.parametrize("width", [390, 1440])
def test_rendered_rejected_suggestions_preserve_and_deliver_original_goal(tmp_path, monkeypatch, width):
    asyncio.run(exercise(tmp_path, monkeypatch, width=width, unavailable_suggestions=True))


def test_rendered_prepared_card_discloses_demo_source_and_date_checks(tmp_path, monkeypatch):
    asyncio.run(exercise(tmp_path, monkeypatch, width=390, prepared_suggestions=True))


def test_rendered_pending_save_preserves_the_displayed_and_delivered_task(tmp_path, monkeypatch):
    asyncio.run(exercise(tmp_path, monkeypatch, save_pending=True))


@pytest.mark.parametrize("wizard_mode", ["skipped", "disabled"])
@pytest.mark.parametrize("agent_id", ["claude", "codex", "omnigent"])
def test_rendered_direct_harness_launch_without_onboarding(tmp_path, monkeypatch, wizard_mode, agent_id):
    asyncio.run(exercise(tmp_path, monkeypatch, direct_launch=agent_id, wizard_mode=wizard_mode))


@pytest.mark.parametrize("agent_id", ["claude", "codex", "omnigent"])
def test_rendered_wizard_service_failure_cannot_block_skip_or_harness_launch(tmp_path, monkeypatch, agent_id):
    asyncio.run(exercise(tmp_path, monkeypatch, direct_launch=agent_id, skip_failure=True))


def test_rendered_pending_skip_write_cannot_block_direct_launch_or_reload(tmp_path, monkeypatch):
    asyncio.run(exercise(tmp_path, monkeypatch, direct_launch="claude", skip_pending=True))
