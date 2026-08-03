"""Workshop Polly variants — the model sets attendees compare.

The variants are derived from the stock polly bundle at App startup rather than
vendored, so these tests work against a synthetic "stock" bundle and assert the
properties of the derivation, not the contents of any particular upstream
prompt. That keeps them from failing on every Omnigent upgrade while still
pinning the parts we own.
"""

import importlib.util
import sys
from pathlib import Path

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]
APP_DIR = ROOT / "deploy" / "omnigent-app"

STOCK_PROMPT = "You are polly, a multi-agent CODING orchestrator.\nSix workers exist.\n"


def _load():
    """Load the module by path — it ships in the App bundle, not the server package.

    Registering it in ``sys.modules`` before execution is required, not
    incidental: ``@dataclass`` resolves annotations through
    ``sys.modules[cls.__module__]``, which is absent for a module loaded purely
    from a spec.
    """
    name = "workshop_polly_variants"
    spec = importlib.util.spec_from_file_location(name, APP_DIR / "polly_variants.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


polly_variants = _load()


@pytest.fixture()
def stock(tmp_path, monkeypatch):
    """A synthetic stock bundle standing in for the one inside the wheel."""
    source = tmp_path / "stock" / "polly"
    (source / "skills" / "fanout").mkdir(parents=True)
    (source / "skills" / "fanout" / "SKILL.md").write_text("# fanout\n")
    (source / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "spec_version": 1,
                "name": "polly",
                "description": "stock description",
                "spawn": True,
                "executor": {
                    "type": "omnigent",
                    "context_window": 1000000,
                    "config": {"harness": "claude-sdk"},
                },
                "prompt": STOCK_PROMPT,
                "tools": {
                    "agents": [
                        "claude_code",
                        "codex",
                        "opencode",
                        "cursor",
                        "hermes",
                        "pi",
                    ]
                },
                "guardrails": {"ask_timeout": 86400},
            },
            sort_keys=False,
        )
    )
    for worker in ("claude_code", "codex", "opencode", "cursor", "hermes", "pi"):
        directory = source / "agents" / worker
        directory.mkdir(parents=True)
        (directory / "config.yaml").write_text(
            yaml.safe_dump(
                {
                    "spec_version": 1,
                    "name": worker,
                    "executor": {"type": "omnigent", "config": {"harness": worker}},
                    "prompt": f"You are {worker}.\n",
                },
                sort_keys=False,
            )
        )
    monkeypatch.setattr(polly_variants, "stock_polly_dir", lambda: source)
    return source


def _build(tmp_path, stock, env=None):
    out = tmp_path / "built"
    out.mkdir(exist_ok=True)
    bundles = polly_variants.build(out, env=env or {})
    return {b.name: yaml.safe_load((b / "config.yaml").read_text()) for b in bundles}


def _worker(tmp_path, tier, worker):
    path = tmp_path / "built" / tier / "agents" / worker / "config.yaml"
    return yaml.safe_load(path.read_text())


def test_each_tier_registers_under_its_own_name(tmp_path, stock):
    """Distinct names are what make the tiers separately selectable; a shared
    name would have each registration overwrite the last."""
    built = _build(tmp_path, stock)

    assert set(built) == {"polly-economy", "polly-balanced", "polly-frontier"}
    for name, config in built.items():
        assert config["name"] == name


def test_the_stock_bundle_is_never_mutated(tmp_path, stock):
    """Stock polly stays the default an attendee gets by ignoring all of this,
    so deriving from it must not edit it in place — the bundle lives inside the
    installed wheel and a mutation would leak into every other session."""
    before = (stock / "config.yaml").read_text()

    _build(tmp_path, stock)

    assert (stock / "config.yaml").read_text() == before
    assert sorted(p.name for p in (stock / "agents").iterdir()) == [
        "claude_code",
        "codex",
        "cursor",
        "hermes",
        "opencode",
        "pi",
    ]


