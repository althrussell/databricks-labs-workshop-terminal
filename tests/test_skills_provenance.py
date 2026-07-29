"""Guards on the vendored skills fallback and the names agents are told to use.

These are offline checks: they compare the committed fallback, the reviewed
manifest, and `SKILLS_SOURCE.md` against each other. Verifying any of them
against the live upstream repo needs the network and belongs to
`scripts/refresh_vendored_skills.py --check`, which the scheduled workflow runs.
"""

import os
import re

from server.bootstrap import install
from server.bootstrap.artifacts import directory_checksum, load_manifest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SKILLS_DIR = os.path.join(REPO, "assets", "skills")
SOURCE_DOC = os.path.join(SKILLS_DIR, "SKILLS_SOURCE.md")

# Names that were vendored from the deprecated ai-dev-kit and no longer exist
# upstream. An agent told to use one of these gets no skill at all and no error,
# so a stale name in prose is worse than a missing instruction.
RETIRED_SKILLS = (
    "databricks-bundles",
    "databricks-config",
    "databricks-genie",
    "databricks-lakebase-autoscale",
    "databricks-lakebase-provisioned",
    "databricks-spark-declarative-pipelines",
    "spark-python-data-source",
)


def _skill_entry():
    return load_manifest("")["artifacts"]["databricks_agent_skills"]


def _source_doc_fields():
    text = open(SOURCE_DOC).read()
    fields = {}
    for label, key in (
        ("Pinned tag", "version"),
        ("Pinned commit", "commit"),
        ("Content SHA-256", "content_sha256"),
        ("Upstream repo", "source"),
    ):
        match = re.search(rf"^\|\s*{re.escape(label)}\s*\|\s*`?([^`|]+)`?\s*\|",
                          text, re.MULTILINE)
        assert match, f"{SOURCE_DOC} has no `{label}` row"
        fields[key] = match.group(1).strip()
    return fields


def test_source_doc_agrees_with_the_reviewed_manifest():
    """`SKILLS_SOURCE.md` is what a human reads to answer "which skills are we
    on?". If it drifts from the manifest, the answer is a lie."""
    entry = _skill_entry()
    doc = _source_doc_fields()

    assert doc["version"] == entry["version"]
    assert doc["commit"] == entry["commit"]
    assert doc["content_sha256"] == entry["content_sha256"]
    # The doc links the repo (no .git suffix); the manifest clones it.
    assert entry["source"].startswith(doc["source"])


def test_vendored_fallback_digest_matches_the_reviewed_manifest():
    """The fallback must be the same content boot installs, so a network failure
    degrades the *source* of the skills, never the skill set itself."""
    import sys

    sys.path.insert(0, os.path.join(REPO, "scripts"))
    from refresh_vendored_skills import FORK_ONLY

    vendored = {
        name
        for name in os.listdir(SKILLS_DIR)
        if os.path.isdir(os.path.join(SKILLS_DIR, name))
    }
    upstream_names = vendored - FORK_ONLY
    assert upstream_names, "no upstream skills are vendored"
    assert directory_checksum(SKILLS_DIR, upstream_names) == (
        _skill_entry()["content_sha256"]
    ), (
        "vendored assets/skills differs from the reviewed manifest digest; run "
        "scripts/refresh_vendored_skills.py --write"
    )


def test_fork_only_skills_survive_a_refresh():
    """The refresh script deletes any vendored directory that is neither
    upstream nor listed as fork-only, so an unlisted fork skill would silently
    disappear on the next skills bump."""
    import sys

    sys.path.insert(0, os.path.join(REPO, "scripts"))
    from refresh_vendored_skills import FORK_ONLY

    for name in ("promote", "refresh-databricks-skills", "databricks-app-apx"):
        assert os.path.isdir(os.path.join(SKILLS_DIR, name))
        assert name in FORK_ONLY


# These two guards may only police text this fork owns. Skills vendored verbatim
# from upstream cross-reference their own history, and hand-editing one would
# break the fallback digest -- upstream's wording is upstream's problem.
_SKIP_DIRS = {
    ".git", ".databricks", "node_modules", "__pycache__", ".pytest_cache",
    ".venv", "static", "dist", ".ruff_cache",
}
_SELF = "tests/test_skills_provenance.py"


def _fork_owned_text_files():
    import sys

    sys.path.insert(0, os.path.join(REPO, "scripts"))
    from refresh_vendored_skills import FORK_ONLY

    upstream_skill_dirs = {
        os.path.join("assets", "skills", name)
        for name in os.listdir(SKILLS_DIR)
        if os.path.isdir(os.path.join(SKILLS_DIR, name)) and name not in FORK_ONLY
    }
    for root, dirs, files in os.walk(REPO):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS]
        relative_root = os.path.relpath(root, REPO)
        if any(
            relative_root == skill or relative_root.startswith(skill + os.sep)
            for skill in upstream_skill_dirs
        ):
            continue
        for name in files:
            if not name.endswith(
                (".py", ".md", ".ts", ".tsx", ".yml", ".yaml", ".json", ".sh")
            ):
                continue
            path = os.path.join(root, name)
            if os.path.islink(path):
                continue
            try:
                yield os.path.relpath(path, REPO), open(path, encoding="utf-8").read()
            except (UnicodeDecodeError, OSError):
                continue


def test_no_retired_skill_name_is_referenced_in_fork_owned_text():
    offenders = []
    for relative, text in _fork_owned_text_files():
        # The retired-names table and these guards must name them to do their job.
        if relative in {
            os.path.join("assets", "skills", "SKILLS_SOURCE.md"),
            _SELF,
            os.path.join("tests", "test_user_content.py"),
        }:
            continue
        for retired in RETIRED_SKILLS:
            if retired in text:
                offenders.append(f"{relative}: {retired}")
    assert not offenders, "retired skill names still referenced:\n" + "\n".join(
        offenders
    )


def test_fork_owned_text_carries_no_stale_project_codename():
    """This terminal was forked from an internal project with its own codename.
    Comments and attendee-facing text still crediting it are meaningless to
    anyone reading the repo now."""
    offenders = [
        relative
        for relative, text in _fork_owned_text_files()
        if re.search(r"\bcoda\b", text, re.IGNORECASE) and relative != _SELF
    ]
    assert not offenders, f"stale project codename found in: {offenders}"


def test_skills_upstream_directory_is_the_current_one():
    """The deprecated kit served skills from `databricks-skills/`; the current
    repo uses `skills/`. Reading the wrong one copies nothing and silently falls
    back to the vendored set."""
    assert install.SKILLS_UPSTREAM_DIR == "skills"
