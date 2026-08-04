"""Workshop Polly variants — one orchestrator, three model sets.

The workshop's proposition is that an attendee builds something, reads the
session's real token and cost figures, then builds the same thing again on a
different set of models and compares. Omnigent already reports per-session
tokens and per-model attribution, so nothing here measures anything: all these
variants do is make the *model choice* selectable, which is the only part an
attendee cannot otherwise control.

Each variant is derived at App startup from the stock ``polly`` bundle that
ships inside the installed wheel, then patched. Deriving rather than vendoring
three copies is deliberate: the stock bundle is a ~350-line prompt plus six
worker specs, three skills, and a guardrail policy set, and a vendored copy
would silently keep running the old prompt after an Omnigent upgrade. Patching
a fresh copy each boot means a variant inherits every upstream fix and the diff
we own stays small enough to read.

Stock ``polly`` is registered by nothing here and stays the default, so an
attendee who ignores all of this gets exactly the pre-existing behaviour.

What we deliberately do NOT patch:

- The upstream prompt's prose. A trimmed roster contradicts it (it introduces
  six sub-agents by name), so we append an authoritative override block rather
  than editing 200 lines we would have to re-edit on every upgrade.
- Reasoning effort. In 0.7.0 it is a per-session/per-task hint on the
  conversation, not a spec field, so a variant cannot pin it. The frontier
  description tells the attendee to set it in the session instead of us
  pretending it is configured.
"""

from __future__ import annotations

import logging
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

# Only these three CLIs are installed in the Workshop Terminal image, so a
# roster naming opencode / cursor / hermes would burn the brain's first turn
# discovering they cannot boot. Cross-vendor review needs two vendors, so every
# tier keeps at least two.
_CLAUDE = "claude_code"
_CODEX = "codex"
_PI = "pi"


@dataclass(frozen=True)
class Tier:
    """One selectable model set.

    :param name: Agent name, and what the attendee picks in the App.
    :param headline: Attendee-facing description shown beside the name.
    :param brain_harness: Harness driving the orchestrator itself.
    :param brain_model: Model for the orchestrator, or ``None`` for the
        harness's configured default.
    :param workers: Worker directory name -> pinned model. Order is the roster
        order handed to the brain.
    """

    name: str
    headline: str
    brain_harness: str
    brain_model: str | None
    workers: dict[str, str] = field(default_factory=dict)


# The Claude workers can only be pinned to Claude models: claude-native speaks
# the Anthropic wire API, so a GPT or GLM pin on that slot cannot resolve. That
# is why the economy tier drops the Claude worker entirely rather than pinning
# it to a cheap Claude — the workers are where a tier's spend actually lands,
# so keeping them off the Claude endpoints is what makes the comparison real,
# and codex + pi still supply two vendors for cross-review.
#
# Each tier gives its three worker slots three different vendors. Balanced and
# frontier used to pin codex and pi to the same model, which cost them a vendor
# and made the session's per-model cost table unreadable: two roster entries
# reporting the same model name, so an attendee clicking into a sub-agent could
# not tell which slot they were looking at. The tier rungs are the Claude and
# GPT chains; the pi slot is the third vendor throughout.
#
# Non-Claude pins are spelled `system.ai.<model>`, not `databricks-<model>`.
# Both reach the same endpoint, but pi is the constraint: it picks a surface by
# looking the id up in the per-surface model lists omnigent builds from the
# workspace, and those are spelled `system.ai.*`. A `databricks-` pin matches
# none of them and falls through to pi's primary provider — the gateway's
# Anthropic surface — which refuses a non-Claude model outright ("API type
# 'anthropic/v1/messages' is not supported by ..."). That is what silently sent
# every balanced dispatch to the Claude worker. The spelling also survives the
# pi slot flipping to the codex harness, which accepts either. Claude pins keep
# the `databricks-` form: the Anthropic surface is where they belong anyway.
TIERS: tuple[Tier, ...] = (
    Tier(
        name="polly-economy",
        headline=(
            "Cheapest model set: GLM-5.2 and GPT-5.6 Luna workers, orchestrated "
            "by Haiku. No Claude worker spend. Start here, then compare against "
            "polly-frontier."
        ),
        # The brain dispatches and reviews rather than writing code, so it is a
        # small slice of a tier's tokens and Haiku keeps it a small slice of the
        # cost too. It runs on claude-sdk, not pi, for the same reason balanced
        # and frontier do: the stock polly prompt was written against claude-sdk
        # and holds its dispatch protocol there, whereas the pi brain this tier
        # used to run re-sent titles that were still in flight and filled the
        # attendee's transcript with already-running errors. Reaching a GPT
        # brain again means moving the harness too, which is why the pair is
        # overridable together.
        brain_harness="claude-sdk",
        brain_model="databricks-claude-haiku-4-5",
        workers={
            _CODEX: "system.ai.gpt-5-6-luna",
            _PI: "system.ai.glm-5-2",
        },
    ),
    Tier(
        name="polly-balanced",
        headline=(
            "Sonnet 5 orchestrating, with Sonnet, GPT-5.6 Terra and GLM-5.2 "
            "workers to compare and review each other. A sensible default for "
            "real work."
        ),
        brain_harness="claude-sdk",
        brain_model="databricks-claude-sonnet-5",
        workers={
            _CLAUDE: "databricks-claude-sonnet-5",
            _CODEX: "system.ai.gpt-5-6-terra",
            _PI: "system.ai.glm-5-2",
        },
    ),
    Tier(
        name="polly-frontier",
        headline=(
            "Opus 5 orchestrating, with Opus, GPT-5.6 Sol and GLM-5.2 workers. "
            "The strongest and most expensive set — set reasoning effort to "
            "high in the session to push it further."
        ),
        brain_harness="claude-sdk",
        brain_model="databricks-claude-opus-5",
        # Sol rather than Terra, which is where balanced sits: the GPT-5.6 tiers
        # are capability names, Sol being the flagship, and a tier whose whole
        # purpose is to be the expensive end of the comparison should take the
        # flagship on both vendors rather than the mid-tier on one.
        #
        # GLM on the third slot is the weakest pin in this tier, and it is here
        # for coverage rather than strength: a frontier reviewer that shares a
        # vendor with the implementer is the one thing this tier cannot afford,
        # and GLM-5.2 is the strongest third vendor we have confirmed served.
        # Re-pin it the moment a stronger one lands.
        workers={
            _CLAUDE: "databricks-claude-opus-5",
            _CODEX: "system.ai.gpt-5-6-sol",
            _PI: "system.ai.glm-5-2",
        },
    ),
)

