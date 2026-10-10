"""Synthetic native transcript bridge contracts; no harness/cloud processes."""

import json
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from .conftest import ALICE, BOB


@pytest.fixture()
def evaluation_seat(monkeypatch, tmp_path):
    from server import config, evaluation
    from server.users import email_slug
    owner = "alice@example.com"
    monkeypatch.setenv("DATA_ROOT", str(tmp_path.resolve()))
    for key, value in {
        "WORKSHOP_EVALUATION_ENABLED": "true", "WORKSHOP_EVALUATION_MARKER": "wt-eval-bakery-1",
        "WORKSHOP_EVALUATION_ATTENDEE_EMAIL": owner, "WORKSHOP_ATTENDEE_EMAIL": owner,
        "WORKSHOP_RUN_ID": "opaque-ct-run-id", "WORKSHOP_UNIT_ID": "opaque-ct-unit-id",
        "WORKSHOP_EVALUATION_RUN_ID": "opaque-ct-run-id", "WORKSHOP_EVALUATION_UNIT_ID": "opaque-ct-unit-id",
        "MAX_SESSIONS_PER_USER": "1", "MAX_SESSIONS_GLOBAL": "1", "ALLOW_SHARED_TOPOLOGY": "false",
    }.items():
        monkeypatch.setenv(key, value)
    session = SimpleNamespace(id=str(uuid4()), owner_email=owner, agent_id="claude", created_at=time.time() - 2,
                              exited=False)
    monkeypatch.setattr(evaluation.session_manager, "get", lambda sid, email:
                        session if sid == session.id and email == owner else None)
    monkeypatch.setattr(evaluation.session_manager, "active", lambda: session)
    home = Path(config.users_root()) / email_slug(owner)
    return SimpleNamespace(session=session, home=home, native_id=str(uuid4()),
                           timestamp=datetime.fromtimestamp(session.created_at + .5, timezone.utc).isoformat())


def endpoint(seat, query=""):
    return f"/api/admin/evaluation/sessions/{seat.session.id}/messages" + query


def test_native_observer_pins_match_installed_harness_artifacts():
    from server import evaluation
    manifest = json.loads((Path(__file__).parents[1] / "assets/artifacts/manifest.json").read_text())["artifacts"]
    assert evaluation.SUPPORTED_PINS == {
        "claude": manifest["claude_binary"]["version"],
        "codex": manifest["codex_npm_launcher_package"]["version"],
    }


def claude(seat, text="Who will use this?", *, role="assistant", complete=True, **extra):
    return {"type": role, "sessionId": seat.native_id, "version": "2.1.296", "cwd": str(seat.home / "projects"),
            "timestamp": seat.timestamp, "uuid": str(uuid4()),
            "message": {"role": role, "stop_reason": "end_turn" if complete else None,
                        "content": [{"type": "thinking", "thinking": "private chain of thought"},
                                    {"type": "text", "text": text}]}, **extra}


def write_claude(seat, records=None, *, native_id=None):
    path = seat.home / ".claude" / "projects" / "encoded-projects-cwd" / ((native_id or seat.native_id) + ".jsonl")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(record) + "\n" for record in (records or [claude(seat)])))
    return path


def write_codex(seat, records=None):
    seat.session.agent_id = "codex"
    path = seat.home / ".codex" / "sessions" / "2026" / "10" / "08" / ("rollout-2026-10-08T01-00-00-" + seat.native_id + ".jsonl")
    path.parent.mkdir(parents=True, exist_ok=True)
    metadata = {"type": "session_meta", "timestamp": seat.timestamp, "payload": {
        "id": seat.native_id, "cli_version": "0.162.1", "cwd": str(seat.home / "projects"),
        "timestamp": seat.timestamp, "source": "cli"}}
    turns = records or [{"type": "response_item", "timestamp": seat.timestamp, "payload": {
        "type": "message", "id": "native-msg-1", "role": "assistant", "phase": "final_answer",
        "content": [{"type": "output_text", "text": "Should this remember your changes?"}]}}]
    path.write_text("".join(json.dumps(record) + "\n" for record in [metadata, *turns]))
    return path


def test_bridge_is_off_by_default_and_reads_no_files(client, as_admin, monkeypatch):
    from server import evaluation
    monkeypatch.delenv("WORKSHOP_EVALUATION_ENABLED", raising=False)
    monkeypatch.setattr(evaluation, "_files", lambda *_args: pytest.fail("disabled bridge touched runtime homes"))
    response = client.get(f"/api/admin/evaluation/sessions/{uuid4()}/messages", headers=ALICE)
    assert response.status_code == 404


