"""Deterministic workshop routing over checksum-verified platform skills.

Keep raw upstream provenance separate from the delivered digest. This projection
only retires the competing UX skill and its callers; platform/API guidance stays
upstream-owned. Refresh and bootstrap use the same projection.
"""
from pathlib import Path
import shutil

RETIRED_UX_SKILLS = frozenset({"workshop-design-studio", "databricks-app-design"})
RETIRED_MANAGED_SKILLS = RETIRED_UX_SKILLS | {"databricks-app-apx"}
APX_ROUTE = """<!-- workshop-app-stack:v1 -->
## Workshop app framework

For a new custom Databricks App, default to official APX (React + FastAPI):
read `apx` and scaffold with `workshop-init-project <name> --apx`.
Use `impeccable` as the single UX authority. The AppKit-specific scaffold,
plugins, data gates and validation below apply only to an explicit AppKit
request or an existing AppKit project. Keep the chosen framework when fixing
setup failures. Never choose Streamlit unless explicitly requested.
The shared platform permissions and resource rules apply to every framework.
<!-- /workshop-app-stack -->

"""


def project_skills(root: str | Path) -> None:
    root = Path(root)
    for name in RETIRED_MANAGED_SKILLS:
        target = root / name
        if target.is_symlink():
            target.unlink()
        elif target.is_dir():
            shutil.rmtree(target)
    apps = root / "databricks-apps/SKILL.md"
    if apps.is_file():
        text = apps.read_text()
        text = text.replace(
            "Evaluates data access patterns (analytics vs Lakebase synced tables) before scaffolding. Invoke BEFORE starting implementation.",
            "New custom apps default to the apx skill (React + FastAPI). This skill covers platform rules, explicit AppKit requests and existing AppKit projects.",
        )
        if "<!-- workshop-app-stack:v1 -->" not in text:
            # Keep the YAML frontmatter intact for native skill discovery.
            if text.startswith("---\n"):
                front, separator, body = text[4:].partition("\n---\n")
                if separator:
                    text = "---\n" + front + separator + "\n" + APX_ROUTE + body.lstrip("\n")
            else:
                text = APX_ROUTE + text
        text = text.replace("### AppKit (Recommended)", "### AppKit (explicit requests or existing projects)")
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
    python = root / "databricks-apps-python/SKILL.md"
    if python.is_file():
        python.write_text(python.read_text().replace(
            "`databricks-apps` (AppKit — Node/TypeScript/React) — reach for it first.",
            "`apx` (React + FastAPI) — reach for it first.",
        ).replace(
            "**[databricks-apps](../databricks-apps/SKILL.md)** (AppKit — Node.js + TypeScript + React SDK)",
            "**[apx](../apx/SKILL.md)** (React + FastAPI)",
        ).replace(
            "**[databricks-apps](../databricks-apps/SKILL.md)** — the default for new Databricks Apps (AppKit / Node / TypeScript + React); load it first unless a Python backend is explicitly required",
            "**[apx](../apx/SKILL.md)** — the default for new custom Databricks Apps (React + FastAPI); keep an existing project's framework",
        ))
