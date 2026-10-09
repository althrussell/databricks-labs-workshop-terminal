#!/usr/bin/env python3
"""Run a novice journey against an exact standalone WT simulation receipt.

Default is a local plan. --execute performs read-only workspace verification and
the ordinary attendee UI journey, including owned session cleanup. No Control
Tower endpoint, deployment operation, credential impersonation or repair prompt
is used. Generated app discovery is separate from independent app acceptance.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from urllib.parse import parse_qs, urlsplit
from uuid import UUID

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from evals.generated_apps.adapters.browser import AuthenticatedBrowser, BrowserJourneyError, BrowserLaunchConfig, WorkshopBrowserDriver, validate_app_url
from evals.generated_apps.adapters.harness import redact_evidence
from evals.generated_apps.adapters.simulated_control_tower import bind_created_app, deployment_path_allowed
from evals.generated_apps.journey import AppObservation, JourneyConfig, NativeEvidenceError, run_journey
from evals.generated_apps.report import validate_budgets, write_evidence
from evals.generated_apps.simulator import NoviceSimulator, SimulatorBudget, load_simulator_scenario
from evals.generated_apps.interactions import validate_question


class GateError(NativeEvidenceError):
    """Stable evidence code; never include remote bodies or authentication."""


def require(condition, reason):
    if not condition:
        raise GateError(reason)


class StartupQualifiedDriver(WorkshopBrowserDriver):
    """Opt-in startup-only screen handling; native transcripts still judge turns.

    This cannot qualify the wizard's eager starter delivery. It accepts only the
    Claude trust controls for the receipt-bound owned projects folder, including
    the reviewed current CLI's default No selection.
    Every other confirmation remains unresolved rather than receiving keys.
    """

    def __init__(self, page, *, binding, output):
        super().__init__(page)
        try:
            import pyte
        except ImportError as exc:
            raise GateError("startup_screen_renderer_unavailable") from exc
        self._pyte = pyte
        self._startup_binding = binding
        email = binding["attendee"]["email"]
        local = re.sub(r"[^a-z0-9]+", "-", email.split("@")[0].lower()).strip("-") or "user"
        slug = local + "-" + hashlib.sha256(email.encode()).hexdigest()[:8]
        data_root = binding["environment"].get("DATA_ROOT", "")
        require(data_root == f"/app/python/source_code/data/{binding['marker']}", "startup_owned_home_unverified")
        self._expected_cwd = f"{data_root}/users/{slug}/projects"
        self._startup_artifacts = startup_artifact_dir(output)
        self._screens = {}
        self._startup_complete = False
        self.startup_evidence = {"enabled": True, "source": "attendee_browser_existing_pty_startup_screen",
                                 "status": "awaiting_startup", "expected_cwd": self._expected_cwd,
                                 "actions": [], "captures": [], "native_prompt_verified": False,
                                 "user_scope_consent_inferred": False, "assistant_turns_inferred": False}
        page.on("websocket", self._startup_socket)

    def _startup_socket(self, socket):
        parsed, target = urlsplit(socket.url), urlsplit(self._startup_binding["app"]["url"])
        sid = self.harness.session_id(socket.url)
        if parsed.scheme != "wss" or parsed.netloc != target.netloc or not canonical_uuid(sid):
            return
        screen = self._pyte.Screen(200, 60)
        state = {"screen": screen, "stream": self._pyte.Stream(screen), "resized": False, "bytes": 0, "invalid": False}
        self._screens[sid] = state
        socket.on("framesent", lambda payload: self._startup_frame(state, "sent", payload))
        socket.on("framereceived", lambda payload: self._startup_frame(state, "received", payload))

    def _startup_frame(self, state, direction, payload):
        try:
            frame = json.loads(payload)
            if not isinstance(frame, dict):
                return
            if direction == "sent" and frame.get("t") == "resize":
                cols, rows = frame.get("cols"), frame.get("rows")
                if type(cols) is not int or type(rows) is not int or not 40 <= cols <= 400 or not 10 <= rows <= 200:
                    state["invalid"] = True
                    return
                state["screen"].resize(lines=rows, columns=cols)
                state["resized"] = True
            elif direction == "received" and frame.get("t") in {"replay", "output"} and isinstance(frame.get("data"), str):
                state["bytes"] += len(frame["data"].encode())
                if state["bytes"] > 2 * 1024 * 1024:
                    state["invalid"] = True
                    return
                state["stream"].feed(frame["data"])
        except Exception:
            state["invalid"] = True

    def _startup_screen(self):
        sid = self.evidence.session_id
        if (self.harness.session_ids != {sid} or set(self._screens) != {sid}
                or sid in self.harness.closed_sessions or self.evidence.identity.casefold() != self._startup_binding["attendee"]["email"].casefold()
                or (urlsplit(self.page.url).scheme, urlsplit(self.page.url).netloc)
                != (urlsplit(self._startup_binding["app"]["url"]).scheme, urlsplit(self._startup_binding["app"]["url"]).netloc)):
            raise BrowserJourneyError("startup_session_binding_changed", "Startup requires the sole receipt-bound attendee session.")
        state = self._screens[sid]
        if state["invalid"]:
            raise BrowserJourneyError("startup_screen_unverified", "The startup screen exceeded its supported bounds.")
        return "\n".join(state["screen"].display).rstrip() if state["resized"] and state["bytes"] else ""

    def _selected_owned_trust(self, screen):
        lines = [line.strip() for line in screen.splitlines()]
        return self._current_owned_trust(screen, selected_yes=True) or (self.evidence.agent_id == "claude" and self._expected_cwd in lines
                and any(heading in screen for heading in ("Do you trust the files in this folder?",
                                                          "Is this a project you created or one you trust?"))
                and any(re.fullmatch(r"[>❯›]\s*1\. Yes, I trust this folder", line) for line in lines)
                and "2. No, exit" in lines and "Enter to confirm" in screen)

    def _current_owned_trust(self, screen, *, selected_yes):
        lines = [line.strip() for line in screen.splitlines()]
        selected = "Yes, I trust this folder" if selected_yes else "No, exit"
        other = "No, exit" if selected_yes else "Yes, I trust this folder"
        return (self.evidence.agent_id == "claude"
                and self._startup_binding["environment"].get("CLAUDE_CODE_VERSION") == "2.1.283"
                and self._expected_cwd in lines and "Accessing workspace:" in lines
                and "Is this a project you created or one you trust?" in screen
                and any(re.fullmatch(r"[>❯›]\s*" + re.escape(selected), line) for line in lines)
                and other in lines and "Enter to confirm · Esc to cancel" in lines)

    def _native_prompt(self, screen):
        env = self._startup_binding["environment"]
        banner = {"claude": "Claude Code v" + env.get("CLAUDE_CODE_VERSION", "2.1.237"),
                  "codex": "OpenAI Codex (v" + env.get("CODEX_CLI_VERSION", "0.148.0") + ")"}.get(self.evidence.agent_id)
        confirmations = ("Do you trust", "Is this a project you created", "Yes, I trust this folder", "No, exit",
                         "Enter to confirm", "Require approval", "Allow Codex to work")
        prompt = (r'[>❯›]\s*(?:Try "[^"\r\n]{1,120}")?' if self.evidence.agent_id == "claude"
                  else r"[>❯›]\s*")
        return bool(banner and banner in screen and not any(text in screen for text in confirmations)
                    and any(re.fullmatch(prompt, line.strip()) for line in screen.splitlines()))

    async def _capture_startup(self, label, screen):
        self._startup_artifacts.mkdir(mode=0o700, parents=False, exist_ok=True)
        screenshot = self._startup_artifacts / f"{len(self.startup_evidence['captures']) + 1}-{label}.png"
        text = screenshot.with_suffix(".txt")
        await self.page.locator(".terminal-container").screenshot(path=str(screenshot), timeout=min(10000, self._timeout_ms()))
        screenshot.chmod(0o600)
        text.write_text(redact_evidence(screen), encoding="utf-8")
        text.chmod(0o600)
        self.startup_evidence["captures"].append({"label": label, "session_id": self.evidence.session_id,
                                                  "screenshot": str(screenshot.resolve()), "screen_text": str(text.resolve()),
                                                  "screenshot_sha256": hashlib.sha256(screenshot.read_bytes()).hexdigest(),
                                                  "screen_text_sha256": hashlib.sha256(text.read_bytes()).hexdigest()})
        if self._startup_screen() != screen:
            raise BrowserJourneyError("startup_screen_changed", "Startup changed during the recorded browser capture.")

    async def _qualify_startup(self):
        deadline = time.monotonic() + min(30, self._timeout_ms() / 1000)
        trusted = False
        trust_selection_changed = False
        previous_screen, stable_since = "", time.monotonic()
        self.startup_evidence["session_id"] = self.evidence.session_id
        try:
            while time.monotonic() < deadline:
                screen = self._startup_screen()
                if screen != previous_screen:
                    previous_screen, stable_since = screen, time.monotonic()
                if time.monotonic() - stable_since < .25:
                    await asyncio.sleep(.05)
                    continue
                if not trusted and not trust_selection_changed and self._current_owned_trust(screen, selected_yes=False):
                    await self._capture_startup("owned-folder-trust-default-no", screen)
                    terminal = self.page.locator(".terminal-container .xterm-helper-textarea")
                    await terminal.focus(timeout=self._timeout_ms())
                    if self._startup_screen() != screen or not self._current_owned_trust(self._startup_screen(), selected_yes=False):
                        raise BrowserJourneyError("startup_screen_changed_before_confirmation", "The owned-folder trust choices changed before selection.")
                    await self.page.keyboard.press("ArrowDown")
                    self.startup_evidence["actions"].append({"kind": "select_owned_folder_trust", "key": "ArrowDown",
                                                              "session_id": self.evidence.session_id, "cwd": self._expected_cwd})
                    trust_selection_changed = True
                elif not trusted and self._selected_owned_trust(screen):
                    await self._capture_startup("owned-folder-trust", screen)
                    self.evidence.friction.append("owned_folder_trust_requires_confirmation")
                    terminal = self.page.locator(".terminal-container .xterm-helper-textarea")
                    await terminal.focus(timeout=self._timeout_ms())
                    if self._startup_screen() != screen or not self._selected_owned_trust(self._startup_screen()):
                        raise BrowserJourneyError("startup_screen_changed_before_confirmation", "The selected owned-folder trust screen changed before Enter.")
                    await self.page.keyboard.press("Enter")
                    self.startup_evidence["actions"].append({"kind": "selected_owned_folder_trust", "key": "Enter",
                                                              "session_id": self.evidence.session_id, "cwd": self._expected_cwd})
                    trusted = True
                    self.evidence.friction.append("evaluator_confirmed_owned_folder_trust")
                elif self._native_prompt(screen) and not self._selected_owned_trust(screen):
                    await self._capture_startup("native-prompt", screen)
                    self._startup_native_screen = screen
                    self.startup_evidence.update(status="native_prompt_observed", native_prompt_verified=False,
                                                  owned_folder_trust_confirmed=trusted)
                    return
                await asyncio.sleep(.05)
            screen = self._startup_screen()
            if screen:
                try:
                    await self._capture_startup("unqualified-startup", screen)
                except Exception as error:
                    self.startup_evidence["final_capture_error_type"] = type(error).__name__
            raise BrowserJourneyError("startup_native_prompt_unverified", "No recognized pinned native prompt appeared before the startup deadline.")
        except BrowserJourneyError as error:
            self.startup_evidence.update(status="unverified", reason=error.code)
            raise

    async def enter(self, config):
        require(config.entry_path == "skip_wizard", "startup_qualification_requires_skip_wizard")
        return await super().enter(config)

    async def submit_reply(self, text, *, submit_only=False):
        if self._startup_complete:
            await super().submit_reply(text, submit_only=submit_only)
            return
        try:
            await self._qualify_startup()
            terminal = self.page.locator(".terminal-container .xterm-helper-textarea")
            await terminal.wait_for(state="attached", timeout=self._timeout_ms())
            await terminal.focus(timeout=self._timeout_ms())
            screen = self._startup_screen()
            if screen != self._startup_native_screen or not self._native_prompt(screen):
                raise BrowserJourneyError("startup_screen_changed_before_opening", "The pinned native prompt changed before the original opening.")
            if not submit_only:
                if not text.strip():
                    raise ValueError("Novice replies cannot be empty")
                await self.page.keyboard.insert_text(text)
                # Input echo can change the empty prompt, but a newly appeared
                # confirmation must not receive the subsequent Enter key.
                if any(control in self._startup_screen() for control in ("Do you trust", "Is this a project you created",
                                                                         "Enter to confirm", "Require approval", "Allow Codex to work")):
                    raise BrowserJourneyError("startup_confirmation_before_submit", "A confirmation appeared before submitting the original opening.")
            await self.page.keyboard.press("Enter")
            self._startup_complete = True
            self.evidence.harness_readiness_verified = True
            self.startup_evidence.update(status="qualified", native_prompt_verified=True)
        except BrowserJourneyError as error:
            self.evidence.harness_readiness_verified = False
            self.startup_evidence.update(status="unverified", reason=error.code, native_prompt_verified=False)
            raise

    async def _wait_screen(self, predicate):
        try:
            while True:
                self._timeout_ms()
                screen = self._startup_screen()
                if predicate(screen):
                    return screen
                await asyncio.sleep(.05)
        except (asyncio.CancelledError, BrowserJourneyError):
            # Preserve the actual unrecognized control before owned cleanup
            # removes the terminal; this capture supplies no additional input.
            try:
                await self._capture_startup("unqualified-native-question-control", self._startup_screen())
            except Exception:
                pass
            raise

    @staticmethod
    def _screen_text(value):
        # The pinned CLI frames wrapped questions with a left box border.
        # Strip only that line decoration before matching the full authored text.
        value = re.sub(r"(?m)^\s*[│┃]\s?", "", value)
        return re.sub(r"\s+", " ", value).strip()

    @staticmethod
    def _selected_number(screen):
        choices = re.findall(r"^\s*[>❯›]\s*(\d+)\.\s", screen, re.MULTILINE)
        return int(choices[0]) if len(choices) == 1 else None

    async def submit_question_answers(self, interaction, answers):
        validate_question(interaction)
        if (not self._startup_complete or self.evidence.agent_id != "claude"
                or len(answers) != len(interaction["questions"])
                or any(not isinstance(answer, str) or not answer.strip() or len(answer) > 3000 for answer in answers)):
            raise BrowserJourneyError("native_question_ui_unqualified", "The native question or answers are unqualified.")
        terminal = self.page.locator(".terminal-container .xterm-helper-textarea")
        for index, (question, answer) in enumerate(zip(interaction["questions"], answers)):
            def question_visible(screen):
                normal = self._screen_text(screen)
                return (self._screen_text(question["question"]) in normal
                        and "Enter to select" in normal and self._selected_number(screen) is not None
                        and all(self._screen_text(option["label"]) in normal for option in question["options"]))
            screen = await self._wait_screen(question_visible)
            customs = re.findall(r"^\s*(\d+)\.\s*(?:\[[^\]]*\]\s*)?Type something\.?\s*$", screen, re.MULTILINE)
            if len(customs) != 1:
                raise BrowserJourneyError("native_question_custom_control_unverified", "The visible native question has no unique free-text choice.")
            target, selected = int(customs[0]), self._selected_number(screen)
            if not 1 <= target <= 5 or not 1 <= selected <= 5:
                raise BrowserJourneyError("native_question_custom_control_unverified", "The native choice positions are unqualified.")
            await self._capture_startup("native-question-" + str(index + 1), screen)
            await terminal.focus(timeout=self._timeout_ms())
            if self._startup_screen() != screen:
                raise BrowserJourneyError("native_question_screen_changed", "The native question changed before answer selection.")
            direction = "ArrowDown" if target >= selected else "ArrowUp"
            for _step in range(abs(target - selected)):
                selected += 1 if direction == "ArrowDown" else -1
                await self.page.keyboard.press(direction)
                await self._wait_screen(lambda s: question_visible(s) and self._selected_number(s) == selected)
            # Selecting the native Type something row focuses its inline editor.
            # Enter on an empty editor cancels the question; type directly.
            await self._wait_screen(lambda s: question_visible(s) and self._selected_number(s) == target)
            await self.page.keyboard.insert_text(answer)
            await self._wait_screen(lambda s: question_visible(s) and self._screen_text(answer) in self._screen_text(s))
            await self._capture_startup("native-custom-answer-" + str(index + 1), self._startup_screen())
            await self.page.keyboard.press("Enter")
            if question["multiSelect"]:
                # Current CLI commits the free-text field before navigating to
                # the explicit Submit entry; the review screen verifies values.
                await self.page.keyboard.press("Enter")
                await self.page.keyboard.press("ArrowDown")
                await self.page.keyboard.press("Enter")
        def review_visible(screen):
            normal = self._screen_text(screen)
            return ("Review your answers" in normal and "Ready to submit your answers?" in normal
                    and self._selected_number(screen) == 1 and "1. Submit answers" in normal
                    and all(self._screen_text(q["question"]) in normal for q in interaction["questions"])
                    and all(self._screen_text(answer) in normal for answer in answers))
        screen = await self._wait_screen(review_visible)
        await self._capture_startup("native-question-answer-review", screen)
        await terminal.focus(timeout=self._timeout_ms())
        if self._startup_screen() != screen or not review_visible(self._startup_screen()):
            raise BrowserJourneyError("native_question_screen_changed", "The reviewed native answers changed before submission.")
        await self.page.keyboard.press("Enter")
        self.startup_evidence.setdefault("question_answers", []).append({
            "tool_use_id": interaction["tool_use_id"], "question_count": len(answers),
            "review_verified": True, "ui_submitted": True})


def startup_artifact_dir(output):
    return Path(str(output) + ".startup-browser")


def timestamp(value):
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        require(stamp.tzinfo is not None, "timestamp_unverified")
        return stamp.timestamp()
    except (ValueError, AttributeError, TypeError) as exc:
        raise GateError("timestamp_unverified") from exc


def enum(value):
    return getattr(value, "value", value)


def canonical_uuid(value):
    try:
        return isinstance(value, str) and str(UUID(value)) == value
    except ValueError:
        return False


def canonical_path(value):
    require(isinstance(value, str) and value.startswith("/") and "\\" not in value
            and str(PurePosixPath(value)) == value and ".." not in PurePosixPath(value).parts,
            "workspace_path_unverified")
    # Workspace APIs can omit the /Workspace filesystem alias.
    return value.removeprefix("/Workspace") or "/"


def validate_receipt(receipt, *, now=None):
    now = now or datetime.now(timezone.utc)
    require(isinstance(receipt, dict) and type(receipt.get("schema_version")) is int
            and receipt["schema_version"] == 1 and receipt.get("scope") == "simulated_control_tower"
            and type(receipt.get("control_tower_requests")) is int and receipt["control_tower_requests"] == 0
            and receipt.get("accepted") is False and receipt.get("status") == "deployment_submitted",
            "standalone_deployment_receipt_required")
    plan = receipt.get("plan", {})
    require(isinstance(plan, dict) and plan.get("scope") == "simulated_control_tower"
            and plan.get("disposable") is True and plan.get("control_tower_requests") == 0,
            "isolated_plan_required")
    marker = plan.get("marker", "")
    require(isinstance(marker, str) and re.fullmatch(r"wt-eval-[a-z0-9](?:[a-z0-9-]{0,13}[a-z0-9])?", marker),
            "marker_unverified")
    names, environment = plan.get("names", {}), plan.get("environment", {})
    require(names.get("app_name") == marker + "-wt"
            and names.get("source_path") == f"/Workspace/Shared/{marker}/workshop-terminal",
            "marker_binding_unverified")
    workspace_id = environment.get("DATABRICKS_WORKSPACE_ID")
    require(isinstance(workspace_id, str) and re.fullmatch(r"[1-9][0-9]{0,31}", workspace_id),
            "expected_workspace_id_required")
    require(isinstance(plan.get("profile"), str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", plan["profile"]),
            "profile_unverified")
    host = validate_app_url(plan.get("workspace_host", ""))
    require(urlsplit(host).scheme == "https" and urlsplit(host).path == "", "workspace_host_unverified")
    attendee = plan.get("attendee", {})
    require(attendee.get("mode") in {"operator_bound", "synthetic_attendee"}
            and isinstance(attendee.get("email"), str)
            and re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", attendee["email"]), "attendee_binding_unverified")
    require(environment.get("WORKSHOP_ATTENDEE_EMAIL") == attendee["email"]
            and plan.get("run_id") == "sim-" + marker and plan.get("unit_id") == "sim-" + marker + "-unit-1",
            "simulated_attribution_unverified")
    for key, expected in {
        "WORKSHOP_RUN_ID": plan["run_id"], "WORKSHOP_UNIT_ID": plan["unit_id"],
        "WORKSHOP_EVALUATION_ENABLED": "true", "WORKSHOP_EVALUATION_MARKER": marker,
        "WORKSHOP_EVALUATION_ATTENDEE_EMAIL": attendee["email"],
        "WORKSHOP_EVALUATION_RUN_ID": plan["run_id"], "WORKSHOP_EVALUATION_UNIT_ID": plan["unit_id"],
        "ALLOW_SHARED_TOPOLOGY": "false", "MAX_SESSIONS_GLOBAL": "1", "MAX_SESSIONS_PER_USER": "1",
    }.items():
        require(environment.get(key) == expected, "evaluation_contract_unverified")
    bounds = plan.get("bounds", {})
    start, expiry = timestamp(bounds.get("created_at")), timestamp(bounds.get("expires_at"))
    require(type(bounds.get("attendee_count")) is int and bounds["attendee_count"] == 1 and start <= now.timestamp() < expiry
            and 0 < expiry - start <= 14400, "evaluation_expired_or_unbounded")
    resources = receipt.get("created_resources", [])
    require(isinstance(resources, list) and len(resources) == 4 and all(isinstance(item, dict) for item in resources),
            "exact_resource_receipt_required")
    indexed = {item.get("kind"): item for item in resources}
    require(set(indexed) == {"app", "group", "catalog", "workspace_source"}, "exact_resource_receipt_required")
    app = indexed["app"]
    require(app.get("name") == names["app_name"] and app.get("state") == "created"
            and isinstance(app.get("id"), str) and re.fullmatch(r"[a-zA-Z0-9-]{1,128}", app["id"])
            and type(app.get("service_principal_id")) is int and app["service_principal_id"] > 0
            and canonical_uuid(app.get("service_principal_client_id")), "app_identity_receipt_unverified")
    url = validate_app_url(app.get("url", ""))
    parsed = urlsplit(url)
    require(parsed.scheme == "https" and parsed.path == "" and parsed.hostname.endswith(".databricksapps.com")
            and ("-" + workspace_id + ".") in parsed.hostname, "app_url_workspace_binding_unverified")
    require(indexed["workspace_source"].get("path") == names["source_path"]
            and indexed["workspace_source"].get("state") == "created"
            and indexed["group"].get("name") == names.get("admin_group")
            and indexed["group"].get("state") == "created"
            and indexed["catalog"].get("name") == names.get("catalog")
            and indexed["catalog"].get("state") == "created_and_grants_verified", "resource_marker_binding_unverified")
    expected_bound = bind_created_app(plan, {"name": app["name"], "service_principal_id": app["service_principal_id"], "url": url})
    require(receipt.get("bound_plan") == expected_bound, "bound_plan_changed")
    deployment = receipt.get("deployment", {})
    require(isinstance(deployment.get("deployment_id"), str)
            and re.fullmatch(r"[a-zA-Z0-9-]{1,128}", deployment["deployment_id"])
            and canonical_path(deployment.get("source_code_path")) == canonical_path(names["source_path"]),
            "exact_deployment_receipt_required")
    manifest = receipt.get("uploaded_source", {})
    records = manifest.get("files", [])
    require(isinstance(records, list) and 1 <= len(records) <= 2000
            and manifest.get("digest_kind") == "sorted-runtime-file-manifest-sha256", "runtime_manifest_unverified")
    seen, total = set(), 0
    package = plan.get("ct_compatible_contract", {}).get("deployment_mode") == "package"
    if package:
        from evals.generated_apps.ct_deployment import PACKAGE_FILES
    for record in records:
        require(isinstance(record, dict) and set(record) == {"path", "size", "sha256"}
                and (record["path"] in PACKAGE_FILES if package else deployment_path_allowed(record["path"])) and record["path"] not in seen
                and type(record["size"]) is int and 0 <= record["size"] <= 20 * 1024 * 1024
                and isinstance(record["sha256"], str) and re.fullmatch(r"[a-f0-9]{64}", record["sha256"]),
                "runtime_manifest_unverified")
        seen.add(record["path"])
        total += record["size"]
    required = PACKAGE_FILES if package else {"app.yaml", "server/main.py", "server/evaluation.py", "static/index.html"}
    require(total <= 100 * 1024 * 1024 and required <= seen,
            "runtime_manifest_unverified")
    digest = hashlib.sha256(json.dumps(records, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    identity = receipt.get("source_identity", {})
    require(records == sorted(records, key=lambda item: item["path"]) and digest == manifest.get("digest")
            and identity.get("uploaded_runtime_digest") == digest
            and identity.get("planned_unpatched_digest") == plan.get("release", {}).get("source_digest")
            and identity.get("runtime_environment_patched") is True
            and identity.get("instrumented_release") is True, "runtime_source_identity_unverified")
    if package:
        contract = plan["ct_compatible_contract"]
        require(contract.get("version") == 2 and identity.get("package_bytes_unchanged") is True
                and identity.get("package_sha256") == contract["package_manifest"]["sha256"]
                and receipt.get("staged_package", {}).get("state") == "independently_verified"
                and receipt.get("attendee_app_access", {}).get("minimum_permission") == "CAN_MANAGE"
                and receipt.get("attendee_app_access", {}).get("state") == "independently_verified",
                "ct_package_contract_unverified")
    return {"marker": marker, "profile": plan["profile"], "workspace_host": host, "workspace_id": workspace_id,
            "app": app, "deployment_id": deployment["deployment_id"], "source_path": names["source_path"],
            "manifest": manifest, "attendee": attendee, "created_at": start, "expires_at": expiry,
            "cost_budget_usd": bounds.get("cost_budget_usd"), "environment": expected_bound["environment"],
            "catalog_owner": indexed["catalog"].get("owner") if package else attendee["email"],
            "package": plan.get("ct_compatible_contract") if package else None}


def verify_snapshot(client, root, manifest, *, expected_environment=None):
    expected = {item["path"]: item for item in manifest["files"]}
    files = set()
    prefix = canonical_path(root) + "/"
    for index, item in enumerate(client.workspace.list(root, recursive=True)):
        require(index < 6000, "workspace_listing_budget")
        path = canonical_path(item.path)
        require(path.startswith(prefix), "snapshot_path_escape")
        if enum(item.object_type) == "DIRECTORY":
            continue
        require(enum(item.object_type) == "FILE", "snapshot_object_type_unverified")
        relative = path[len(prefix):]
        require(relative not in files, "snapshot_duplicate_file")
        files.add(relative)
    require(files == set(expected), "snapshot_file_set_changed")
    def read_record(record):
        with client.workspace.download(root + "/" + record["path"]) as stream:
            content = stream.read(record["size"] + 1)
        require(len(content) == record["size"] and hashlib.sha256(content).hexdigest() == record["sha256"],
                "snapshot_file_digest_changed")
        return content if record["path"] == "app.yaml" else None
    with ThreadPoolExecutor(max_workers=8) as workers:
        contents = list(workers.map(read_record, expected.values()))
    if expected_environment is not None:
        import yaml
        app_yaml = yaml.safe_load(next(content for content in contents if content is not None))
        entries = app_yaml.get("env", [])
        require(isinstance(entries, list) and all(isinstance(item, dict) for item in entries), "runtime_environment_unverified")
        environment = {item.get("name"): item.get("value") for item in entries}
        require(len(environment) == len(entries), "runtime_environment_unverified")
        require(all(environment.get(key) == value for key, value in expected_environment.items()), "runtime_marker_or_contract_mismatch")
        require(all(environment.get(key) == "" for key in (
            "CONTROL_TOWER_URL", "CONTROL_TOWER_INGEST_URL", "CONTROL_TOWER_INGEST_TOKEN", "WORKSHOP_PAT")),
            "runtime_external_delivery_enabled")
    return {"snapshot_path": root, "manifest_digest": manifest["digest"], "file_count": len(files),
            "bytes_verified": sum(item["size"] for item in expected.values()), "all_file_hashes_verified": True}


def verify_deployment(binding, client, *, now=None):
    now = now or datetime.now(timezone.utc)
    require(client.config.host.rstrip("/") == binding["workspace_host"], "workspace_host_mismatch")
    # Do not accept a Config workspace_id cache as a live identity observation.
    header = client.api_client.do("GET", "/api/2.0/preview/scim/v2/Me", response_headers=["X-Databricks-Org-Id"])
    require(str(header.get("X-Databricks-Org-Id")) == binding["workspace_id"], "workspace_id_mismatch")
    me = client.current_user.me()
    require(isinstance(me.user_name, str), "operator_identity_unverified")
    if binding["attendee"]["mode"] == "operator_bound":
        require(me.user_name.casefold() == binding["attendee"]["email"].casefold(), "operator_attendee_identity_mismatch")
    expected = binding["app"]
    app = client.apps.get(expected["name"])
    require(app.id == expected["id"] and app.name == expected["name"] and app.url.rstrip("/") == expected["url"]
            and app.service_principal_id == expected["service_principal_id"]
            and app.service_principal_client_id == expected["service_principal_client_id"], "live_app_identity_mismatch")
    require(binding["created_at"] <= timestamp(app.create_time) <= now.timestamp() + 5, "terminal_app_freshness_unverified")
    require(app.active_deployment is not None and app.active_deployment.deployment_id == binding["deployment_id"],
            "active_deployment_mismatch")
    require(enum(getattr(app.app_status, "state", None)) == "RUNNING"
            and enum(getattr(app.compute_status, "state", None)) == "ACTIVE", "terminal_not_running")
    deployment = client.apps.get_deployment(expected["name"], binding["deployment_id"])
    require(deployment.deployment_id == binding["deployment_id"]
            and enum(getattr(deployment.status, "state", None)) == "SUCCEEDED"
            and canonical_path(deployment.source_code_path) == canonical_path(binding["source_path"])
            and binding["created_at"] <= timestamp(deployment.create_time) <= now.timestamp() + 5,
            "live_deployment_freshness_unverified")
    snapshot = getattr(deployment.deployment_artifacts, "source_code_path", None)
    require(isinstance(snapshot, str) and canonical_path(snapshot) == f"/Users/{expected['id']}/src/{binding['deployment_id']}",
        "owned_deployment_snapshot_unverified")
    source = verify_snapshot(client, snapshot, binding["manifest"], expected_environment=binding["environment"])
    current = client.apps.get(expected["name"])
    require(current.id == expected["id"] and current.active_deployment is not None
            and current.active_deployment.deployment_id == binding["deployment_id"], "deployment_changed_during_verification")
    return {"verified": True, "workspace_id": binding["workspace_id"], "workspace_host": binding["workspace_host"],
            "app_name": expected["name"], "app_id": expected["id"], "app_url": expected["url"],
            "service_principal_id": expected["service_principal_id"], "operator_email": me.user_name,
            "attendee_mode": binding["attendee"]["mode"], "deployment_id": binding["deployment_id"],
            "deployment_state": "SUCCEEDED", "runtime_snapshot": source,
            "instrumented_release": True, "control_tower_requests": 0,
            "obo_consent_verified": False, "event_attendee_equivalence_verified": False}


class OperatorFetcher:
    """Separate operator HTTP client; no bearer is ever placed in the browser."""
    def __init__(self, client, base_url, *, http_client=None):
        self.client, self.base_url = client, validate_app_url(base_url)
        self.http = http_client
        self.requests = []
        self.collection_failure = None

    async def __aenter__(self):
        if self.http is None:
            import httpx
            self.http = httpx.AsyncClient(timeout=15, follow_redirects=False, trust_env=False)
        return self

    async def __aexit__(self, *_exc):
        await self.http.aclose()

    async def project_precondition(self):
        """Read only the designated synthetic home; never clear attendee work."""
        headers = await asyncio.to_thread(self.client.config.authenticate)
        async with self.http.stream("GET", self.base_url + "/api/admin/evaluation/native-format",
                                    headers=headers, follow_redirects=False) as response:
            require(response.status_code == 200, "fresh_project_precondition_unverified")
            raw = bytearray()
            async for chunk in response.aiter_bytes():
                raw.extend(chunk)
                require(len(raw) <= 300 * 1024, "fresh_project_precondition_unverified")
            payload = json.loads(raw)
        state = payload.get("projects_state", {})
        require(payload.get("status") == "metadata_observed" and set(state) == {"directory_exists", "entry_count", "empty"}
                and type(state["directory_exists"]) is bool and type(state["entry_count"]) is int
                and state["entry_count"] >= 0 and state["empty"] is (state["entry_count"] == 0),
                "fresh_project_precondition_unverified")
        return {"observed_at": datetime.now(timezone.utc).isoformat(), "source": "restricted_synthetic_home_metadata",
                **state}

    async def __call__(self, path):
        try:
            return await self._collect(path)
        except GateError:
            raise
        except Exception as exc:
            self.collection_failure = {"reason": "operator_observation_collection_unverified", "error_type": type(exc).__name__}
            raise GateError("operator_observation_collection_unverified") from None

    async def _collect(self, path):
        parsed = urlsplit(path)
        require(not parsed.scheme and not parsed.netloc and not parsed.fragment
                and re.fullmatch(r"/api/admin/evaluation/sessions/[a-f0-9-]{36}/messages", parsed.path), "operator_endpoint_rejected")
        sid = parsed.path.split("/")[-2]
        try:
            require(str(UUID(sid)) == sid, "operator_endpoint_rejected")
        except ValueError as exc:
            raise GateError("operator_endpoint_rejected") from exc
        query = parse_qs(parsed.query, keep_blank_values=True)
        require(set(query) == {"cursor", "limit"} and query["limit"] == ["30"] and len(query["cursor"]) == 1
                and re.fullmatch(r"[A-Za-z0-9_-]{0,1024}", query["cursor"][0]), "operator_endpoint_rejected")
        headers = await asyncio.to_thread(self.client.config.authenticate)
        async with self.http.stream("GET", self.base_url + path, headers=headers, follow_redirects=False) as response:
            self.requests.append({"session_id": sid, "http_status": response.status_code})
            require(len(self.requests) <= 2000 and response.status_code == 200, "operator_observation_http_unverified")
            raw = bytearray()
            async for chunk in response.aiter_bytes():
                raw.extend(chunk)
                require(len(raw) <= 300 * 1024, "operator_observation_response_budget")
            try:
                payload = json.loads(raw)
            except (ValueError, UnicodeDecodeError) as exc:
                raise GateError("operator_observation_json_unverified") from exc
            require(isinstance(payload, dict), "operator_observation_json_unverified")
            return payload


class AppDiscovery:
    """Inventory differences; observed deployments never imply app acceptance."""
    def __init__(self, client, binding):
        self.client, self.binding = client, binding
        self.baseline = set()
        self.started = 0.0
        self.evidence = {"status": "not_started", "candidates": [], "unattributed_new_attendee_apps": 0}

    def inventory(self):
        apps = []
        for index, app in enumerate(self.client.apps.list(page_size=100)):
            require(index < 2000 and isinstance(app.id, str) and app.id, "app_inventory_unverified")
            apps.append(app)
        require(len({app.id for app in apps}) == len(apps), "app_inventory_ambiguous")
        return apps

    def start(self):
        self.baseline = {app.id for app in self.inventory()}
        self.started = datetime.now(timezone.utc).timestamp()
        self.evidence.update(status="observing", baseline_count=len(self.baseline))

    def observe(self):
        binding = self.binding
        creators = {binding["attendee"]["email"].casefold(), binding["app"]["service_principal_client_id"].casefold()}
        candidates, unattributed = [], 0
        for listed in self.inventory():
            if listed.id in self.baseline or listed.id == binding["app"]["id"]:
                continue
            if not isinstance(listed.creator, str) or listed.creator.casefold() not in creators:
                continue
            app = self.client.apps.get(listed.name)
            require(app.id == listed.id and app.creator == listed.creator, "app_inventory_identity_changed")
            if timestamp(app.create_time) < self.started:
                continue
            require(timestamp(app.create_time) <= datetime.now(timezone.utc).timestamp() + 5, "new_app_freshness_unverified")
            deployment = app.active_deployment
            source = getattr(deployment, "source_code_path", "") or ""
            marker_bound = (app.name.startswith(binding["marker"] + "-")
                            or binding["marker"] in PurePosixPath(source).parts)
            unique_sp_bound = app.creator.casefold() == binding["app"]["service_principal_client_id"].casefold()
            if not marker_bound and not unique_sp_bound:
                unattributed += 1
                continue
            candidates.append({"app_id": app.id, "app_name": app.name, "creator": app.creator,
                               "namespace_marker_observed": marker_bound, "unique_terminal_sp_creator": unique_sp_bound,
                               "source_code_path": source,
                               "deployment_id": getattr(deployment, "deployment_id", None),
                               "deployment_state": enum(getattr(getattr(deployment, "status", None), "state", None)),
                               "app_state": enum(getattr(app.app_status, "state", None)), "url": app.url})
        self.evidence.update(candidates=candidates, unattributed_new_attendee_apps=unattributed)
        if len(candidates) > 1 or unattributed:
            self.evidence["status"] = "attribution_needs_review"
            return AppObservation("needs_review", source="live workspace app inventory difference")
        if len(candidates) == 1:
            candidate = candidates[0]
            if candidate["deployment_state"] == "SUCCEEDED" and candidate["app_state"] == "RUNNING" and candidate["url"]:
                deployed = self.client.apps.get_deployment(candidate["app_name"], candidate["deployment_id"])
                require(deployed.deployment_id == candidate["deployment_id"]
                        and enum(getattr(deployed.status, "state", None)) == "SUCCEEDED"
                        and self.started <= timestamp(deployed.create_time) <= datetime.now(timezone.utc).timestamp() + 5
                        and canonical_path(deployed.source_code_path) == canonical_path(candidate["source_code_path"]),
                        "new_app_deployment_unverified")
                parsed = urlsplit(validate_app_url(candidate["url"]))
                require(parsed.scheme == "https" and parsed.hostname.endswith(".databricksapps.com")
                        and ("-" + binding["workspace_id"] + ".") in parsed.hostname, "new_app_url_workspace_mismatch")
                current = self.client.apps.get(candidate["app_name"])
                require(current.id == candidate["app_id"] and current.creator == candidate["creator"]
                        and current.url == candidate["url"] and current.active_deployment is not None
                        and current.active_deployment.deployment_id == candidate["deployment_id"]
                        and enum(getattr(current.app_status, "state", None)) == "RUNNING",
                        "new_app_changed_during_observation")
                self.evidence["status"] = "deployment_observed"
                return AppObservation("deployment_observed", candidate["url"],
                                      ("workspace-app:" + candidate["app_id"],), "live workspace app inventory difference")
        return AppObservation(source="live workspace app inventory difference")

    async def __call__(self, _snapshot):
        try:
            return await asyncio.to_thread(self.observe)
        except GateError:
            raise
        except Exception as exc:
            self.evidence.update(status="collection_unverified", collection_error_type=type(exc).__name__)
            raise GateError("app_discovery_collection_unverified") from None


def assess_journey_outcome(result):
    """Separate an observed operational stop from unavailable builder evidence."""
    opening_correlated = any(delivery.get("decision") == "opening" and delivery.get("native_correlated") is True
                             for delivery in result.deliveries)
    text_available = result.first_complete_assistant is not None and opening_correlated
    timed_out = result.stop_reason in {"no_material_consultation_within_budget", "consultation_scope_not_agreed",
                                     "total_deadline", "operation_timeout"}
    unknown_coverage_timeout = result.status == "failed" and timed_out and not text_available
    operation_timeout = result.stop_reason == "operation_timeout"
    cleanup_failed = result.cleanup.get("status") == "failed"
    entry_unverified = not opening_correlated and (result.entry.get("opening_submitted") is False
                                                   or result.stop_reason == "browser_entry_timeout")
    verdict = "failed" if cleanup_failed or result.status == "failed" and not (entry_unverified or unknown_coverage_timeout or operation_timeout) else "unverified"
    return {"verdict": verdict, "complete_assistant_text_available": text_available,
            "consultation": {"verdict": (result.consultation_status if result.consultation_status in
                                           {"passed", "failed", "unverified"} else "unverified"),
                             "reason": result.consultation_stop_reason or "consultation_outcome_unverified",
                             "closed_elapsed_seconds": result.consultation_closed_elapsed_seconds,
                             "deadline_enforced": result.consultation_deadline_enforced,
                             "scope_agreement_delivered": result.scope_agreement_delivered,
                             "no_question_before_scope_agreement_observed": result.no_question_before_scope_agreement_observed},
            "native_format_qualification_verified": False,
            "quality_failure_inferred_from_silence": False,
            "reason": "browser_entry_unverified" if entry_unverified else "operation_timeout_unverified" if operation_timeout else
                      "native_assistant_coverage_unverified" if unknown_coverage_timeout else "full_app_acceptance_unverified",
            "operational_status": result.status, "operational_stop_reason": result.stop_reason}


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--receipt", required=True)
    p.add_argument("--budget", required=True)
    p.add_argument("--wt-browser-state", help="Private genuine attendee Playwright auth state; never included in evidence")
    p.add_argument("--qualification", help="Independent CT-compatible package/model qualification result")
    p.add_argument("--agent", choices=["claude", "codex"], required=True)
    p.add_argument("--mode", choices=["consultation_probe", "build"], default="consultation_probe")
    p.add_argument("--entry-path", choices=["wizard", "skip_wizard", "wizard_disabled"], default="skip_wizard")
    p.add_argument("--allow-industry-step", action="store_true")
    p.add_argument("--qualify-startup", action="store_true", help="Controlled skip-wizard arm: record owned-folder trust and await native CLI prompt before opening")
    p.add_argument("--industry")
    p.add_argument("--show-browser", action="store_true")
    p.add_argument("--execute", action="store_true")
    p.add_argument("--output", required=True)
    return p


def workspace_client(profile):
    from databricks.sdk import WorkspaceClient
    from databricks.sdk.core import Config
    return WorkspaceClient(config=Config(profile=profile, http_timeout_seconds=10, retry_timeout_seconds=15))


def wt_artifact_dir(output):
    destination = Path(output)
    return destination.with_name(destination.name + ".wt-browser")


async def capture_wt_page(page, result, binding, output):
    """Capture only a previously identified attendee page at the exact WT origin."""
    if result.entry.get("identity", "").casefold() != binding["attendee"]["email"].casefold():
        return {"status": "skipped", "reason": "browser_identity_unverified"}
    actual, expected = urlsplit(getattr(page, "url", "")), urlsplit(binding["app"]["url"])
    if (actual.scheme, actual.netloc) != (expected.scheme, expected.netloc):
        return {"status": "skipped", "reason": "terminal_origin_unverified"}
    directory = wt_artifact_dir(output)
    capture = {"status": "capturing", "source": "identified_attendee_terminal_browser", "artifacts": {}}
    try:
        directory.mkdir(mode=0o700, parents=False, exist_ok=False)
        screenshot, aria = directory / "final-wt.png", directory / "final-wt.aria.txt"
        await asyncio.wait_for(page.screenshot(path=str(screenshot), full_page=True, timeout=10000), timeout=10)
        screenshot.chmod(0o600)
        capture["artifacts"]["screenshot"] = str(screenshot.resolve())
        snapshot = await asyncio.wait_for(page.locator("body").aria_snapshot(timeout=10000), timeout=10)
        require(isinstance(snapshot, str) and len(snapshot) <= 256 * 1024, "terminal_capture_text_budget")
        aria.write_text(redact_evidence(snapshot), encoding="utf-8")
        aria.chmod(0o600)
        capture["artifacts"]["accessibility_snapshot"] = str(aria.resolve())
        capture["status"] = "captured"
    except Exception as error:
        capture.update(status="capture_failed", reason="terminal_browser_capture_unverified", error_type=type(error).__name__)
    return capture


async def execute(args, binding, budget, report, *, client=None):
    if client is None:
        # Config can perform credential/host discovery; keep it off the event loop.
        client = await asyncio.wait_for(asyncio.to_thread(workspace_client, binding["profile"]), timeout=30)
    report["deployment_verification"] = await asyncio.wait_for(asyncio.to_thread(verify_deployment, binding, client), timeout=180)
    discovery = AppDiscovery(client, binding) if args.mode == "build" else None
    if discovery:
        await asyncio.wait_for(asyncio.to_thread(discovery.start), timeout=30)
    scenario = load_simulator_scenario()
    require(binding["expires_at"] > datetime.now(timezone.utc).timestamp() + 10,
            "evaluation_expired_before_browser_entry")
    async with OperatorFetcher(client, binding["app"]["url"]) as fetch:
        if args.mode == "build":
            report["project_precondition"] = await asyncio.wait_for(fetch.project_precondition(), timeout=30)
            require(report["project_precondition"]["empty"] is True, "preexisting_project_requires_separate_warm_start_cell")
        async with bounded_browser(args, binding) as browser:
            # Setup time cannot extend the original disposable-resource bounds.
            remaining = binding["expires_at"] - datetime.now(timezone.utc).timestamp() - 10
            require(remaining > 0, "evaluation_expired_before_browser_entry")
            total = min(budget["total_seconds"], remaining)
            consultation = min(budget["consultation_seconds"], total)
            report["effective_budget"] = {**budget, "total_seconds": total, "consultation_seconds": consultation,
                                          "token_and_spend_enforcement_verified": False}
            simulator = NoviceSimulator(scenario, SimulatorBudget(total_seconds=total, consultation_seconds=consultation))
            opening = simulator.opening().text
            driver = (StartupQualifiedDriver(browser.page, binding=binding, output=args.output)
                      if getattr(args, "qualify_startup", False) else WorkshopBrowserDriver(browser.page))
            result = await run_journey(driver, BrowserLaunchConfig(
                wt_url=binding["app"]["url"], agent_id=args.agent, opening_message=opening,
                expected_attendee_email=binding["attendee"]["email"], entry_path=args.entry_path,
                industry=args.industry, allow_industry_step=args.allow_industry_step,
                deadline_seconds=min(180, total), run_deadline_seconds=total), simulator, fetch,
                JourneyConfig(mode=args.mode, total_seconds=total, consultation_seconds=consultation,
                              operation_timeout_seconds=30, poll_interval_seconds=2,
                              harness_version=binding["environment"][{
                                  "claude": "CLAUDE_CODE_VERSION", "codex": "CODEX_CLI_VERSION"}[args.agent]]), app_observer=discovery)
            report["journey"] = result.to_dict()
            if getattr(args, "qualify_startup", False):
                report["startup_qualification"] = driver.startup_evidence
            report["terminal_browser"] = await capture_wt_page(browser.page, result, binding, args.output)
        report["operator_observation"] = {"separate_from_attendee_browser": True, "requests": fetch.requests,
                                          "collection_failure": getattr(fetch, "collection_failure", None)}
    if discovery:
        report["app_discovery"] = discovery.evidence
    report["assessment"] = assess_journey_outcome(result)
    report.update(status="journey_finished", verdict=report["assessment"]["verdict"], accepted=False)


@asynccontextmanager
async def bounded_browser(args, binding):
    remaining = binding["expires_at"] - datetime.now(timezone.utc).timestamp() - 10
    require(remaining > 0, "evaluation_expired_before_browser_entry")
    browser = AuthenticatedBrowser(args.wt_browser_state, headless=not args.show_browser)
    try:
        await asyncio.wait_for(browser.__aenter__(), timeout=min(30, remaining))
        yield browser
    finally:
        # Also clean up a partially started browser after its startup timeout.
        await asyncio.wait_for(browser.__aexit__(None, None, None), timeout=10)


def main(argv=None):
    args = parser().parse_args(argv)
    destination = Path(args.output)
    inputs = [Path(args.receipt), Path(args.budget)] + ([Path(args.wt_browser_state)] if args.wt_browser_state else [])
    if args.qualification:
        inputs.append(Path(args.qualification))
    temporary = destination.with_name(destination.name + ".tmp")
    artifacts = wt_artifact_dir(destination)
    startup_artifacts = startup_artifact_dir(destination)
    if (destination.exists() or destination.is_symlink() or temporary.exists() or temporary.is_symlink()
            or artifacts.exists() or artifacts.is_symlink()
            or startup_artifacts.exists() or startup_artifacts.is_symlink()
            or destination.resolve() in {path.resolve() for path in inputs}
            or temporary.resolve() in {path.resolve() for path in inputs}
            or artifacts.resolve() in {path.resolve() for path in inputs}):
        print(json.dumps({"verdict": "unverified", "accepted": False, "reason": "fresh_distinct_output_required"}))
        return 2
    evaluator_files = ("scripts/run_generated_app_journey.py", "evals/generated_apps/journey.py",
        "evals/generated_apps/simulator.py", "evals/generated_apps/interactions.py", "evals/generated_apps/report.py",
        "evals/generated_apps/adapters/browser.py", "evals/generated_apps/adapters/harness.py",
        "evals/generated_apps/adapters/simulated_control_tower.py")
    evaluator = [{"path": name, "sha256": hashlib.sha256((ROOT / name).read_bytes()).hexdigest()}
                 for name in evaluator_files]
    report = {"schema_version": 1, "scope": "simulated_control_tower", "control_tower_requests": 0,
              "evaluator_source": evaluator,
              "accepted": False, "verdict": "unverified", "status": "planning", "mode": args.mode,
              "entry_path": args.entry_path, "agent_id": args.agent,
              "startup_qualification_requested": args.qualify_startup,
              "unverified_checks": ["event_attendee_equivalence", "obo_consent", "token_and_spend_enforcement",
                                    "tool_and_worker_attribution", "implementation_timing", "native_transcript_authenticity",
                                    "critical_user_task", "backend_persistence", "app_restart_persistence", "first_and_final_ux"],
              "limitations": ["A consultation probe or deployed-app observation does not establish full app acceptance."]}
    try:
        receipt = json.loads(Path(args.receipt).read_text())
        require(not args.qualify_startup or args.entry_path == "skip_wizard", "startup_qualification_requires_skip_wizard")
        binding = validate_receipt(receipt)
        if binding.get("package"):
            require(args.qualification, "ct_package_model_qualification_required")
            qualification = json.loads(Path(args.qualification).read_text())
            require(qualification.get("deployment_receipt_sha256") == hashlib.sha256(Path(args.receipt).read_bytes()).hexdigest()
                    and qualification.get("novice_journey_eligible") is True and qualification.get("status") == "qualified"
                    and qualification.get("observer_route_verified") is True
                    and len(qualification.get("calls", [])) == 3
                    and {call.get("role") for call in qualification["calls"]} == {"driver", "codex", "wizard"}
                    and all(call.get("result", {}).get("invocation_verified") is True
                            and call["result"].get("application_id") == binding["app"]["service_principal_client_id"]
                            for call in qualification["calls"]), "ct_package_model_qualification_unverified")
            report["model_qualification"] = {"receipt_sha256": qualification["deployment_receipt_sha256"],
                "all_roles_invoked": True, "application_id": binding["app"]["service_principal_client_id"]}
        budget = validate_budgets(json.loads(Path(args.budget).read_text()))
        require(isinstance(binding["cost_budget_usd"], (int, float))
                and budget["spend_ceiling_usd"] <= binding["cost_budget_usd"], "budget_exceeds_receipt")
        report["target"] = {key: binding[key] for key in ("marker", "workspace_host", "workspace_id", "deployment_id", "source_path", "attendee")}
        report["target"].update(app_id=binding["app"]["id"], app_name=binding["app"]["name"], app_url=binding["app"]["url"])
        report["declared_budget"] = budget
        report["status"] = "planned"
        if args.execute:
            require(args.wt_browser_state and Path(args.wt_browser_state).is_file(), "genuine_attendee_browser_state_required")
        # Prove evidence destination before even read-only SDK/auth activity.
        write_evidence(destination, report)
        if args.execute:
            asyncio.run(execute(args, binding, budget, report))
    except Exception as exc:
        report.update(status="blocked", verdict="unverified", error_type=type(exc).__name__)
        if isinstance(exc, GateError):
            report["reason"] = str(exc)
        write_evidence(destination, report)
        print(json.dumps({"output": args.output, "verdict": "unverified", "accepted": False,
                          "error_type": type(exc).__name__, "control_tower_requests": 0}))
        return 2
    write_evidence(destination, report)
    print(json.dumps({"output": args.output, "status": report["status"], "verdict": report["verdict"],
                      "accepted": False, "control_tower_requests": 0}))
    return 0 if not args.execute else 1


if __name__ == "__main__":
    raise SystemExit(main())