@pytest.mark.parametrize("key,value", [
    ("WORKSHOP_EVALUATION_MARKER", "normal-event"),
    ("WORKSHOP_EVALUATION_RUN_ID", "wrong-run"),
    ("WORKSHOP_EVALUATION_UNIT_ID", "wrong-unit"),
    ("WORKSHOP_EVALUATION_ATTENDEE_EMAIL", "bob@example.com"),
    ("WORKSHOP_ATTENDEE_EMAIL", ""),
    ("WORKSHOP_ATTENDEE_EMAIL", "not-an-email"),
    ("ALLOW_SHARED_TOPOLOGY", "true"),
    ("MAX_SESSIONS_GLOBAL", "2"),
])
def test_all_synthetic_ct_gates_are_required(client, as_admin, evaluation_seat, monkeypatch, key, value):
    monkeypatch.setenv(key, value)
    assert client.get(endpoint(evaluation_seat), headers=ALICE).status_code == 404


def test_only_genuine_operator_access_can_observe(client, as_non_admin, evaluation_seat):
    write_claude(evaluation_seat)
    response = client.get(endpoint(evaluation_seat), headers=ALICE)
    assert response.status_code == 403
    assert "Who will use" not in response.text


def test_native_format_reports_only_bounded_metadata_for_the_synthetic_home(client, as_admin, evaluation_seat):
    seat = evaluation_seat
    write_claude(seat, [{"type": "queue-operation", "sessionId": seat.native_id,
                         "content": "must never leave metadata qualification"}, claude(seat)])
    response = client.get("/api/admin/evaluation/native-format", headers=ALICE)
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "metadata_observed" and not payload["quality_verified"]
    metadata = payload["files"][0]["records"]
    assert metadata[0]["type"] == "queue-operation" and not metadata[0]["cwd_present"]
    assert metadata[1]["cwd_matches"] and metadata[1]["version_matches"]
    assert "must never" not in response.text and "Who will" not in response.text and "private chain" not in response.text
    assert str(seat.home) not in response.text and seat.native_id not in response.text
    assert client.get("/api/admin/evaluation/native-format?path=/tmp", headers=ALICE).status_code == 422


def test_current_claude_bookkeeping_prefix_cannot_prevent_or_supply_native_binding(client, as_admin, evaluation_seat):
    seat = evaluation_seat
    prefix = [{"type": "queue-operation", "sessionId": seat.native_id},
              {"type": "unknown-current-bookkeeping", "sessionId": seat.native_id}]
    write_claude(seat, [*prefix, claude(seat, "Bakery orders please", role="user")])
    result = client.get(endpoint(seat), headers=ALICE).json()
    assert result["binding_verified"] and result["messages"][0]["text"] == "Bakery orders please"
    write_claude(seat, prefix)
    result = client.get(endpoint(seat), headers=ALICE).json()
    assert result["status"] == "unverified" and result["messages"] == []


def test_claude_compaction_summary_is_not_an_attendee_reply(client, as_admin, evaluation_seat):
    from server import evaluation
    seat = evaluation_seat
    summary = claude(seat, "private compaction context " * 700, role="user", isCompactSummary=True)
    write_claude(seat, [claude(seat, "Bakery please", role="user"), summary,
                        claude(seat, "Here is the preview.")])
    payload = client.get(endpoint(seat), headers=ALICE).json()
    assert payload["status"] == "ready"
    assert [message["text"] for message in payload["messages"]] == ["Bakery please", "Here is the preview."]
    assert "private compaction context" not in json.dumps(payload)
    metadata = client.get("/api/admin/evaluation/native-format", headers=ALICE).json()["files"][0]
    assert metadata["compaction_summary_count"] == 1
    assert metadata["oversized_text_records"] == [{"type": "user", "is_compact_summary": True,
        "text_chars": len(summary["message"]["content"][1]["text"])}]
    assert metadata["oversized_text_records"][0]["text_chars"] > evaluation.MAX_MESSAGE_CHARS


@pytest.mark.parametrize("marker", [False, "true", 1, None])
def test_oversized_authored_message_is_not_hidden_as_compaction(client, as_admin, evaluation_seat, marker):
    seat = evaluation_seat
    record = claude(seat, "attendee prose " * 1000, role="user", isCompactSummary=marker)
    write_claude(seat, [claude(seat, "Bakery please", role="user"), record])
    result = client.get(endpoint(seat), headers=ALICE).json()
    assert result["status"] == "unverified" and result["reason"] == "native_message_size_budget"


@pytest.mark.parametrize("status", ["completed", "failed", "stopped"])
def test_claude_native_task_notification_is_not_an_attendee_reply(client, as_admin, evaluation_seat, status):
    seat = evaluation_seat
    notification = ("<task-notification>\n<task-id>kxpugrgrd</task-id>\n"
        f"<status>{status}</status>\n<summary>MCP task finished.</summary>\n"
        "<result>private task output</result>\n</task-notification>")
    write_claude(seat, [claude(seat, "Bakery please", role="user"),
        claude(seat, notification, role="user"), claude(seat, "I'll inspect the deployment error.")])
    payload = client.get(endpoint(seat), headers=ALICE).json()
    assert [message["text"] for message in payload["messages"]] == ["Bakery please", "I'll inspect the deployment error."]
    assert "private task output" not in json.dumps(payload)
    metadata = client.get("/api/admin/evaluation/native-format", headers=ALICE).json()["files"][0]
    assert metadata["task_notification_count"] == 1


