from __future__ import annotations

from server import friction_scanner, telemetry


def test_friction_scanner_classifies_and_deduplicates_tool_errors(monkeypatch):
    emitted: list[tuple[str, str, dict]] = []

    def fake_emit(event_type: str, attendee: str, payload: dict) -> bool:
        emitted.append((event_type, attendee, payload))
        return True

    monkeypatch.setattr(telemetry, "emit", fake_emit)
    friction_scanner.reset_for_tests()

    lines = [
        "API Error: 400 {'error': {'message': 'web_search is not supported for this model'}}",
        "Permission denied by hook: policy_eval_relay_failure",
        "AnalysisException: [INSUFFICIENT_PERMISSIONS] User does not have USE SCHEMA on Schema 'workshop_demo_data.energy_utilities_legacy'",
    ]
    out1 = friction_scanner.scan_error_lines("alice@example.com", lines, agent="claude")
    assert {item["code"] for item in out1} == {
        "web_search_unsupported",
        "omnigent_hook_denied",
        "uc_permission_denied",
    }
    assert len(emitted) == 3

    # Re-scanning the exact same lines emits no new events
    out2 = friction_scanner.scan_error_lines("alice@example.com", lines, agent="claude")
    assert out2 == []
    assert len(emitted) == 3

    # Adding one more occurrence emits only the delta
    lines.append("Permission denied by hook: policy_eval_relay_failure")
    out3 = friction_scanner.scan_error_lines("alice@example.com", lines, agent="claude")
    assert len(out3) == 1
    assert out3[0]["code"] == "omnigent_hook_denied"
    assert out3[0]["count"] == 1
