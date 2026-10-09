"""Offline transport contracts; these do not count as a live generated-app run."""

import asyncio
import json
from pathlib import Path

import pytest

from evals.generated_apps.adapters.browser import (
    AuthenticatedBrowser,
    BackendPersistenceEvidence,
    BrowserAction,
    BrowserLaunchConfig,
    GeneratedAppBrowserVerifier,
    RoleLocator,
    WorkshopBrowserDriver,
    validate_app_url,
)
from evals.generated_apps.adapters.harness import HarnessObserver, load_exported_transcript


class FakeRequest:
    def __init__(self, method="GET", body=None):
        self.method = method
        self.post_data_json = body


class FakeResponse:
    def __init__(self, path, body, *, method="GET", status=200, request_body=None):
        self.url = "https://wt.example" + path
        self.request = FakeRequest(method, request_body)
        self.status = status
        self.ok = status < 400
        self.body = body

    async def json(self):
        return self.body


class FakeSocket:
    url = "wss://wt.example/ws/sessions/fresh-session"

    def __init__(self):
        self.listeners = {}

    def on(self, event, callback):
        self.listeners[event] = callback

    def frame(self, direction, **message):
        self.listeners["framesent" if direction == "sent" else "framereceived"](json.dumps(message))


class FakeExpectedResponse:
    def __init__(self, page, predicate):
        self.page, self.predicate = page, predicate
        self.value = None

    async def __aenter__(self):
        self.value = asyncio.get_running_loop().create_future()
        self.page.expected = self
        return self

    async def __aexit__(self, *_exc):
        self.page.expected = None


class FakeKeyboard:
    def __init__(self, page):
        self.page = page
        self.typed = []

    async def insert_text(self, text):
        self.typed.append(text)
        self.page.socket.frame("sent", t="input", data=text)

    async def press(self, key):
        self.typed.append(key)
        self.page.socket.frame("sent", t="input", data="\r" if key == "Enter" else key)


class FakeLocator:
    def __init__(self, page, name=""):
        self.page, self.name = page, name

    def locator(self, selector):
        return FakeLocator(self.page, selector)

    def get_by_role(self, role, name=None, exact=True):
        return FakeLocator(self.page, name or role)

    async def wait_for(self, **_options):
        return None

    async def fill(self, value, **_options):
        self.page.goal = value

    async def is_enabled(self):
        return self.page.industry_chosen if self.name == "Next" else True

    async def is_visible(self):
        if self.name.startswith("Close "):
            return self.page.terminal_visible
        if self.name == "dialog":
            return self.page.wizard_enabled and not self.page.wizard_skipped
        if self.name == ".hero .hero-cards":
            return not self.page.wizard_enabled or self.page.wizard_skipped
        return True

    async def count(self):
        return self.page.close_count if self.name.startswith("Close ") else 1

    async def focus(self, **_options):
        self.page.focused = self.name

    async def inner_text(self, **_options):
        return "Packed"

    async def click(self, **_options):
        self.page.clicked.append(self.name)
        if self.name == "Retail":
            self.page.industry_chosen = True
        elif self.name == "Next":
            self.page.emit_response(FakeResponse("/api/wizard", {
                "brief": {"what_building": self.page.goal, "industry": "retail", "persona": "business"},
                "starter_prompt": self.page.goal,
            }, method="POST", status=self.page.save_status))
        elif self.name == "Skip":
            self.page.wizard_skipped = True
            self.page.emit_response(FakeResponse("/api/wizard", {"brief": {"skipped": True}}, method="POST"))
        elif self.name == "Claude Code":
            self.page.sessions = [{"id": "fresh-session", "agent_id": "claude", "exited": False}]
            self.page.emit_response(FakeResponse("/api/sessions", {
                "session": {"id": "fresh-session", "agent_id": "claude"}}, method="POST"))
            self.page.listeners["websocket"](self.page.socket)
            self.page.socket.frame("sent", t="resize", cols=120, rows=40)
            self.page.socket.frame("received", t="output", data="Ready")
            if self.page.wizard_enabled:
                self.page.emit_response(FakeResponse("/api/sessions/fresh-session/type", {"status": "ok"},
                                                    method="POST", request_body={"text": self.page.goal}))
        elif self.name == "Close Claude Code":
            self.page.sessions = []
            self.page.emit_response(FakeResponse("/api/sessions/fresh-session", {"status": "ok"}, method="DELETE"))
        elif self.name == 'button[title="Back to Claude Code"]':
            self.page.terminal_visible = True