def test_the_roster_is_trimmed_to_installed_clis(tmp_path, stock):
    """opencode / cursor / hermes are not in the Workshop Terminal image. Naming
    them would spend the brain's first turn discovering they cannot boot."""
    built = _build(tmp_path, stock)

    for tier, config in built.items():
        assert set(config["tools"]["agents"]) <= {"claude_code", "codex", "pi"}
        present = {p.name for p in (tmp_path / "built" / tier / "agents").iterdir()}
        # The specs must go too: an undeclared-but-present spec can still be
        # dispatched to, on a model this tier is meant to exclude.
        assert present == set(config["tools"]["agents"])


def test_every_tier_keeps_two_vendors_so_cross_review_still_works(tmp_path, stock):
    """Cross-vendor review is polly's core value: a reviewer must be a different
    vendor than the implementer, which takes at least two workers."""
    built = _build(tmp_path, stock)

    for config in built.values():
        assert len(config["tools"]["agents"]) >= 2


def test_economy_spends_nothing_on_claude(tmp_path, stock):
    """The cheap tier's point is a genuinely different cost profile. A Claude
    worker would put Claude spend back in, and claude-native cannot run a GPT or
    GLM pin instead, so the slot is dropped rather than repriced."""
    built = _build(tmp_path, stock)
    economy = built["polly-economy"]

    assert "claude_code" not in economy["tools"]["agents"]
    assert "claude" not in economy["executor"]["model"]
    for worker in economy["tools"]["agents"]:
        assert "claude" not in _worker(tmp_path, "polly-economy", worker)["executor"]["model"]


def test_economy_brain_avoids_the_anthropic_wire_api(tmp_path, stock):
    """claude-sdk speaks Anthropic Messages, so it cannot drive the GPT model
    this tier's brain is meant to run. Pinning the model without also moving the
    harness would fail at resolve time."""
    built = _build(tmp_path, stock)

    assert built["polly-economy"]["executor"]["config"]["harness"] == "pi"


def test_worker_models_are_pinned_per_tier(tmp_path, stock):
    """Pinning on the spec is what makes a tier a fixed model set rather than a
    suggestion the brain can drift away from."""
    _build(tmp_path, stock)

    economy_pi = _worker(tmp_path, "polly-economy", "pi")
    frontier_claude = _worker(tmp_path, "polly-frontier", "claude_code")

    assert economy_pi["executor"]["model"] == "databricks-glm-5-2"
    assert frontier_claude["executor"]["model"] == "databricks-claude-opus-5"


def test_the_prompt_override_is_appended_not_substituted(tmp_path, stock):
    """Upstream's prompt keeps arriving from the wheel; we only append. Editing
    it would mean re-editing 200 lines of someone else's prose every upgrade."""
    built = _build(tmp_path, stock)
    prompt = built["polly-economy"]["prompt"]

    assert prompt.startswith(STOCK_PROMPT)
    assert "WORKSHOP MODEL POLICY" in prompt


def test_the_override_names_only_roster_workers_and_their_probes(tmp_path, stock):
    """The stock prose introduces six workers by name, so a trimmed roster
    contradicts it. The override has to be specific enough to win."""
    built = _build(tmp_path, stock)
    prompt = built["polly-economy"]["prompt"]

    assert "`codex`" in prompt and "`pi`" in prompt
    assert "opencode" not in prompt.split("WORKSHOP MODEL POLICY")[1]
    # The preflight must not probe for CLIs this tier dropped, or the brain
    # reports them missing at the human and muddies the roster.
    assert 'command -v codex pi || true' in prompt


def test_the_override_tells_the_brain_not_to_repick_models(tmp_path, stock):
    """A tier exists so the attendee sees what THESE models cost. A brain that
    helpfully upgrades a worker via args.model destroys the comparison."""
    built = _build(tmp_path, stock)

    assert "do NOT pass `args.model`" in built["polly-balanced"]["prompt"]


