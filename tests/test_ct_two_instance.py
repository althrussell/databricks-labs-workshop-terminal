import base64
import copy
import hashlib
import json

import pytest
import requests
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from scripts import ct_two_instance


KEY = b"test-signing-key-with-enough-entropy"
CT_KEY_ID = "ct-key-2026-07"
CT_PRIVATE_KEY = Ed25519PrivateKey.generate()
CT_PUBLIC_KEY = CT_PRIVATE_KEY.public_key()
CT_PUBLIC_KEY_BYTES = CT_PUBLIC_KEY.public_bytes(
    encoding=serialization.Encoding.Raw,
    format=serialization.PublicFormat.Raw,
)
CT_PUBLIC_KEY_B64 = base64.b64encode(CT_PUBLIC_KEY_BYTES).decode()


def _canonical(value):
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()


def _ct_signed(
    report,
    phase,
    payload,
    *,
    private_key=CT_PRIVATE_KEY,
    key_id=CT_KEY_ID,
    nonce=None,
    attested_at="2026-07-21T03:00:00Z",
):
    nonce = nonce or f"{phase}-nonce"
    message = {
        "schema_version": report["header"]["schema_version"],
        "report_id": report["header"]["report_id"],
        "run_id": report["header"]["run_id"],
        "inventory": report["header"]["apps"],
        "phase": phase,
        "attested_at": attested_at,
        "nonce": nonce,
        "payload_hash": hashlib.sha256(_canonical(payload)).hexdigest(),
    }
    signature = base64.b64encode(private_key.sign(_canonical(message))).decode()
    return {
        **copy.deepcopy(payload),
        "ct_attestation": {
            "key_id": key_id,
            "algorithm": "Ed25519",
            "attested_at": attested_at,
            "nonce": nonce,
            "signature": signature,
        },
    }


class Response:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


def _apps(*, runtime_tokens=False):
    apps = [
        {
            "name": "app-b",
            "url": "https://b.example",
            "workspace_host": "https://workspace-b.example",
        },
        {
            "name": "app-a",
            "url": "https://a.example",
            "workspace_host": "https://workspace-a.example",
        },
    ]
    if runtime_tokens:
        for app in apps:
            suffix = app["name"][-1]
            app["token"] = f"admin-value-{suffix}"
            app["attendee_token"] = f"attendee-value-{suffix}"
    return apps


def _report():
    return ct_two_instance.new_report(
        _apps(),
        report_id="report-1",
        run_id="event-run-1",
        signing_key=KEY,
        ct_key_id=CT_KEY_ID,
        now=lambda: 0,
    )


def _ready_checks(value=True):
    return {
        check: value
        for check in ct_two_instance.REQUIRED_READY_CHECKS
    }


def _late_grant():
    return {
        "apps": [
            {
                "name": name,
                "before_state": "unhealthy",
                "after_state": "rotating",
                "credential_source": "app_identity_oauth",
                "workshop_pat_present": False,
            }
            for name in ("app-a", "app-b")
        ]
    }


def _baseline(manifest="same", *, ownership=True):
    return {
        "apps": [
            {
                "name": name,
                "ready": True,
                "ready_checks": _ready_checks(),
                "setup_ready": True,
                "prewarm_reusable": True,
                "readyz_manifest": {"digest": manifest},
                "setup_manifest": {"digest": manifest},
                "prewarm_manifest": {"digest": manifest},
                "credential": {
                    "state": "rotating",
                    "source": "app_identity_oauth",
                    "workshop_pat_present": False,
                },
                "agents_ready": True,
                "session": {"created": True, "listed": True, "closed": True},
                "restart_marker": f"{name}-marker",
                "resource_verification": {
                    "catalog": "event_catalog",
                    "catalog_owner": ownership,
                    "all_privileges": True,
                    "attendee_access": True,
                    "attendee_identity_bound": True,
                    "resources": [
                        {
                            "type": "jobs",
                            "id": f"{name}-job",
                            "state": "handed_off",
                            "permission_level": "IS_OWNER",
                        }
                    ],
                },
            }
            for name in ("app-a", "app-b")
        ]
    }


def _restart():
    return {
        "apps": [
            {"name": name, "ready": True, "restart_ghost": True}
            for name in ("app-a", "app-b")
        ]
    }


def _lunch():
    return {
        "attestation": {
            "pause_started_at": "1970-01-01T01:00:00Z",
            "pause_completed_at": "1970-01-01T02:30:00Z",
            "closed_laptop_pause_seconds": 5400,
            "wifi_reconnected": True,
        },
        "apps": [
            {"name": name, "ready": True, "obo_fresh": True}
            for name in ("app-a", "app-b")
        ],
    }


def _teardown():
    return {
        "attestation": {
            "control_tower_run_id": "ct-run-123",
            "completed_at": "2026-07-21T03:00:00Z",
        },
        "apps": [
            {
                "name": name,
                "deployment_id": f"deployment-{name}",
                "app_id": f"app-id-{name}",
                "workspace_id": f"workspace-{name}",
                "catalog": f"catalog_{name[-1]}",
                "receipts": [
                    {
                        "receipt_id": f"{name}-{resource_type}-receipt",
                        "resource_type": resource_type,
                        "resource_id": resource_id,
                        "action": action,
                        "status": "succeeded",
                        "completed_at": "2026-07-21T03:00:00Z",
                        "provenance": "control_tower",
                    }
                    for resource_type, resource_id, action in (
                        ("deployment", f"deployment-{name}", "teardown_completed"),
                        ("app", f"app-id-{name}", "deleted"),
                        ("workspace", f"workspace-{name}", "deleted"),
                        ("catalog", f"catalog_{name[-1]}", "deleted"),
                        ("credential", f"credential-{name}", "revoked"),
                    )
                ],
            }
            for name in ("app-a", "app-b")
        ],
    }


def _record(report, phase, evidence, provenance, timestamp):
    if phase in {"late_grant", "teardown"} and "ct_attestation" not in evidence:
        evidence = _ct_signed(
            report,
            phase,
            evidence,
            nonce=f"{phase}-{timestamp}",
        )
    return ct_two_instance.record_phase(
        report,
        phase,
        evidence,
        signing_key=KEY,
        provenance=provenance,
        ct_public_key=CT_PUBLIC_KEY,
        ct_key_id=CT_KEY_ID,
        now=lambda: timestamp,
    )


def _evaluate(report, *, ct_public_key=CT_PUBLIC_KEY, ct_key_id=CT_KEY_ID):
    return ct_two_instance.evaluate_report(
        report,
        signing_key=KEY,
        ct_public_key=ct_public_key,
        ct_key_id=ct_key_id,
    )


