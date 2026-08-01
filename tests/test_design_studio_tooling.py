"""The design skill's scripts have to work on an attendee's container.

`workshop-design-gate` is a blocking step in "is this build done?", so these
scripts are load-bearing: if one of them crashes, every attendee either ships an
unaudited app or gets stuck behind a gate that cannot go green. They also run
under whatever `python3` the image provides with no third-party packages
available, which is easy to break by importing something convenient.

Ported from the skill package's own suite so the checks live where CI runs them.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "assets" / "skills" / "workshop-design-studio"
SCRIPTS = SKILL / "scripts"
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "design-studio"


def run(script: str, *args: str, cwd: Path | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPTS / script), *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )


def system_data() -> dict:
    return {
        "product": "Travel Experience",
        "mode": "product-led",
        "goal": "Help families plan a memorable trip",
        "concept": "Editorial Minimalism",
        "style": {"style": "Editorial Minimalism", "description": "Refined"},
        "palette": {
            "name": "Oat and Cobalt",
            "background": "#F6F2EA",
            "foreground": "#172033",
            "primary": "#2457FF",
            "accent": "#E85D3F",
            "surface": "#FFFFFF",
            "muted": "#5E6878",
        },
        "typography": {
            "name": "Editorial Authority",
            "display_stack": "Georgia, serif",
            "body_stack": "Inter, system-ui, sans-serif",
        },
        "layout": {"name": "Editorial Split", "structure": "Narrative and visual"},
        "imagery": {"name": "Editorial Photography", "direction": "Confident crops"},
        "voice": {
            "name": "Editorial Evocative",
            "headline": "Memorable lines",
            "body": "Precise narrative",
        },
        "motion": {"name": "Section Reveal", "implementation": "Stagger once"},
        "signature_moment": "An itinerary transforms into a visual journey.",
        "avoid": ["Generic card grid"],
        "dials": {
            "expression": 7,
            "motion": 4,
            "density": 4,
            "depth": 5,
            "brand_fidelity": 8,
        },
    }


# --- the gate blocks the right things -----------------------------------------


def audit(name: str) -> tuple[int, dict]:
    result = run("audit_project.py", "--root", str(FIXTURES / name))
    return result.returncode, json.loads(result.stdout)


def test_a_well_built_app_passes_the_audit():
    code, data = audit("good-app")

    assert code == 0
    assert data["summary"]["critical"] == 0
    assert "NO_DESIGN_SYSTEM" not in {item["code"] for item in data["findings"]}


def test_the_audit_catches_what_makes_an_app_unusable():
    """Not style opinions — the four that make an app unusable for somebody:
    images nobody on a screen reader can identify, no visible focus for keyboard
    users, no reduced-motion path, and a layout that breaks on a phone."""
    code, data = audit("bad-app")

    assert code == 1
    codes = {item["code"] for item in data["findings"]}
    assert {"IMG_ALT", "NO_FOCUS", "NO_REDUCED_MOTION", "FIXED_WIDTH"} <= codes


def test_the_quality_gate_agrees_with_the_audit():
    """`workshop-design-gate` exits on the gate, not the audit, so a gate that
    passed a failing app would make the whole blocking step decorative."""
    config = SKILL / "templates" / "quality-gate.json"

    good = run(
        "quality_gate.py", "--root", str(FIXTURES / "good-app"), "--config", str(config)
    )
    bad = run(
        "quality_gate.py", "--root", str(FIXTURES / "bad-app"), "--config", str(config)
    )

    assert good.returncode == 0, good.stdout + good.stderr
    assert json.loads(good.stdout)["ok"] is True
    assert bad.returncode == 1
    assert json.loads(bad.stdout)["ok"] is False


def test_the_audit_never_tells_an_attendee_to_look_like_databricks():
    """The attendee's app is their product. The platform it deploys to is not an
    art direction, and a finding that says otherwise would push every workshop
    app towards the same console-grey look."""
    _, data = audit("good-app")

    messages = " ".join(item["message"] for item in data["findings"]).lower()
    assert "databricks" not in messages


# --- direction generation ------------------------------------------------------


def test_three_directions_are_genuinely_different():
    """The agent picks one of these without showing the attendee. Three variants
    of the same idea would make that choice meaningless."""
    result = run(
        "generate_design_system.py",
        "premium family travel planner app warm editorial visual",
        "--project-name",
        "Wayfinder",
        "--directions",
        "3",
        "--json",
    )
    assert result.returncode == 0, result.stderr

    directions = json.loads(result.stdout)["directions"]
    assert len(directions) == 3
    assert len({item["concept"] for item in directions}) >= 2
    assert all(item["signature_moment"] for item in directions)


def test_a_second_run_does_not_overwrite_a_decision_already_made(tmp_path: Path):
    """Later phases regenerate the system. If that clobbered MASTER.md, an app
    would silently change visual language halfway through a build."""
    first = run(
        "generate_design_system.py",
        "playful science learning app for children",
        "--project-name",
        "Curiosity Lab",
        "--persist",
        "--output-dir",
        str(tmp_path),
        "--json",
    )
    assert first.returncode == 0, first.stderr

    master = tmp_path / ".design-studio" / "MASTER.md"
    master.write_text("human decision\n", encoding="utf-8")

    second = run(
        "generate_design_system.py",
        "completely different request",
        "--project-name",
        "Curiosity Lab",
        "--persist",
        "--output-dir",
        str(tmp_path),
        "--json",
    )
    assert second.returncode == 0, second.stderr
    assert master.read_text(encoding="utf-8") == "human decision\n"
    assert json.loads(second.stdout)["persistence"]["master_preserved"] is True


def test_out_of_range_dials_are_clamped_rather_than_crashing():
    result = run(
        "generate_design_system.py",
        "developer tool",
        "--expression",
        "99",
        "--motion",
        "0",
        "--json",
    )
    assert result.returncode == 0, result.stderr

    dials = json.loads(result.stdout)["directions"][0]["dials"]
    assert dials["expression"] == 10
    assert dials["motion"] == 1


def test_existing_brand_assets_win_over_inventing_a_new_look(tmp_path: Path):
    public = tmp_path / "public"
    public.mkdir()
    (public / "company-logo.svg").write_text("<svg/>", encoding="utf-8")

    result = run("generate_design_system.py", "customer account website",
                 "--root", str(tmp_path), "--json")
    assert result.returncode == 0, result.stderr

    assert json.loads(result.stdout)["directions"][0]["mode"] == "brand-led"


# --- supporting tools ----------------------------------------------------------


def test_the_moodboard_renders_with_a_reduced_motion_path(tmp_path: Path):
    source = tmp_path / "system.json"
    output = tmp_path / "moodboard.html"
    source.write_text(json.dumps(system_data()), encoding="utf-8")

    result = run(
        "render_moodboard.py",
        "--system", str(source),
        "--output", str(output),
        "--project-name", "Wayfinder",
    )
    assert result.returncode == 0, result.stderr

    html = output.read_text(encoding="utf-8")
    assert "Wayfinder" in html
    assert "Editorial Minimalism" in html
    assert "prefers-reduced-motion" in html


def test_the_contrast_checker_accepts_a_readable_system(tmp_path: Path):
    source = tmp_path / "system.json"
    source.write_text(json.dumps(system_data()), encoding="utf-8")

    result = run("check_contrast.py", "--system", str(source))

    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout)["ok"] is True


def test_the_implementation_brief_describes_the_product_not_the_platform(tmp_path: Path):
    studio = tmp_path / ".design-studio"
    studio.mkdir()
    (studio / "design-system.json").write_text(
        json.dumps(system_data()), encoding="utf-8"
    )
    (tmp_path / "package.json").write_text(
        '{"dependencies":{"react":"latest"}}', encoding="utf-8"
    )
    output = studio / "IMPLEMENTATION.md"

    result = run(
        "build_implementation_brief.py", "--root", str(tmp_path), "--output", str(output)
    )
    assert result.returncode == 0, result.stderr

    text = output.read_text(encoding="utf-8").lower()
    assert "deployment platform is not the brand" in text
    assert "editorial minimalism" in text
    assert "must use databricks" not in text


def test_the_stack_is_detected_from_a_real_project(tmp_path: Path):
    (tmp_path / "package.json").write_text(
        '{"dependencies":{"react":"latest","tailwindcss":"latest"}}', encoding="utf-8"
    )

    result = run("project_detect.py", "--root", str(tmp_path), "--json")
    assert result.returncode == 0, result.stderr

    data = json.loads(result.stdout)
    assert "react" in data["stacks"]
    assert "tailwind" in data["styling"]


def test_brand_assets_already_in_the_project_are_found(tmp_path: Path):
    assets = tmp_path / "public"
    assets.mkdir()
    (assets / "brand-logo.svg").write_text("<svg/>", encoding="utf-8")

    result = run("project_detect.py", "--root", str(tmp_path), "--json")
    assert result.returncode == 0, result.stderr

    data = json.loads(result.stdout)
    assert any("brand-logo.svg" in item for item in data["brand_assets"])


# --- retrieval -----------------------------------------------------------------


@pytest.fixture(scope="module")
def core():
    sys.path.insert(0, str(SCRIPTS))
    try:
        import core  # noqa: PLC0415
    finally:
        sys.path.remove(str(SCRIPTS))
    return core


def test_retrieval_finds_the_intent_it_was_asked_for(core):
    result = core.search_domain(
        "premium sustainable fashion ecommerce product storytelling", "product", 3
    )

    assert result["count"] > 0
    assert any(
        "commerce" in row.get("product", "").lower() or "fashion" in str(row).lower()
        for row in result["results"]
    )


def test_no_match_is_disclosed_rather_than_faked(core):
    """A fabricated "closest match" would send the agent off building to a
    direction nothing in the corpus actually supports."""
    result = core.search_domain("xyzzynonexistentterm", "style", 3)

    assert result["count"] == 0
    assert isinstance(result["suggestions"], list)


def test_stack_guidance_comes_from_the_matching_stack_file(core):
    result = core.search_stack("responsive accessible component", "react", 3)

    assert result["count"] > 0
    assert result["source"].endswith("react.csv")


def test_an_unknown_domain_says_so(core):
    result = core.search_domain("anything", "unknown", 3)

    assert "error" in result
    assert "available" in result


# --- the corpus itself ---------------------------------------------------------


def test_every_curated_palette_is_readable_before_an_agent_picks_it():
    """The agent selects a palette without a human reviewing it, so an
    unreadable pair in the corpus ships straight to an attendee's app."""

    def luminance(value: str) -> float:
        value = value.lstrip("#")
        channels = [int(value[i:i + 2], 16) / 255 for i in (0, 2, 4)]
        linear = [
            c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4
            for c in channels
        ]
        return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]

    def contrast(a: str, b: str) -> float:
        high, low = sorted((luminance(a), luminance(b)), reverse=True)
        return (high + 0.05) / (low + 0.05)

    with (SKILL / "data" / "palettes.csv").open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))

    assert rows
    for row in rows:
        assert contrast(row["foreground"], row["background"]) >= 4.5, row["name"]
        assert contrast(row["foreground"], row["surface"]) >= 4.5, row["name"]


def test_the_scripts_run_on_a_bare_python(core):
    """The attendee container ships a plain python3 with no third-party packages
    and no network to install any, so a stray `import requests` here would fail
    at the gate rather than in CI."""
    third_party = {"requests", "numpy", "pandas", "yaml", "jinja2", "PIL", "bs4"}

    for script in sorted(SCRIPTS.glob("*.py")):
        source = script.read_text(encoding="utf-8")
        for module in third_party:
            assert f"import {module}" not in source, f"{script.name} imports {module}"