def test_only_the_pi_slot_can_switch_harness_mid_session(tmp_path, stock):
    """args.harness needs an allowlist opt-in. pi and codex both serve gateway
    models, so that slot can flip vendor in-session; an allowlist including a
    Claude harness would let the brain pin a model the slot cannot resolve."""
    _build(tmp_path, stock)

    pi = _worker(tmp_path, "polly-balanced", "pi")
    codex = _worker(tmp_path, "polly-balanced", "codex")

    assert pi["executor"]["config"]["allowed_harnesses"] == ["pi", "codex"]
    assert "allowed_harnesses" not in codex["executor"].get("config", {})


def test_model_pins_are_env_overridable(tmp_path, stock):
    """A renamed or withdrawn serving endpoint has to be fixable as a values
    change: an operator finding a dead pin mid-workshop cannot wait on a
    release."""
    built = _build(
        tmp_path,
        stock,
        env={
            "POLLY_ECONOMY_WORKER_PI_MODEL": "databricks-something-else",
            "POLLY_FRONTIER_BRAIN_MODEL": "databricks-claude-opus-9",
        },
    )

    assert built["polly-frontier"]["executor"]["model"] == "databricks-claude-opus-9"
    assert (
        _worker(tmp_path, "polly-economy", "pi")["executor"]["model"]
        == "databricks-something-else"
    )


def test_the_economy_brain_can_be_reverted_to_a_claude_brain(tmp_path, stock):
    """The pi brain is the least-proven choice here, so the escape hatch that
    makes it reversible without a code change is worth pinning."""
    built = _build(
        tmp_path,
        stock,
        env={
            "POLLY_ECONOMY_BRAIN_HARNESS": "claude-sdk",
            "POLLY_ECONOMY_BRAIN_MODEL": "databricks-claude-haiku-4-5",
        },
    )
    economy = built["polly-economy"]

    assert economy["executor"]["config"]["harness"] == "claude-sdk"
    assert economy["executor"]["model"] == "databricks-claude-haiku-4-5"


def test_rebuilding_is_idempotent(tmp_path, stock):
    """Registration runs on every App start, including restarts, so a second
    build must not accumulate a doubled prompt or leftover worker dirs."""
    first = _build(tmp_path, stock)
    second = _build(tmp_path, stock)

    assert first == second
    assert second["polly-economy"]["prompt"].count("WORKSHOP MODEL POLICY") == 1


def test_skills_survive_the_derivation(tmp_path, stock):
    """polly's skills (fanout / cross-review / investigate) are how it actually
    orchestrates; a variant that loses them is a downgrade, not a model swap."""
    _build(tmp_path, stock)

    assert (tmp_path / "built" / "polly-economy" / "skills" / "fanout" / "SKILL.md").is_file()


def test_guardrails_and_spawn_survive_the_derivation(tmp_path, stock):
    """The guardrail policies are the mechanism-layer enforcement polly relies
    on; silently dropping them would make a variant less safe than stock."""
    built = _build(tmp_path, stock)

    for config in built.values():
        assert config["guardrails"]["ask_timeout"] == 86400
        assert config["spawn"] is True


def test_every_pin_has_a_declared_env_knob(tmp_path, stock):
    """Databricks Apps env is declared in app.yaml. A pin whose knob is missing
    there is not actually overridable, which is exactly the case an operator
    hits when a model name goes stale."""
    declared = {
        item["name"]
        for item in yaml.safe_load((APP_DIR / "app.yaml").read_text())["env"]
        if isinstance(item, dict)
    }

    for tier in polly_variants.TIERS:
        assert polly_variants._env_key(tier.name, "BRAIN_MODEL") in declared
        for worker in tier.workers:
            key = polly_variants._env_key(tier.name, f"WORKER_{worker.upper()}_MODEL")
            assert key in declared, f"{key} missing from deploy/omnigent-app/app.yaml"