def _probe(report, phase, evidence, timestamp):
    return ct_two_instance.record_probe_attempt(
        report,
        phase,
        evidence,
        signing_key=KEY,
        ct_public_key=CT_PUBLIC_KEY,
        ct_key_id=CT_KEY_ID,
        now=lambda: timestamp,
    )


def _successful_report():
    report = _report()
    report = _record(report, "late_grant", _late_grant(), "ct_attestation", 10)
    report = _record(report, "baseline", _baseline(), "automated_collector", 20)
    report = _record(report, "restart", _restart(), "automated_collector", 30)
    report = _record(report, "lunch_resume", _lunch(), "operator_attestation", 9_000)
    return _record(report, "teardown", _teardown(), "ct_attestation", 11_000)


def test_real_credential_status_shape_requires_app_identity_oauth():
    report = _report()
    report = _record(report, "late_grant", _late_grant(), "ct_attestation", 10)
    baseline = _baseline()
    baseline["apps"][0]["credential"]["source"] = "app-identity"
    report = _record(report, "baseline", baseline, "automated_collector", 20)

    result = _evaluate(report)

    assert result["gates"]["baseline"]["credentials_rotating_no_pat"] is False


@pytest.mark.parametrize("count", [0, 1, 3])
def test_report_enforces_exactly_two_instances(count):
    apps = _apps()[:count] if count < 3 else _apps() + [{
        "name": "app-c",
        "url": "https://c.example",
    }]
    with pytest.raises(ValueError, match="exactly two"):
        ct_two_instance.new_report(
            apps,
            report_id="count-report",
            run_id="count-test",
            signing_key=KEY,
            ct_key_id=CT_KEY_ID,
        )


@pytest.mark.parametrize("field,value", [
    ("run_id", "../unsafe"),
    ("name", "app one"),
])
def test_report_sanitizes_identifiers(field, value):
    apps = _apps()
    run_id = "safe-run"
    if field == "run_id":
        run_id = value
    else:
        apps[0]["name"] = value
    with pytest.raises(ValueError, match="invalid identifier"):
        ct_two_instance.new_report(
            apps,
            report_id="safe-report",
            run_id=run_id,
            signing_key=KEY,
            ct_key_id=CT_KEY_ID,
        )


def test_phase_order_is_enforced_and_replacement_is_rejected():
    report = _report()
    with pytest.raises(ValueError, match="expected late_grant"):
        _record(report, "baseline", _baseline(), "automated_collector", 10)
    report = _record(report, "late_grant", _late_grant(), "ct_attestation", 10)
    with pytest.raises(ValueError, match="expected baseline"):
        _record(report, "late_grant", _late_grant(), "ct_attestation", 11)


def test_failed_probe_attempt_is_retained_and_successful_retry_seals_phase():
    report = _record(
        _report(),
        "late_grant",
        _late_grant(),
        "ct_attestation",
        10,
    )
    failed = _baseline()
    for app in failed["apps"]:
        app["probe_status"] = "request_failed"
        app["ready"] = False
        app["restart_marker"] = None
    report = _probe(report, "baseline", failed, 20)

    assert [event["event_type"] for event in report["checkpoints"]] == [
        "phase_completion",
        "probe_attempt",
    ]
    assert report["checkpoints"][-1]["outcome"] == "failed"
    first_attempt_hash = report["checkpoints"][-1]["hash"]
    incomplete = _evaluate(report)
    assert incomplete["status"] == "incomplete"
    assert incomplete["attempts"]["baseline"] == ["failed"]

    report = _probe(report, "baseline", _baseline(), 30)

    assert report["checkpoints"][1]["hash"] == first_attempt_hash
    assert [event["event_type"] for event in report["checkpoints"][-2:]] == [
        "probe_attempt",
        "phase_completion",
    ]
    assert _evaluate(report)["gates"]["baseline"]["both_ready"] is True
    with pytest.raises(ValueError, match="expected restart"):
        _probe(report, "baseline", _baseline(), 40)


@pytest.mark.parametrize("phase", ["restart", "lunch_resume"])
def test_restart_and_lunch_network_attempts_can_retry_without_erasure(phase):
    report = _record(_report(), "late_grant", _late_grant(), "ct_attestation", 10)
    report = _record(report, "baseline", _baseline(), "automated_collector", 20)
    if phase == "lunch_resume":
        report = _record(report, "restart", _restart(), "automated_collector", 30)
        failed = _lunch()
        for app in failed["apps"]:
            app["ready"] = False
    else:
        failed = _restart()
        for app in failed["apps"]:
            app["ready"] = False
            app["restart_ghost"] = False
    report = _probe(report, phase, failed, 9_000)
    failed_hash = report["checkpoints"][-1]["hash"]
    successful = _lunch() if phase == "lunch_resume" else _restart()
    report = _probe(report, phase, successful, 10_000)

    events = [
        event
        for event in report["checkpoints"]
        if event["phase"] == phase
    ]
    assert events[0]["hash"] == failed_hash
    assert [event["outcome"] for event in events if event["event_type"] == "probe_attempt"] == [
        "failed",
        "passed",
    ]
    assert events[-1]["event_type"] == "phase_completion"


def test_tampering_with_retained_failed_attempt_breaks_chain():
    report = _record(_report(), "late_grant", _late_grant(), "ct_attestation", 10)
    failed = _baseline()
    failed["apps"][0]["ready"] = False
    report = _probe(report, "baseline", failed, 20)
    report["checkpoints"][-1]["evidence"]["apps"][0]["ready"] = True

    assert _evaluate(report)["status"] == "tampered"


def test_each_checkpoint_is_timestamped_hash_chained_and_signed():
    report = _successful_report()

    assert report["header"]["signature"]
    assert [item["phase"] for item in report["checkpoints"]] == list(
        ct_two_instance.REQUIRED_PHASES
    )
    assert all(item["recorded_at"] and item["hash"] and item["signature"]
               for item in report["checkpoints"])
    assert report["checkpoints"][0]["previous_hash"] == report["header"]["hash"]
    assert report["checkpoints"][1]["previous_hash"] == report["checkpoints"][0]["hash"]
    assert "test-signing-key" not in json.dumps(report)


def test_checkpoint_timestamp_cannot_precede_previous_checkpoint():
    report = _record(
        _report(),
        "late_grant",
        _late_grant(),
        "ct_attestation",
        20,
    )
    with pytest.raises(ValueError, match="timestamp order"):
        _record(report, "baseline", _baseline(), "automated_collector", 10)


