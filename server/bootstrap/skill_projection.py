"""Deterministic workshop routing over checksum-verified platform skills.

Keep raw upstream provenance separate from the delivered digest. This projection
only retires the competing UX skill and its callers and leaves out the skills the
workshop doesn't use; platform/API guidance stays upstream-owned. Refresh and
bootstrap use the same projection.
"""
from pathlib import Path
import shutil

RETIRED_UX_SKILLS = frozenset({"workshop-design-studio", "databricks-app-design"})
RETIRED_WORKSHOP_SKILLS = frozenset({"promote"})
#: Upstream skills this workshop doesn't install. Attendees build apps and agents
#: in an afternoon, and these invite detours; every other skill stays exactly as
#: databricks-agent-skills publishes it (owner decision, 26 Oct 2026).
WORKSHOP_UNUSED_SKILLS = frozenset({
    "databricks-ai-runtime", "databricks-iceberg", "databricks-zerobus-ingest",
    "databricks-serverless-migration", "databricks-ml-training",
    "databricks-spark-structured-streaming", "databricks-lakeflow-connect",
    "databricks-unstructured-pdf-generation", "databricks-setup-local",
    "databricks-metric-views", "databricks-mlflow-evaluation",
})
#: Upstream skills the workshop projection removes from the published tree.
PROJECTED_OUT = RETIRED_UX_SKILLS | WORKSHOP_UNUSED_SKILLS
RETIRED_MANAGED_SKILLS = (RETIRED_UX_SKILLS | RETIRED_WORKSHOP_SKILLS | {"databricks-app-apx"}
                          | WORKSHOP_UNUSED_SKILLS)
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


def project_skills(root: str | Path, *, strict: bool = False) -> None:
    """Apply the workshop projection to a skills tree.

    ``strict`` is for the manifest generator, which reads the real pinned upstream:
    a missing anchor there means the upstream moved and the digest must not be
    regenerated silently. Boot and the unit tests use fake trees, so they leave it off.
    """
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

    _teach_model_calls(root, strict=strict)
    _apply_overlays(root, strict=strict)


# Workshop overlays: whole files that replace the upstream copy. They live beside
# the repo's assets so release packaging ships them with the runtime.
OVERLAY_ROOT = Path(__file__).resolve().parents[2] / "assets" / "skill-overlays"


def _apply_overlays(root: Path, *, strict: bool) -> None:
    """Replace upstream files with the workshop versions under ``assets/skill-overlays``."""
    for source in sorted(OVERLAY_ROOT.rglob("*")):
        if not source.is_file():
            continue
        target = root / source.relative_to(OVERLAY_ROOT)
        if not target.parent.is_dir():
            if strict:
                raise RuntimeError(
                    f"skill overlay {source.relative_to(OVERLAY_ROOT)} has no upstream directory"
                )
            continue
        target.write_bytes(source.read_bytes())


def _replace_once(path: Path, old: str, new: str, *, strict: bool) -> None:
    """Replace one exact passage. A missing anchor is an error only when ``strict``."""
    text = path.read_text()
    if text.count(old) != 1:
        if strict:
            raise RuntimeError(
                f"workshop projection anchor not found once in {path.name}; "
                "update server/bootstrap/skill_projection.py for the new upstream text"
            )
        return
    path.write_text(text.replace(old, new))


def _replace_span(path: Path, start: str, end: str, new: str, *, strict: bool) -> None:
    """Replace the text from ``start`` up to (not including) ``end``, both found exactly once."""
    text = path.read_text()
    if text.count(start) != 1 or text.count(end) != 1:
        if strict:
            raise RuntimeError(
                f"workshop projection anchors not found once in {path.name}; "
                "update server/bootstrap/skill_projection.py for the new upstream text"
            )
        return
    if text.index(end) < text.index(start):
        if strict:
            raise RuntimeError(f"workshop projection anchors out of order in {path.name}")
        return
    head, _, rest = text.partition(start)
    _, _, tail = rest.partition(end)
    path.write_text(head + new + end + tail)


