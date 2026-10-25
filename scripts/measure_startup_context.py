#!/usr/bin/env python3
"""Measure what each coding agent loads at session start in a fresh WT project.

Reproducible and offline: it provisions a throwaway attendee home from this
checkout's packaged assets, creates a project with ``workshop-init-project``
(no scaffold) and reports, per agent:

- the always-read instruction files (home and project memory), and
- the skill catalogue each harness lists before the first prompt: one entry per
  discovered ``SKILL.md`` (name, description and path).

Claude Code shows a skill name once, with the personal copy winning over a
project copy. Codex lists each discovery scope's copy. These are the modelled
harness rules; the real first-request input also carries each harness's own
system prompt and tool schemas, which WT does not control and this omits.

Tokens are estimated as bytes / 4. Use ``system.ai_gateway.usage``
first-request input tokens for live numbers (see docs/startup-context.md).

    python scripts/measure_startup_context.py [--json]
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

#: Discovery directories, highest precedence first, relative to (home, project).
CLAUDE_SKILLS = (("home", ".claude/skills"), ("project", ".claude/skills"))
CODEX_SKILLS = (("project", ".agents/skills"), ("home", ".agents/skills"))
CLAUDE_MEMORY = (("home", ".claude/CLAUDE.md"), ("project", "CLAUDE.md"))
CODEX_MEMORY = (("home", ".codex/AGENTS.md"), ("project", "AGENTS.md"))

_FRONTMATTER = re.compile(r"\A---\n(.*?)\n---", re.DOTALL)


def _tokens(size: int) -> int:
    return (size + 3) // 4


#: Absolute paths differ by checkout and temp directory; count them as one
#: fixed-length token each so measurements compare across machines.
_PLACEHOLDERS: list[tuple[str, str]] = []


def _normalised(text: str) -> bytes:
    for path, token in _PLACEHOLDERS:
        text = text.replace(path, token)
    return text.encode()


def skill_entry(skill: Path) -> int:
    """Bytes of the catalogue entry a harness lists for one skill."""
    text = (skill / "SKILL.md").read_text(errors="replace")
    match = _FRONTMATTER.match(text)
    fields = {}
    for line in (match.group(1).splitlines() if match else []):
        key, _, value = line.partition(":")
        if key.strip() in {"name", "description"}:
            fields[key.strip()] = value.strip().strip('"\'')
    entry = f"- {fields.get('name', skill.name)}: {fields.get('description', '')} ({skill})"
    return len(_normalised(entry))


def _skills(base: Path, relative: str) -> dict[str, Path]:
    directory = base / relative
    if not directory.is_dir():
        return {}
    return {child.name: child for child in sorted(directory.iterdir())
            if (child / "SKILL.md").is_file()}


def measure_agent(home: Path, project: Path, memory, skills, *, dedupe: bool) -> dict:
    roots = {"home": home, "project": project}
    instruction_bytes = sum(len(_normalised((roots[scope] / rel).read_text(errors="replace")))
                            for scope, rel in memory if (roots[scope] / rel).is_file())
    seen: set[str] = set()
    listed = 0
    entries = 0
    per_skill: dict[str, int] = {}
    for scope, rel in skills:
        for name, path in _skills(roots[scope], rel).items():
            if dedupe and name in seen:
                continue
            seen.add(name)
            size = skill_entry(path)
            listed += size
            per_skill[name] = per_skill.get(name, 0) + size
            entries += 1
    return {"instruction_bytes": instruction_bytes, "skill_entries": entries, "skill_listing_bytes": listed,
            "estimated_tokens": _tokens(instruction_bytes + listed), "per_skill_bytes": per_skill}


def project_skill_footprint(project: Path) -> dict:
    files = 0
    size = 0
    for directory in (project / ".agents/skills", project / ".claude/skills"):
        for path in directory.rglob("*") if directory.is_dir() else ():
            inside = path.relative_to(project).parents
            if path.is_file() and not path.is_symlink() and not any((project / p).is_symlink() for p in inside):
                files += 1
                size += path.stat().st_size
    return {"files": files, "bytes": size}


def measure(dump: str | None = None) -> dict:
    with tempfile.TemporaryDirectory(prefix="wt-startup-") as temporary:
        root = Path(temporary)
        _PLACEHOLDERS[:] = [(os.path.realpath(temporary), "<tmp>"), (temporary, "<tmp>"), (str(ROOT), "<wt>")]
        # Local mode: no CLI installers, packaged assets only (as the tests run).
        os.environ.setdefault("LOCAL_DEV", "1")
        os.environ.setdefault("DATA_ROOT", str(root / "data"))
        os.environ.setdefault("DATABRICKS_HOST", "https://measure.cloud.databricks.com")
        from server import config, user_content
        from server.users import User

        config.users_root = lambda: str(root / "users")  # type: ignore[assignment]
        config.shared_prefix = lambda: str(root / "shared")  # type: ignore[assignment]
        user = User("measure@example.com")
        user.bootstrap_home()
        user_content.provision(user)
        home = Path(user.home)
        env = {**os.environ, "HOME": str(home), "GIT_CONFIG_GLOBAL": "/dev/null",
               "GIT_AUTHOR_NAME": "Measure", "GIT_AUTHOR_EMAIL": "measure@example.com",
               "GIT_COMMITTER_NAME": "Measure", "GIT_COMMITTER_EMAIL": "measure@example.com"}
        subprocess.run(["bash", str(ROOT / "assets/bin/workshop-init-project"), "measure-app"],
                       env=env, capture_output=True, text=True, check=True, timeout=60)
        project = home / "projects/measure-app"
        if dump:
            target = Path(dump)
            for scope, base, rel in (("home", home, ".claude/CLAUDE.md"), ("home", home, ".codex/AGENTS.md"),
                                     ("project", project, "CLAUDE.md"), ("project", project, "AGENTS.md")):
                out = target / scope / rel
                out.parent.mkdir(parents=True, exist_ok=True)
                out.write_bytes(_normalised((base / rel).read_text(errors="replace")))
        claude = measure_agent(home, project, CLAUDE_MEMORY, CLAUDE_SKILLS, dedupe=True)
        codex = measure_agent(home, project, CODEX_MEMORY, CODEX_SKILLS, dedupe=False)
        return {"claude": claude, "codex": codex, "project_skills": project_skill_footprint(project)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--skill", action="append", default=[], help="also print one skill's listing bytes")
    parser.add_argument("--dump", help="write the measured instruction files (paths normalised) here")
    args = parser.parse_args()
    result = measure(args.dump)
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        for agent in ("claude", "codex"):
            row = result[agent]
            print(f"{agent}: ~{row['estimated_tokens']} tokens (instructions {row['instruction_bytes']} B, "
                  f"{row['skill_entries']} skill entries {row['skill_listing_bytes']} B)")
        footprint = result["project_skills"]
        print(f"project skill copies: {footprint['files']} files, {footprint['bytes']} B")
        for name in args.skill:
            print(f"{name} listing: claude {result['claude']['per_skill_bytes'].get(name, 0)} B, "
                  f"codex {result['codex']['per_skill_bytes'].get(name, 0)} B")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
