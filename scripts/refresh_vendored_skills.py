#!/usr/bin/env python3
"""Re-vendor assets/skills from the skills ref the reviewed manifest pins.

The vendored tree is the offline fallback for the boot-time overlay, so it must
be the *same* content boot would install. This clones the manifest's pinned
commit, verifies the clone against the manifest's ``content_sha256``, and only
then replaces the upstream-sourced skill directories -- leaving the fork-only
skills (the apx skill, this repo's workflow/superpowers set, and the refresh
skill itself) untouched.

``--check`` verifies the committed fallback without rewriting it, which is what
CI runs; a drift means an operator hand-edited a vendored skill or bumped the
manifest without re-vendoring.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile


sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server.bootstrap import install  # noqa: E402
from server.bootstrap.artifacts import directory_checksum, load_manifest  # noqa: E402


VENDORED_DIR = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "assets", "skills")
)
# Skills with no upstream counterpart. A refresh must never delete these.
FORK_ONLY = frozenset({
    "bdd-features",
    "bdd-run",
    "bdd-scaffold",
    "bdd-steps",
    "brainstorming",
    "databricks-app-apx",
    "dispatching-parallel-agents",
    "executing-plans",
    "finishing-a-development-branch",
    "promote",
    "receiving-code-review",
    "refresh-databricks-skills",
    "requesting-code-review",
    "subagent-driven-development",
    "systematic-debugging",
    "test-driven-development",
    "using-git-worktrees",
    "using-superpowers",
    "verification-before-completion",
    "writing-plans",
    "writing-skills",
})


def _directories(path: str) -> set[str]:
    return {
        name
        for name in os.listdir(path)
        if os.path.isdir(os.path.join(path, name))
    }


def _clone_reviewed_skills(destination: str) -> str:
    """Clone the manifest's pinned commit and return its skills directory."""
    entry = load_manifest("")["artifacts"]["databricks_agent_skills"]
    subprocess.run(
        ["git", "clone", "--quiet", "--depth", "1", "--branch", entry["version"],
         entry["source"], destination],
        check=True, capture_output=True, text=True, timeout=600,
    )
    commit = subprocess.run(
        ["git", "-C", destination, "rev-parse", "HEAD"],
        check=True, capture_output=True, text=True, timeout=60,
    ).stdout.strip()
    if commit.lower() != str(entry["commit"]).lower():
        raise SystemExit(
            f"clone of {entry['version']} is {commit}, "
            f"manifest pins {entry['commit']}"
        )
    upstream = os.path.join(destination, install.SKILLS_UPSTREAM_DIR)
    names = _directories(upstream)
    if not names:
        raise SystemExit(f"{install.SKILLS_UPSTREAM_DIR}/ is empty at {commit}")
    actual = directory_checksum(upstream, names)
    if actual != entry["content_sha256"]:
        raise SystemExit(
            f"clone digest {actual} does not match manifest "
            f"{entry['content_sha256']}"
        )
    return upstream


def refresh(*, write: bool) -> int:
    with tempfile.TemporaryDirectory(prefix="refresh-skills-") as clone:
        upstream = _clone_reviewed_skills(clone)
        upstream_names = _directories(upstream)
        vendored_names = _directories(VENDORED_DIR)
        retired = sorted(vendored_names - FORK_ONLY - upstream_names)
        added = sorted(upstream_names - vendored_names)
        if not write:
            drift = directory_checksum(VENDORED_DIR, upstream_names) != (
                directory_checksum(upstream, upstream_names)
            )
            if retired or added or drift:
                print(
                    "vendored fallback differs from the reviewed skills ref; "
                    "run --write",
                    file=sys.stderr,
                )
                for name in retired:
                    print(f"  retired: {name}", file=sys.stderr)
                for name in added:
                    print(f"  missing: {name}", file=sys.stderr)
                if drift:
                    print("  content digest differs", file=sys.stderr)
                return 1
            print(f"vendored fallback matches {len(upstream_names)} reviewed skills")
            return 0
        for name in retired:
            shutil.rmtree(os.path.join(VENDORED_DIR, name))
        for name in upstream_names:
            target = os.path.join(VENDORED_DIR, name)
            shutil.rmtree(target, ignore_errors=True)
            shutil.copytree(os.path.join(upstream, name), target)
    print(f"vendored {len(upstream_names)} skills into {VENDORED_DIR}")
    for name in retired:
        print(f"  retired: {name}")
    for name in added:
        print(f"  added:   {name}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--write", action="store_true", help="re-vendor assets/skills"
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="fail when the vendored fallback differs from the reviewed ref",
    )
    args = parser.parse_args()
    if not args.write and not args.check:
        parser.error("pass --write or --check")
    return refresh(write=args.write)


if __name__ == "__main__":
    raise SystemExit(main())