# Letting the brain flip this slot's harness mid-session is the in-session
# counterpart to picking a tier up front: the same task can be re-run against
# another vendor without leaving the conversation. Only the pi slot gets it —
# pi and codex are the two harnesses that can serve a gateway model, and an
# allowlist wide enough to include a Claude harness would let the brain pin a
# model that slot cannot resolve.
_PI_ALLOWED_HARNESSES = ("pi", "codex")


def _env_key(tier_name: str, suffix: str) -> str:
    """``polly-economy`` + ``BRAIN_MODEL`` -> ``POLLY_ECONOMY_BRAIN_MODEL``."""
    return f"{tier_name.upper().replace('-', '_')}_{suffix}"


def _resolved(tier: Tier, env: dict[str, str]) -> Tier:
    """Apply env overrides to a tier.

    Every model pin is overridable so a renamed or withdrawn endpoint is a
    deployment values change rather than a code change — these names are the
    part of this module most likely to go stale, and an operator discovering a
    dead pin mid-workshop needs a fix that does not require a release.
    """
    brain_model = env.get(_env_key(tier.name, "BRAIN_MODEL"), "").strip()
    brain_harness = env.get(_env_key(tier.name, "BRAIN_HARNESS"), "").strip()
    workers = {}
    for worker, model in tier.workers.items():
        override = env.get(
            _env_key(tier.name, f"WORKER_{worker.upper()}_MODEL"), ""
        ).strip()
        workers[worker] = override or model
    return Tier(
        name=tier.name,
        headline=tier.headline,
        brain_harness=brain_harness or tier.brain_harness,
        brain_model=brain_model or tier.brain_model,
        workers=workers,
    )


