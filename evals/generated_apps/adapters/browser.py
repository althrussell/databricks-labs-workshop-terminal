"""External browser adapters for the normal WT journey and independent app tasks.

Playwright is optional to local unit tests. Live runs require an already
authenticated synthetic-attendee browser state; operator bearer credentials are
never injected by this adapter. All WT mutations are made through visible UI.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Awaitable, Callable, Iterable
from urllib.parse import urlsplit

from .harness import HarnessObserver, redact_evidence


class BrowserDependencyError(RuntimeError):
    pass


class BrowserJourneyError(RuntimeError):
    """An explicit blocked browser journey; never silently rescued via the API."""

    def __init__(self, code: str, detail: str):
        super().__init__(detail)
        self.code = code


def require_playwright():
    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:
        raise BrowserDependencyError(
            "The external evaluation runner needs Playwright. Prepare its Python "
            "package and Chromium browser outside the attendee container before a live run."
        ) from exc
    return async_playwright


def validate_app_url(value: str) -> str:
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username or parsed.password:
        raise ValueError("Browser app URLs must be HTTP(S) without embedded credentials")
    if parsed.query or parsed.fragment:
        raise ValueError("Browser app URLs must not contain a query or fragment")
    return value.rstrip("/")


class AuthenticatedBrowser:
    """Create separate browser contexts from separately supplied auth states."""

    def __init__(self, storage_state_path: str | Path | None, *, headless: bool = True,
                 clear_app_storage_origins: Iterable[str] = ()):
        self.storage_state_path = Path(storage_state_path) if storage_state_path else None
        self.headless = headless
        self.clear_app_storage_origins = tuple(clear_app_storage_origins)
        self._playwright = None
        self.browser = None
        self.context = None
        self.page = None

    async def __aenter__(self):
        if self.storage_state_path and not self.storage_state_path.is_file():
            raise BrowserJourneyError("browser_auth_state_missing",
                                      "The supplied browser authentication state does not exist.")
        self._playwright = await require_playwright()().start()
        try:
            self.browser = await self._playwright.chromium.launch(headless=self.headless)
            self.context = await self.new_context()
            self.page = await self.context.new_page()
        except Exception as exc:
            await self.__aexit__(None, None, None)
            if "Executable doesn't exist" in str(exc):
                raise BrowserDependencyError(
                    "Chromium is not prepared for Playwright. Prepare it on the external "
                    "runner before a live run; do not install it in the attendee app."
                ) from exc
            raise
        return self

    async def new_context(self):
        if self.browser is None:
            raise RuntimeError("Browser must be started before creating an authenticated context")
        options = {"viewport": {"width": 1440, "height": 900}}
        if self.storage_state_path:
            if self.clear_app_storage_origins:
                state = json.loads(self.storage_state_path.read_text(encoding="utf-8"))
                blocked_origins = set(self.clear_app_storage_origins)
                state["origins"] = [origin for origin in state.get("origins", [])
                                    if origin.get("origin") not in blocked_origins]
                # Retain the platform's auth cookies, clear the generated app's
                # local/IndexedDB caches, and never save the modified auth state.
                options["storage_state"] = state
            else:
                options["storage_state"] = str(self.storage_state_path)
        # A fresh context shares only explicitly supplied platform auth, never
        # app local/session storage from the first task context.
        return await self.browser.new_context(**options)

    async def __aexit__(self, *_exc):
        if self.browser:
            await self.browser.close()
        if self._playwright:
            await self._playwright.stop()


@dataclass(frozen=True)
class BrowserLaunchConfig:
    wt_url: str
    agent_id: str
    opening_message: str
    expected_attendee_email: str
    entry_path: str = "wizard"
    industry: str | None = None
    allow_industry_step: bool = False
    deadline_seconds: float = 180.0
    run_deadline_seconds: float = 1800.0
    require_fresh_session: bool = True

    def __post_init__(self):
        validate_app_url(self.wt_url)
        if self.entry_path not in {"wizard", "skip_wizard", "wizard_disabled"}:
            raise ValueError("entry_path must be wizard, skip_wizard or wizard_disabled")
        if not self.expected_attendee_email.strip() or not self.opening_message.strip():
            raise ValueError("Expected attendee identity and opening message are required")
        if self.deadline_seconds <= 0:
            raise ValueError("Browser deadline must be positive")
        if self.run_deadline_seconds < self.deadline_seconds:
            raise ValueError("Run deadline cannot be shorter than browser entry deadline")


@dataclass
class BrowserEntryEvidence:
    identity: str = ""
    entry_path: str = ""
    session_id: str = ""
    agent_id: str = ""
    wizard_enabled: bool | None = None
    wizard_should_show: bool | None = None
    saved_brief: dict = field(default_factory=dict)
    starter_prompt_from_save: str = ""
    prompt_delivery_request: str = ""
    prompt_delivery_http_status: int | None = None
    friction: list[str] = field(default_factory=list)
    opening_submitted: bool = False
    harness_readiness_verified: bool = False
    prompt_preservation_verified: bool = False
    error_code: str = ""
    error_detail: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


class WorkshopBrowserDriver:
    """Drive an existing authenticated page or one from AuthenticatedBrowser."""

    def __init__(self, page, *, secrets: Iterable[str] = ()):
        self.page = page
        self.secrets = tuple(secrets)
        self.harness = HarnessObserver(secrets=self.secrets)
        self.evidence = BrowserEntryEvidence()
        self._config = None
        self._agents: list[dict] | None = None
        self._sessions: list[dict] | None = None
        self._wizard: dict | None = None
        self._wt_origin = ""
        self._deadline = 0.0
        self._run_deadline = 0.0
        self._response_tasks: set[asyncio.Task] = set()
        self._response_errors: list[str] = []
        self._entry_started = False

    def _timeout_ms(self) -> float:
        remaining = self._deadline - time.monotonic()
        if remaining <= 0:
            raise BrowserJourneyError("entry_deadline", "Workshop browser entry exceeded its deadline.")
        return remaining * 1000

    def _observe_response(self, response) -> None:
        task = asyncio.create_task(self._record_response(response))
        self._response_tasks.add(task)
        task.add_done_callback(self._response_tasks.discard)

    async def _record_response(self, response) -> None:
        parsed = urlsplit(response.url)
        if f"{parsed.scheme}://{parsed.netloc}" != self._wt_origin:
            return
        path = parsed.path
        if path not in {"/api/config", "/api/agents", "/api/sessions", "/api/wizard"} and not (
                path.startswith("/api/sessions/") and path.endswith("/type")):
            return
        method = response.request.method
        try:
            body = await response.json()
            if not isinstance(body, dict):
                return
            if path == "/api/config" and response.ok:
                user = body.get("user", {})
                self.evidence.identity = str(user.get("email", ""))
                self.evidence.wizard_enabled = body.get("onboarding_wizard", {}).get("enabled")
                # Retain only the config fields the driver needs; credential and
                # OBO payloads are not copied to evidence.
                self._config = {"user": user, "onboarding_wizard": body.get("onboarding_wizard", {})}
            elif path == "/api/agents" and response.ok:
                self._agents = body.get("agents", [])
            elif path == "/api/sessions" and method == "GET" and response.ok:
                self._sessions = body.get("sessions", [])
            elif path == "/api/sessions" and method == "POST" and response.ok:
                session = body.get("session", body)
                self.evidence.session_id = str(session.get("id", ""))
                self.evidence.agent_id = str(session.get("agent_id", ""))
            elif path == "/api/wizard" and method == "GET" and response.ok:
                # Hero renders before the first wizard request finishes. Its
                # visibility alone cannot establish that onboarding is settled.
                if type(body.get("enabled")) is bool and type(body.get("should_show")) is bool:
                    self._wizard = {key: body[key] for key in ("enabled", "should_show")}
                    self.evidence.wizard_should_show = body["should_show"]
            elif path == "/api/wizard" and method == "POST" and response.ok:
                brief = body.get("brief", {})
                saved_brief = {k: v for k, v in brief.items() if k in {
                    "record_id", "what_building", "industry", "industry_stated", "intent", "idea_id",
                    "current_stack", "persona", "seen", "skipped", "completed_at", "revision"}}
                self.evidence.saved_brief = json.loads(redact_evidence(json.dumps(saved_brief), self.secrets))
                self.evidence.starter_prompt_from_save = redact_evidence(
                    str(body.get("starter_prompt", "")), self.secrets)
            elif path.endswith("/type") and method == "POST":
                request_body = response.request.post_data_json or {}
                self.evidence.prompt_delivery_request = redact_evidence(
                    str(request_body.get("text", "")), self.secrets)
                self.evidence.prompt_delivery_http_status = response.status
        except Exception as exc:
            # Never capture response bodies/headers as diagnostic exception text.
            self._response_errors.append(type(exc).__name__)

    async def _wait_for(self, predicate: Callable[[], bool]) -> None:
        while not predicate():
            self._timeout_ms()
            await asyncio.sleep(0.05)

    async def submit_question_answers(self, interaction, answers):
        raise BrowserJourneyError("native_question_ui_unqualified", "Structured questions require the qualified native UI adapter.")

    async def enter(self, config: BrowserLaunchConfig) -> BrowserEntryEvidence:
        if self._entry_started:
            raise BrowserJourneyError("driver_already_used", "A fresh journey needs a new browser driver.")
        self._entry_started = True
        self._deadline = time.monotonic() + config.deadline_seconds
        self._run_deadline = time.monotonic() + config.run_deadline_seconds
        self.evidence.entry_path = config.entry_path
        parsed = urlsplit(config.wt_url)
        self._wt_origin = f"{parsed.scheme}://{parsed.netloc}"
        self.page.on("response", self._observe_response)
        self.page.on("websocket", lambda socket: self.harness.attach(socket, wt_origin=self._wt_origin))
        try:
            await self.page.goto(config.wt_url, wait_until="domcontentloaded", timeout=self._timeout_ms())
            if urlsplit(self.page.url).netloc != parsed.netloc:
                raise BrowserJourneyError("wt_browser_auth_required",
                                          "WT redirected this browser to a separate sign-in surface.")
            await self._wait_for(lambda: self._config is not None and self._agents is not None
                                 and self._sessions is not None)
            if self.evidence.identity.casefold() != config.expected_attendee_email.casefold():
                raise BrowserJourneyError("attendee_identity_mismatch",
                                          "The WT browser identity differs from the designated synthetic attendee.")
            if config.require_fresh_session and any(not s.get("exited") for s in self._sessions or []):
                raise BrowserJourneyError("existing_attendee_session",
                                          "A fresh run requires an attendee with no existing live session.")
            agents = [a for a in self._agents or [] if a.get("id") == config.agent_id]
            if len(agents) != 1:
                raise BrowserJourneyError("agent_not_offered", "The requested harness is not offered by this WT.")
            agent = agents[0]
            if not agent.get("ready"):
                raise BrowserJourneyError("agent_not_ready", "The requested harness is installing or blocked.")
            await self._wait_for(lambda: self._wizard is not None)
            if self._wizard["enabled"] != self.evidence.wizard_enabled:
                raise BrowserJourneyError("wizard_state_changed", "WT changed onboarding state during entry.")
            dialog = self.page.get_by_role("dialog")
            if config.entry_path == "wizard":
                if self.evidence.wizard_enabled is not True:
                    raise BrowserJourneyError("wizard_disabled", "The run requested onboarding, but this WT has it disabled.")
                await dialog.wait_for(state="visible", timeout=self._timeout_ms())
                await dialog.locator("textarea").fill(config.opening_message, timeout=self._timeout_ms())
                next_button = dialog.get_by_role("button", name="Next", exact=True)
                if not await next_button.is_enabled():
                    self.evidence.friction.append("plain_goal_blocked_by_industry_gate")
                    if not config.allow_industry_step or not config.industry:
                        raise BrowserJourneyError("industry_required",
                                                  "The typed novice goal cannot continue without confirming an industry.")
                    await dialog.get_by_role("button", name=config.industry, exact=True).click(
                        timeout=self._timeout_ms())
                    self.evidence.friction.append(f"explicit_industry_step:{config.industry}")
                context_button = dialog.get_by_role("button", name="A little context (optional)", exact=True)
                if await context_button.count():
                    await context_button.click(timeout=self._timeout_ms())
                    await dialog.get_by_role("button", name="Plain language", exact=True).click(
                        timeout=self._timeout_ms())
                async with self.page.expect_response(
                        lambda r: urlsplit(r.url).path == "/api/wizard" and r.request.method == "POST",
                        timeout=self._timeout_ms()) as saved:
                    await next_button.click(timeout=self._timeout_ms())
                if not (await saved.value).ok:
                    raise BrowserJourneyError("wizard_save_failed", "The normal UI could not save the novice brief.")
                await self._wait_for(lambda: bool(self.evidence.saved_brief))
                await dialog.get_by_role("heading", name="Pick your agent and go", exact=True).wait_for(
                    state="visible", timeout=self._timeout_ms())
                picker = dialog.locator(".hero-cards")
            else:
                if config.entry_path == "wizard_disabled" and self.evidence.wizard_enabled is not False:
                    raise BrowserJourneyError("wizard_enabled", "The disabled-wizard arm requires a disabled deployment.")
                if config.entry_path == "skip_wizard" and self.evidence.wizard_enabled:
                    if self._wizard["should_show"]:
                        await dialog.wait_for(state="visible", timeout=self._timeout_ms())
                        await dialog.get_by_role("button", name="Skip", exact=True).click(timeout=self._timeout_ms())
                await dialog.wait_for(state="hidden", timeout=self._timeout_ms())
                picker = self.page.locator(".hero .hero-cards")
                await picker.wait_for(state="visible", timeout=self._timeout_ms())
            async with self.page.expect_response(
                    lambda r: urlsplit(r.url).path == "/api/sessions" and r.request.method == "POST",
                    timeout=self._timeout_ms()) as created:
                await picker.get_by_role("button", name=agent["label"], exact=False).click(
                    timeout=self._timeout_ms())
            if not (await created.value).ok:
                raise BrowserJourneyError("session_launch_failed", "The normal UI could not launch the requested harness.")
            await self._wait_for(lambda: bool(self.evidence.session_id))
            if self.evidence.agent_id != config.agent_id:
                raise BrowserJourneyError("wrong_harness", "The UI launched a different harness than requested.")
            await self._wait_for(lambda: self.evidence.session_id in self.harness.session_ids)
            await self._wait_for(lambda: any(
                frame.session_id == self.evidence.session_id and frame.direction == "sent" and frame.kind == "resize"
                for frame in self.harness.frames))
            if config.entry_path == "wizard":
                await self._wait_for(lambda: self.evidence.prompt_delivery_http_status is not None)
                if self.evidence.prompt_delivery_http_status != 200:
                    raise BrowserJourneyError("prompt_delivery_failed", "The saved starter was not delivered to the PTY.")
                if len(self.evidence.prompt_delivery_request) > 500:
                    self.evidence.friction.append("starter_exceeds_current_server_500_character_type_limit")
                    raise BrowserJourneyError(
                        "starter_prompt_truncated",
                        "The saved starter exceeds WT's current 500-character delivery limit; "
                        "prompt preservation cannot pass.")
                self.evidence.prompt_preservation_verified = (
                    self.evidence.prompt_delivery_request == self.evidence.starter_prompt_from_save)
                if not self.evidence.prompt_preservation_verified:
                    raise BrowserJourneyError("starter_prompt_changed",
                                              "The UI delivery request differs from the saved starter.")
                await self.submit_reply("", submit_only=True)
            else:
                await self.submit_reply(config.opening_message)
            self.evidence.opening_submitted = True
            self._deadline = self._run_deadline
        except BrowserJourneyError as exc:
            self.evidence.error_code, self.evidence.error_detail = exc.code, str(exc)
        except Exception as exc:
            self.evidence.error_code = "browser_ui_error"
            self.evidence.error_detail = (
                "The normal browser journey failed: " + type(exc).__name__ + ". "
                "Check browser access, visible UI, and the bounded driver artifacts.")
        return self.evidence

    async def submit_reply(self, text: str, *, submit_only: bool = False) -> None:
        terminal = self.page.locator(".terminal-container .xterm-helper-textarea")
        await terminal.wait_for(state="attached", timeout=self._timeout_ms())
        await terminal.focus(timeout=self._timeout_ms())
        if not submit_only:
            if not text.strip():
                raise ValueError("Novice replies cannot be empty")
            await self.page.keyboard.insert_text(text)
        await self.page.keyboard.press("Enter")

    async def reconnect(self) -> None:
        """Reload the visible page; rely on WT's ordinary owned-session replay."""
        sid = self.evidence.session_id
        previous_frames = len(self.harness.frames)
        await self.page.reload(wait_until="domcontentloaded", timeout=self._timeout_ms())
        await self._wait_for(lambda: any(
            frame.session_id == sid and frame.direction == "received" and frame.kind in {"replay", "output"}
            for frame in self.harness.frames[previous_frames:]))
        if not sid:
            raise BrowserJourneyError("session_reconnect_unverified", "No replay of the original owned session was observed.")

    async def close_launched_session(self, label: str, *, expected_session_id: str | None = None,
                                    timeout_seconds: float = 10) -> None:
        """Close only the run's UI-launched session using its visible close button."""
        sid = self.evidence.session_id
        if not sid or not label or (expected_session_id is not None and sid != expected_session_id):
            raise BrowserJourneyError("cleanup_session_changed", "The owned UI session is no longer identifiable.")
        if any(observed != sid for observed in self.harness.session_ids):
            raise BrowserJourneyError("cleanup_session_changed", "More than the run's owned session was observed.")
        if not 0 < timeout_seconds <= 60:
            raise ValueError("Cleanup timeout must be positive and at most 60 seconds")
        owner = self.evidence.identity
        # Refresh through the normal page before clicking a label-only control.
        # A stale same-harness label must never close a replacement session.
        end = time.monotonic() + timeout_seconds
        self._sessions = None
        self._config = None
        await self.page.reload(wait_until="domcontentloaded", timeout=timeout_seconds * 1000)
        while self._sessions is None or self._config is None:
            if time.monotonic() >= end:
                raise BrowserJourneyError("cleanup_session_unverified", "The current owned UI session could not be verified.")
            await asyncio.sleep(0.05)
        active = [session for session in self._sessions if not session.get("exited")]
        if not active and self.evidence.identity == owner:
            # The normal owned-session list confirms the harness already exited.
            await self.page.get_by_role("button", name=f"Close {label}", exact=True).wait_for(
                state="hidden", timeout=max(1, (end - time.monotonic()) * 1000))
            return
        if (len(active) != 1 or active[0].get("id") != sid
                or active[0].get("agent_id") != self.evidence.agent_id
                or self.evidence.identity != owner):
            raise BrowserJourneyError("cleanup_session_changed", "The refreshed page does not show only the owned session.")
        def remaining_ms():
            remaining = (end - time.monotonic()) * 1000
            if remaining <= 0:
                raise BrowserJourneyError("cleanup_deadline", "Owned-session cleanup exceeded its deadline.")
            return remaining
        button = self.page.get_by_role("button", name=f"Close {label}", exact=True)
        if not await button.is_visible():
            back = self.page.locator(f'button[title="Back to {label}"]')
            if await back.count() == 1:
                await back.click(timeout=remaining_ms())
        await button.wait_for(state="visible", timeout=remaining_ms())
        if await button.count() != 1:
            raise BrowserJourneyError("cleanup_session_changed", "The owned UI close control is ambiguous.")
        # Cleanup has its own small grace period: the run deadline is often the
        # reason for closing, so using _timeout_ms() here would prevent cleanup.
        async with self.page.expect_response(
                lambda response: urlsplit(response.url).path == f"/api/sessions/{sid}"
                and response.request.method == "DELETE", timeout=remaining_ms()) as deleted:
            await button.click(timeout=remaining_ms())
        if not (await deleted.value).ok:
            raise BrowserJourneyError("cleanup_delete_failed", "The owned UI session close request failed.")
        await button.wait_for(state="hidden", timeout=remaining_ms())


