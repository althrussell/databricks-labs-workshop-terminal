"""The smoke matrix, and the verdict it publishes.

A model that answers a question is not a model that can be an agent. These tests
pin the difference: prose where a tool call was required is a failure, a patch
that does not name the file is a failure, and a failure has to be able to drop
the model from what attendees are offered without anyone shipping code.
"""

from __future__ import annotations

import json

import pytest

from scripts import smoke_models
from server import models


def _reply(content: str = "", tool: str = "", arguments: dict | None = None) -> dict:
    message: dict = {"content": content}
    if tool:
        message["tool_calls"] = [
            {
                "type": "function",
                "function": {"name": tool, "arguments": json.dumps(arguments or {})},
            }
        ]
    return {"choices": [{"message": message}]}


_GOOD_PATCH = (
    "*** Begin Patch\n*** Update File: app.py\n@@\n"
    '-print("hello")\n+print("goodbye")\n*** End Patch'
)


def test_a_model_that_answers_passes_the_turn():
    ok, _detail = smoke_models.judge_turn(_reply("A Delta table is a Parquet table."))
    assert ok is True


def test_an_empty_answer_is_not_a_turn():
    ok, detail = smoke_models.judge_turn(_reply(""))
    assert ok is False
    assert "empty" in detail


def test_prose_where_a_tool_call_was_required_fails():
    """The failure mode that matters: helpful-sounding, useless to an agent."""
    ok, detail = smoke_models.judge_tool_call(
        _reply("I don't have access to live weather data.")
    )
    assert ok is False
    assert "prose" in detail


def test_calling_the_tool_with_readable_arguments_passes():
    ok, _detail = smoke_models.judge_tool_call(
        _reply(tool="get_weather", arguments={"city": "Paris"})
    )
    assert ok is True


def test_a_tool_call_with_unusable_arguments_fails():
    ok, detail = smoke_models.judge_tool_call(
        _reply(tool="get_weather", arguments={"location": "somewhere"})
    )
    assert ok is False
    assert "arguments" in detail


def test_a_well_formed_patch_passes_the_file_edit():
    ok, _detail = smoke_models.judge_file_edit(
        _reply(tool="apply_patch", arguments={"input": _GOOD_PATCH})
    )
    assert ok is True


@pytest.mark.parametrize(
    "patch",
    [
        '*** Begin Patch\n*** Update File: other.py\n+print("goodbye")\n*** End Patch',
        'print("goodbye")',
    ],
)
def test_a_patch_that_would_not_apply_fails(patch):
    ok, detail = smoke_models.judge_file_edit(
        _reply(tool="apply_patch", arguments={"input": patch})
    )
    assert ok is False
    assert "missing" in detail


def test_an_unreachable_model_is_a_verdict_not_a_crash():
    def explode(url, token, payload):
        raise RuntimeError("HTTP 404: endpoint not found")

    result = smoke_models.run_check("https://ws", "tok", "gone", "turn", post=explode)

    assert result["ok"] is False
    assert "404" in result["detail"]


def test_the_matrix_publishes_the_line_that_drops_a_failure():
    def answer(url, token, payload):
        if payload["model"] == "bad" and "tools" in payload:
            return _reply("I cannot do that.")
        if "tools" not in payload:
            return _reply("Sure.")
        if payload["tools"][0]["function"]["name"] == "get_weather":
            return _reply(tool="get_weather", arguments={"city": "Paris"})
        return _reply(tool="apply_patch", arguments={"input": _GOOD_PATCH})

    results = smoke_models.smoke(
        "https://ws", "tok", {"good": "good", "bad": "bad"}, post=answer
    )
    rendered = smoke_models.render(results)

    assert [row["supported"] for row in results] == [True, False]
    assert "WORKSHOP_CODEX_COMPARE=good" in rendered
    # The operator has to see *why*, or the only move left is dropping everything.
    assert "bad/tool_call" in rendered


def test_nothing_passing_publishes_a_refusal_rather_than_an_empty_set():
    def refuse(url, token, payload):
        return _reply("")

    rendered = smoke_models.render(
        smoke_models.smoke("https://ws", "tok", {"glm": "databricks-glm-5-2"}, post=refuse)
    )

    assert "do not run the comparison" in rendered


def test_the_published_verdict_drops_a_model_from_what_attendees_get(monkeypatch):
    """The whole point of measuring: a values change, not a release."""
    monkeypatch.setenv("WORKSHOP_CODEX_COMPARE", "glm,gemini")

    assert set(models.comparison_models()) == {"glm", "gemini"}


def test_an_unmeasured_deployment_offers_everything_it_serves(monkeypatch):
    monkeypatch.delenv("WORKSHOP_CODEX_COMPARE", raising=False)

    assert models.comparison_supported() is None
    assert set(models.comparison_models()) == {"glm", "kimi", "gemini"}


def test_the_config_endpoint_publishes_what_codex_was_configured_with(
    client, monkeypatch
):
    from server import main

    monkeypatch.setenv("WORKSHOP_CODEX_COMPARE", "kimi")

    published = main.model_comparison()

    assert published == [
        {
            "profile": "kimi",
            "model": "databricks-kimi-k3",
            "label": "Kimi K3",
            "command": "codex --profile kimi",
        }
    ]
