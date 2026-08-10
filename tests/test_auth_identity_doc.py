"""Keep the identity model honest about what was measured.

This document is the reason nobody re-litigates the credential design every
week: it records what was tried against a live deployment and what came back.
The failure mode it guards against is quiet decay — the ceilings getting
softened into "not currently configured", or an aspirational target state
outliving the measurement that disproved it, so the same dead-end gets designed
again six weeks later.

The assertions are deliberately about claims, not prose. Rewording is fine;
losing a measured ceiling is not.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

DOCS = Path(__file__).resolve().parent.parent / "docs"
DOC = DOCS / "auth-identity-model.md"
ADR = DOCS / "adr" / "0002-attendee-credential.md"

# The complete Apps user-authorization vocabulary as the scope picker lists it.
# Seventeen, with nothing that can write.
SCOPES = (
    "sql",
    "sql:restricted-query",
    "sql.statement-execution",
    "sql.warehouses:read",
    "postgres",
    "catalog.catalogs:read",
    "catalog.schemas:read",
    "catalog.tables:read",
    "catalog.connections",
    "files",
    "ai-gateway",
    "model-serving",
    "vector-search",
    "genie",
    "workspace.workspace",
    "mcp.external",
    "mcp.functions",
)


@pytest.fixture(scope="module")
def doc() -> str:
    """Whitespace-normalised: a claim is a claim wherever the line wraps."""
    return re.sub(r"\s+", " ", DOC.read_text())


def test_the_full_scope_vocabulary_is_recorded_not_summarised(doc):
    """"Some read scopes" is a claim someone will try to widen. The list is not."""
    missing = [scope for scope in SCOPES if scope not in doc]

    assert missing == [], f"scopes dropped from the measured vocabulary: {missing}"
    assert "17" in doc, "the size of the ceiling is the point"


def test_the_ceiling_is_stated_as_permanent_rather_than_unconfigured(doc):
    assert re.search(r"permanent, not a configuration gap", doc, re.I), (
        "a reader who thinks this is a config gap will spend a day on scopes"
    )
    assert "all-apis" in doc and "rejected outright" in doc


def test_every_route_to_an_attendee_write_credential_is_recorded_as_closed(doc):
    """Four dead ends, each of which someone reaches for in order."""
    assert "does not have required scopes: authentication" in doc, (
        "the self-mint failure has to carry its exact error or it reads as a guess"
    )
    assert re.search(r"create-obo-token[^.]*service principals only", doc, re.I), (
        "admins minting on the attendee's behalf is the next thing tried"
    )
    assert re.search(r"proxy rejects PATs", doc, re.I)


def test_the_target_state_no_longer_promises_a_credential_that_cannot_exist(doc):
    """The doc used to name a per-attendee PAT as the shipping answer. The
    measurements in the same document disprove it."""
    target = doc.split("## What ships")[-1]

    assert "attendee PAT" not in target, (
        "the target state contradicts the measured ceiling above it"
    )
    assert "app SP, via the CLI wrapper" in target


def test_the_shipping_split_names_the_reconciler_as_load_bearing(doc):
    """If creates run as the SP, the reconciler is the only thing making the
    result usable — that is a correctness requirement, not a nicety."""
    assert re.search(r"reconciler is load-bearing|load-bearing rather than a", doc, re.I)
    assert "entitlements.health" in doc


def test_the_durable_credential_route_is_named_and_scoped_out(doc):
    assert "custom OAuth" in doc and "offline_access" in doc
    assert re.search(r"out of scope for event week", doc, re.I)


def test_the_retired_designs_are_recorded_as_retired_not_deleted(doc):
    """Both get re-proposed by anyone who has not read the ceilings, so the
    reason they cannot work has to outlive the design docs themselves."""
    adr = ADR.read_text()

    assert ADR.exists()
    for design in ("wrapper service principal", "credential broker"):
        assert design in doc, f"{design} is not recorded as retired in the model"
        assert design in adr
    assert re.search(r"Retire the wrapper-SP auth provider", adr)


def test_the_spike_records_the_questions_that_can_kill_the_design(doc):
    """A spike without kill criteria becomes an implementation."""
    adr = ADR.read_text()

    assert "custom-app-integrations" in adr, "the exact API to ask has to be named"
    assert re.search(r"refresh.token lifetime and rotation", adr, re.I)
    assert re.search(r"consent be pre-approved", adr, re.I)


def test_the_adr_states_the_reconciler_consequence_plainly(doc):
    adr = re.sub(r"\s+", " ", ADR.read_text())

    assert "load-bearing, not a safety net" in adr
    assert "Lakeview dashboards are not handed off" in adr


def test_the_cli_parity_fix_records_what_it_deliberately_did_not_do(doc):
    """The wrapper's narrowness is the whole design. A future reader "simplifying"
    it into a config merge hands the runner's SDK fallback a silent path to the
    service principal."""
    section = doc.split("### 1.")[-1].split("### 2.")[0]

    assert "_write_databricks_cli_wrapper" in section
    assert "argv" in section, "why the runner's SDK is unaffected has to be stated"
    assert re.search(r"still cannot become the service principal", section, re.I)


def test_the_topology_still_says_both_surfaces_share_one_host(doc):
    """Two front doors, one bug. Losing this sends a debugger to the wrong app."""
    assert re.search(r"one bug with two front doors", doc, re.I)


def test_the_doc_points_at_the_verification_evidence(doc):
    assert "verification-gate.md" in doc
    assert "operator-runbook.md" in doc
    assert (DOCS / "verification-gate.md").exists()