@pytest.mark.parametrize("tamper", [
    lambda report: report["checkpoints"][0]["evidence"]["apps"][0].update(
        {"after_state": "unhealthy"}
    ),
    lambda report: report["checkpoints"][1].update({"previous_hash": "0" * 64}),
    lambda report: report["header"].update({"run_id": "other-run"}),
    lambda report: report["checkpoints"].reverse(),
])
def test_evaluate_rejects_tampered_or_reordered_report(tamper):
    report = _successful_report()
    tamper(report)

    result = _evaluate(report)

    assert result["status"] == "tampered"
    assert result["exit_code"] == 1


def test_evaluate_rejects_wrong_hmac_key():
    result = ct_two_instance.evaluate_report(
        _successful_report(),
        signing_key=b"different-signing-key",
        ct_public_key=CT_PUBLIC_KEY,
        ct_key_id=CT_KEY_ID,
    )

    assert result["status"] == "tampered"


@pytest.mark.parametrize("case", ["missing", "wrong_key", "wrong_key_id"])
def test_late_grant_rejects_missing_or_wrong_ct_signature(case):
    report = _report()
    evidence = _late_grant()
    if case == "wrong_key":
        evidence = _ct_signed(
            report,
            "late_grant",
            evidence,
            private_key=Ed25519PrivateKey.generate(),
        )
    elif case == "wrong_key_id":
        evidence = _ct_signed(
            report,
            "late_grant",
            evidence,
            key_id="wrong-key-id",
        )
    with pytest.raises(ValueError, match="CT attestation"):
        ct_two_instance.record_phase(
            report,
            "late_grant",
            evidence,
            signing_key=KEY,
            provenance="ct_attestation",
            ct_public_key=CT_PUBLIC_KEY,
            ct_key_id=CT_KEY_ID,
            now=lambda: 10,
        )


def test_evaluate_rejects_wrong_ct_verification_key():
    wrong_public_key = Ed25519PrivateKey.generate().public_key()
    result = _evaluate(_successful_report(), ct_public_key=wrong_public_key)

    assert result["status"] == "tampered"
    assert result["error"] == "ct_attestation"


def test_validator_preserves_external_ct_signature_without_generating_one():
    initial = _report()
    evidence = _ct_signed(initial, "late_grant", _late_grant())
    external_signature = evidence["ct_attestation"]["signature"]

    report = _record(initial, "late_grant", evidence, "ct_attestation", 10)

    stored = report["checkpoints"][0]["evidence"]["ct_attestation"]
    assert stored["signature"] == external_signature
    assert report["checkpoints"][0]["signature"] != external_signature


def test_validator_source_has_no_ct_private_key_or_signing_path():
    source = open(ct_two_instance.__file__, encoding="utf-8").read()

    assert "Ed25519PrivateKey" not in source
    assert "CT_ATTESTATION_KEY=" not in source
    assert ".sign(" not in source


@pytest.mark.parametrize("change", ["report", "run", "inventory", "phase"])
def test_ct_signature_rejects_cross_context_replay(change):
    original = _report()
    signed = _ct_signed(original, "late_grant", _late_grant(), nonce="replay-nonce")
    if change == "report":
        target = ct_two_instance.new_report(
            _apps(),
            report_id="report-2",
            run_id="event-run-1",
            signing_key=KEY,
            ct_key_id=CT_KEY_ID,
            now=lambda: 0,
        )
        phase = "late_grant"
    elif change == "run":
        target = ct_two_instance.new_report(
            _apps(),
            report_id="report-1",
            run_id="event-run-2",
            signing_key=KEY,
            ct_key_id=CT_KEY_ID,
            now=lambda: 0,
        )
        phase = "late_grant"
    elif change == "inventory":
        target = ct_two_instance.new_report(
            [
                    {
                        "name": "app-a",
                        "url": "https://different.example",
                        "workspace_host": "https://workspace-a.example",
                    },
                    {
                        "name": "app-b",
                        "url": "https://b.example",
                        "workspace_host": "https://workspace-b.example",
                    },
            ],
            report_id="report-1",
            run_id="event-run-1",
            signing_key=KEY,
            ct_key_id=CT_KEY_ID,
            now=lambda: 0,
        )
        phase = "late_grant"
    else:
        target = _record(
            original,
            "late_grant",
            _ct_signed(original, "late_grant", _late_grant(), nonce="valid-late"),
            "ct_attestation",
            10,
        )
        target = _record(target, "baseline", _baseline(), "automated_collector", 20)
        target = _record(target, "restart", _restart(), "automated_collector", 30)
        target = _record(
            target,
            "lunch_resume",
            _lunch(),
            "operator_attestation",
            10_000,
        )
        phase = "teardown"
    with pytest.raises(ValueError, match="CT attestation"):
        ct_two_instance.record_phase(
            target,
            phase,
            signed,
            signing_key=KEY,
            provenance="ct_attestation",
            ct_public_key=CT_PUBLIC_KEY,
            ct_key_id=CT_KEY_ID,
            now=lambda: 10,
        )


def test_duplicate_ct_nonce_is_rejected():
    report = _report()
    late = _ct_signed(report, "late_grant", _late_grant(), nonce="same-nonce")
    report = _record(report, "late_grant", late, "ct_attestation", 10)
    report = _record(report, "baseline", _baseline(), "automated_collector", 20)
    report = _record(report, "restart", _restart(), "automated_collector", 30)
    report = _record(report, "lunch_resume", _lunch(), "operator_attestation", 10_000)
    teardown = _ct_signed(
        report,
        "teardown",
        _teardown(),
        nonce="same-nonce",
    )

    with pytest.raises(ValueError, match="nonce"):
        _record(report, "teardown", teardown, "ct_attestation", 11_000)


def test_ct_public_key_loads_from_base64_env_or_file(tmp_path):
    key_file = tmp_path / "ct-public-key.txt"
    key_file.write_text(CT_PUBLIC_KEY_B64)

    from_env = ct_two_instance.load_ct_public_key({
        "CT_ATTESTATION_PUBLIC_KEY": CT_PUBLIC_KEY_B64,
    })
    from_file = ct_two_instance.load_ct_public_key({
        "CT_ATTESTATION_PUBLIC_KEY_FILE": str(key_file),
    })

    assert from_env.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    ) == CT_PUBLIC_KEY_BYTES
    assert from_file.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    ) == CT_PUBLIC_KEY_BYTES