class FakePage:
    """Only visible controls and observation interfaces; no backend request API."""
    def __init__(self, *, email="attendee@example.com", wizard_enabled=True, sessions=None, save_status=200):
        self.listeners = {}
        self.expected = None
        self.email = email
        self.wizard_enabled = wizard_enabled
        self.sessions = sessions or []
        self.save_status = save_status
        self.clicked = []
        self.goal = ""
        self.industry_chosen = False
        self.url = "https://wt.example"
        self.socket = FakeSocket()
        self.keyboard = FakeKeyboard(self)
        self.context = object()
        self.close_count = 1
        self.terminal_visible = True
        self.wizard_skipped = False

    def on(self, event, callback):
        self.listeners[event] = callback

    def emit_response(self, response):
        self.listeners["response"](response)
        if self.expected and self.expected.predicate(response):
            self.expected.value.set_result(response)

    async def goto(self, *_args, **_kwargs):
        self.emit_response(FakeResponse("/api/config", {
            "user": {"email": self.email}, "onboarding_wizard": {"enabled": self.wizard_enabled},
            "credential": {"access_token": "never-copy-me"},
        }))
        self.emit_response(FakeResponse("/api/agents", {
            "agents": [{"id": "claude", "label": "Claude Code", "ready": True}]}))
        self.emit_response(FakeResponse("/api/sessions", {"sessions": self.sessions}))
        self.emit_response(FakeResponse("/api/wizard", {
            "enabled": self.wizard_enabled,
            "should_show": self.wizard_enabled and not self.wizard_skipped,
        }))

    async def reload(self, **_options):
        await self.goto(self.url)

    def get_by_role(self, role, name=None, exact=True):
        return FakeLocator(self, name or role)

    def locator(self, selector):
        return FakeLocator(self, selector)

    def expect_response(self, predicate, **_options):
        return FakeExpectedResponse(self, predicate)


def config(**overrides):
    values = dict(wt_url="https://wt.example", agent_id="claude", opening_message="Help with bakery orders.",
                  expected_attendee_email="attendee@example.com", deadline_seconds=2, run_deadline_seconds=3)
    values.update(overrides)
    return BrowserLaunchConfig(**values)


def test_baseline_industry_gate_is_reported_without_launch_rescue():
    async def run():
        page = FakePage()
        driver = WorkshopBrowserDriver(page)
        evidence = await driver.enter(config())
        assert evidence.error_code == "industry_required"
        assert evidence.friction == ["plain_goal_blocked_by_industry_gate"]
        assert "Claude Code" not in page.clicked
        assert not evidence.opening_submitted
        assert not hasattr(page, "request")
        assert "never-copy-me" not in json.dumps(evidence.to_dict())
    asyncio.run(run())


def test_compatible_wizard_arm_launches_and_submits_only_public_goal():
    async def run():
        page = FakePage()
        driver = WorkshopBrowserDriver(page)
        evidence = await driver.enter(config(allow_industry_step=True, industry="Retail"))
        assert evidence.error_code == ""
        assert evidence.session_id == "fresh-session"
        assert evidence.saved_brief["what_building"] == "Help with bakery orders."
        assert "Plain language" in page.clicked
        assert "explicit_industry_step:Retail" in evidence.friction
        assert evidence.prompt_delivery_request == "Help with bakery orders."
        assert evidence.prompt_preservation_verified
        assert not evidence.harness_readiness_verified
        assert page.keyboard.typed == ["Enter"]  # carried prompt is already UNSENT
        assert evidence.opening_submitted
        assert driver.harness.evidence()["structured_events_supported"] is False
    asyncio.run(run())


