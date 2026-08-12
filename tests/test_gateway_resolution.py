"""AI Gateway resolution — and the AWS asymmetry that makes it a CT dependency.

Without a gateway every CLI falls back to ``<host>/serving-endpoints``, which
serves every model an attendee needs — Claude, the GPT Responses models, and the
chat-completions-only models like GLM all answer there. That is exactly why the
gap is easy to miss: the workshop runs. What the fallback costs is governance,
since gateway policy, usage tracking and rate limits only apply on the gateway
path.

These tests pin the asymmetry that forces the deployment to inject the value:
auto-construction reads the workspace id from DATABRICKS_WORKSPACE_ID or an
``adb-<digits>`` hostname, which is Azure's shape. An AWS ``dbc-...`` workspace
matches neither.
"""

import pytest

from server import cli_config


@pytest.fixture(autouse=True)
def _reset_gateway_cache(monkeypatch):
    """gateway_host() memoises per process; each test needs a clean resolve."""
    monkeypatch.setattr(cli_config, "_gateway_resolved", None)
    monkeypatch.delenv("DATABRICKS_GATEWAY_HOST", raising=False)
    monkeypatch.delenv("DATABRICKS_WORKSPACE_ID", raising=False)
    monkeypatch.delenv("DATABRICKS_HOST", raising=False)


@pytest.fixture()
def probe(monkeypatch):
    """Record probe calls so a test can prove a candidate was never even built."""
    calls = []

    def _probe(url):
        calls.append(url)
        return True

    monkeypatch.setattr(cli_config, "_probe", _probe)
    return calls


def test_aws_workspace_host_cannot_auto_construct_a_gateway(monkeypatch, probe):
    """The finding: on AWS the probe is not merely failing, it never runs.

    The workspace id regex matches ``adb-<digits>`` only, so a dbc- host yields
    no id, no candidate URL, and no probe — which is why this cannot be fixed by
    making the gateway reachable. It has to be injected.
    """
    monkeypatch.setenv("DATABRICKS_HOST", "https://dbc-af3ed11d-d267.cloud.databricks.com")

    status = cli_config.gateway_status()

    assert status["resolved"] is False
    assert status["source"] == "unresolved"
    assert status["workspace_id_derivable"] is False
    assert probe == [], "no candidate URL should have been built to probe"


def test_azure_workspace_host_auto_constructs_without_help(monkeypatch, probe):
    """The other half of the asymmetry — Azure's hostname carries the id, so
    Azure deployments need no injected value and would mask the AWS gap."""
    monkeypatch.setenv("DATABRICKS_HOST", "https://adb-123456789.0.azuredatabricks.net")

    status = cli_config.gateway_status()

    assert status["resolved"] is True
    assert status["source"] == "constructed"
    assert status["omnigent_gateway_form"] is True
    assert probe == ["https://123456789.0.ai-gateway.azuredatabricks.net"]


def test_workspace_id_env_is_a_sufficient_lever_on_aws(monkeypatch, probe):
    """Injecting the id alone is enough — CT can supply either lever."""
    monkeypatch.setenv("DATABRICKS_HOST", "https://dbc-af3ed11d-d267.cloud.databricks.com")
    monkeypatch.setenv("DATABRICKS_WORKSPACE_ID", "987654321")

    status = cli_config.gateway_status()

    assert status["resolved"] is True
    assert status["source"] == "constructed"
    assert status["omnigent_gateway_form"] is True
    assert probe == ["https://987654321.ai-gateway.cloud.databricks.com"]


def test_explicit_gateway_host_is_trusted_without_a_probe(monkeypatch, probe):
    """An explicit value must not depend on the app being able to reach the
    gateway at boot, or a slow network turns into a silent downgrade."""
    monkeypatch.setenv(
        "DATABRICKS_GATEWAY_HOST", "https://999.ai-gateway.cloud.databricks.com"
    )

    status = cli_config.gateway_status()

    assert status["resolved"] is True
    assert status["source"] == "explicit"
    assert status["gateway_host_set"] is True
    assert probe == []