@dataclass(frozen=True)
class RoleLocator:
    role: str
    name: str
    exact: bool = True
    scope_role: str | None = None
    scope_name: str | None = None

    def on(self, page):
        scope = page
        if self.scope_role:
            scope = page.get_by_role(self.scope_role, name=self.scope_name, exact=self.exact)
        return scope.get_by_role(self.role, name=self.name, exact=self.exact)


@dataclass(frozen=True)
class BrowserAction:
    name: str
    operation: str
    target: RoleLocator | None = None
    value: str = ""


@dataclass(frozen=True)
class BackendPersistenceEvidence:
    """Observation provided by an external storage adapter, outside the app UI."""
    source: str
    record_id: str
    field: str
    expected: object
    observed: object
    independently_observed: bool

    @property
    def verified(self) -> bool:
        return bool(self.independently_observed and self.source and self.record_id and self.field
                    and self.observed == self.expected)


class GeneratedAppBrowserVerifier:
    """Run private evaluator tasks against the actual app in an isolated context."""

    def __init__(self, page, *, artifact_dir: str | Path, timeout_seconds: float = 30,
                 secrets: Iterable[str] = ()):
        self.page = page
        self.artifact_dir = Path(artifact_dir)
        self.artifact_dir.mkdir(parents=True, exist_ok=True)
        self.timeout_ms = timeout_seconds * 1000
        self.secrets = tuple(secrets)
        self.task_results: list[dict] = []
        self.page_errors: list[str] = []
        page.on("pageerror", lambda error: self.page_errors.append(
            redact_evidence(str(error), self.secrets)))

    async def open(self, app_url: str) -> None:
        app_url = validate_app_url(app_url)
        response = await self.page.goto(app_url, wait_until="domcontentloaded", timeout=self.timeout_ms)
        expected = urlsplit(app_url)
        actual = urlsplit(self.page.url)
        if actual.netloc != expected.netloc or (response and response.status in {401, 403}):
            raise BrowserJourneyError("generated_app_browser_auth_required",
                                      "The generated app requires an authenticated evaluator browser context.")
        if response and response.status >= 400:
            raise BrowserJourneyError("generated_app_http_failure",
                                      f"The generated app returned HTTP {response.status}.")

    async def run_actions(self, actions: Iterable[BrowserAction]) -> list[dict]:
        """Actions/assertions are private evaluator inputs, never builder prompts."""
        for action in actions:
            result = {"name": action.name, "operation": action.operation, "passed": False}
            try:
                target = action.target.on(self.page) if action.target else None
                if action.operation == "click" and target is not None:
                    await target.click(timeout=self.timeout_ms)
                elif action.operation == "fill" and target is not None:
                    await target.fill(action.value, timeout=self.timeout_ms)
                elif action.operation == "expect_visible" and target is not None:
                    await target.wait_for(state="visible", timeout=self.timeout_ms)
                elif action.operation == "expect_text" and target is not None:
                    deadline = time.monotonic() + self.timeout_ms / 1000
                    while True:
                        remaining_ms = max(1, (deadline - time.monotonic()) * 1000)
                        text = await target.inner_text(timeout=remaining_ms)
                        if action.value in text:
                            break
                        if time.monotonic() >= deadline:
                            raise BrowserJourneyError("task_assertion_failed", "Expected product text was absent.")
                        await asyncio.sleep(0.05)
                elif action.operation == "reload":
                    await self.page.reload(wait_until="domcontentloaded", timeout=self.timeout_ms)
                elif action.operation == "press" and target is not None:
                    await target.press(action.value, timeout=self.timeout_ms)
                else:
                    raise ValueError("Unsupported declarative browser action or missing target")
                result["passed"] = True
            except Exception as exc:
                result["error_type"] = type(exc).__name__
                if isinstance(exc, BrowserJourneyError):
                    result["error_code"] = exc.code
                self.task_results.append(result)
                break
            self.task_results.append(result)
        return self.task_results

    async def capture_viewport(self, name: str, width: int, height: int) -> dict:
        if not name or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_" for c in name):
            raise ValueError("Artifact names must contain only letters, numbers, underscores or hyphens")
        await self.page.set_viewport_size({"width": width, "height": height})
        await self.page.screenshot(path=str(self.artifact_dir / f"{name}.png"), full_page=True)
        snapshot = await self.page.locator("body").aria_snapshot(timeout=self.timeout_ms)
        (self.artifact_dir / f"{name}.aria.txt").write_text(
            redact_evidence(snapshot, self.secrets), encoding="utf-8")
        overflow = await self.page.evaluate(
            "() => ({ viewport: window.innerWidth, content: document.documentElement.scrollWidth, "
            "horizontalOverflow: document.documentElement.scrollWidth > window.innerWidth })")
        return {"name": name, "width": width, "height": height, "screenshot": f"{name}.png",
                "accessibility_snapshot": f"{name}.aria.txt", "layout_observation": overflow}

    async def verify_persistence(self, *, reload_actions: Iterable[BrowserAction],
                                 second_context_factory: Callable[[], Awaitable], app_url: str,
                                 second_context_actions: Iterable[BrowserAction],
                                 backend_observer: Callable[[], Awaitable[BackendPersistenceEvidence]] | None = None) -> dict:
        reload_actions = list(reload_actions)
        second_context_actions = list(second_context_actions)
        before = len(self.task_results)
        await self.run_actions([BrowserAction("reload", "reload"), *reload_actions])
        reload_passed = bool(reload_actions) and all(r["passed"] for r in self.task_results[before:])
        context = await second_context_factory()
        second_results = []
        try:
            if context is self.page.context:
                raise BrowserJourneyError("second_context_not_independent",
                                          "Persistence needs a fresh authenticated browser context.")
            second_page = await context.new_page()
            verifier = GeneratedAppBrowserVerifier(second_page, artifact_dir=self.artifact_dir / "second-context",
                                                    timeout_seconds=self.timeout_ms / 1000, secrets=self.secrets)
            await verifier.open(app_url)
            second_results = await verifier.run_actions(second_context_actions)
            second_passed = bool(second_results) and all(r["passed"] for r in second_results)
        except BrowserJourneyError as exc:
            second_passed = False
            second_results = [{"passed": False, "error_code": exc.code}]
        finally:
            if context is not self.page.context:
                await context.close()
        backend = await backend_observer() if backend_observer else None
        return {
            "reload_passed": reload_passed,
            "second_context_passed": second_passed,
            "second_context_tasks": second_results,
            "backend": asdict(backend) if backend else None,
            "backend_verified": backend.verified if backend else False,
            "persistence_verified": bool(reload_passed and second_passed and backend and backend.verified),
            "process_restart_verified": False,
            "unverified_checks": ["app_process_restart"] + ([] if backend else ["backend_storage_truth"]),
        }
