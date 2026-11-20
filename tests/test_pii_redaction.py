"""PII redaction for insight v2 text (server/discovery.py ``redact_pii``).

Two failure modes matter, and each has a test class:

- **leaks**: an identifier survives into an account-team field;
- **over-redaction**: a version, date or time is replaced, and the brief reads as
  shredded prose.
"""

from __future__ import annotations

import pytest

from server import discovery

from .synthetic_secrets import DAPI_TOKEN, JWT, PG_URL


@pytest.mark.parametrize(
    "text",
    [
        "reach me at ada@example.com today",
        "first.last+tag@sub.example.co.uk",
        "+61 412 345 678 is the on-call number",
        "call +1 (415) 555-0100 after lunch",
        "02 1234 5678 is the Sydney office",
        "mobile 0412 345 678",
        "host 10.0.0.12 is the gateway",
        "IPv6 2001:db8:85a3::8a2e:370:7334 here",
        "loopback ::1 in the config",
        "link https://dbc-1234.cloud.databricks.com/?token=abc123",
        "sig https://storage.example.com/x.png?sig=AAAA",
        "https://api.example.com/v1/resource?api_key=zzz",
        "workspace adb-99.azuredatabricks.net",
        "gcp dbc-7.gcp.databricks.com",
        "app my-app-123.databricksapps.com",
    ],
)
def test_identifiers_are_replaced_with_labelled_markers(text):
    cleaned, count = discovery.redact_pii(text)

    assert count >= 1
    assert "[redacted-" in cleaned
    for raw in ("ada@", "412 345 678", "415) 555", "02 1234", "10.0.0.12", "2001:db8",
                "::1", "token=abc123", "sig=AAAA", "api_key=zzz", "adb-99", "dbc-7",
                "my-app-123", "dbc-1234"):
        assert raw not in cleaned


def test_each_kind_gets_its_own_marker():
    cleaned, count = discovery.redact_pii(
        "ada@example.com 10.0.0.12 +61 412 345 678 https://x.cloud.databricks.com/?token=t adb-1.azuredatabricks.net"
    )

    assert count == 5
    assert cleaned.count("[redacted-email]") == 1
    assert cleaned.count("[redacted-ip]") == 1
    assert cleaned.count("[redacted-phone]") == 1
    assert cleaned.count("[redacted-url]") == 1
    assert cleaned.count("[redacted-workspace-host]") == 1


def test_a_url_without_a_credential_parameter_is_kept_whole():
    text = "docs at https://docs.databricks.com/en/dev-tools/index.html?lang=en"

    assert discovery.redact_pii(text) == (text, 0)


class TestNoOverRedaction:
    @pytest.mark.parametrize(
        "text",
        [
            "shipped on 2026-11-20 at 12:30:45",
            "using pyspark 3.5.1 and fastapi 0.142.2",
            "the std::vector and ratio 3.14159",
            "order 20261120 of 12 items",
            "time 09:30 and 1 PM",
        ],
    )
    def test_dates_versions_and_times_survive(self, text):
        cleaned, count = discovery.redact_pii(text)
        assert cleaned == text
        assert count == 0

    def test_a_plain_sentence_about_a_business_is_untouched(self):
        text = "We run churn analysis on 12 regions and want results by Friday."
        assert discovery.redact_pii(text) == (text, 0)

    def test_empty_input_is_empty(self):
        assert discovery.redact_pii("") == ("", 0)


def test_redact_pii_runs_after_secret_redaction_and_both_apply():
    text = f"token {DAPI_TOKEN} for ada@example.com and {PG_URL}"

    secret_clean, _ = discovery.redact(text)
    cleaned, _ = discovery.redact_pii(secret_clean)

    assert DAPI_TOKEN not in cleaned
    assert "ada@example.com" not in cleaned
    assert "s3cretpw" not in cleaned


def test_a_jwt_is_still_removed_by_the_secret_pass_not_mangled_by_pii():
    cleaned, _ = discovery.redact_pii(discovery.redact(f"auth {JWT}")[0])

    assert JWT not in cleaned
    assert "[redacted]" in cleaned