def _roster_override(tier: Tier) -> str:
    """The block appended to the stock prompt to make the roster authoritative.

    The upstream prompt introduces six sub-agents and a preflight that probes
    all six. A tier declares fewer, and an undeclared name fails at dispatch,
    so the brain has to be told plainly which names exist. Appending wins over
    editing: the stock prose keeps arriving from upstream untouched, and this
    block is the entire diff a reader has to hold in their head.

    The override is scoped to the roster and the model pins for a reason. It
    used to claim it overrode "anything above it", which is everything the
    stock prompt says about dispatching — including the rule that a worker's
    result arrives through the inbox rather than from the send. A brain that
    discounts that re-sends the same title while its first turn is still
    running, is refused every time, and spins there; the weakest brain in the
    set (economy's) is the one that did. We only ever meant to fix which
    workers exist and what they run, so that is all this claims.
    """
    roster = "\n".join(
        f"  - `{worker}` — runs `{model}`." for worker, model in tier.workers.items()
    )
    probes = " ".join(
        {_CLAUDE: "claude", _CODEX: "codex", _PI: "pi"}[worker]
        for worker in tier.workers
    )
    return f"""

  ---

  WORKSHOP MODEL POLICY — this section overrides the ROSTER and the MODEL
  pins above it, and nothing else. Every other rule in this prompt still
  applies in full, in particular the dispatch and inbox protocol.

  You are `{tier.name}`, a fixed-model-set variant of polly used in a workshop
  where attendees compare what different models cost for the same task. Your
  roster is EXACTLY these workers, and no others exist:

{roster}

  Any worker named earlier in this prompt that is absent from that list is NOT
  available to you: never dispatch to it. Your roster preflight is
  `sys_os_shell("command -v {probes} || true")` — do not probe for other CLIs.

  Each worker is already pinned to the model shown above, which is the point of
  this variant: the attendee chose this tier to see what these models cost. So
  do NOT pass `args.model` to change a worker's model unless the human asks you
  to. Omitting `model` runs the pinned one, which is what you want.

  Cross-vendor review still applies: a diff is reviewed by a DIFFERENT worker
  than the one that wrote it.

  Dispatch stays asynchronous, exactly as described above: `sys_session_send`
  returns a launching handle, NOT the worker's answer, which arrives later in
  your inbox. Re-sending a title whose turn is still launching or running is
  rejected outright. So a re-send in place of a wait makes no progress and
  fills the attendee's transcript with errors — dispatch once, then wait.
"""


def _patch_config(path: Path, tier: Tier) -> None:
    config: dict[str, Any] = yaml.safe_load(path.read_text())
    config["name"] = tier.name
    config["description"] = tier.headline
    executor = config.setdefault("executor", {})
    executor.setdefault("config", {})["harness"] = tier.brain_harness
    if tier.brain_model:
        executor["model"] = tier.brain_model
    else:
        executor.pop("model", None)
    config.setdefault("tools", {})["agents"] = list(tier.workers)
    config["prompt"] = str(config.get("prompt", "")) + _roster_override(tier)
    path.write_text(yaml.safe_dump(config, sort_keys=False, width=100))


def _patch_worker(path: Path, model: str, *, allowed_harnesses: tuple[str, ...]) -> None:
    config: dict[str, Any] = yaml.safe_load(path.read_text())
    executor = config.setdefault("executor", {})
    executor["model"] = model
    if allowed_harnesses:
        executor.setdefault("config", {})["allowed_harnesses"] = list(allowed_harnesses)
    path.write_text(yaml.safe_dump(config, sort_keys=False, width=100))


def stock_polly_dir() -> Path:
    """Locate the polly bundle inside the installed omnigent wheel."""
    import omnigent

    return Path(omnigent.__file__).parent / "resources" / "examples" / "polly"


def build(dest_root: Path, *, env: dict[str, str] | None = None) -> list[Path]:
    """Materialize every tier under *dest_root*, returning the bundle dirs.

    :param dest_root: Directory to populate; one subdirectory per tier.
    :param env: Environment used for model overrides. Defaults to ``os.environ``.
    :returns: Bundle directories, ready to hand to Omnigent's registration.
    :raises FileNotFoundError: If the stock polly bundle is not in the wheel.
    """
    environ = dict(os.environ if env is None else env)
    source = stock_polly_dir()
    if not (source / "config.yaml").is_file():
        raise FileNotFoundError(f"stock polly bundle not found at {source}")

    built = []
    for tier in TIERS:
        resolved = _resolved(tier, environ)
        dest = dest_root / resolved.name
        if dest.exists():
            shutil.rmtree(dest)
        shutil.copytree(source, dest)

        _patch_config(dest / "config.yaml", resolved)
        agents_dir = dest / "agents"
        for worker_dir in sorted(agents_dir.iterdir()) if agents_dir.is_dir() else []:
            if not worker_dir.is_dir():
                continue
            model = resolved.workers.get(worker_dir.name)
            if model is None:
                # Drop specs the roster no longer names. Leaving them would let
                # a dispatch to an unlisted worker resolve and quietly run on a
                # model this tier is supposed to exclude.
                shutil.rmtree(worker_dir)
                continue
            _patch_worker(
                worker_dir / "config.yaml",
                model,
                allowed_harnesses=_PI_ALLOWED_HARNESSES if worker_dir.name == _PI else (),
            )
        built.append(dest)
        logger.info(
            "polly variant %s: brain %s on %s, workers %s",
            resolved.name,
            resolved.brain_model or "(harness default)",
            resolved.brain_harness,
            ", ".join(f"{w}={m}" for w, m in resolved.workers.items()),
        )
    return built