@pytest.mark.parametrize("page_kwargs,code", [
    ({"email": "operator@example.com"}, "attendee_identity_mismatch"),
    ({"sessions": [{"id": "someone-else", "exited": False}]}, "existing_attendee_session"),
    ({"wizard_enabled": False}, "wizard_disabled"),
    ({"save_status": 500}, "wizard_save_failed"),
])
def test_entry_failures_never_launch_a_second_harness(page_kwargs, code):
    async def run():
        page = FakePage(**page_kwargs)
        evidence = await WorkshopBrowserDriver(page).enter(config(allow_industry_step=True, industry="Retail"))
        assert evidence.error_code == code
        assert "Claude Code" not in page.clicked
    asyncio.run(run())


def test_disabled_wizard_path_types_public_goal_in_visible_terminal():
    async def run():
        page = FakePage(wizard_enabled=False)
        evidence = await WorkshopBrowserDriver(page).enter(config(entry_path="wizard_disabled"))
        assert evidence.opening_submitted
        assert page.keyboard.typed == ["Help with bakery orders.", "Enter"]
        assert not evidence.saved_brief
    asyncio.run(run())


@pytest.mark.parametrize("already_skipped", [False, True])
def test_skip_waits_for_server_wizard_decision_before_selecting_background_agent(already_skipped):
    async def run():
        page = FakePage()
        page.wizard_skipped = already_skipped
        evidence = await WorkshopBrowserDriver(page).enter(config(entry_path="skip_wizard"))
        assert evidence.opening_submitted and not evidence.error_code
        assert evidence.wizard_should_show is not already_skipped
        assert page.clicked == (["Claude Code"] if already_skipped else ["Skip", "Claude Code"])
    asyncio.run(run())


def test_current_server_starter_truncation_blocks_preservation_gate():
    async def run():
        page = FakePage()
        driver = WorkshopBrowserDriver(page)
        evidence = await driver.enter(config(opening_message="x" * 501, allow_industry_step=True, industry="Retail"))
        assert evidence.error_code == "starter_prompt_truncated"
        assert not evidence.prompt_preservation_verified
        assert not evidence.opening_submitted
        assert page.keyboard.typed == []
    asyncio.run(run())


def test_cleanup_refreshes_owned_session_and_works_after_run_deadline():
    async def run():
        page = FakePage(wizard_enabled=False)
        driver = WorkshopBrowserDriver(page)
        await driver.enter(config(entry_path="wizard_disabled"))
        driver._deadline = 0
        await driver.close_launched_session("Claude Code", expected_session_id="fresh-session", timeout_seconds=1)
        assert page.clicked[-1] == "Close Claude Code"
        assert page.sessions == []
    asyncio.run(run())


@pytest.mark.parametrize("mutation", ["replaced", "different_owner", "ambiguous_button", "observed_other_socket", "wrong_expected"])
def test_cleanup_never_clicks_a_replaced_or_ambiguous_session(mutation):
    async def run():
        page = FakePage(wizard_enabled=False)
        driver = WorkshopBrowserDriver(page)
        await driver.enter(config(entry_path="wizard_disabled"))
        expected = "fresh-session"
        if mutation == "replaced":
            page.sessions = [{"id": "replacement", "agent_id": "claude"}]
        elif mutation == "different_owner":
            page.email = "someone-else@example.com"
        elif mutation == "ambiguous_button":
            page.close_count = 2
        elif mutation == "observed_other_socket":
            driver.harness.session_ids.add("replacement")
        elif mutation == "wrong_expected":
            expected = "replacement"
        from evals.generated_apps.adapters.browser import BrowserJourneyError
        with pytest.raises(BrowserJourneyError):
            await driver.close_launched_session("Claude Code", expected_session_id=expected, timeout_seconds=1)
        assert "Close Claude Code" not in page.clicked
    asyncio.run(run())


