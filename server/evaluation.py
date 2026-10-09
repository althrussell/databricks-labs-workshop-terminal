"""Restricted instrumentation for explicitly designated synthetic CT evaluations.

Default OFF and separate from commercial insights/diagnostics. It never writes
instructions, changes CLI launch arguments, captures PTY prose, or accepts a file
path. A native transcript must bind unambiguously to the sole active WT session
by native session ID, pinned version, initial cwd and start timestamp. Ambiguous
or unsupported runtime formats return no messages and an unverified result.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import os
import re
import stat
import threading
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse
import requests

from . import config
from .auth import require_admin
from .diagnostics import redact
from .sessions import session_manager
from .users import email_slug

router = APIRouter(prefix="/api/admin/evaluation", dependencies=[Depends(require_admin)])

SUPPORTED_PINS = {"claude": "2.1.237", "codex": "0.148.0"}
MAX_FILES = 32
MAX_DIRECTORY_ENTRIES = 256
MAX_FILE_BYTES = 4 * 1024 * 1024
MAX_SCAN_BYTES = 8 * 1024 * 1024
# Current Claude may put a complete skill-read result into one JSONL record.
# Keep parsing bounded independently of the much smaller exported prose limit.
MAX_LINE_BYTES = 1024 * 1024
MAX_MESSAGE_CHARS = 12_000
MAX_RESPONSE_CHARS = 64 * 1024
_CURSOR_KEY = os.urandom(32)
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_DIRECTORY = getattr(os, "O_DIRECTORY", 0)
CANARY_TIMEOUT_SECONDS = 30
CANARY_MAX_RESPONSE_BYTES = 64 * 1024
_CANARY_LOCK = threading.Lock()
_CANARY_ROLES = frozenset({"driver", "codex", "wizard"})


class CanaryUnverified(RuntimeError):
    """Only fixed classification codes may leave the invocation worker."""


def _canary_remaining(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise CanaryUnverified("timeout")
    return remaining


def _canary_url(url: str) -> bool:
    try:
        value = urlsplit(url)
        return bool(value.scheme == "https" and value.hostname and value.port in {None, 443}
                    and not value.username and not value.password and not value.query and not value.fragment
                    and value.hostname.endswith((".cloud.databricks.com", ".azuredatabricks.net", ".gcp.databricks.com")))
    except ValueError:
        return False


def _canary_gateway_url(url: str, host: str, role: str) -> bool:
    if not _canary_url(url):
        return False
    target, workspace = urlsplit(url), urlsplit(host)
    suffix = {"driver": "/anthropic/v1/messages", "codex": "/codex/v1/responses",
              "wizard": "/mlflow/v1/chat/completions"}[role]
    if target.hostname == workspace.hostname:
        return target.path == "/ai-gateway" + suffix
    workspace_id = config.workspace_id()
    if not re.fullmatch(r"[0-9]+", workspace_id):
        return False
    cloud = ".0.ai-gateway.azuredatabricks.net" if workspace.hostname.endswith(".azuredatabricks.net") else ".ai-gateway.cloud.databricks.com"
    return target.hostname == workspace_id + cloud and target.path == suffix


def _canary_response_model(body: object) -> str | None:
    model = body.get("model") if isinstance(body, dict) else None
    if (isinstance(model, str) and len(model) <= 128
            and re.fullmatch(r"(?:system\.ai\.)?(?:claude|gpt|gemini|llama|qwen|deepseek|glm|kimi)[A-Za-z0-9_.-]*(?:\[[A-Za-z0-9_-]+\])?", model)):
        return model
    return None


def _canary_http(method: str, url: str, token: str, deadline: float, *, payload=None, observation=None, status_field="http_status"):
    remaining = _canary_remaining(deadline)
    headers = {"Authorization": f"Bearer {token}"}
    if "/ai-gateway/" in url:
        try:
            from . import model_policy
            headers["Databricks-Ai-Gateway-Request-Tags"] = model_policy.request_tags("evaluation")
        except ImportError:
            pass
    if "/anthropic/" in url:
        headers["anthropic-version"] = "2023-06-01"
    # Redirects must never carry this app's bearer elsewhere. The outer deadline
    # also bounds inactivity-based requests timeouts and SDK token acquisition.
    with requests.request(method, url, headers=headers, json=payload, allow_redirects=False,
                          stream=True, timeout=(min(5, remaining), min(15, remaining))) as response:
        status = response.status_code
        if observation is not None:
            observation[status_field] = status
        raw = bytearray()
        for chunk in response.iter_content(chunk_size=4096):
            _canary_remaining(deadline)
            raw.extend(chunk)
            if len(raw) > CANARY_MAX_RESPONSE_BYTES:
                raise CanaryUnverified("response_size_budget")
        try:
            body = json.loads(raw)
        except (ValueError, UnicodeDecodeError) as exc:
            if status != 200:
                return status, None
            raise CanaryUnverified("invalid_json_response") from exc
        if status != 200:
            if observation is not None and isinstance(body, dict):
                # Fixed-size sanitized provider evidence; never export headers,
                # credentials or a complete response object.
                error = body.get("error", body)
                if isinstance(error, dict):
                    observation["provider_error"] = {key: redact(str(error[key]))[:800]
                        for key in ("error_code", "code", "type", "message") if key in error}
            return status, None
        return status, body if isinstance(body, dict) else None


def _canary_usage(body: dict, role: str) -> dict:
    usage = body.get("usage")
    fields = ("prompt_tokens", "completion_tokens") if role == "wizard" else ("input_tokens", "output_tokens")
    if not isinstance(usage, dict) or any(type(usage.get(field)) is not int or not 0 < usage[field] < 10**9 for field in fields):
        raise CanaryUnverified("invocation_usage_unverified")
    return {field: usage[field] for field in fields}


def _canary_response(body: dict | None, role: str, model: str) -> dict:
    from . import models
    if not isinstance(body, dict) or ("error" in body and body["error"] is not None):
        raise CanaryUnverified("invalid_provider_response")
    # Provider IDs can differ from the UC routing name. These exact aliases
    # were observed on the CT-pinned release; unrelated models still fail.
    aliases = {"gpt-5-6-terra": "gpt-5.6-terra", "gpt-oss-120b": "gpt-oss-120b-080525"}
    expected = models.short_name(model)
    if body.get("model") is not None and (not isinstance(body["model"], str)
            or models.short_name(body["model"]) not in {expected, aliases.get(expected, expected)}):
        raise CanaryUnverified("response_model_mismatch")
    usage = _canary_usage(body, role)
    text = ""
    kinds = set()
    if role == "driver":
        blocks = body.get("content")
        if (body.get("type") != "message" or body.get("role") != "assistant" or not isinstance(blocks, list)
                or not blocks or body.get("stop_reason") not in {"end_turn", "max_tokens", "stop_sequence"}
                or any(not isinstance(block, dict) or block.get("type") not in {"text", "thinking", "redacted_thinking"} for block in blocks)):
            raise CanaryUnverified("invalid_provider_response")
        kinds = {block["type"] for block in blocks}
        text = "".join(block["text"] for block in blocks if block.get("type") == "text" and isinstance(block.get("text"), str))
    elif role == "codex":
        blocks = body.get("output")
        if ((body.get("object") or body.get("type")) != "response" or body.get("status") not in {"completed", "incomplete"}
                or not isinstance(blocks, list) or not blocks
                or any(not isinstance(block, dict) or block.get("type") not in {"message", "reasoning"} for block in blocks)):
            raise CanaryUnverified("invalid_provider_response")
        kinds = {block["type"] for block in blocks}
        for block in blocks:
            if block["type"] == "message":
                if block.get("role") != "assistant" or not isinstance(block.get("content"), list):
                    raise CanaryUnverified("invalid_provider_response")
                text += "".join(item["text"] for item in block["content"] if isinstance(item, dict)
                                and item.get("type") == "output_text" and isinstance(item.get("text"), str))
    else:
        choices = body.get("choices")
        if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict):
            raise CanaryUnverified("invalid_provider_response")
        choice, message = choices[0], choices[0].get("message")
        if not isinstance(message, dict) or message.get("role") != "assistant" or choice.get("finish_reason") not in {"stop", "length"}:
            raise CanaryUnverified("invalid_provider_response")
        content = message.get("content")
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            text = "".join(block["text"] for block in content if isinstance(block, dict) and isinstance(block.get("text"), str))
        else:
            raise CanaryUnverified("invalid_provider_response")
        kinds = {"assistant_text"}
    return {"usage": usage, "output_kinds": sorted(kinds), "ok_answer_verified": text.strip() == "OK"}


def _canary_run(role: str, binding: dict, result: dict, deadline: float) -> dict:
    from . import cli_config, credentials, models, wizard_llm
    if not _CANARY_LOCK.acquire(blocking=False):
        return dict(result, error_classification="canary_in_progress")
    try:
        host = config.databricks_host()
        expected_app = os.environ.get("DATABRICKS_CLIENT_ID", "").strip()
        expected_sp = os.environ.get("WORKSHOP_APP_SP_ID", "").strip()
        try:
            if str(UUID(expected_app)) != expected_app or not re.fullmatch(r"[0-9]+", expected_sp):
                raise ValueError()
        except ValueError as exc:
            raise CanaryUnverified("app_identity_configuration_invalid") from exc
        client = credentials.workspace_client()
        if (config.local_dev() or client is None or client.config.auth_type != "oauth-m2m"
                or client.config.client_id != expected_app or client.config.host.rstrip("/") != host or not _canary_url(host)):
            raise CanaryUnverified("app_oauth_identity_unavailable")
        token = credentials.app_identity_bearer()
        _canary_remaining(deadline)
        if not token:
            raise CanaryUnverified("app_oauth_identity_unavailable")
        result.update(application_id=expected_app, service_principal_id=expected_sp)
        for path in ("/api/2.0/current-user/me", "/api/2.0/preview/scim/v2/Me"):
            status, identity = _canary_http("GET", host + path, token, deadline, observation=result, status_field="identity_http_status")
            result["identity_http_status"] = status
            if status == 200 and isinstance(identity, dict):
                app_ids = [identity[key] for key in ("applicationId", "application_id") if identity.get(key) is not None]
                if any(app_id != expected_app for app_id in app_ids):
                    raise CanaryUnverified("app_identity_mismatch")
                app_id = app_ids[0] if app_ids else None
                if not app_id and path.endswith("/Me"):
                    app_id = identity.get("userName")
                if app_id:
                    if app_id != expected_app or str(identity.get("id", "")) != expected_sp:
                        raise CanaryUnverified("app_identity_mismatch")
                    result["identity_verified"] = True
                    break
        if not result["identity_verified"]:
            raise CanaryUnverified("app_identity_unverified")
        # Reuse the deployed selection policy, including a live wizard override.
        if role == "wizard":
            model = wizard_llm._pick_model(token)
            url = cli_config.unified_chat_url()
        else:
            available = cli_config.current_model_catalogue(token) if hasattr(cli_config, "current_model_catalogue") else cli_config.discover_model_services(token)
            if hasattr(cli_config, "model_policy"):
                model = cli_config.model_policy.resolve_service(role, available)
            else:
                model = models.resolve(role, available)
            url = cli_config.gateway_host() + "/anthropic/v1/messages" if role == "driver" else cli_config._codex_base_url() + "/responses"
        _canary_remaining(deadline)
        if not _canary_gateway_url(url, host, role) or not re.fullmatch(r"system\.ai\.[A-Za-z0-9_.-]+(?:\[[A-Za-z0-9_-]+\])?", model):
            raise CanaryUnverified("model_configuration_invalid")
        result.update(model=model, wire=models.wire(role), gateway_url=url)
        if config.evaluation_observation_binding() != binding:
            raise CanaryUnverified("evaluation_binding_changed")
        if role == "codex":
            payload = {"model": model, "input": "Return exactly OK.", "max_output_tokens": 32, "stream": False}
        else:
            payload = {"model": model, "messages": [{"role": "user", "content": "Return exactly OK."}], "max_tokens": 32, "stream": False}
        status, body = _canary_http("POST", url, token, deadline, payload=payload, observation=result)
        result["http_status"] = status
        if status != 200:
            reason = {401: "unauthenticated", 403: "permission_denied", 404: "model_or_wire_unavailable", 429: "rate_limited"}.get(status)
            raise CanaryUnverified(reason or ("provider_error" if status >= 500 else "request_rejected"))
        result["response_model"] = _canary_response_model(body)
        evidence = _canary_response(body, role, model)
        _canary_remaining(deadline)
        if config.evaluation_observation_binding() != binding:
            raise CanaryUnverified("evaluation_binding_changed")
        result.update(evidence, invocation_verified=True, error_classification=None)
    except CanaryUnverified as exc:
        result["error_classification"] = str(exc)
    except requests.Timeout:
        result["error_classification"] = "timeout"
    except requests.RequestException:
        result["error_classification"] = "network_error"
    except Exception:
        # Exception strings can contain tokens, headers or provider response text.
        result["error_classification"] = "instrumentation_error"
    finally:
        _CANARY_LOCK.release()
    return result


@router.post("/model-canary")
async def model_canary(request: Request):
    binding = config.evaluation_observation_binding()
    if binding is None:
        raise HTTPException(status_code=404, detail="Synthetic evaluation observation is disabled")
    if request.query_params:
        raise HTTPException(status_code=422, detail="Unsupported evaluation query field")
    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > 1024:
            raise HTTPException(status_code=422, detail="Unsupported canary request")
    try:
        body = json.loads(raw)
        if not isinstance(body, dict) or set(body) != {"role"} or body["role"] not in _CANARY_ROLES:
            raise ValueError()
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=422, detail="Unsupported canary request") from exc
    result = {"schema_version": 1, "instrumentation": "app_identity_model_canary_v1", **{key: binding[key] for key in ("marker", "run_id", "unit_id")},
              "role": body["role"], "model": None, "response_model": None, "wire": None, "application_id": None, "service_principal_id": None,
              "identity_verified": False, "identity_http_status": None, "http_status": None, "usage": {},
              "output_kinds": [], "ok_answer_verified": False, "invocation_verified": False,
              "error_classification": "unverified", "started_at": datetime.now().astimezone().isoformat(),
              "max_output_tokens": 32, "timeout_seconds": CANARY_TIMEOUT_SECONDS, "accepted": False}
    try:
        result = await asyncio.wait_for(asyncio.to_thread(_canary_run, body["role"], binding, result,
                                                         time.monotonic() + CANARY_TIMEOUT_SECONDS), CANARY_TIMEOUT_SECONDS)
    except TimeoutError:
        result = dict(result, invocation_verified=False, ok_answer_verified=False, error_classification="timeout")
    return JSONResponse(result, headers={"Cache-Control": "no-store"})


class Unverified(RuntimeError):
    pass


def _directory(path: Path) -> int:
    """Open every absolute ancestor without following a symlink."""
    path = path.absolute()
    fd = os.open(path.anchor, os.O_RDONLY | _DIRECTORY | _NOFOLLOW)
    try:
        for component in path.parts[1:]:
            next_fd = os.open(component, os.O_RDONLY | _DIRECTORY | _NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        return fd
    except BaseException:
        os.close(fd)
        raise


def _files(root: Path, agent: str) -> list[Path]:
    result = []
    visited = 0
    max_depth = 2 if agent == "claude" else 3

    def walk(fd: int, parts: tuple[str, ...], depth: int):
        nonlocal visited
        with os.scandir(fd) as entries:
            for entry in entries:
                visited += 1
                if visited > MAX_DIRECTORY_ENTRIES:
                    raise Unverified("native_directory_budget")
                info = entry.stat(follow_symlinks=False)
                if stat.S_ISLNK(info.st_mode):
                    raise Unverified("unsafe_native_path")
                if stat.S_ISDIR(info.st_mode) and depth < max_depth:
                    child = os.open(entry.name, os.O_RDONLY | _DIRECTORY | _NOFOLLOW, dir_fd=fd)
                    try:
                        walk(child, (*parts, entry.name), depth + 1)
                    finally:
                        os.close(child)
                elif stat.S_ISREG(info.st_mode) and entry.name.endswith(".jsonl"):
                    if agent == "claude":
                        try:
                            UUID(entry.name[:-6])
                        except ValueError:
                            continue  # Subagent logs are not the attendee's chat.
                    elif not entry.name.startswith("rollout-"):
                        continue
                    result.append(root.joinpath(*parts, entry.name))
                    if len(result) > MAX_FILES:
                        raise Unverified("native_file_budget")
    try:
        fd = _directory(root)
    except FileNotFoundError:
        return []
    try:
        walk(fd, (), 0)
    finally:
        os.close(fd)
    return result


def _read(path: Path) -> tuple[bytes, str]:
    directory = _directory(path.parent)
    try:
        fd = os.open(path.name, os.O_RDONLY | _NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    finally:
        os.close(directory)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise Unverified("unsafe_native_file")
        if info.st_size > MAX_FILE_BYTES:
            raise Unverified("native_file_size_budget")
        with os.fdopen(fd, "rb", closefd=False) as file:
            raw = file.read(MAX_FILE_BYTES + 1)
        if len(raw) > MAX_FILE_BYTES:
            raise Unverified("native_file_size_budget")
        fingerprint = hashlib.sha256(f"{info.st_dev}:{info.st_ino}:{path.name}".encode()).hexdigest()
        return raw, fingerprint
    finally:
        os.close(fd)


def _records(raw: bytes):
    offset = 0
    for line in raw.splitlines(keepends=True):
        if len(line) > MAX_LINE_BYTES:
            raise Unverified("native_line_size_budget")
        if not line.endswith(b"\n"):
            break  # The live CLI may be mid-write; never emit a partial record.
        offset += len(line)
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except (ValueError, UnicodeDecodeError) as exc:
            raise Unverified("malformed_native_record") from exc
        if not isinstance(record, dict):
            raise Unverified("unsupported_native_record")
        yield offset, record


def _epoch(value: object) -> float | None:
    if not isinstance(value, str):
        return None
    try:
        timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return timestamp.timestamp() if timestamp.tzinfo else None
    except ValueError:
        return None


def _binding(raw: bytes, path: Path, session, home: Path, version: str) -> str | None:
    expected_cwd = str(home / "projects")
    for _offset, record in _records(raw):
        if session.agent_id == "codex":
            if record.get("type") != "session_meta":
                continue
            metadata = record.get("payload", {})
            if not isinstance(metadata, dict):
                return None
            sid, cwd, pin = metadata.get("id"), metadata.get("cwd"), metadata.get("cli_version")
            timestamp = _epoch(metadata.get("timestamp") or record.get("timestamp"))
        else:
            # Current Claude logs begin with bookkeeping that carries a
            # sessionId but lacks cwd/version/time. Only a primary chat record
            # can establish the native session's exact startup binding.
            if record.get("type") not in {"user", "assistant"} or record.get("isSidechain") or record.get("isMeta"):
                continue
            sid, cwd, pin = record.get("sessionId"), record.get("cwd"), record.get("version")
            timestamp = _epoch(record.get("timestamp"))
        try:
            canonical_sid = str(UUID(sid))
        except (ValueError, TypeError, AttributeError):
            return None
        if (sid != canonical_sid or cwd != expected_cwd or pin != version or timestamp is None
                or not session.created_at <= timestamp <= time.time() + 5):
            return None
        if not path.name.endswith(canonical_sid + ".jsonl"):
            return None
        return canonical_sid
    return None


def _cursor(session_id: str, native_id: str, fingerprint: str, offset: int) -> str:
    body = json.dumps({"v": 1, "w": session_id, "n": native_id, "f": fingerprint, "o": offset},
                      separators=(",", ":"), sort_keys=True).encode()
    signature = hmac.new(_CURSOR_KEY, body, hashlib.sha256).hexdigest().encode()
    return base64.urlsafe_b64encode(body + b"." + signature).decode().rstrip("=")


def _cursor_offset(value: str, session_id: str, native_id: str, fingerprint: str, size: int) -> int:
    if not value:
        return 0
    try:
        if len(value) > 1024 or not re.fullmatch(r"[A-Za-z0-9_-]+", value):
            raise ValueError()
        raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
        body, signature = raw.rsplit(b".", 1)
        expected = hmac.new(_CURSOR_KEY, body, hashlib.sha256).hexdigest().encode()
        if not hmac.compare_digest(signature, expected):
            raise ValueError()
        cursor = json.loads(body)
        if (set(cursor) != {"v", "w", "n", "f", "o"} or cursor["v"] != 1
                or cursor["w"] != session_id or cursor["n"] != native_id or cursor["f"] != fingerprint
                or type(cursor["o"]) is not int or not 0 <= cursor["o"] <= size):
            raise ValueError()
        return cursor["o"]
    except (ValueError, TypeError, KeyError, UnicodeDecodeError) as exc:
        raise HTTPException(status_code=409, detail="Evaluation cursor no longer matches this native session") from exc


def _question_input(block: dict) -> dict:
    value = block.get("input", {})
    questions = value.get("questions") if isinstance(value, dict) else None
    if (not isinstance(block.get("id"), str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,160}", block["id"])
            or not isinstance(questions, list) or not 1 <= len(questions) <= 4):
        raise Unverified("unsupported_native_question")
    projected = []
    for question in questions:
        if not isinstance(question, dict):
            raise Unverified("unsupported_native_question")
        item = {key: question.get(key) for key in ("question", "header")}
        # AskUserQuestion's optional multiSelect defaults to a single choice.
        # Preserve explicit invalid values rather than coercing them.
        item["multiSelect"] = question.get("multiSelect", False)
        options = question.get("options")
        if (any(not isinstance(item[key], str) or not 0 < len(item[key]) <= 1500 for key in ("question", "header"))
                or type(item["multiSelect"]) is not bool or not isinstance(options, list) or not 2 <= len(options) <= 4):
            raise Unverified("unsupported_native_question")
        item["options"] = []
        for option in options:
            if (not isinstance(option, dict) or any(not isinstance(option.get(key), str)
                    or not 0 < len(option[key]) <= 1500 for key in ("label", "description"))):
                raise Unverified("unsupported_native_question")
            item["options"].append({key: redact(option[key]) for key in ("label", "description")})
        item["question"], item["header"] = redact(item["question"]), redact(item["header"])
        projected.append(item)
    if len({q["question"] for q in projected}) != len(projected):
        raise Unverified("unsupported_native_question")
    return {"kind": "claude_ask_user_question", "tool_use_id": block["id"], "questions": projected}


def _message(record: dict, agent: str, native_id: str, offset: int, question_calls=None) -> dict | None:
    if agent == "claude":
        if record.get("sessionId") != native_id or record.get("isSidechain") or record.get("isMeta"):
            return None
        if record.get("type") not in {"user", "assistant"}:
            return None
        message = record.get("message")
        event_id = record.get("uuid")
    else:
        if record.get("type") != "response_item":
            return None
        message = record.get("payload")
        event_id = message.get("id") if isinstance(message, dict) else None
        if not isinstance(message, dict) or message.get("type") != "message":
            return None
    if not isinstance(message, dict) or message.get("role") not in {"user", "assistant"}:
        return None
    role = message["role"]
    # Claude writes its internal compaction context as a synthetic user record.
    # It is neither an attendee reply nor prose to export or size-check.
    if (agent == "claude" and record.get("type") == "user" and role == "user"
            and record.get("isCompactSummary") is True):
        return None
    interaction = None
    content = message.get("content")
    if agent == "claude" and role == "assistant" and isinstance(content, list):
        asks = [block for block in content if isinstance(block, dict)
                and block.get("type") == "tool_use" and block.get("name") == "AskUserQuestion"]
        if asks:
            if len(asks) != 1:
                raise Unverified("unsupported_native_question")
            interaction = _question_input(asks[0])
            if question_calls is not None:
                question_calls[interaction["tool_use_id"]] = interaction
    if role == "assistant":
        complete = message.get("stop_reason") in {"end_turn", "stop_sequence"} if agent == "claude" else (
            message.get("phase") == "final_answer")
        if not complete and interaction is None:
            return None
    if agent == "claude" and role == "user" and isinstance(content, list) and question_calls is not None:
        results = [block for block in content if isinstance(block, dict) and block.get("type") == "tool_result"
                   and block.get("tool_use_id") in question_calls]
        if results:
            if len(results) != 1:
                raise Unverified("unsupported_native_question_answer")
            call = question_calls[results[0]["tool_use_id"]]
            tool_result = record.get("toolUseResult", {})
            answers = tool_result.get("answers") if isinstance(tool_result, dict) else None
            expected = [q["question"] for q in call["questions"]]
            if (not isinstance(answers, dict) or set(answers) != set(expected)
                    or any(not isinstance(answers[q], str) or not 0 < len(answers[q]) <= 3000 for q in expected)):
                raise Unverified("unsupported_native_question_answer")
            text = "\n".join(answers[q] for q in expected)
        else:
            text = "\n".join(block["text"] for block in content if isinstance(block, dict)
                             and block.get("type") == "text" and isinstance(block.get("text"), str))
    elif interaction is not None:
        text = "\n".join([block["text"] for block in content if isinstance(block, dict)
                           and block.get("type") == "text" and isinstance(block.get("text"), str)] +
                         [q["question"] + "\n" + "\n".join(o["label"] + ": " + o["description"] for o in q["options"])
                          for q in interaction["questions"]])
    elif isinstance(content, str):
        text = content
    elif isinstance(content, list):
        text = "\n".join(block["text"] for block in content if isinstance(block, dict)
                         and block.get("type") in {"text", "input_text", "output_text"}
                         and isinstance(block.get("text"), str))
    else:
        return None
    if not text.strip():
        return None
    if len(text) > MAX_MESSAGE_CHARS:
        raise Unverified("native_message_size_budget")
    timestamp = record.get("timestamp")
    if _epoch(timestamp) is None:
        raise Unverified("native_message_timestamp_unverified")
    message_id = hashlib.sha256(f"{native_id}:{event_id or offset}".encode()).hexdigest()
    result = {"message_id": message_id, "timestamp": timestamp,
            "role": role, "visibility": "user", "complete": True, "text": redact(text)}
    if interaction is not None:
        # Complete authored question, explicitly distinguished from end_turn.
        result["interaction"] = interaction
    return result


def _unverified(reason: str) -> dict:
    return {"schema_version": 1, "instrumentation": "read_only_native_transcript_v1",
            "status": "unverified", "binding_verified": False, "reason": reason, "messages": [],
            "next_cursor": "", "tool_and_worker_coverage_verified": False,
            "transcript_authenticity_verified": False,
            "unverified_checks": ["tool_and_worker_attribution", "implementation_timing", "native_transcript_authenticity"]}


@router.get("/native-format")
def native_format(request: Request):
    """Bounded metadata qualification for the designated synthetic home only.

    No arbitrary paths, prose, tool output, credentials or thinking leave this
    route. This diagnoses unsupported log envelopes separately from quality.
    """
    binding = config.evaluation_observation_binding()
    if binding is None:
        raise HTTPException(status_code=404, detail="Synthetic evaluation observation is disabled")
    if request.query_params:
        raise HTTPException(status_code=422, detail="Unsupported evaluation query field")
    home = Path(config.users_root()) / email_slug(binding["attendee_email"])
    files, scanned = [], 0
    try:
        for agent, relative in (("claude", ".claude/projects"), ("codex", ".codex/sessions")):
            for path in _files(home / relative, agent):
                raw, fingerprint = _read(path)
                scanned += len(raw)
                if scanned > MAX_SCAN_BYTES:
                    raise Unverified("native_scan_budget")
                metadata, question_formats, oversized_text_records = [], [], []
                compaction_summary_count = 0
                for _offset, record in _records(raw):
                    message = record.get("message", {})
                    content = message.get("content", []) if isinstance(message, dict) else []
                    if agent == "claude":
                        compact = (record.get("type") == "user" and isinstance(message, dict)
                                   and message.get("role") == "user" and record.get("isCompactSummary") is True)
                        compaction_summary_count += int(compact)
                        text_chars = len(content) if isinstance(content, str) else sum(
                            len(block["text"]) for block in content if isinstance(block, dict)
                            and block.get("type") == "text" and isinstance(block.get("text"), str)
                        ) if isinstance(content, list) else 0
                        if text_chars > MAX_MESSAGE_CHARS and len(oversized_text_records) < 8:
                            oversized_text_records.append({"type": record.get("type")
                                if record.get("type") in {"user", "assistant"} else "unknown",
                                "is_compact_summary": compact, "text_chars": text_chars})
                    if agent == "claude" and isinstance(content, list):
                        for block in content:
                            if (len(question_formats) < 8 and isinstance(block, dict)
                                    and block.get("type") == "tool_use" and block.get("name") == "AskUserQuestion"):
                                try:
                                    projected = _question_input(block)
                                    source = block["input"]["questions"]
                                    question_formats.append({"projection_supported": True,
                                        "question_count": len(projected["questions"]),
                                        "multi_select_present": ["multiSelect" in q for q in source]})
                                except Unverified:
                                    question_formats.append({"projection_supported": False})
                    if len(metadata) >= 12:
                        continue
                    payload = record.get("payload", {}) if agent == "codex" else record
                    if not isinstance(payload, dict):
                        continue
                    kind = record.get("type", "")
                    # Record labels are from a small explicit protocol list;
                    # unknown labels are never echoed from writable files.
                    allowed = {"user", "assistant", "system", "queue-operation", "file-history-snapshot",
                               "session_meta", "response_item", "progress", "summary"}
                    metadata.append({"type": kind if kind in allowed else "unknown",
                        "has_session_id": isinstance(payload.get("sessionId", payload.get("id")), str),
                        "cwd_present": "cwd" in payload,
                        "cwd_matches": payload.get("cwd") == str(home / "projects"),
                        "version_present": ("cli_version" if agent == "codex" else "version") in payload,
                        "version_matches": payload.get("cli_version" if agent == "codex" else "version") == SUPPORTED_PINS.get(agent),
                        "timestamp_valid": _epoch(payload.get("timestamp", record.get("timestamp"))) is not None})
                files.append({"agent_id": agent, "file_fingerprint": fingerprint, "bytes": len(raw),
                              "max_record_bytes": max((len(line) for line in raw.splitlines(keepends=True)), default=0),
                              "records": metadata, "question_formats": question_formats,
                              "compaction_summary_count": compaction_summary_count,
                              "oversized_text_records": oversized_text_records})
        projects = home / "projects"
        project_count = 0
        if projects.exists():
            descriptor = _directory(projects)
            try:
                project_count = len(os.listdir(descriptor))
                if project_count > MAX_DIRECTORY_ENTRIES:
                    raise Unverified("native_directory_budget")
            finally:
                os.close(descriptor)
        return JSONResponse({"schema_version": 1, "status": "metadata_observed", "files": files,
                             "projects_state": {"directory_exists": projects.exists(),
                                                "entry_count": project_count, "empty": project_count == 0},
                             "quality_verified": False}, headers={"Cache-Control": "no-store"})
    except (Unverified, OSError):
        return JSONResponse({"schema_version": 1, "status": "unverified", "files": [],
                             "quality_verified": False}, headers={"Cache-Control": "no-store"})


@router.get("/sessions/{session_id}/messages")
def messages(session_id: str, request: Request, cursor: str = "", limit: int = Query(30, ge=1, le=100)):
    binding = config.evaluation_observation_binding()
    if binding is None:
        raise HTTPException(status_code=404, detail="Synthetic evaluation observation is disabled")
    if set(request.query_params) - {"cursor", "limit"}:
        raise HTTPException(status_code=422, detail="Unsupported evaluation query field")
    try:
        if str(UUID(session_id)) != session_id:
            raise ValueError()
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="Evaluation session not found") from exc
    session = session_manager.get(session_id, binding["attendee_email"])
    if session is None or session is not session_manager.active() or session.exited:
        raise HTTPException(status_code=404, detail="Evaluation session not found")
    version = SUPPORTED_PINS.get(session.agent_id)
    if version is None:
        return JSONResponse(_unverified("unsupported_harness"), headers={"Cache-Control": "no-store"})
    home = Path(config.users_root()) / email_slug(binding["attendee_email"])
    native_root = home / (".claude/projects" if session.agent_id == "claude" else ".codex/sessions")
    try:
        candidates = []
        scanned = 0
        for path in _files(native_root, session.agent_id):
            raw, fingerprint = _read(path)
            scanned += len(raw)
            if scanned > MAX_SCAN_BYTES:
                raise Unverified("native_scan_budget")
            native_id = _binding(raw, path, session, home, version)
            if native_id:
                candidates.append((native_id, raw, fingerprint))
        if len(candidates) != 1:
            raise Unverified("ambiguous_native_session" if candidates else "native_session_not_bound")
        native_id, raw, fingerprint = candidates[0]
        start = _cursor_offset(cursor, session_id, native_id, fingerprint, len(raw))
        output = []
        consumed = start
        chars = 0
        question_calls = {}
        for end, record in _records(raw):
            if session.agent_id == "claude":
                if ((record.get("sessionId") and record.get("sessionId") != native_id and not record.get("isSidechain"))
                        or (record.get("version") and record.get("version") != version)):
                    raise Unverified("mixed_native_session_or_version")
            elif record.get("type") == "session_meta":
                metadata = record.get("payload", {})
                if not isinstance(metadata, dict) or metadata.get("id") != native_id or metadata.get("cli_version") != version:
                    raise Unverified("mixed_native_session_or_version")
            event = _message(record, session.agent_id, native_id, end, question_calls)
            if end <= start:
                continue
            if event:
                if len(output) >= limit or chars + len(event["text"]) > MAX_RESPONSE_CHARS:
                    break
                output.append(event)
                chars += len(event["text"])
            consumed = end
        # Recheck ownership after reads so a concurrently closed/switched seat
        # cannot leak stale observation as belonging to the new active session.
        if session_manager.get(session_id, binding["attendee_email"]) is not session or session_manager.active() is not session:
            raise Unverified("wt_session_changed")
        result = {"schema_version": 1, "instrumentation": "read_only_native_transcript_v1",
                  "status": "ready" if output else "awaiting_complete_message", "binding_verified": True,
                  "harness_id": session.agent_id, "harness_version": version, "wt_session_id": session_id,
                  "native_session_id": native_id, "messages": output,
                  "next_cursor": _cursor(session_id, native_id, fingerprint, consumed),
                  "tool_and_worker_coverage_verified": False,
                  # Native files belong to the builder's writable HOME. Binding
                  # establishes attribution, not a tamper-proof execution log.
                  "transcript_authenticity_verified": False,
                  "unverified_checks": ["tool_and_worker_attribution", "implementation_timing", "native_transcript_authenticity"]}
    except Unverified as exc:
        result = _unverified(str(exc))
    except OSError:
        result = _unverified("native_file_unavailable_or_unsafe")
    return JSONResponse(result, headers={"Cache-Control": "no-store"})
