"""Acceptance must fail closed when physical evidence or budgets are missing."""
import json

import pytest

from evals.generated_apps.report import REQUIRED_ACCEPTANCE, RunReport, validate_budgets, write_evidence


def test_completed_label_cannot_accept_missing_observations():
    report = RunReport("synthetic-1", "completed")
    report.add("real_deployment", "passed", "Apps deployment observed", "deployment.json")
    result = report.to_dict()
    assert not result["accepted"]
    assert "persisted_backend_update" in result["missing_acceptance_checks"]


def test_raw_transport_cannot_replace_consultation_evidence():
    report = RunReport("synthetic-1", "completed")
    for name in REQUIRED_ACCEPTANCE:
        report.add(name, "unverified" if name == "requirements_and_advice" else "passed",
                   "Observed" if name != "requirements_and_advice" else "PTY has no structured assistant turns",
                   "observation.json")
    assert report.to_dict()["verdict"] == "unverified"


def test_failure_cannot_be_erased_by_repair():
    report = RunReport("synthetic-1", "completed")
    report.add("critical_user_task", "failed", "Update forgotten on restart")
    with pytest.raises(ValueError):
        report.add("critical_user_task", "passed", "Fixed", "retry.json")


def test_evidence_write_redacts_known_and_common_credentials(tmp_path):
    path = tmp_path / "report.json"
    secret = "private-test-credential"
    digest = write_evidence(path, {"message": secret, "auth": "Bearer abcdef-token"}, secrets=(secret,))
    result = json.loads(path.read_text())
    assert result == {"message": "[REDACTED]", "auth": "Bearer [REDACTED]"}
    assert len(digest) == 64 and path.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("missing", ["total_seconds", "consultation_seconds", "token_ceiling", "spend_ceiling_usd"])
def test_partial_budget_is_not_a_bounded_live_run(missing):
    budget = {"total_seconds": 1800, "consultation_seconds": 180, "token_ceiling": 50000, "spend_ceiling_usd": 10}
    budget.pop(missing)
    with pytest.raises(ValueError):
        validate_budgets(budget)


def test_nan_or_unlimited_budget_rejected():
    with pytest.raises(ValueError):
        validate_budgets({"total_seconds": 1800, "consultation_seconds": 180,
                          "token_ceiling": 50000, "spend_ceiling_usd": float("nan")})