@pytest.mark.parametrize("text", ["Can you explain <task-notification>?", "<task-notification>real attendee text</task-notification>",
    "<task-notification><task-id>test</task-id><status>unknown</status><summary>Question</summary></task-notification>"])
def test_task_notification_fragments_are_not_silently_dropped(client, as_admin, evaluation_seat, text):
    write_claude(evaluation_seat, [claude(evaluation_seat, text, role="user")])
    payload = client.get(endpoint(evaluation_seat), headers=ALICE).json()
    assert payload["messages"][0]["text"] == text


def question_record(seat):
    record = claude(seat, complete=False)
    record["message"]["content"] = [{"type": "thinking", "thinking": "private chain"},
        {"type": "tool_use", "id": "tool_question_1", "name": "AskUserQuestion", "input": {"questions": [{
            "question": "Which orders need attention?", "header": "Attention", "multiSelect": True,
            "options": [{"label": "Late", "description": "Show overdue orders first"},
                        {"label": "Unpaid", "description": "Show outstanding balances"}]}]}}]
    return record


def test_authored_native_questions_and_only_their_answers_cross_the_observer(client, as_admin, evaluation_seat):
    seat = evaluation_seat
    path = write_claude(seat, [claude(seat, "Bakery please", role="user"), question_record(seat)])
    first = client.get(endpoint(seat), headers=ALICE).json()
    assert first["binding_verified"] and len(first["messages"]) == 2
    question = first["messages"][1]
    assert question["interaction"]["kind"] == "claude_ask_user_question"
    assert "Which orders need attention?" in question["text"] and "private chain" not in json.dumps(first)
    answer = claude(seat, role="user")
    answer["message"]["content"] = [{"type": "tool_result", "tool_use_id": "tool_question_1", "content": "arbitrary stdout not exported"}]
    answer["toolUseResult"] = {"answers": {"Which orders need attention?": "Late unpacked orders first."}}
    with path.open("a") as handle:handle.write(json.dumps(answer) + "\n")
    second = client.get(endpoint(seat, "?cursor=" + first["next_cursor"]), headers=ALICE).json()
    assert second["messages"][0]["text"] == "Late unpacked orders first."
    assert "stdout" not in json.dumps(second)


def test_optional_single_choice_and_other_tools_are_projected_independently(client, as_admin, evaluation_seat):
    seat = evaluation_seat
    record = question_record(seat)
    del record["message"]["content"][1]["input"]["questions"][0]["multiSelect"]
    record["message"]["content"].append({"type": "tool_use", "name": "Read", "id": "other",
                                          "input": {"file_path": "private arbitrary path"}})
    write_claude(seat, [claude(seat, "Bakery please", role="user"), record])
    payload = client.get(endpoint(seat), headers=ALICE).json()
    assert payload["binding_verified"]
    assert payload["messages"][1]["interaction"]["questions"][0]["multiSelect"] is False
    assert "private arbitrary path" not in json.dumps(payload)


@pytest.mark.parametrize("change", ["other_tool", "wrong_question", "oversized_answer"])
def test_unknown_native_tool_results_never_become_attendee_answers(client, as_admin, evaluation_seat, change):
    seat = evaluation_seat
    answer = claude(seat, role="user")
    answer["message"]["content"] = [{"type": "tool_result", "tool_use_id": "tool_question_1", "content": "arbitrary stdout"}]
    answer["toolUseResult"] = {"answers": {"Which orders need attention?": "Late orders"}}
    if change == "other_tool":answer["message"]["content"][0]["tool_use_id"] = "unrelated_tool"
    elif change == "wrong_question":answer["toolUseResult"]["answers"] = {"Unasked secret?": "secret"}
    else:answer["toolUseResult"]["answers"]["Which orders need attention?"] = "x" * 3001
    write_claude(seat, [claude(seat, "Bakery please", role="user"), question_record(seat), answer])
    response = client.get(endpoint(seat), headers=ALICE)
    assert "stdout" not in response.text and "secret" not in response.text
    if change != "other_tool":assert response.json()["status"] == "unverified"


def test_claude_messages_are_complete_redacted_and_separate_from_tool_traffic(client, as_admin, evaluation_seat):
    seat = evaluation_seat
    tool_result = claude(seat, role="user")
    tool_result["message"]["content"] = [{"type": "tool_result", "content": "private tool stdout"}]
    records = [claude(seat, "Bakery orders please", role="user"), claude(seat, "Who will use it? token=dapi" + "a" * 32),
               claude(seat, "unfinished", complete=False), claude(seat, "worker question", isSidechain=True), tool_result]
    write_claude(seat, records)
    response = client.get(endpoint(seat), headers=ALICE)
    assert response.status_code == 200
    payload = response.json()
    assert payload["binding_verified"] and payload["status"] == "ready"
    assert [message["role"] for message in payload["messages"]] == ["user", "assistant"]
    assert all(message["complete"] for message in payload["messages"])
    assert "private chain" not in response.text and "private tool" not in response.text
    assert "unfinished" not in response.text and "worker question" not in response.text
    assert "dapi" + "a" * 32 not in response.text
    assert response.headers["cache-control"] == "no-store"
    assert not payload["tool_and_worker_coverage_verified"]
    assert not payload["transcript_authenticity_verified"]
    assert "implementation_timing" in payload["unverified_checks"]


