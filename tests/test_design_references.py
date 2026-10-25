"""W5 composition references: present, honest, accessible, compilable, and never self-approved."""
from __future__ import annotations

import json
from pathlib import Path
import shutil
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import check_design_references as refs  # noqa: E402


def test_all_five_references_pass_their_checks():
    report = refs.check_all()
    assert report["problems"] == []
    assert sorted(report["references"]) == sorted(refs.PATTERNS)
    for result in report["references"].values():
        assert result["imports"] == ["react"] and result["controls"] >= 1


def test_token_contrast_meets_wcag_aa():
    assert round(refs.contrast("#000000", "#FFFFFF"), 1) == 21.0
    measured = refs.check_tokens(json.loads((refs.REFERENCES / "tokens.json").read_text()))
    assert measured["problems"] == []
    assert all(pair["ratio"] >= pair["minimum"] for pair in measured["pairs"])


@pytest.mark.parametrize("planted, problem", [
    ("onClick={() => {}}", "a no-op callback"),
    ("// TODO wire this", "placeholder or mock wording"),
    ('<button onClick={() => setItems([])}>Reset</button>', "a button without an explicit type"),
])
def test_the_checker_catches_dishonest_or_inaccessible_references(tmp_path, planted, problem):
    folder = tmp_path / "queue-detail"
    shutil.copytree(refs.REFERENCES / "queue-detail", folder)
    source = folder / "Reference.tsx"
    source.write_text(source.read_text().replace("export function QueueDetailReference", planted + "\nexport function QueueDetailReference", 1))
    assert problem in refs.check_reference("queue-detail", folder)["problems"]


def test_a_reference_must_show_every_state(tmp_path):
    folder = tmp_path / "guided-form"
    shutil.copytree(refs.REFERENCES / "guided-form", folder)
    source = folder / "Reference.tsx"
    source.write_text(source.read_text().replace('"loading"', '"busy"'))
    assert "no 'loading' example" in refs.check_reference("guided-form", folder)["problems"]


def test_references_copy_into_a_scaffolded_apx_ui_for_compilation(tmp_path):
    (tmp_path / "src" / "app" / "ui" / "components").mkdir(parents=True)
    written = refs.copy_into(tmp_path)
    assert sorted(path.stem for path in written) == sorted(refs.PATTERNS)
    assert all(path.parent.name == "design-references" for path in written)


def test_baselines_are_measured_but_never_claim_human_approval():
    for name in refs.PATTERNS:
        baseline = json.loads((refs.REFERENCES / name / "baseline.json").read_text())
        assert baseline["status"] == "pending_owner_review"
        assert baseline["measured"]["problems"] == []
        assert "approved" not in json.dumps(baseline["screenshots"])


def test_the_apx_qualification_compiles_the_references():
    source = (ROOT / "scripts" / "check_apx_toolchain.py").read_text()
    assert source.index("copy_into(project)") < source.index('["apx", "dev", "check"]')


def test_agents_find_the_references_without_extra_start_up_context():
    from server.users import User

    env = User("ref@example.com").shell_env()
    assert Path(env["WORKSHOP_DESIGN_REFERENCES"]).joinpath("queue-detail", "README.md").is_file()
    contract = (ROOT / "assets" / "instructions" / "workshop_contract.md").read_text()
    assert "$WORKSHOP_DESIGN_REFERENCES" in contract
