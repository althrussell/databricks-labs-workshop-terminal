"""Exact, opt-in quality-test identity; historical specifications keep their pins."""
import json
from pathlib import Path


def validate_test_baseline(name, artifacts, policy):
    baseline = json.loads(Path(__file__).with_name("test-baseline.json").read_text())
    if name != baseline["id"]:
        raise ValueError("Unknown quality test baseline")
    if any(artifacts["artifacts"].get(key, {}).get("version") != value
           for key, value in baseline["artifacts"].items()):
        raise ValueError("Packaged toolchain differs from the quality test baseline")
    for role, capability in (("driver", "claude"), ("codex", "codex")):
        # One approved service per harness prevents policy resolution from
        # silently selecting an older model when a requested service is absent.
        allowed = {entry["service_name"] for entry in policy["pool"]
                   if capability in entry["capabilities"]}
        if allowed != {baseline["models"][role]}:
            raise ValueError("Quality test policy must allow exactly the requested harness models")
    return baseline


def baseline_canary_matches(baseline, role, result):
    expected = baseline["models"].get(role)
    return expected is None or (result.get("model") == expected
                                and result.get("invocation_verified") is True)