def test_workspace_host_with_ai_gateway_path_is_a_form_omnigent_accepts():
    """The curl-proven shape: no dedicated gateway subdomain, just the workspace
    host plus /ai-gateway, accepted via upstream's path rule."""
    assert cli_config._is_omnigent_gateway_form(
        "https://dbc-af3ed11d-d267.cloud.databricks.com/ai-gateway/anthropic"
    )


def test_the_bare_ai_gateway_root_does_not_satisfy_upstreams_path_rule():
    """Upstream tests ``path.startswith("/ai-gateway/")``, so the trailing slash
    is load-bearing and a bare root fails. Pinned because it is the reason
    gateway_status judges the derived base URL instead of the configured root —
    judging the root would report amber for a deployment that works."""
    assert not cli_config._is_omnigent_gateway_form(
        "https://dbc-af3ed11d-d267.cloud.databricks.com/ai-gateway"
    )


def test_a_workspace_hosted_gateway_root_still_reports_green(monkeypatch, probe):
    """The end-to-end consequence of the above: CT setting the bare
    /ai-gateway root is a correct configuration, because what we hand Omnigent
    is <root>/anthropic."""
    monkeypatch.setenv(
        "DATABRICKS_GATEWAY_HOST",
        "https://dbc-af3ed11d-d267.cloud.databricks.com/ai-gateway",
    )

    status = cli_config.gateway_status()

    assert status["resolved"] is True
    assert status["omnigent_gateway_form"] is True


def test_serving_endpoints_fallback_is_not_a_form_omnigent_accepts():
    """The reason an unresolved gateway is not harmless: the fallback URL has
    neither the DNS label nor the path, so Pi never treats it as a gateway."""
    assert not cli_config._is_omnigent_gateway_form(
        "https://dbc-af3ed11d-d267.cloud.databricks.com/serving-endpoints/anthropic"
    )
    assert not cli_config._is_omnigent_gateway_form("")


def test_a_gateway_lookalike_hostname_is_not_mistaken_for_the_label():
    """The label must match a whole DNS label, not a substring — otherwise a
    host merely containing the words would be reported green."""
    assert not cli_config._is_omnigent_gateway_form(
        "https://not-ai-gateway-really.example.com"
    )


def test_an_untrusted_host_is_rejected_however_gateway_shaped_it_looks():
    """Upstream gates on a Databricks-owned domain suffix before honouring the
    label or path, precisely to avoid forwarding a bearer token to a look-alike.
    A mirror that skipped this would report green on a URL Omnigent refuses."""
    assert not cli_config._is_omnigent_gateway_form(
        "https://anything.ai-gateway.evil.test/anthropic"
    )
    # Anchored on the leading dot, so suffix-smuggling is rejected too.
    assert not cli_config._is_omnigent_gateway_form(
        "https://x.ai-gateway.cloud.databricks.com.evil.test/anthropic"
    )


def test_plain_http_is_rejected():
    """Upstream requires https; a downgraded URL would forward the bearer in
    clear text, so it must never be reported as a usable gateway."""
    assert not cli_config._is_omnigent_gateway_form(
        "http://123.ai-gateway.cloud.databricks.com/anthropic"
    )


def test_beta_negotiation_is_only_enabled_on_the_gateway():
    """Claude Code's beta set is settled differently on and off the gateway.

    On the gateway it negotiates, so the betas stay on and MCP tool search
    (which rides on ``advanced-tool-use``) keeps loading schemas on demand. Off
    it, the ``/serving-endpoints/anthropic`` fallback has not been shown to
    accept those flags, and a 400 "invalid beta flag" would break the session —
    so the older disable workaround stays in place there.
    """
    assert cli_config.beta_negotiation_env(True) == {"CLAUDE_CODE_USE_GATEWAY": "1"}
    assert cli_config.beta_negotiation_env(False) == {
        "CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS": "1"
    }


def test_the_two_beta_flags_are_never_sent_together():
    """Omnigent 0.9.0 reads CLAUDE_CODE_USE_GATEWAY to decide whether to re-add
    the disable flag. Sending both would be contradictory on either path."""
    for gateway_backed in (True, False):
        env = cli_config.beta_negotiation_env(gateway_backed)
        assert len(env) == 1
