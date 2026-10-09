"""External observer for an unchanged CT WT PEX; never changes builder policy."""
import importlib.util
import os
from pathlib import Path
import re
import sys


def observation_binding():
    from server import config
    env = os.environ
    owner = env.get("WORKSHOP_EVALUATION_ATTENDEE_EMAIL", "").lower()
    marker = env.get("WORKSHOP_EVALUATION_MARKER", "")
    run = env.get("WORKSHOP_EVALUATION_RUN_ID", "")
    unit = env.get("WORKSHOP_EVALUATION_UNIT_ID", "")
    if (env.get("WORKSHOP_EVALUATION_ENABLED") != "true" or not re.fullmatch(r"wt-eval-[a-z0-9-]{1,24}", marker)
            or owner != config.workshop_attendee_email() or not owner or not run or not unit
            or run != config.workshop_run_id() or unit != config.workshop_unit_id()
            or config.allow_shared_topology() or config.max_sessions_per_user() != 1 or config.max_sessions_global() != 1):
        return None
    return {"marker": marker, "attendee_email": owner, "run_id": run, "unit_id": unit}


def application():
    from server import config, main as wt_main
    config.evaluation_observation_binding = observation_binding
    path = Path(os.environ["WT_EVALUATION_SOURCE_ROOT"]) / "wt_evaluation.py"
    spec = importlib.util.spec_from_file_location("server.evaluation", path)
    observer = importlib.util.module_from_spec(spec)
    sys.modules["server.evaluation"] = observer
    spec.loader.exec_module(observer)
    observer.SUPPORTED_PINS = {"claude": os.environ["CLAUDE_CODE_VERSION"], "codex": os.environ["CODEX_CLI_VERSION"]}
    original_routes = list(wt_main.app.router.routes)
    wt_main.app.include_router(observer.router)
    # WT's final GET fallback serves its SPA for unknown paths. Evaluation GETs
    # must precede that fallback; preserve the order of all release-owned routes.
    added_routes = wt_main.app.router.routes[len(original_routes):]
    wt_main.app.router.routes[:] = added_routes + original_routes
    return wt_main.app


def main():
    # Keep the release's OTel launcher, environment merge, dependency interpreter,
    # and uvicorn options. Only the ASGI factory adds the isolated observer.
    from server import otel_bootstrap
    original = otel_bootstrap.uvicorn_command
    def command(env):
        args = original(env)
        args[args.index("server.main:app")] = "wt_evaluation_bootstrap:application"
        args.append("--factory")
        return args
    otel_bootstrap.uvicorn_command = command
    otel_bootstrap.main()
