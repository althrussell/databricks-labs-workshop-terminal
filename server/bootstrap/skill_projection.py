"""Deterministic workshop routing over checksum-verified platform skills.

Keep raw upstream provenance separate from the delivered digest. This projection
only retires the competing UX skill and its callers; platform/API guidance stays
upstream-owned. Refresh and bootstrap use the same projection.
"""
from pathlib import Path
import shutil

RETIRED_UX_SKILLS = frozenset({"workshop-design-studio", "databricks-app-design"})


def project_skills(root: str | Path) -> None:
    root = Path(root)
    for name in RETIRED_UX_SKILLS:
        target = root / name
        if target.is_symlink():
            target.unlink()
        elif target.is_dir():
            shutil.rmtree(target)
    apps = root / "databricks-apps/SKILL.md"
    if apps.is_file():
        text = apps.read_text()
        lines = []
        for line in text.splitlines(keepends=True):
            if line.startswith("**For data UI design"):
                line = ("**Interface design:** use `impeccable` for task-appropriate UX, "
                        "including data and conversational surfaces. Follow the workshop "
                        "interaction contract for context and pacing; this skill owns "
                        "platform APIs and scaffolding.\n")
            else:
                line = line.replace("`databricks-app-design`", "`impeccable`")
            lines.append(line)
        apps.write_text("".join(lines))
    dashboards = root / "databricks-aibi-dashboards/SKILL.md"
    if dashboards.is_file():
        dashboards.write_text(dashboards.read_text().replace(
            "(which brings in `databricks-app-design` for the data-screen UX)",
            "with `impeccable` for the interface UX",
        ))