def test_codex_final_phase_is_required_and_tool_outputs_never_leave(client, as_admin, evaluation_seat):
    seat = evaluation_seat
    write_codex(seat, [
        {"type": "response_item", "timestamp": seat.timestamp, "payload": {
            "type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "partial commentary"}]}},
        {"type": "response_item", "timestamp": seat.timestamp, "payload": {
            "type": "function_call_output", "output": "secret stdout"}},
        {"type": "response_item", "timestamp": seat.timestamp, "payload": {
            "type": "message", "role": "assistant", "phase": "final_answer",
            "content": [{"type": "output_text", "text": "Should this remember changes?"}]}},
    ])
    response = client.get(endpoint(seat), headers=ALICE)
    assert response.json()["messages"][0]["text"] == "Should this remember changes?"
    assert "secret stdout" not in response.text and "partial commentary" not in response.text


@pytest.mark.parametrize("field,value", [("cwd", "/tmp/unrelated"), ("version", "2.1.999"),
                                         ("timestamp", "2020-01-01T00:00:00Z"), ("sessionId", "../../outside")])
def test_native_metadata_must_correlate_to_exact_wt_session(client, as_admin, evaluation_seat, field, value):
    write_claude(evaluation_seat, [claude(evaluation_seat, **{field: value})])
    result = client.get(endpoint(evaluation_seat), headers=ALICE).json()
    assert result["status"] == "unverified" and result["messages"] == []


def test_two_plausible_native_sessions_are_ambiguous_and_emit_nothing(client, as_admin, evaluation_seat):
    seat = evaluation_seat
    write_claude(seat)
    other_id = str(uuid4())
    write_claude(seat, [claude(seat, sessionId=other_id)], native_id=other_id)
    result = client.get(endpoint(seat), headers=ALICE).json()
    assert result["reason"] == "ambiguous_native_session" and result["messages"] == []


def test_recent_previous_native_session_is_not_mistaken_for_new_active_wt_session(client, as_admin, evaluation_seat):
    seat = evaluation_seat
    old_timestamp = datetime.fromtimestamp(seat.session.created_at - .01, timezone.utc).isoformat()
    write_claude(seat, [claude(seat, timestamp=old_timestamp)])
    result = client.get(endpoint(seat), headers=ALICE).json()
    assert result["reason"] == "native_session_not_bound" and result["messages"] == []


def test_cursor_is_bounded_authenticated_and_bound_to_native_file(client, as_admin, evaluation_seat):
    seat = evaluation_seat
    first = claude(seat, "First question?")
    second = claude(seat, "Second question?")
    path = write_claude(seat, [first, second])
    result = client.get(endpoint(seat, "?limit=1"), headers=ALICE).json()
    assert [message["text"] for message in result["messages"]] == ["First question?"]
    next_result = client.get(endpoint(seat, "?cursor=" + result["next_cursor"]), headers=ALICE).json()
    assert [message["text"] for message in next_result["messages"]] == ["Second question?"]
    assert client.get(endpoint(seat, "?cursor=../../outside"), headers=ALICE).status_code == 409
    assert client.get(endpoint(seat, "?cursor=" + "A" * 1100), headers=ALICE).status_code == 409
    old_cursor = next_result["next_cursor"]
    path.unlink()
    write_claude(seat, [first])
    assert client.get(endpoint(seat, "?cursor=" + old_cursor), headers=ALICE).status_code == 409


def test_no_path_or_other_attendee_query_is_accepted(client, as_admin, evaluation_seat):
    assert client.get(endpoint(evaluation_seat, "?path=/tmp/secret"), headers=ALICE).status_code == 422
    assert client.get(endpoint(evaluation_seat, "?attendee=bob@example.com"), headers=ALICE).status_code == 422
    assert client.get("/api/admin/evaluation/sessions/not-a-uuid/messages", headers=ALICE).status_code == 404


def test_symlink_or_hardlink_native_file_is_never_read(client, as_admin, evaluation_seat, tmp_path):
    seat = evaluation_seat
    path = write_claude(seat)
    outside = tmp_path / "outside-secret.jsonl"
    outside.write_text(path.read_text().replace("Who will use this?", "outside secret"))
    path.unlink()
    path.symlink_to(outside)
    response = client.get(endpoint(seat), headers=ALICE)
    assert response.json()["status"] == "unverified" and "outside secret" not in response.text
    path.unlink()
    os.link(outside, path)
    response = client.get(endpoint(seat), headers=ALICE)
    assert response.json()["status"] == "unverified" and "outside secret" not in response.text


def test_size_and_line_budgets_fail_closed(client, as_admin, evaluation_seat, monkeypatch):
    from server import evaluation
    seat = evaluation_seat
    write_claude(seat, [claude(seat, "x" * 2000)])
    monkeypatch.setattr(evaluation, "MAX_FILE_BYTES", 1000)
    assert client.get(endpoint(seat), headers=ALICE).json()["reason"] == "native_file_size_budget"
    monkeypatch.setattr(evaluation, "MAX_FILE_BYTES", 10000)
    monkeypatch.setattr(evaluation, "MAX_LINE_BYTES", 1000)
    assert client.get(endpoint(seat), headers=ALICE).json()["reason"] == "native_line_size_budget"


def test_large_skill_result_is_bounded_but_never_exported(client, as_admin, evaluation_seat):
    seat = evaluation_seat
    result = claude(seat, role="user")
    result["message"]["content"] = [{"type": "tool_result", "tool_use_id": "skill_read",
                                     "content": "private skill output " * 14000}]
    write_claude(seat, [claude(seat, "Bakery please", role="user"), result, question_record(seat)])
    payload = client.get(endpoint(seat), headers=ALICE).json()
    assert payload["binding_verified"] and len(payload["messages"]) == 2
    assert payload["messages"][1]["interaction"]["kind"] == "claude_ask_user_question"
    assert "private skill output" not in json.dumps(payload)


def test_partial_live_record_is_not_advanced_or_emitted(client, as_admin, evaluation_seat):
    seat = evaluation_seat
    path = write_claude(seat, [claude(seat, "Initial goal", role="user")])
    final = json.dumps(claude(seat, "Who will use this?"))
    with path.open("a") as file:
        file.write(final[:30])
    result = client.get(endpoint(seat), headers=ALICE).json()
    assert [message["text"] for message in result["messages"]] == ["Initial goal"]
    with path.open("a") as file:
        file.write(final[30:] + "\n")
    result = client.get(endpoint(seat, "?cursor=" + result["next_cursor"]), headers=ALICE).json()
    assert [message["text"] for message in result["messages"]] == ["Who will use this?"]


def test_only_active_bound_owned_session_is_observable(client, as_admin, evaluation_seat, monkeypatch):
    from server import evaluation
    write_claude(evaluation_seat)
    monkeypatch.setattr(evaluation.session_manager, "active", lambda: None)
    assert client.get(endpoint(evaluation_seat), headers=ALICE).status_code == 404


CANARY_ENDPOINT = "/api/admin/evaluation/model-canary"


@pytest.fixture()
def canary_runtime(monkeypatch, evaluation_seat):
    from server import cli_config, config, credentials, evaluation, models, wizard_llm
    app_id = "11111111-1111-4111-8111-111111111111"
    monkeypatch.setenv("DATABRICKS_CLIENT_ID", app_id)
    monkeypatch.setenv("WORKSHOP_APP_SP_ID", "12345")
    monkeypatch.setenv("ANTHROPIC_MODEL", "claude-sonnet-4-6")
    monkeypatch.setenv("CODEX_MODEL", "gpt-6-sol")
    monkeypatch.setenv("WORKSHOP_WIZARD_MODEL", "gpt-5-4-mini")
    monkeypatch.setattr(config, "local_dev", lambda: False)
    client = SimpleNamespace(config=SimpleNamespace(auth_type="oauth-m2m", client_id=app_id,
                                                   host=config.databricks_host()))
    monkeypatch.setattr(credentials, "workspace_client", lambda: client)
    monkeypatch.setattr(credentials, "app_identity_bearer", lambda: "app-canary-secret")
    monkeypatch.setattr(credentials.credential_manager, "token", lambda: pytest.fail("canary used attendee/PAT token source"))
    monkeypatch.setattr(credentials, "vended_pat", lambda: pytest.fail("canary used emergency PAT fallback"))
    monkeypatch.setattr(evaluation, "_CANARY_LOCK", threading.Lock())
    gateway = config.databricks_host() + "/ai-gateway"
    monkeypatch.setattr(cli_config, "gateway_host", lambda: gateway)
    catalogue = {"claude-sonnet-4-6": {models.ANTHROPIC_MESSAGES}, "gpt-6-sol": {models.OPENAI_RESPONSES},
                 "gpt-5-4-mini": {models.CHAT_COMPLETIONS}}
    monkeypatch.setattr(cli_config, "discover_model_services", lambda token: catalogue)
    monkeypatch.setattr(wizard_llm, "_served_models", lambda token: catalogue)
    monkeypatch.setattr(wizard_llm, "_model_override", None)
    state = SimpleNamespace(client=client, calls=[], provider_status=200, provider_error=None,
                            provider_body=None, identity={"applicationId": app_id, "id": "12345"})

    class Response:
        def __init__(self, status, body):
            self.status_code, self.body = status, body
        def __enter__(self):
            return self
        def __exit__(self, *_args):
            return False
        def iter_content(self, chunk_size):
            yield json.dumps(self.body).encode()

    def request(method, url, **kwargs):
        state.calls.append({"method": method, "url": url, "thread": threading.current_thread().name, **kwargs})
        if method == "GET":
            return Response(200, state.identity)
        if state.provider_error:
            raise state.provider_error
        model = kwargs["json"]["model"]
        if url.endswith("/messages"):
            body = {"type": "message", "role": "assistant", "model": model, "stop_reason": "end_turn",
                    "content": [{"type": "text", "text": "OK"}], "usage": {"input_tokens": 5, "output_tokens": 1}}
        elif url.endswith("/responses"):
            body = {"object": "response", "status": "completed", "model": model,
                    "output": [{"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "OK"}]}],
                    "usage": {"input_tokens": 5, "output_tokens": 1}}
        else:
            body = {"model": model, "choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": "OK"}}],
                    "usage": {"prompt_tokens": 5, "completion_tokens": 1}}
        return Response(state.provider_status, state.provider_body if state.provider_body is not None else body)

    monkeypatch.setattr(evaluation.requests, "request", request)
    return state


@pytest.mark.parametrize("role,suffix,model,wire", [
    ("driver", "/anthropic/v1/messages", "system.ai.claude-sonnet-4-6", "anthropic/v1/messages"),
    ("codex", "/codex/v1/responses", "system.ai.gpt-6-sol", "openai/v1/responses"),
    ("wizard", "/mlflow/v1/chat/completions", "system.ai.gpt-5-4-mini", "mlflow/v1/chat/completions"),
])
def test_model_canary_uses_app_oauth_native_wire_and_fixed_bounded_prompt(client, as_admin, canary_runtime, role, suffix, model, wire):
    response = client.post(CANARY_ENDPOINT, headers=ALICE, json={"role": role})
    result = response.json()
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    assert result["identity_verified"] and result["invocation_verified"] and result["ok_answer_verified"]
    assert result["model"] == model and result["wire"] == wire and result["http_status"] == 200
    assert result["marker"] == "wt-eval-bakery-1" and result["run_id"] == "opaque-ct-run-id" and result["unit_id"] == "opaque-ct-unit-id"
    assert result["application_id"] == canary_runtime.client.config.client_id and result["service_principal_id"] == "12345"
    assert result["accepted"] is False and "app-canary-secret" not in response.text and '"OK"' not in response.text
    assert len(canary_runtime.calls) == 2
    call = canary_runtime.calls[-1]
    assert call["url"].endswith(suffix) and call["allow_redirects"] is False and call["stream"] is True
    assert call["headers"]["Authorization"] == "Bearer app-canary-secret"
    assert call["timeout"][0] <= 5 and call["timeout"][1] <= 15
    assert call["thread"].startswith("asyncio_")
    assert call["json"]["max_output_tokens" if role == "codex" else "max_tokens"] == 32
    assert call["json"]["input"] == "Return exactly OK." if role == "codex" else call["json"]["messages"] == [{"role": "user", "content": "Return exactly OK."}]


def test_model_canary_is_default_off_and_non_admin_cannot_invoke(client, as_admin, canary_runtime, monkeypatch):
    monkeypatch.delenv("WORKSHOP_EVALUATION_ENABLED")
    assert client.post(CANARY_ENDPOINT, headers=ALICE, json={"role": "driver"}).status_code == 404
    assert canary_runtime.calls == []


def test_model_canary_requires_operator(client, as_non_admin, canary_runtime):
    assert client.post(CANARY_ENDPOINT, headers=ALICE, json={"role": "driver"}).status_code == 403
    assert canary_runtime.calls == []


@pytest.mark.parametrize("key,value", [("WORKSHOP_EVALUATION_RUN_ID", "wrong-run"), ("WORKSHOP_EVALUATION_UNIT_ID", "wrong-unit"),
                                        ("WORKSHOP_EVALUATION_MARKER", "normal-event"), ("MAX_SESSIONS_GLOBAL", "2")])
def test_model_canary_requires_exact_evaluation_contract(client, as_admin, canary_runtime, monkeypatch, key, value):
    monkeypatch.setenv(key, value)
    assert client.post(CANARY_ENDPOINT, headers=ALICE, json={"role": "driver"}).status_code == 404
    assert canary_runtime.calls == []


@pytest.mark.parametrize("body", [{"role": "unknown"}, {"role": []}, {"role": "driver", "prompt": "private-secret"},
                                  {"role": "driver", "token": "private-secret"}, {"role": "driver", "url": "https://outside.example"},
                                  {"role": "driver", "model": "system.ai.other"}, {}])
def test_model_canary_rejects_inputs_without_echo_or_network(client, as_admin, canary_runtime, body):
    response = client.post(CANARY_ENDPOINT, headers=ALICE, json=body)
    assert response.status_code == 422 and "private-secret" not in response.text and canary_runtime.calls == []
    assert client.post(CANARY_ENDPOINT + "?token=private-secret", headers=ALICE, json={"role": "driver"}).status_code == 422


@pytest.mark.parametrize("field,value", [("applicationId", "22222222-2222-4222-8222-222222222222"), ("id", "99999")])
def test_model_canary_never_invokes_for_different_app_identity(client, as_admin, canary_runtime, field, value):
    canary_runtime.identity[field] = value
    result = client.post(CANARY_ENDPOINT, headers=ALICE, json={"role": "driver"}).json()
    assert result["error_classification"] == "app_identity_mismatch" and not result["invocation_verified"]
    assert len(canary_runtime.calls) == 1 and canary_runtime.calls[0]["method"] == "GET"


@pytest.mark.parametrize("key,value", [("DATABRICKS_CLIENT_ID", "not-uuid"), ("WORKSHOP_APP_SP_ID", ""), ("WORKSHOP_APP_SP_ID", "not-numeric")])
def test_model_canary_requires_exact_app_ids_before_network(client, as_admin, canary_runtime, monkeypatch, key, value):
    monkeypatch.setenv(key, value)
    result = client.post(CANARY_ENDPOINT, headers=ALICE, json={"role": "driver"}).json()
    assert result["error_classification"] == "app_identity_configuration_invalid" and canary_runtime.calls == []


def test_model_canary_does_not_fall_back_to_emergency_pat(client, as_admin, canary_runtime, monkeypatch):
    from server import credentials
    monkeypatch.setattr(credentials, "app_identity_bearer", lambda: None)
    result = client.post(CANARY_ENDPOINT, headers=ALICE, json={"role": "driver"}).json()
    assert result["error_classification"] == "app_oauth_identity_unavailable" and canary_runtime.calls == []


@pytest.mark.parametrize("status,classification", [(401, "unauthenticated"), (403, "permission_denied"), (404, "model_or_wire_unavailable"),
                                                  (429, "rate_limited"), (503, "provider_error"), (302, "request_rejected")])
def test_model_canary_preserves_status_with_bounded_error_evidence_without_retry(client, as_admin, canary_runtime, status, classification):
    canary_runtime.provider_status = status
    response = client.post(CANARY_ENDPOINT, headers=ALICE, json={"role": "driver"})
    assert response.json()["http_status"] == status and response.json()["error_classification"] == classification
    assert not response.json()["invocation_verified"] and len(canary_runtime.calls) == 2


@pytest.mark.parametrize("body", [{"error": {"message": "private-secret"}}, {},
                                  {"type": "message", "role": "assistant", "usage": {"input_tokens": True, "output_tokens": 1}},
                                  {"type": "message", "role": "assistant", "usage": {"input_tokens": 5, "output_tokens": 0}}])
def test_model_canary_http_200_does_not_prove_invocation(client, as_admin, canary_runtime, body):
    canary_runtime.provider_body = body
    response = client.post(CANARY_ENDPOINT, headers=ALICE, json={"role": "driver"})
    assert response.json()["http_status"] == 200 and not response.json()["invocation_verified"]
    assert "private-secret" not in response.text


def test_model_canary_thinking_only_proves_invocation_but_no_usable_answer(client, as_admin, canary_runtime):
    canary_runtime.provider_body = {"type": "message", "role": "assistant", "stop_reason": "max_tokens",
                                   "content": [{"type": "thinking", "thinking": "private-secret"}],
                                   "usage": {"input_tokens": 5, "output_tokens": 32}}
    response = client.post(CANARY_ENDPOINT, headers=ALICE, json={"role": "driver"})
    assert response.json()["invocation_verified"] and not response.json()["ok_answer_verified"]
    assert "private-secret" not in response.text


def test_model_canary_timeout_and_exception_strings_are_not_exposed(client, as_admin, canary_runtime):
    from server import evaluation
    canary_runtime.provider_error = evaluation.requests.Timeout("private-secret Authorization response prose")
    response = client.post(CANARY_ENDPOINT, headers=ALICE, json={"role": "driver"})
    assert response.json()["error_classification"] == "timeout" and not response.json()["invocation_verified"]
    assert "private-secret" not in response.text and len(canary_runtime.calls) == 2


def test_model_canary_outer_deadline_bounds_stalled_auth_worker(client, as_admin, canary_runtime, monkeypatch):
    from server import evaluation
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    def worker(role, binding, result, deadline):
        entered.set()
        release.wait(.5)
        finished.set()
        return dict(result, invocation_verified=True)
    monkeypatch.setattr(evaluation, "CANARY_TIMEOUT_SECONDS", .02)
    monkeypatch.setattr(evaluation, "_canary_run", worker)
    try:
        response = client.post(CANARY_ENDPOINT, headers=ALICE, json={"role": "driver"})
        assert entered.is_set() and not finished.is_set()
        assert response.json()["error_classification"] == "timeout" and not response.json()["invocation_verified"]
    finally:
        release.set()
        assert finished.wait(.5)


def test_model_canary_refuses_external_gateway_before_bearer_post(client, as_admin, canary_runtime, monkeypatch):
    from server import cli_config
    monkeypatch.setattr(cli_config, "gateway_host", lambda: "https://outside.example/ai-gateway")
    result = client.post(CANARY_ENDPOINT, headers=ALICE, json={"role": "driver"}).json()
    assert result["error_classification"] == "model_configuration_invalid" and not result["invocation_verified"]
    assert len(canary_runtime.calls) == 1


def test_model_canary_honors_current_wizard_override(client, as_admin, canary_runtime, monkeypatch):
    from server import wizard_llm
    monkeypatch.setattr(wizard_llm, "_model_override", "system.ai.gpt-5-6-luna")
    result = client.post(CANARY_ENDPOINT, headers=ALICE, json={"role": "wizard"}).json()
    assert result["model"] == "system.ai.gpt-5-6-luna" and result["invocation_verified"]


@pytest.mark.parametrize("gateway", ["https://different-workspace.cloud.databricks.com/ai-gateway",
                                     "https://999.ai-gateway.cloud.databricks.com",
                                     "https://test.cloud.databricks.com/alternate-route"])
def test_model_canary_binds_gateway_to_exact_runtime_workspace(client, as_admin, canary_runtime, monkeypatch, gateway):
    from server import cli_config
    monkeypatch.setenv("DATABRICKS_WORKSPACE_ID", "123")
    monkeypatch.setattr(cli_config, "gateway_host", lambda: gateway)
    result = client.post(CANARY_ENDPOINT, headers=ALICE, json={"role": "driver"}).json()
    assert result["error_classification"] == "model_configuration_invalid" and len(canary_runtime.calls) == 1


def test_model_canary_accepts_own_dedicated_gateway(client, as_admin, canary_runtime, monkeypatch):
    from server import cli_config
    monkeypatch.setenv("DATABRICKS_WORKSPACE_ID", "123")
    monkeypatch.setattr(cli_config, "gateway_host", lambda: "https://123.ai-gateway.cloud.databricks.com")
    result = client.post(CANARY_ENDPOINT, headers=ALICE, json={"role": "driver"}).json()
    assert result["invocation_verified"] and len(canary_runtime.calls) == 2


def test_model_canary_rejects_conflicting_identity_aliases(client, as_admin, canary_runtime):
    canary_runtime.identity["application_id"] = "22222222-2222-4222-8222-222222222222"
    result = client.post(CANARY_ENDPOINT, headers=ALICE, json={"role": "driver"}).json()
    assert result["error_classification"] == "app_identity_mismatch" and len(canary_runtime.calls) == 1


def test_model_canary_records_safe_provider_alias_without_claiming_match(client, as_admin, canary_runtime):
    canary_runtime.provider_body = {"model": "claude-sonnet-4-6-20261001"}
    result = client.post(CANARY_ENDPOINT, headers=ALICE, json={"role": "driver"}).json()
    assert result["response_model"] == "claude-sonnet-4-6-20261001"
    assert result["error_classification"] == "response_model_mismatch" and not result["invocation_verified"]


@pytest.mark.parametrize("returned,verified", [("gpt-6.1-sol", True), ("gpt-6.1-terra", False)])
def test_quality_sol_alias_is_exact(returned, verified):
    from server.evaluation import _canary_response, CanaryUnverified
    body = {"object": "response", "status": "completed", "model": returned,
        "output": [{"type": "message", "role": "assistant",
                    "content": [{"type": "output_text", "text": "OK"}]}],
        "usage": {"input_tokens": 5, "output_tokens": 1}}
    if verified:
        assert _canary_response(body, "codex", "system.ai.gpt-6-1-sol")["ok_answer_verified"]
    else:
        with pytest.raises(CanaryUnverified, match="response_model_mismatch"):
            _canary_response(body, "codex", "system.ai.gpt-6-1-sol")


@pytest.mark.parametrize("returned,verified", [
    ("gpt-5.4-mini-2026-03-17", True),
    ("gpt-5.4-mini-2026-03-18", False),
    ("gpt-5.4-2026-03-17", False),
    ("gpt-5.4-nano-2026-03-17", False),
])
def test_preferred_wizard_mini_alias_is_exact(returned, verified):
    from server.evaluation import _canary_response, CanaryUnverified
    body = {"model": returned, "choices": [{"message": {"role": "assistant", "content": "OK"},
            "finish_reason": "stop"}], "usage": {"prompt_tokens": 5, "completion_tokens": 1}}
    if verified:
        assert _canary_response(body, "wizard", "system.ai.gpt-5-4-mini")["ok_answer_verified"]
    else:
        with pytest.raises(CanaryUnverified, match="response_model_mismatch"):
            _canary_response(body, "wizard", "system.ai.gpt-5-4-mini")


def test_model_canary_does_not_echo_arbitrary_response_model_prose(client, as_admin, canary_runtime):
    canary_runtime.provider_body = {"model": "private-secret bearer response prose"}
    response = client.post(CANARY_ENDPOINT, headers=ALICE, json={"role": "driver"})
    assert response.json()["response_model"] is None and "private-secret" not in response.text