def test_normal_ui_driver_is_single_use():
    async def run():
        page = FakePage(wizard_enabled=False)
        driver = WorkshopBrowserDriver(page)
        await driver.enter(config(entry_path="wizard_disabled"))
        from evals.generated_apps.adapters.browser import BrowserJourneyError
        with pytest.raises(BrowserJourneyError, match="new browser driver"):
            await driver.enter(config(entry_path="wizard_disabled"))
        assert page.clicked.count("Claude Code") == 1
    asyncio.run(run())


def test_transport_redacts_credentials_split_across_frames_and_preserves_unknown_coverage():
    observer = HarnessObserver(secrets=["private-auth-token"])
    observer.feed("received", json.dumps({"t": "output", "data": "private-"}))
    observer.feed("received", json.dumps({"t": "output", "data": "auth-token"}))
    observer.feed("sent", json.dumps({"t": "input", "data": "private-"}))
    observer.feed("sent", json.dumps({"t": "input", "data": "auth-token"}))
    report = observer.evidence()
    assert "private-" not in json.dumps(report)
    assert report["terminal_replay_and_output"] == "[REDACTED]"
    assert report["composed_input_transport"] == "[REDACTED]"
    assert report["assistant_turns"] == []
    assert "scope_before_implementation" in report["unverified_checks"]


def test_transport_bounds_replay_and_ignores_unrelated_websockets():
    observer = HarnessObserver(max_output_bytes=8)
    observer.feed("received", json.dumps({"t": "replay", "data": "old history "}))
    observer.feed("received", json.dumps({"t": "output", "data": "new"}))
    report = observer.evidence()
    assert report["output_bytes_observed"] == 15
    assert report["output_bytes_omitted"] == 7
    assert report["terminal_replay_and_output"] == "tory new"
    socket = FakeSocket()
    socket.url = "wss://unrelated.example/ws/sessions/attendee"
    assert observer.attach(socket, wt_origin="https://wt.example") is False
    assert socket.listeners == {}


@pytest.mark.parametrize("url", ["file:///tmp/app", "https://token@example.com", "https://a.example?token=x"])
def test_browser_urls_cannot_embed_credentials(url):
    with pytest.raises(ValueError):
        validate_app_url(url)


def test_backend_verification_requires_independent_truth_and_matching_value():
    evidence = BackendPersistenceEvidence("independent SQL adapter", "BK-1001", "packed", True, True, True)
    assert evidence.verified
    assert not BackendPersistenceEvidence("app says success", "BK-1001", "packed", True, True, False).verified
    assert not BackendPersistenceEvidence("independent SQL adapter", "BK-1001", "packed", True, False, True).verified


def test_fresh_evaluator_context_keeps_platform_auth_and_discards_app_caches(tmp_path):
    async def run():
        state = {"cookies": [{"name": "auth-cookie", "value": "private-cookie"}], "origins": [
            {"origin": "https://app.example", "localStorage": [{"name": "packed", "value": "true"}]},
            {"origin": "https://workspace.example", "localStorage": [{"name": "platform", "value": "auth"}]},
        ]}
        path = tmp_path / "private-browser-auth.json"
        original = json.dumps(state)
        path.write_text(original)
        class FakeBrowser:
            async def new_context(self, **options):
                return options
        auth = AuthenticatedBrowser(path, clear_app_storage_origins=["https://app.example"])
        auth.browser = FakeBrowser()
        context = await auth.new_context()
        assert context["storage_state"]["cookies"] == state["cookies"]
        assert context["storage_state"]["origins"] == [state["origins"][1]]
        assert path.read_text() == original
    asyncio.run(run())


def test_failed_independent_task_stops_later_actions(tmp_path):
    async def run():
        page = FakePage()
        verifier = GeneratedAppBrowserVerifier(page, artifact_dir=tmp_path, timeout_seconds=0.01)
        results = await verifier.run_actions([
            BrowserAction("look for saved change", "expect_text", RoleLocator("row", "BK-1001"), "Unpacked"),
            BrowserAction("later mutation", "click", RoleLocator("button", "Pack")),
        ])
        assert len(results) == 1
        assert results[0]["passed"] is False
        assert "Pack" not in page.clicked
    asyncio.run(run())


