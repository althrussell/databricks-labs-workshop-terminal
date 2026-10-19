"""Public registry selection for deployed, hash-checked Python installers."""
from __future__ import annotations

from collections.abc import Mapping

PUBLIC_PYPI = "https://pypi.org/simple"


def public_python_environment(environment: Mapping[str, str]) -> dict[str, str]:
    """Ignore inherited laptop/user index, link and configuration settings."""
    result = dict(environment)
    for name in list(result):
        if ((name.startswith("UV_INDEX_") and name.endswith("_URL"))
                or name in {"UV_INDEX", "UV_EXTRA_INDEX_URL", "UV_FIND_LINKS", "UV_CONFIG_FILE",
                            "PIP_EXTRA_INDEX_URL", "PIP_FIND_LINKS", "PIP_TRUSTED_HOST"}):
            result.pop(name)
    result.update(UV_DEFAULT_INDEX=PUBLIC_PYPI, UV_INDEX_URL=PUBLIC_PYPI,
                  PIP_INDEX_URL=PUBLIC_PYPI, UV_NO_CONFIG="true", PIP_CONFIG_FILE="/dev/null")
    return result