# How an App calls a Unity Catalog model service. The model call runs as the
# signed-in attendee; the App's service principal is never the caller, and no
# MODEL_SERVICE resource is declared. See the workshop contract, "Models in an App".
_PYTHON_RESOURCE_ROW = (
    "| UC model service | `model` | Execute | UC-native model serving (`system.ai.<model>` or "
    "`catalog.schema.name`); declare a resource of type `uc_securable` (`securable_type: MODEL_SERVICE`) "
    "and inject with `valueFrom: <resource-name>`, which resolves to the service FQN |"
)
_PYTHON_RESOURCE_ROW_WORKSHOP = (
    "| UC model service | none | the signed-in user's EXECUTE, with the `ai-gateway` user scope | "
    "UC-native model serving (`system.ai.<model>` or `catalog.schema.name`). Not an app resource: "
    "call it as the attendee with their forwarded token on `/ai-gateway/mlflow/v1` |"
)
_PYTHON_MIGRATE_SENTENCE = (
    "To migrate, declare the model service as a `uc_securable` resource (`securable_type: MODEL_SERVICE`, "
    "`permission: EXECUTE`) — on deploy the app's service principal gets `EXECUTE`, plus `USE CATALOG` / "
    "`USE SCHEMA` on the parents when needed — inject it with `valueFrom: <resource-name>` (the env var "
    "resolves to the service's full name, e.g. `system.ai.<model>`), and pass that name as `model` on the "
    "`/ai-gateway/mlflow/v1` path."
)
_PYTHON_MIGRATE_WORKSHOP = (
    "To migrate, call the model service as the signed-in user: send the attendee's forwarded token "
    "(`X-Forwarded-Access-Token`, which needs the `ai-gateway` user scope) to "
    "`<host>/ai-gateway/mlflow/v1` with `model` set to `system.ai.<model>`. Do not declare a "
    "`uc_securable` MODEL_SERVICE resource: the app's service principal is not the caller."
)
_APPKIT_LINE = (
    "- **Call a foundation model / UC model service (`system.ai.*`)**: Declare a `uc_securable` "
    "(`MODEL_SERVICE`) resource and call the service name on `/ai-gateway/mlflow/v1` — see "
    "[Unity Catalog model services](references/appkit/model-serving.md#unity-catalog-model-services) "
    "and the `databricks-unity-gateway` skill (not `serving()`, which takes serving-endpoint names)"
)
_APPKIT_LINE_WORKSHOP = (
    "- **Call a foundation model / UC model service (`system.ai.*`)**: call it as the signed-in user "
    "with their forwarded token (`X-Forwarded-Access-Token`) on `/ai-gateway/mlflow/v1`, with the "
    "`ai-gateway` user scope and no MODEL_SERVICE resource — see "
    "[Unity Catalog model services](references/appkit/model-serving.md#unity-catalog-model-services) "
    "and the `databricks-unity-gateway` skill (not `serving()`, which takes serving-endpoint names)"
)
_PLATFORM_ROW = (
    "| UC Model Service | EXECUTE | Declare as a `uc_securable` resource (`securable_type: MODEL_SERVICE`); "
    "grants the app SP `EXECUTE`, plus `USE CATALOG` / `USE SCHEMA` on the parents when needed. The "
    "`serving_endpoint`/`CAN_QUERY` resource remains for custom, external, and MPS-backed serving endpoints |"
)
_PLATFORM_ROW_WORKSHOP = (
    "| UC Model Service | the user's EXECUTE | Not an app resource. Call it as the signed-in user with "
    "the forwarded token and the `ai-gateway` user scope; the app's service principal is not the caller. "
    "The `serving_endpoint`/`CAN_QUERY` resource remains for custom, external, and MPS-backed serving endpoints |"
)
_APPKIT_SECTION = """Call the model service as the signed-in user, never as the app's service principal, and do not declare it as a `uc_securable` resource. The Apps proxy forwards the attendee's token in `X-Forwarded-Access-Token` when the app declares the `ai-gateway` user scope (`user_api_scopes`). Send that token on `/ai-gateway/mlflow/v1`:

```typescript
// Server route. `req` is the Express request.
const token = req.header('x-forwarded-access-token');
if (!token) throw new Error('No forwarded user token: the app needs the ai-gateway user scope');
const host = process.env.DATABRICKS_HOST!.replace(/^(?!https?:\\/\\/)/, 'https://').replace(/\\/$/, '');
const response = await fetch(`${host}/ai-gateway/mlflow/v1/chat/completions`, {
  method: 'POST',
  headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' },
  body: JSON.stringify({
    model: process.env.DATABRICKS_MODEL, // the event's driver, e.g. system.ai.claude-opus-5-5
    messages: [{ role: 'user', content: 'Summarize this support case.' }],
    max_tokens: 256,
  }),
});
if (!response.ok) throw new Error(`Model call failed: HTTP ${response.status}`);
const data = await response.json();
```

`serving()` and its endpoint aliases take Model Serving endpoint names only. Do not pass a model-service name to `serving()`. Background work with no signed-in user runs as the app; the Agent Bricks exception is in the workshop contract.

"""


def _teach_model_calls(root: Path, *, strict: bool) -> None:
    """Point the upstream teaching at the signed-in user's token, not the App's identity."""
    resources = root / "databricks-apps-python/references/2-app-resources.md"
    if resources.is_file():
        _replace_once(resources, _PYTHON_RESOURCE_ROW, _PYTHON_RESOURCE_ROW_WORKSHOP, strict=strict)
        _replace_once(resources, _PYTHON_MIGRATE_SENTENCE, _PYTHON_MIGRATE_WORKSHOP, strict=strict)
    apps = root / "databricks-apps/SKILL.md"
    if apps.is_file():
        _replace_once(apps, _APPKIT_LINE, _APPKIT_LINE_WORKSHOP, strict=strict)
    platform = root / "databricks-apps/references/platform-guide.md"
    if platform.is_file():
        _replace_once(platform, _PLATFORM_ROW, _PLATFORM_ROW_WORKSHOP, strict=strict)
    appkit_models = root / "databricks-apps/references/appkit/model-serving.md"
    if appkit_models.is_file():
        _replace_once(
            appkit_models,
            "Create a model service that references the PT endpoint and declare that model service as the `uc_securable` below.",
            "Create a model service that references the PT endpoint, and call that model service as the signed-in user (below).",
            strict=strict,
        )
        _replace_span(
            appkit_models,
            "Declare the model service as a `uc_securable` app resource",
            "## Troubleshooting",
            _APPKIT_SECTION,
            strict=strict,
        )