def test_persistence_cannot_pass_without_backend_truth_or_assertions(tmp_path):
    async def run():
        page = FakePage()
        async def reload(**_options):
            pass
        page.reload = reload
        verifier = GeneratedAppBrowserVerifier(page, artifact_dir=tmp_path)
        async def second_context():
            return page.context  # same browser storage is not independent
        result = await verifier.verify_persistence(
            reload_actions=[], second_context_factory=second_context,
            app_url="https://app.example", second_context_actions=[],
        )
        assert not result["reload_passed"]
        assert not result["second_context_passed"]
        assert not result["persistence_verified"]
        assert not result["process_restart_verified"]
        assert "backend_storage_truth" in result["unverified_checks"]
    asyncio.run(run())


def write_export(path, records):
    path.write_text("\n".join(json.dumps(record) for record in records) + "\n")
    return path


def claude_record(text, *, complete=True, sidechain=False):
    return {"type": "assistant", "sessionId": "native-session", "version": "2.1.237",
            "uuid": "message-id-" + text, "timestamp": "2026-10-08T00:00:00Z", "isSidechain": sidechain,
            "message": {"role": "assistant", "stop_reason": "end_turn" if complete else None,
                        "content": [{"type": "thinking", "thinking": "private reasoning must stay out"},
                                    {"type": "text", "text": text}]}}


def test_native_claude_export_only_exposes_complete_parent_assistant_turns(tmp_path):
    path = write_export(tmp_path / "synthetic-claude.jsonl", [
        claude_record("Who will use the page?"),
        claude_record("partial text", complete=False),
        claude_record("worker-only question", sidechain=True),
        {"type": "user", "sessionId": "native-session", "version": "2.1.237",
         "message": {"role": "user", "content": [{"type": "tool_result", "content": "tool stdout"}]}},
    ])
    export = load_exported_transcript(path, harness_id="claude", harness_version="2.1.237", session_id="native-session")
    messages = export.complete_assistant_turns()
    assert [message.text for message in messages] == ["Who will use the page?"]
    assert export.incomplete_assistant_messages == 1
    report = json.dumps(export.evidence())
    assert "worker-only" not in report
    assert "tool stdout" not in report
    assert "private reasoning" not in report
    assert not export.evidence()["tool_and_worker_coverage_verified"]


def test_native_codex_export_requires_session_marker_and_final_phase(tmp_path):
    path = write_export(tmp_path / "synthetic-codex.jsonl", [
        {"type": "session_meta", "payload": {"id": "native-session", "cli_version": "0.148.0"}},
        {"type": "response_item", "payload": {"type": "message", "id": "msg-1", "role": "assistant",
          "content": [{"type": "output_text", "text": "unfinished commentary"}]}},
        {"type": "response_item", "payload": {"type": "message", "id": "msg-2", "role": "assistant",
          "phase": "final_answer", "content": [{"type": "output_text", "text": "Should this remember changes?"}]}},
        {"type": "response_item", "payload": {"type": "function_call_output", "output": "secret stdout"}},
    ])
    export = load_exported_transcript(path, harness_id="codex", harness_version="0.148.0", session_id="native-session")
    assert [message.text for message in export.complete_assistant_turns()] == ["Should this remember changes?"]
    assert "secret stdout" not in json.dumps(export.evidence())


@pytest.mark.parametrize("records", [
    [claude_record("Question?") | {"sessionId": "different-native-session"}],
    [{"type": "assistant", "message": {"role": "assistant", "content": "unattributed text"}}],
])
def test_native_exports_cannot_be_attributed_without_matching_native_metadata(tmp_path, records):
    path = write_export(tmp_path / "export.jsonl", records)
    with pytest.raises(ValueError):
        load_exported_transcript(path, harness_id="claude", harness_version="2.1.237", session_id="native-session")


def test_raw_terminal_text_is_never_a_structured_export(tmp_path):
    path = tmp_path / "raw-pty.txt"
    path.write_text("Coach: Who will use it?\n")
    with pytest.raises(ValueError, match="Malformed structured transcript"):
        load_exported_transcript(path, harness_id="claude", harness_version="2.1.237", session_id="native-session")
