"""AI Gateway resolution — three sources, and no surface to fall back to.

There is exactly one place the models an attendee needs now answer: Unity AI
Gateway. The legacy ``<host>/serving-endpoints`` fallback these tests were
originally written around has been retired along with the ``databricks-*``
endpoints it served, and now 404s, so an unresolved gateway is a broken event
rather than an ungoverned one.

That raised the stakes on an asymmetry these tests used to merely record:
dedicated-subdomain construction reads the workspace id from
DATABRICKS_WORKSPACE_ID or an ``adb-<digits>`` hostname, which is Azure's shape,
and an AWS ``dbc-...`` workspace matches neither. What closes it is the
workspace-hosted form ``<host>/ai-gateway``, which needs no id at all. The
dedicated-subdomain path is still preferred where it is configured and reachable
— it is what gateway policy and rate limits are attached to in those
deployments — so these tests pin all three sources and the order between them.
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


def test_aws_workspace_host_resolves_without_a_workspace_id(monkeypatch, probe):
    """The gap that used to need injecting, closed.

    The workspace id regex matches ``adb-<digits>`` only, so a dbc- host yields
    no id, no dedicated-subdomain candidate and no probe — and used to yield no
    gateway either, leaving the deployment on a fallback that is now gone. The
    workspace-hosted form is derived from the host alone, so nothing has to be
    injected for an AWS event to reach a model.
    """
    monkeypatch.setenv(
        "DATABRICKS_HOST", "https://dbc-af3ed11d-d267.cloud.databricks.com"
    )

    status = cli_config.gateway_status()

    assert status["resolved"] is True
    assert status["source"] == "workspace"
    assert status["workspace_id_derivable"] is False
    assert status["omnigent_gateway_form"] is True
    assert probe == [], "no dedicated-subdomain candidate should have been built"
    assert (
        cli_config.gateway_host()
        == "https://dbc-af3ed11d-d267.cloud.databricks.com/ai-gateway"
    )


def test_no_host_at_all_is_the_only_unresolved_case(monkeypatch, probe):
    """The one lever left. With no workspace host there is nothing to derive a
    gateway from and no second surface to try, which is what /readyz reports."""
    status = cli_config.gateway_status()

    assert status["resolved"] is False
    assert status["source"] == "unresolved"
    assert cli_config.gateway_host() == ""


def test_an_unreachable_dedicated_subdomain_falls_back_to_the_workspace_form(
    monkeypatch,
):
    """A workspace id that names a gateway subdomain nobody stood up must not
    strand the event on the old fallback, because the old fallback is a 404."""
    monkeypatch.setenv(
        "DATABRICKS_HOST", "https://dbc-af3ed11d-d267.cloud.databricks.com"
    )
    monkeypatch.setenv("DATABRICKS_WORKSPACE_ID", "987654321")
    monkeypatch.setattr(cli_config, "_probe", lambda url: False)

    assert (
        cli_config.gateway_host()
        == "https://dbc-af3ed11d-d267.cloud.databricks.com/ai-gateway"
    )
    assert cli_config.gateway_status()["source"] == "workspace"


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
    """Kept as a guard rather than a description of live behaviour: nothing
    writes this URL any more, and if a regression reintroduced it Omnigent would
    decline to route it — it carries neither the DNS label nor the path."""
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
    (which rides on ``advanced-tool-use``) keeps loading schemas on demand.
    Every configured deployment is on the gateway now, so the other branch
    covers only the no-host case — where there is no base URL to hand a CLI at
    all, and disabling the experimental betas is the conservative default for
    whatever the CLI falls back to on its own.
    """
    assert cli_config.beta_negotiation_env(True) == {"CLAUDE_CODE_USE_GATEWAY": "1"}
    assert cli_config.beta_negotiation_env(False) == {
        "CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS": "1"
    }


def test_the_two_beta_flags_are_never_sent_together():
    """Omnigent 0.10.0 reads CLAUDE_CODE_USE_GATEWAY to decide whether to re-add
    the disable flag. Sending both would be contradictory on either path."""
    for gateway_backed in (True, False):
        env = cli_config.beta_negotiation_env(gateway_backed)
        assert len(env) == 1
