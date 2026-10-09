#!/usr/bin/env python3
"""Check optional evaluator versions and actual native APIs without cloud writes."""
import json
import sys
from importlib.metadata import version
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from evals.generated_apps.mlflow_evaluation import _sdk


def main():
    expected = {"mlflow": "3.17.0", "databricks-agents": "1.12.0",
                "databricks-sdk": "0.150.0", "openai": "3.23.0"}
    actual = {name: version(name) for name in expected}
    if actual != expected:
        raise RuntimeError("Optional quality environment differs from reviewed versions")
    _sdk()
    from openai import OpenAI
    # Construct the client without credentials or network requests. Runtime
    # tests authenticate against the gateway separately with their exact SP.
    client = OpenAI(api_key="local-contract-check", base_url="https://example.invalid/v1")
    assert callable(client.responses.create) and callable(client.chat.completions.create)
    client.close()
    print(json.dumps({"versions": actual, "native_api_contract_verified": True,
                      "workspace_requests": 0, "model_requests": 0}))


if __name__ == "__main__":
    main()
