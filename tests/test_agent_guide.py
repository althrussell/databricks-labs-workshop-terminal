"""Keep the coding-agent guide navigable and keep it out of attendee sessions.

The guide (AGENTS.md, docs/agent-guide/, nested AGENTS.md files) is only useful
while every path, symbol, and link it names still exists. These tests fail when
code moves without its documentation, which is the point: update the guide in
the same change (see docs/agent-guide/CHANGE_GUIDE.md#maintenance-table).

They also hold the one placement rule that protects attendees. Source deploys
put this repository at /app/python/source_code and attendee homes below it
(DATA_ROOT=/app/python/source_code/data). Claude Code loads CLAUDE.md,
CLAUDE.local.md and .claude/CLAUDE.md from every ancestor of its working
directory, so a repo-root copy would be injected into every attendee's agent.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
GUIDE = ROOT / "docs" / "agent-guide"
ENTRY = ROOT / "AGENTS.md"

# Directories never worth walking: dependencies, build output, VCS internals,
# and nested worktrees some tools create under .claude/worktrees.
_PRUNE = {"node_modules", ".venv", ".git", "__pycache__", "dist", "dist-repro", "test-results"}
_PRUNE_RELATIVE = {".claude/worktrees", "data"}
# Packaged into the PEX (scripts/build_release.py::RUNTIME_ROOTS) and partly
# copied into attendee homes, so they must not carry repo instructions.
_RUNTIME_ROOTS = ("server", "static", "content", "assets")
_TOP_LEVEL = (
    "server/", "frontend/", "static/", "assets/", "content/", "scripts/", "tests/",
    "evals/", "deploy/", "docs/", ".github/", ".claude/",
)
_ROOT_FILES = {
    "AGENTS.md", "Makefile", "app.yaml", "pyproject.toml", "uv.lock", "README.md",
    "requirements.txt", "requirements.in", "requirements-dev.txt", "requirements-dev.in",
    "dependency_sources.py", ".gitignore", ".gitattributes",
}
# Named by the guide precisely because they must not exist (see the first test).
_FORBIDDEN_AT_ROOT = ("CLAUDE.md", "CLAUDE.local.md", ".claude/CLAUDE.md", ".claude/CLAUDE.local.md", ".claude/rules")
_FENCE = re.compile(r"^```.*?^```", re.S | re.M)
_SHELL_FENCE = re.compile(r"^```(?:bash|sh|shell)\n(.*?)^```", re.S | re.M)
_LINK = re.compile(r"\[[^\]]*\]\(([^)\s]+)\)")
_CODE = re.compile(r"`([^`\s]+)`")
_SHELL_PATH = re.compile(
    r"(?<![\w/.$-])((?:server|frontend|static|assets|content|scripts|tests|evals|deploy|docs|\.github)/[\w./*-]*[\w*])"
)


def _walk_markdown(name: str) -> list[Path]:
    found = []
    stack = [ROOT]
    while stack:
        directory = stack.pop()
        for child in directory.iterdir():
            relative = child.relative_to(ROOT).as_posix()
            if child.is_dir():
                if child.name not in _PRUNE and relative not in _PRUNE_RELATIVE and not child.is_symlink():
                    stack.append(child)
            elif child.name == name:
                found.append(child)
    return sorted(found)


def _guide_documents() -> list[Path]:
    return sorted({ENTRY, *GUIDE.rglob("*.md"), *_walk_markdown("AGENTS.md")})


def _slug(heading: str) -> str:
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", heading).strip().lower()
    return re.sub(r"[^\w\- ]", "", text).replace(" ", "-")


def _anchors(path: Path) -> set[str]:
    text = _FENCE.sub("", path.read_text())
    seen: Counter[str] = Counter()
    anchors = set()
    for line in text.splitlines():
        match = re.match(r"^#{1,6}\s+(.*?)\s*#*\s*$", line)
        if not match:
            continue
        slug = _slug(match.group(1))
        anchors.add(slug if not seen[slug] else f"{slug}-{seen[slug]}")
        seen[slug] += 1
    return anchors


def _resolve(reference: str) -> list[Path]:
    reference = reference.rstrip("/")
    if any(char in reference for char in "*?["):
        return sorted(ROOT.glob(reference))
    path = ROOT / reference
    return [path] if path.exists() else []


def _is_repo_reference(token: str) -> bool:
    if any(char in token for char in "<>{}$~|") or token.startswith(("/", "http")):
        return False
    path = token.split("::", 1)[0]
    if path.rstrip("/") in _FORBIDDEN_AT_ROOT:
        return False
    return path in _ROOT_FILES or path.startswith(_TOP_LEVEL)


@pytest.mark.parametrize("name", _FORBIDDEN_AT_ROOT)
def test_no_ancestor_loaded_claude_memory_at_the_repo_root(name):
    """Measured with Claude Code 2.1.318: ancestor CLAUDE.md, .claude/CLAUDE.md
    and CLAUDE.local.md all load from a subdirectory, so any of these would
    reach attendee agents under /app/python/source_code/data/users/<slug>.
    .claude/rules was not measured and stays forbidden until someone does.
    The scripts/deploy_*.py uploads include gitignored files too."""
    assert not (ROOT / name).exists(), (
        f"{name} at the repo root leaks into every attendee Claude session on a source "
        "deploy; route Claude Code through .claude/settings.json instead (AGENTS.md)"
    )


def test_claude_code_is_pointed_at_agents_md_by_a_session_start_hook():
    """Claude Code 2.1.318 reads AGENTS.md only as a fallback when no CLAUDE.md is
    in the chain; older versions not at all. The hook points them here. Ancestor
    .claude/settings.json is not loaded from subdirectories (measured), so it
    reaches developers who start at the repo root and never attendees."""
    settings = json.loads((ROOT / ".claude" / "settings.json").read_text())
    commands = [
        hook["command"]
        for group in settings["hooks"]["SessionStart"]
        for hook in group["hooks"]
        if hook.get("type") == "command"
    ]
    assert any("AGENTS.md" in command for command in commands)


def test_the_entry_point_stays_small_enough_to_inject_whole():
    text = ENTRY.read_text()
    assert len(text.splitlines()) <= 160, "keep AGENTS.md short; move detail into docs/agent-guide/"
    assert len(text.encode()) <= 10_000, "AGENTS.md is loaded into every session; keep detail in the guide"


def test_every_nested_agents_md_is_routed_and_outside_runtime_roots():
    entry = ENTRY.read_text()
    for path in _walk_markdown("AGENTS.md"):
        relative = path.relative_to(ROOT).as_posix()
        if relative == "AGENTS.md":
            continue
        assert relative in entry, f"root AGENTS.md must list nested instructions: {relative}"
        assert not relative.startswith(tuple(f"{root}/" for root in _RUNTIME_ROOTS)), (
            f"{relative} is inside a packaged runtime root; put local rules in docs/agent-guide/areas/"
        )


@pytest.mark.parametrize("document", _guide_documents(), ids=lambda p: p.relative_to(ROOT).as_posix())
def test_guide_links_and_anchors_resolve(document: Path):
    text = _FENCE.sub("", document.read_text())
    broken = []
    for target in _LINK.findall(text):
        if target.startswith(("http://", "https://", "mailto:")):
            continue
        path_part, _, anchor = target.partition("#")
        linked = (document.parent / path_part).resolve() if path_part else document
        if not linked.exists():
            broken.append(target)
        elif anchor and linked.suffix == ".md" and anchor not in _anchors(linked):
            broken.append(target)
    assert broken == [], f"{document.name}: broken links {broken}"


@pytest.mark.parametrize("document", _guide_documents(), ids=lambda p: p.relative_to(ROOT).as_posix())
def test_guide_paths_and_symbols_exist(document: Path):
    raw = document.read_text()
    tokens = [token for token in _CODE.findall(_FENCE.sub("", raw)) if _is_repo_reference(token)]
    for block in _SHELL_FENCE.findall(raw):
        tokens.extend(_SHELL_PATH.findall(block))
    missing = []
    for token in tokens:
        path, _, symbol = token.partition("::")
        matches = _resolve(path)
        if not matches:
            missing.append(token)
            continue
        if symbol:
            source = matches[0].read_text(errors="replace")
            for part in symbol.removesuffix("()").split("."):
                if not re.search(rf"(?<![\w]){re.escape(part)}(?![\w])", source):
                    missing.append(token)
                    break
    assert missing == [], f"{document.name}: stale paths or symbols {sorted(set(missing))}"
