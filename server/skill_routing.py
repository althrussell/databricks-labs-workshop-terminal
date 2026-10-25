"""Which Impeccable each harness will actually discover, and from where (W1).

Installing the right payload is not enough: a Claude-specific copy loses if a
higher-precedence directory still holds the Codex copy. This walks each
harness's discovery order (home, project, the shared ``.agents`` path and an
isolated worktree's project copy) and records the effective copy: its path,
digest, which provider's payload it is, and whether it is WT-managed or the
attendee's own override.

The precedence modelled here:

- Claude Code: personal ``~/.claude/skills`` wins over a project's
  ``.claude/skills`` for the same skill name.
- Codex: the repository's ``.agents/skills`` comes before ``~/.agents/skills``,
  then the legacy ``~/.codex/skills``. Codex lists every scope's copy; the
  first is the one recorded as effective.

``status`` is ``ok`` (the harness's own payload wins), ``override`` (the
attendee's own copy wins; reported, never replaced), ``missing`` or
``mismatch`` (a managed copy of the other provider's payload wins).
"""
from __future__ import annotations

import os
from pathlib import Path

SKILL = "impeccable"
#: (scope, relative path) in discovery order for each harness.
PRECEDENCE = {
    "claude": (("home", ".claude/skills"), ("project", ".claude/skills")),
    "codex": (("project", ".agents/skills"), ("home", ".agents/skills"), ("home", ".codex/skills")),
}


def content_digest(path: Path) -> str:
    """``directory_checksum`` of the bytes a reader sees, following file links.

    A project's Claude copy links files identical to its Codex copy rather than
    committing them twice; it is still Claude's payload if its content is.
    Matches ``directory_checksum`` exactly for a tree with no links.
    """
    import hashlib

    digest = hashlib.sha256()
    for current, dirs, files in os.walk(path, followlinks=True):
        dirs[:] = sorted(name for name in dirs if name != "__pycache__")
        for name in sorted(n for n in files if not n.endswith((".pyc", ".pyo"))):
            full = os.path.join(current, name)
            relative = os.path.relpath(full, path).replace(os.sep, "/")
            digest.update(relative.encode("utf-8") + b"\0")
            with open(full, "rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
            digest.update(b"\0")
    return digest.hexdigest()


def _provider_digests(tree: str | None) -> dict[str, str]:
    from .bootstrap import impeccable

    if not tree:
        return {}
    providers = impeccable.routing(tree).get("providers", {})
    return {name: entry.get("sha256", "") for name, entry in providers.items() if name in {"claude", "codex"}}


def _managed(path: Path, roots: tuple[str, ...], project: Path | None) -> bool:
    """Whether a copy is WT-managed: a link into a managed tree, or a recorded project copy."""
    if path.is_symlink():
        target = os.path.realpath(path)
        return any(target.startswith(os.path.realpath(root) + os.sep) for root in roots)
    if project is not None and str(path).startswith(str(project) + os.sep):
        import json

        try:
            manifest = json.loads((project / ".agents/workshop-skills.json").read_text())
        except (OSError, ValueError):
            return False
        side = "claude" if ".claude" in path.parts else "skills"
        return SKILL in (manifest.get(side) or {}) or SKILL in (manifest.get("skills") or {})
    return False


def audit(home: str | Path, project: str | Path | None = None, *, tree: str | None = None) -> dict:
    """The effective Impeccable for each harness, with every candidate considered."""
    from .user_content import shared_skills_dir

    home = Path(home)
    project_path = Path(project) if project else None
    tree = tree or shared_skills_dir()
    digests = _provider_digests(tree)
    by_digest = {digest: provider for provider, digest in digests.items() if digest}
    roots = (tree, shared_skills_dir(), str(Path(__file__).resolve().parents[1] / "assets/skills"))
    result: dict = {"version": 1, "skill": SKILL, "routing": digests, "harnesses": {}}
    for harness, chain in PRECEDENCE.items():
        candidates = []
        for scope, relative in chain:
            base = home if scope == "home" else project_path
            if base is None:
                continue
            path = base / relative / SKILL
            if not (path / "SKILL.md").is_file():
                continue
            digest = content_digest(path.resolve())
            candidates.append({
                "scope": scope, "path": str(path), "resolved": os.path.realpath(path), "sha256": digest,
                "provider": by_digest.get(digest, "unknown"), "managed": _managed(path, roots, project_path),
            })
        effective = candidates[0] if candidates else None
        if effective is None:
            status = "missing"
        elif effective["provider"] == harness:
            status = "ok"
        elif not effective["managed"]:
            status = "override"
        else:
            status = "mismatch"
        result["harnesses"][harness] = {"status": status, "effective": effective, "candidates": candidates}
    return result


__all__ = ["PRECEDENCE", "audit"]
