"""The attendee's own copy: say what is true and tell them how to keep it.

The instructions used to promise that committing made an attendee's work "survive
after this environment is torn down". The post-commit hook does sync every commit
to `/Workspace/Users/{email}/projects/...`, which survives a container restart and
a redeploy — but Control Tower's teardown runs `delete_workspace` and
`drop_catalog`, so the Workspace folder and the promote Volume both go with the
event. The promise held right up to the only moment it mattered.

The fix is wording plus a prompt, not storage: persisting attendee content for them
would mean the Terminal owning a durable store for attendee data, which is the
dependency `docs/adr/0001-workshop-insight-capture.md` exists to refuse. So these
tests pin the honesty instead, across every surface that states it — because a
future edit tightening the copy is exactly how the old promise would come back.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
INSTRUCTIONS = ROOT / "assets" / "instructions" / "CLAUDE.md"
README = ROOT / "README.md"
PACK = ROOT / "content" / "default_pack.json"


@pytest.fixture(scope="module")
def instructions() -> str:
    return INSTRUCTIONS.read_text()


@pytest.fixture(scope="module")
def pack() -> dict:
    return json.loads(PACK.read_text())


# --- the false promise is gone, everywhere it was made ------------------------


@pytest.mark.parametrize("path", [INSTRUCTIONS, README])
def test_nothing_claims_the_work_outlives_the_environment(path: Path):
    """The exact phrasings that were wrong. Both files described the Workspace
    sync as the thing that gets an attendee's work out of the workshop."""
    text = path.read_text()

    assert "survives after this environment is torn down" not in text
    assert "survives teardown" not in text


def test_the_sync_is_described_by_what_it_actually_protects(instructions: str):
    """The hook is still worth having — it is why a crashed container or a
    redeploy doesn't cost the morning's work. Overstating it is what broke."""
    assert "restart or a redeploy can't lose it" in instructions
    assert "not** a take-home" in instructions


def test_the_post_commit_hook_does_not_promise_more_than_it_delivers():
    """This comment is the source of the claim: someone reading the hook to
    understand what it guarantees is who wrote the instruction copy."""
    from server.user_content import _POST_COMMIT

    assert "does not outlive the workshop" in _POST_COMMIT
    assert "survives workshop teardown" not in _POST_COMMIT


# --- and the attendee is told what to do instead ------------------------------


def test_the_wrap_guidance_names_both_routes_out(instructions: str):
    """Wording alone would leave the attendee informed and stuck. A push to a
    remote they own takes the history; a download takes the files. Nothing else
    reaches a machine they keep."""
    assert "git remote add origin" in instructions
    assert "git push -u origin main" in instructions
    assert "Download the files they care about" in instructions


def test_the_agent_is_told_not_to_persist_the_attendee_s_token(instructions: str):
    """The obvious way to make `git push` work unattended is to bake a token into
    the remote URL — which commits the attendee's credential into a repo on a
    machine they are about to lose control of."""
    assert "never bake it into the remote URL" in instructions


def test_the_wrap_guidance_distinguishes_committed_from_saved(instructions: str):
    """An attendee who commits all day has every reason to think they are safe;
    that assumption is precisely what needs naming."""
    assert "is not the same as \"it's saved\"" in instructions


# --- the attendee sees it without having to ask -------------------------------


def _nugget(pack: dict, nugget_id: str) -> dict:
    for nugget in pack["nuggets"]:
        if nugget["id"] == nugget_id:
            return nugget
    raise AssertionError(f"no nugget {nugget_id!r} in the default pack")


def test_the_wrap_pane_says_it_unprompted(pack: dict):
    """Untriggered and pinned: an attendee who never mentions saving anything is
    the one who needs to read this."""
    nugget = _nugget(pack, "wrap-take-it-with-you")

    assert nugget["phases"] == ["wrap"]
    assert nugget["triggers"] == []
    assert nugget["pinned"] is True
    assert "Committed is not the same as saved" in nugget["markdown"]


def test_the_wrap_pane_is_specific_about_what_disappears(pack: dict):
    """"Everything is deleted" invites the reader to assume their case is the
    exception. Naming the three places their work sits does not."""
    markdown = _nugget(pack, "wrap-take-it-with-you")["markdown"]

    assert "workspace folder your commits sync to" in markdown
    assert "Volume your handoff docs go to" in markdown


def test_one_click_hands_the_problem_to_the_agent(pack: dict):
    """The nugget's CTA and the wrap chip carry the same prompt, so the attendee
    can act from either surface without composing the ask themselves."""
    nugget = _nugget(pack, "wrap-take-it-with-you")
    chip = next(
        c for c in pack["prompts"]["wrap"] if c["label"] == "Take my work with me"
    )

    assert nugget["prompt"] == chip["prompt"]
    assert "git remote I own" in chip["prompt"]


def test_the_promote_card_no_longer_implies_the_volume_is_durable(pack: dict):
    """The promote docs are worth generating, but the Volume they land in is
    dropped with the catalog — so the card offers them for today, not forever."""
    markdown = _nugget(pack, "wrap-promote-anyway")["markdown"]

    assert "so you can read them today" in markdown