def test_teardown_rejects_missing_ct_signature():
    report = _record(_report(), "late_grant", _late_grant(), "ct_attestation", 10)
    report = _record(report, "baseline", _baseline(), "automated_collector", 20)
    report = _record(report, "restart", _restart(), "automated_collector", 30)
    report = _record(report, "lunch_resume", _lunch(), "operator_attestation", 10_000)

    with pytest.raises(ValueError, match="CT attestation"):
        ct_two_instance.record_phase(
            report,
            "teardown",
            _teardown(),
            signing_key=KEY,
            provenance="ct_attestation",
            ct_public_key=CT_PUBLIC_KEY,
            ct_key_id=CT_KEY_ID,
            now=lambda: 11_000,
        )


def test_failed_checkpoint_requires_a_new_run():
    report = _report()
    late = _late_grant()
    late["apps"][0]["after_state"] = "unhealthy"
    late = _ct_signed(report, "late_grant", late)
    report = _record(report, "late_grant", late, "ct_attestation", 10)

    with pytest.raises(ValueError, match="new run"):
        _record(report, "baseline", _baseline(), "automated_collector", 20)


def test_cli_never_runs_collector_after_failed_checkpoint(
    monkeypatch,
    tmp_path,
):
    report = _report()
    late = _late_grant()
    late["apps"][0]["after_state"] = "unhealthy"
    late = _ct_signed(report, "late_grant", late)
    report = _record(report, "late_grant", late, "ct_attestation", 10)
    report_path = tmp_path / "report.json"
    report_path.write_text(json.dumps(report))
    inventory_path = tmp_path / "inventory.json"
    inventory_path.write_text(json.dumps({
        "apps": [
            {
                "name": "app-a",
                "url": "https://a.example",
                "workspace_host": "https://workspace-a.example",
                "token_env": "ADMIN_A",
                "attendee_token_env": "ATTENDEE_A",
            },
            {
                "name": "app-b",
                "url": "https://b.example",
                "workspace_host": "https://workspace-b.example",
                "token_env": "ADMIN_B",
                "attendee_token_env": "ATTENDEE_B",
            },
        ]
    }))
    manifest_path = tmp_path / "resources.json"
    manifest_path.write_text(json.dumps(_resource_manifest()))
    monkeypatch.setenv("CT_VALIDATION_HMAC_KEY", KEY.decode())
    monkeypatch.setenv("CT_ATTESTATION_PUBLIC_KEY", CT_PUBLIC_KEY_B64)
    monkeypatch.setenv("CT_ATTESTATION_KEY_ID", CT_KEY_ID)
    monkeypatch.setenv("ADMIN_A", "admin-a")
    monkeypatch.setenv("ATTENDEE_A", "attendee-a")
    monkeypatch.setenv("ADMIN_B", "admin-b")
    monkeypatch.setenv("ATTENDEE_B", "attendee-b")
    called = False

    def collector(*args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError("collector must not run")

    monkeypatch.setattr(ct_two_instance, "collect_baseline", collector)
    monkeypatch.setattr(
        "sys.argv",
        [
            "ct_two_instance.py",
            "--report",
            str(report_path),
            "probe-baseline",
            "--inventory",
            str(inventory_path),
            "--resource-manifest",
            str(manifest_path),
        ],
    )

    assert ct_two_instance.main() == 2
    assert called is False


def test_manual_evidence_requires_attestation_provenance():
    with pytest.raises(ValueError, match="provenance"):
        _record(_report(), "late_grant", _late_grant(), "automated_collector", 10)


def test_inventory_requires_distinct_env_names_and_values():
    inventory = {
        "apps": [
            {
                "name": "app-a",
                "url": "https://a.example",
                "token_env": "ADMIN_A",
                "attendee_token_env": "ATTENDEE_A",
            },
            {
                "name": "app-b",
                "url": "https://b.example",
                "token_env": "ADMIN_B",
                "attendee_token_env": "ATTENDEE_B",
            },
        ]
    }
    with pytest.raises(ValueError, match="distinct credential values"):
        ct_two_instance.apps_from_inventory(
            inventory,
            environ={
                "ADMIN_A": "same-value",
                "ATTENDEE_A": "same-value",
                "ADMIN_B": "admin-b",
                "ATTENDEE_B": "attendee-b",
            },
        )
    inventory["apps"][0]["attendee_token_env"] = "ADMIN_A"
    with pytest.raises(ValueError, match="distinct environment variables"):
        ct_two_instance.apps_from_inventory(
            inventory,
            environ={"ADMIN_A": "value", "ADMIN_B": "b", "ATTENDEE_B": "c"},
        )


def test_inventory_rejects_cross_instance_credential_reuse():
    inventory = {
        "apps": [
            {
                "name": "app-a",
                "url": "https://a.example",
                "workspace_host": "https://workspace-a.example",
                "token_env": "ADMIN_A",
                "attendee_token_env": "ATTENDEE_A",
            },
            {
                "name": "app-b",
                "url": "https://b.example",
                "workspace_host": "https://workspace-b.example",
                "token_env": "ADMIN_A",
                "attendee_token_env": "ATTENDEE_B",
            },
        ]
    }

    with pytest.raises(ValueError, match="globally distinct"):
        ct_two_instance.apps_from_inventory(
            inventory,
            environ={
                "ADMIN_A": "admin-a",
                "ATTENDEE_A": "attendee-a",
                "ATTENDEE_B": "attendee-b",
            },
        )


def test_inventory_never_falls_back_to_admin_credential():
    inventory = {
        "apps": [
            {"name": "app-a", "url": "https://a.example", "token_env": "ADMIN_A"},
            {
                "name": "app-b",
                "url": "https://b.example",
                "token_env": "ADMIN_B",
                "attendee_token_env": "ATTENDEE_B",
            },
        ]
    }
    with pytest.raises(ValueError, match="attendee credential environment variable"):
        ct_two_instance.apps_from_inventory(
            inventory,
            environ={"ADMIN_A": "a", "ADMIN_B": "b", "ATTENDEE_B": "c"},
        )


def test_inventory_requires_workspace_host():
    inventory = {
        "apps": [
            {
                "name": "app-a",
                "url": "https://a.example",
                "token_env": "ADMIN_A",
                "attendee_token_env": "ATTENDEE_A",
            },
            {
                "name": "app-b",
                "url": "https://b.example",
                "workspace_host": "https://workspace-b.example",
                "token_env": "ADMIN_B",
                "attendee_token_env": "ATTENDEE_B",
            },
        ]
    }

    with pytest.raises(ValueError, match="workspace_host"):
        ct_two_instance.apps_from_inventory(
            inventory,
            environ={
                "ADMIN_A": "admin-a",
                "ATTENDEE_A": "attendee-a",
                "ADMIN_B": "admin-b",
                "ATTENDEE_B": "attendee-b",
            },
        )


@pytest.mark.parametrize("value", [
    "Bearer top-secret",
    "dapi0123456789abcdef",
    "token_0123456789abcdef",
    "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjMifQ.signaturevalue",
    "https://user:pass@example.com/path",
    "https://example.com/path?access=secret",
    "https://example.com/path#secret",
])
def test_scalar_secret_scanner_rejects_credentials_and_unsafe_urls(value):
    report = _report()
    evidence = _late_grant()
    evidence["note"] = value
    with pytest.raises(ValueError, match="secret-bearing"):
        ct_two_instance.record_phase(
            report,
            "late_grant",
            evidence,
            signing_key=KEY,
            provenance="ct_attestation",
                ct_public_key=CT_PUBLIC_KEY,
                ct_key_id=CT_KEY_ID,
        )


def test_known_secret_values_are_rejected_even_under_neutral_keys():
    evidence = _late_grant()
    evidence["note"] = "prefix-admin-value-a-suffix"
    with pytest.raises(ValueError, match="secret-bearing"):
        ct_two_instance.record_phase(
            _report(),
            "late_grant",
            evidence,
            signing_key=KEY,
            provenance="ct_attestation",
            ct_public_key=CT_PUBLIC_KEY,
            ct_key_id=CT_KEY_ID,
            known_secrets={"admin-value-a"},
        )


def _setup_payload(*, omnigent=False):
    manifest = {
        "ai_dev_kit": {
            "match": True,
            "source": "prewarmed",
            "checksum": "b" * 64,
        },
    }
    if omnigent:
        manifest["omnigent"] = {
            "enabled": True,
            "expected": "1.2.3",
            "actual": "1.2.3",
            "match": True,
        }
    return {
        "steps": {"skills": {"status": "complete"}},
        "release_manifest": manifest,
    }


def _prewarm_payload(*, omnigent=False):
    names = ["node", "claude", "codex", "databricks"]
    if omnigent:
        names.extend(["omnigent", "tmux"])
    binaries = {
        name: {
            "expected": "1.2.3",
            "actual": "1.2.3",
            "actual_checksum": "c" * 64,
            "source": "persistent",
            "reusable": True,
        }
        for name in names
    }
    return {
        "reusable": True,
        "manifest": {
            "expected_binaries": sorted(binaries),
            "binaries": binaries,
            "ai_dev_kit": {
                "expected_ref": "v1.2.3",
                "actual_ref": "v1.2.3",
                "resolved_commit": "a" * 40,
                "expected_checksum": "b" * 64,
                "actual_checksum": "b" * 64,
                "source": "persistent",
                "reusable": True,
            },
        },
    }


def _resource_manifest():
    return {
        "apps": [
            {
                "name": name,
                "catalog": "event_catalog",
                "catalog_owner": "attendee@example.com",
                "attendee_principal": "attendee@example.com",
                "tables": ["event_catalog.default.acceptance_probe"],
                "sql_warehouse_id": "warehouse-id",
                "resources": [
                    {
                        "type": "jobs",
                        "id": f"{name}-job",
                        "required_permission": "IS_OWNER",
                    }
                ],
            }
            for name in ("app-a", "app-b")
        ]
    }


def _request_fake(
    *,
    fail_url=None,
    fail_marker_for=None,
    lost_lifecycle_response_for=None,
    lost_marker_response_for=None,
    lost_delete_response=False,
    cleanup_stuck=False,
    marker_cleanup_stuck=False,
    omnigent_setup=False,
    omnigent_prewarm=False,
    attendee_identity="attendee@example.com",
    attendee_application_id=None,
):
    sessions = {}
    created_counts = {}
    calls = []

    def request(method, url, **kwargs):
        calls.append((method, url, kwargs))
        if fail_url and url.endswith(fail_url):
            raise requests.ConnectionError("Bearer must-not-leak")
        app = "app-a" if url.startswith("https://a.") else "app-b"
        workspace_app = (
            "app-a" if url.startswith("https://workspace-a.") else "app-b"
        )
        if url.endswith("/api/2.0/current-user/me"):
            return Response(200, {
                "id": f"{workspace_app}-attendee-id",
                "userName": attendee_identity,
                "applicationId": attendee_application_id,
            })
        if url.endswith("/api/2.0/preview/scim/v2/Me"):
            return Response(200, {
                "id": f"{workspace_app}-attendee-id",
                "userName": attendee_identity,
                "applicationId": attendee_application_id,
            })
        if "/api/2.1/unity-catalog/catalogs/event_catalog" in url:
            return Response(200, {
                "name": "event_catalog",
                "owner": "attendee@example.com",
            })
        if "/api/2.1/unity-catalog/permissions/catalog/event_catalog" in url:
            return Response(200, {
                "privilege_assignments": [{
                    "principal": "attendee@example.com",
                    "privileges": ["ALL_PRIVILEGES"],
                }]
            })
        if "/api/2.0/permissions/jobs/" in url:
            resource_id = url.rsplit("/", 1)[-1]
            if resource_id != f"{workspace_app}-job":
                return Response(200, {"access_control_list": []})
            return Response(200, {
                "access_control_list": [{
                    "user_name": "attendee@example.com",
                    "all_permissions": [{
                        "permission_level": "IS_OWNER",
                        "inherited": False,
                    }],
                }]
            })
        if "/api/2.1/unity-catalog/tables/event_catalog.default.acceptance_probe" in url:
            return Response(200, {
                "full_name": "event_catalog.default.acceptance_probe",
            })
        if method == "POST" and url.endswith("/api/2.0/sql/statements"):
            return Response(200, {
                "statement_id": f"{workspace_app}-statement",
                "status": {"state": "SUCCEEDED"},
            })
        if url.endswith("/readyz"):
            setup = _setup_payload(omnigent=omnigent_setup)
            return Response(200, {
                "ready": True,
                "checks": {name: {"ok": True} for name in ct_two_instance.REQUIRED_READY_CHECKS},
                "release_manifest": setup["release_manifest"],
            })
        if url.endswith("/api/admin/setup-status"):
            return Response(200, _setup_payload(omnigent=omnigent_setup))
        if url.endswith("/api/admin/prewarm-status"):
            return Response(200, _prewarm_payload(omnigent=omnigent_prewarm))
        if url.endswith("/api/config"):
            return Response(200, {
                "credential": {"state": "rotating", "source": "app_identity_oauth"},
                "obo": {"fresh": True},
                "entitlements": {
                    "ok": True,
                    "verified_email": "attendee@example.com",
                    "verified_catalog": "event_catalog",
                },
            })
        if url.endswith("/api/agents"):
            return Response(200, {"agents": [{"id": "bash", "ready": True}]})
        if url.endswith("/api/entitlements/reconcile"):
            return Response(200, {
                "catalog": "event_catalog",
                "errors": [],
                "handoff": {
                    "details": [{
                        "resource_type": "jobs",
                        "resource_id": f"{app}-job",
                        "state": "handed_off",
                        "permission_level": "IS_OWNER",
                    }]
                },
            })
        if method == "POST" and url.endswith("/api/sessions"):
            existing = sessions.setdefault(app, [])
            created = created_counts.get(app, 0)
            if fail_marker_for == app and created >= 1:
                return Response(503, {})
            created += 1
            created_counts[app] = created
            session_id = f"{app}-session-{created}"
            existing.append(session_id)
            if lost_lifecycle_response_for == app and created == 1:
                raise requests.ConnectionError("lost lifecycle create response")
            if lost_marker_response_for == app and created >= 2:
                raise requests.ConnectionError("lost marker create response")
            return Response(200, {"session": {"id": session_id}})
        if method == "GET" and url.endswith("/api/sessions"):
            return Response(200, {
                "sessions": [{"id": item} for item in sessions.get(app, [])],
                "prior_sessions": [],
            })
        if method == "DELETE" and "/api/sessions/" in url:
            session_id = url.rsplit("/", 1)[-1]
            stuck = cleanup_stuck or (
                marker_cleanup_stuck and session_id.endswith("-session-2")
            )
            if not stuck and session_id in sessions.get(app, []):
                sessions[app].remove(session_id)
            if lost_delete_response:
                raise requests.ConnectionError("lost delete response")
            return Response(200, {"status": "ok"})
        raise AssertionError((method, url))

    return request, calls, sessions


def test_baseline_uses_api_backed_exact_resource_handoff_evidence():
    request, calls, _ = _request_fake()

    evidence = ct_two_instance.collect_baseline(
        _apps(runtime_tokens=True),
        resource_manifest=_resource_manifest(),
        request=request,
    )
    assert all(
        app["resource_verification"]["catalog_owner"] is True
        and app["resource_verification"]["all_privileges"] is True
        and app["resource_verification"]["attendee_access"] is True
        and app["resource_verification"]["attendee_identity_bound"] is True
        for app in evidence["apps"]
    )

    assert all(app["resource_verification"] == {
        "catalog": "event_catalog",
        "catalog_owner": True,
        "all_privileges": True,
        "attendee_access": True,
        "attendee_identity_bound": True,
        "resources": [{
            "type": "jobs",
            "id": f"{app['name']}-job",
            "state": "handed_off",
            "permission_level": "IS_OWNER",
        }],
    } for app in evidence["apps"])
    assert "attendee@example.com" not in json.dumps(evidence)
    workspace_calls = [
        call for call in calls if call[1].startswith("https://workspace-")
    ]
    assert workspace_calls
    assert all(
        "admin-value" in kwargs["headers"]["Authorization"]
        for _, url, kwargs in workspace_calls
        if "/permissions/" in url
    )
    assert all(
        "attendee-value" in kwargs["headers"]["Authorization"]
        for _, url, kwargs in workspace_calls
        if "/tables/" in url or url.endswith("/api/2.0/sql/statements")
    )
    catalog_auth = {
        kwargs["headers"]["Authorization"].split("-value", 1)[0]
        for _, url, kwargs in workspace_calls
        if "/unity-catalog/catalogs/" in url
    }
    assert catalog_auth == {"Bearer admin", "Bearer attendee"}


def test_baseline_independent_proof_does_not_trust_app_handoff_boolean():
    request, _, _ = _request_fake()

    evidence = ct_two_instance.collect_baseline(
        _apps(runtime_tokens=True),
        resource_manifest=_resource_manifest(),
        request=request,
    )
    assert all(
        app["resource_verification"]["catalog_owner"] is True
        and app["resource_verification"]["all_privileges"] is True
        and app["resource_verification"]["attendee_access"] is True
        for app in evidence["apps"]
    )


def test_attendee_identity_mismatch_stops_resource_proof_before_permissions():
    request, calls, _ = _request_fake(attendee_identity="other@example.com")

    evidence = ct_two_instance.collect_baseline(
        _apps(runtime_tokens=True),
        resource_manifest=_resource_manifest(),
        request=request,
    )

    assert all(
        app["resource_verification"]["attendee_identity_bound"] is False
        and app["resource_verification"]["catalog_owner"] is False
        and app["resource_verification"]["all_privileges"] is False
        and app["resource_verification"]["attendee_access"] is False
        for app in evidence["apps"]
    )
    assert not any("/permissions/" in url for _, url, _ in calls)


def test_service_principal_attendee_token_is_rejected_as_ambiguous():
    request, calls, _ = _request_fake(attendee_application_id="app-client-id")

    evidence = ct_two_instance.collect_baseline(
        _apps(runtime_tokens=True),
        resource_manifest=_resource_manifest(),
        request=request,
    )

    assert all(
        app["resource_verification"]["attendee_identity_bound"] is False
        for app in evidence["apps"]
    )
    assert not any("/permissions/" in url for _, url, _ in calls)

def test_baseline_rejects_wrong_resource_id_or_permission():
    request, _, _ = _request_fake()
    manifest = _resource_manifest()
    manifest["apps"][0]["resources"][0]["id"] = "wrong-id"

    evidence = ct_two_instance.collect_baseline(
        _apps(runtime_tokens=True),
        resource_manifest=manifest,
        request=request,
    )

    assert evidence["apps"][0]["resource_verification"]["resources"][0]["state"] == "missing"


def test_baseline_requires_omnigent_and_tmux_when_setup_enables_omnigent():
    request, _, _ = _request_fake(
        omnigent_setup=True,
        omnigent_prewarm=False,
    )

    evidence = ct_two_instance.collect_baseline(
        _apps(runtime_tokens=True),
        resource_manifest=_resource_manifest(),
        request=request,
    )

    assert all(app["expected_binaries"] == [
        "claude",
        "codex",
        "databricks",
        "node",
        "omnigent",
        "tmux",
    ] for app in evidence["apps"])
    assert all(app["prewarm_reusable"] is False for app in evidence["apps"])


def test_manifest_drift_and_red_readiness_fail_closed():
    report = _record(_report(), "late_grant", _late_grant(), "ct_attestation", 10)
    baseline = _baseline()
    baseline["apps"][1]["ready"] = False
    baseline["apps"][1]["prewarm_manifest"] = {"digest": "different"}
    report = _record(
        report,
        "baseline",
        baseline,
        "automated_collector",
        20,
    )

    result = _evaluate(report)

    assert result["status"] == "failed"
    assert result["gates"]["baseline"]["both_ready"] is False
    assert result["gates"]["baseline"]["manifests_equal"] is False


def test_incomplete_signed_chain_fails_final_evaluation():
    report = _record(_report(), "late_grant", _late_grant(), "ct_attestation", 10)

    result = _evaluate(report)

    assert result["status"] == "incomplete"
    assert result["missing_phases"] == list(ct_two_instance.REQUIRED_PHASES[1:])


@pytest.mark.parametrize(
    "mutate,recorded_at",
    [
        (
            lambda evidence: evidence["attestation"].update({
                "pause_started_at": "1970-01-01T02:30:01Z",
                "pause_completed_at": "1970-01-01T02:30:00Z",
            }),
            10_000,
        ),
        (
            lambda evidence: evidence["attestation"].update({
                "closed_laptop_pause_seconds": 5_500,
            }),
            10_000,
        ),
        (
            lambda evidence: evidence["attestation"].update({
                "pause_completed_at": "1970-01-01T03:00:00Z",
                "closed_laptop_pause_seconds": 7_200,
            }),
            10_000,
        ),
        (
            lambda evidence: evidence["attestation"].update({
                "closed_laptop_pause_seconds": -1,
            }),
            10_000,
        ),
    ],
)
def test_lunch_rejects_contradictory_future_or_negative_timing(mutate, recorded_at):
    report = _record(_report(), "late_grant", _late_grant(), "ct_attestation", 10)
    report = _record(report, "baseline", _baseline(), "automated_collector", 20)
    report = _record(report, "restart", _restart(), "automated_collector", 30)
    lunch = _lunch()
    mutate(lunch)

    report = _probe(report, "lunch_resume", lunch, recorded_at)

    events = [
        event
        for event in report["checkpoints"]
        if event["phase"] == "lunch_resume"
    ]
    assert len(events) == 1
    assert events[0]["event_type"] == "probe_attempt"
    assert events[0]["outcome"] == "failed"
    assert "lunch_resume" in _evaluate(report)["missing_phases"]


def test_lunch_pause_must_start_after_signed_restart_completion():
    report = _record(_report(), "late_grant", _late_grant(), "ct_attestation", 10)
    report = _record(report, "baseline", _baseline(), "automated_collector", 20)
    report = _record(report, "restart", _restart(), "automated_collector", 4_000)

    report = _probe(report, "lunch_resume", _lunch(), 10_000)

    event = report["checkpoints"][-1]
    assert event["event_type"] == "probe_attempt"
    assert event["outcome"] == "failed"


def test_network_failure_is_stable_secret_free_and_cleans_created_session():
    request, _, sessions = _request_fake(fail_url="/api/agents")

    evidence = ct_two_instance.collect_baseline(
        _apps(runtime_tokens=True),
        resource_manifest=_resource_manifest(),
        request=request,
    )

    assert all(app["probe_status"] == "request_failed" for app in evidence["apps"])
    assert all(not values for values in sessions.values())
    assert "must-not-leak" not in json.dumps(evidence)
    assert "admin-value" not in json.dumps(evidence)


def test_marker_creation_is_all_or_nothing_and_cleans_partial_markers():
    request, _, sessions = _request_fake(fail_marker_for="app-b")

    evidence = ct_two_instance.collect_baseline(
        _apps(runtime_tokens=True),
        resource_manifest=_resource_manifest(),
        request=request,
    )

    assert all(app["restart_marker"] is None for app in evidence["apps"])
    assert all(not values for values in sessions.values())


def test_lost_delete_response_is_confirmed_by_absence():
    request, _, sessions = _request_fake(lost_delete_response=True)

    evidence = ct_two_instance.collect_baseline(
        _apps(runtime_tokens=True),
        resource_manifest=_resource_manifest(),
        request=request,
        cleanup_retries=2,
    )

    assert all(app["session"]["closed"] is True for app in evidence["apps"])
    assert all(app["residual_session_ids"] == [] for app in evidence["apps"])
    assert all(len(values) == 1 for values in sessions.values())  # restart markers only


def test_lost_session_create_response_discovers_and_cleans_unknown_session():
    request, _, sessions = _request_fake(lost_lifecycle_response_for="app-a")

    evidence = ct_two_instance.collect_baseline(
        _apps(runtime_tokens=True),
        resource_manifest=_resource_manifest(),
        request=request,
        cleanup_retries=2,
    )

    app_a = next(app for app in evidence["apps"] if app["name"] == "app-a")
    assert app_a["probe_status"] == "request_failed"
    assert app_a["session"]["closed"] is True
    assert app_a["residual_session_ids"] == []
    assert all(values == [] for values in sessions.values())


def test_unconfirmed_cleanup_fails_attempt_and_retains_residual_ids():
    request, _, sessions = _request_fake(cleanup_stuck=True)

    evidence = ct_two_instance.collect_baseline(
        _apps(runtime_tokens=True),
        resource_manifest=_resource_manifest(),
        request=request,
        cleanup_retries=2,
    )

    assert all(app["session"]["closed"] is False for app in evidence["apps"])
    assert all(app["residual_session_ids"] for app in evidence["apps"])
    assert all(len(values) == 1 for values in sessions.values())
    report = _record(_report(), "late_grant", _late_grant(), "ct_attestation", 10)
    report = _probe(report, "baseline", evidence, 20)
    assert report["checkpoints"][-1]["outcome"] == "failed"


def test_retry_cleans_residuals_before_creating_new_sessions_without_duplicates():
    request, _, sessions = _request_fake(cleanup_stuck=True)
    failed = ct_two_instance.collect_baseline(
        _apps(runtime_tokens=True),
        resource_manifest=_resource_manifest(),
        request=request,
        cleanup_retries=1,
    )
    residuals = {
        app["name"]: app["residual_session_ids"]
        for app in failed["apps"]
    }
    request, calls, retry_sessions = _request_fake()
    retry_sessions.update({name: list(ids) for name, ids in residuals.items()})

    evidence = ct_two_instance.collect_baseline(
        _apps(runtime_tokens=True),
        resource_manifest=_resource_manifest(),
        request=request,
        residual_sessions=residuals,
        cleanup_retries=2,
    )

    session_calls = [
        (method, url)
        for method, url, _ in calls
        if "/api/sessions" in url
    ]
    first_post = next(index for index, call in enumerate(session_calls) if call[0] == "POST")
    assert session_calls[0][0] == "DELETE"
    assert all(method in {"DELETE", "GET"} for method, _ in session_calls[:first_post])
    assert all(app["residual_session_ids"] == [] for app in evidence["apps"])
    assert all(len(set(values)) == len(values) == 1 for values in retry_sessions.values())


def test_report_residuals_are_carried_into_next_baseline_retry():
    report = _record(_report(), "late_grant", _late_grant(), "ct_attestation", 10)
    failed = _baseline()
    failed["apps"][0]["ready"] = False
    failed["apps"][0]["residual_session_ids"] = ["leftover-session"]
    report = _probe(report, "baseline", failed, 20)

    assert ct_two_instance.baseline_residual_sessions(report) == {
        "app-a": ["leftover-session"],
        "app-b": [],
    }


def test_partial_marker_cleanup_failure_retains_marker_residual():
    request, _, sessions = _request_fake(
        fail_marker_for="app-b",
        marker_cleanup_stuck=True,
    )

    evidence = ct_two_instance.collect_baseline(
        _apps(runtime_tokens=True),
        resource_manifest=_resource_manifest(),
        request=request,
        cleanup_retries=1,
    )

    app_a = next(app for app in evidence["apps"] if app["name"] == "app-a")
    assert app_a["restart_marker"] is None
    assert app_a["residual_session_ids"]
    assert sessions["app-a"]


def test_lost_marker_create_response_discovers_and_cleans_unknown_marker():
    request, _, sessions = _request_fake(lost_marker_response_for="app-b")

    evidence = ct_two_instance.collect_baseline(
        _apps(runtime_tokens=True),
        resource_manifest=_resource_manifest(),
        request=request,
        cleanup_retries=2,
    )

    assert all(app["restart_marker"] is None for app in evidence["apps"])
    assert all(app["residual_session_ids"] == [] for app in evidence["apps"])
    assert all(values == [] for values in sessions.values())


def test_successful_signed_report_passes_without_secrets():
    result = _evaluate(_successful_report())

    assert result["status"] == "passed"
    assert result["exit_code"] == 0
    assert "signing-key" not in json.dumps(result)


def test_teardown_rejects_boolean_only_or_missing_receipts():
    report = _report()
    report = _record(report, "late_grant", _late_grant(), "ct_attestation", 10)
    report = _record(report, "baseline", _baseline(), "automated_collector", 20)
    report = _record(report, "restart", _restart(), "automated_collector", 30)
    report = _record(report, "lunch_resume", _lunch(), "operator_attestation", 10_000)
    teardown = _teardown()
    teardown = {
        key: value
        for key, value in teardown.items()
        if key != "ct_attestation"
    }
    teardown["apps"][0] = {
        "name": "app-a",
        "report_captured": True,
        "resources_removed": True,
    }
    teardown = _ct_signed(report, "teardown", teardown)
    report = _record(report, "teardown", teardown, "ct_attestation", 11_000)

    result = _evaluate(report)

    assert result["status"] == "failed"
    assert result["gates"]["teardown"]["external_receipts_complete"] is False


@pytest.mark.parametrize(
    "duplicate_kind",
    [
        "deployment_id",
        "app_id",
        "workspace_id",
        "catalog",
        "credential_resource_id",
        "receipt_id",
    ],
)
def test_teardown_rejects_cross_instance_identifier_or_receipt_reuse(
    duplicate_kind,
):
    report = _record(_report(), "late_grant", _late_grant(), "ct_attestation", 10)
    report = _record(report, "baseline", _baseline(), "automated_collector", 20)
    report = _record(report, "restart", _restart(), "automated_collector", 30)
    report = _record(
        report,
        "lunch_resume",
        _lunch(),
        "operator_attestation",
        10_000,
    )
    teardown = _teardown()
    first, second = teardown["apps"]
    if duplicate_kind in {"deployment_id", "app_id", "workspace_id", "catalog"}:
        resource_type = {
            "deployment_id": "deployment",
            "app_id": "app",
            "workspace_id": "workspace",
            "catalog": "catalog",
        }[duplicate_kind]
        second[duplicate_kind] = first[duplicate_kind]
        second_receipt = next(
            receipt
            for receipt in second["receipts"]
            if receipt["resource_type"] == resource_type
        )
        second_receipt["resource_id"] = first[duplicate_kind]
    elif duplicate_kind == "credential_resource_id":
        first_receipt = next(
            receipt
            for receipt in first["receipts"]
            if receipt["resource_type"] == "credential"
        )
        second_receipt = next(
            receipt
            for receipt in second["receipts"]
            if receipt["resource_type"] == "credential"
        )
        second_receipt["resource_id"] = first_receipt["resource_id"]
    else:
        second["receipts"][0]["receipt_id"] = first["receipts"][0]["receipt_id"]

    report = _record(report, "teardown", teardown, "ct_attestation", 11_000)
    result = _evaluate(report)

    assert result["status"] == "failed"
    assert result["gates"]["teardown"]["cross_instance_unique"] is False


def test_teardown_accepts_unique_identifiers_and_receipts():
    result = _evaluate(_successful_report())

    assert result["gates"]["teardown"]["cross_instance_unique"] is True


def test_errors_never_echo_secret_values(monkeypatch, capsys, tmp_path):
    monkeypatch.setenv("CT_VALIDATION_HMAC_KEY", "super-secret-hmac-value")
    monkeypatch.setenv("CT_ATTESTATION_PUBLIC_KEY", CT_PUBLIC_KEY_B64)
    monkeypatch.setenv("CT_ATTESTATION_KEY_ID", CT_KEY_ID)
    inventory = tmp_path / "inventory.json"
    inventory.write_text(json.dumps({
        "apps": [
            {
                "name": "app-a",
                "url": "https://a.example",
                "token_env": "ADMIN_A",
                "attendee_token_env": "ATTENDEE_A",
            },
            {
                "name": "app-b",
                "url": "https://b.example",
                "token_env": "ADMIN_B",
                "attendee_token_env": "ATTENDEE_B",
            },
        ]
    }))
    monkeypatch.setenv("ADMIN_A", "dapi-admin-secret")
    monkeypatch.setattr(
        "sys.argv",
        [
            "ct_two_instance.py",
            "--report",
            str(tmp_path / "report.json"),
            "init",
            "--inventory",
            str(inventory),
            "--run-id",
            "bad run id",
        ],
    )

    assert ct_two_instance.main() == 2
    output = capsys.readouterr().out
    assert "super-secret" not in output
    assert "dapi-admin-secret" not in output
