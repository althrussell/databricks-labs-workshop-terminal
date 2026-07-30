"""A minimal JSON Schema assertion helper for contract tests.

`jsonschema` is not a dependency here, and a cross-repo contract is not a good
reason to add one to an app that ships as-cloned to Databricks Apps. This covers
the subset the contract fixtures actually use: ``allOf``, ``if``/``then``/``else``,
``anyOf``, ``const``, ``enum``, ``type`` (including union lists), objects with
``required``/``properties``/``additionalProperties: false``, arrays with
``items``, and string ``minLength``/``pattern``.

Failures raise ``AssertionError`` with the JSON path that failed, so a broken
contract points at the field rather than just saying no.
"""

from __future__ import annotations

import re
from typing import Any


def assert_schema(instance: Any, schema: dict, path: str = "$") -> None:
    """Assert ``instance`` satisfies ``schema``. Raises AssertionError with a path."""
    for constraint in schema.get("allOf", []):
        assert_schema(instance, constraint, path)

    if "if" in schema:
        try:
            assert_schema(instance, schema["if"], path)
        except AssertionError:
            branch = schema.get("else")
        else:
            branch = schema.get("then")
        if branch is not None:
            assert_schema(instance, branch, path)

    if "anyOf" in schema:
        failures = []
        for option in schema["anyOf"]:
            try:
                assert_schema(instance, option, path)
                break
            except AssertionError as exc:
                failures.append(str(exc))
        else:
            raise AssertionError(f"{path} did not match any allowed schema: {failures}")

    if "const" in schema:
        assert instance == schema["const"], f"{path} must equal {schema['const']!r}"
    if "enum" in schema:
        assert instance in schema["enum"], f"{path} must be one of {schema['enum']!r}"

    schema_type = schema.get("type")
    if isinstance(schema_type, list):
        for option in schema_type:
            try:
                assert_schema(instance, {**schema, "type": option}, path)
                return
            except AssertionError:
                pass
        raise AssertionError(f"{path} must have one of types {schema_type!r}")

    if schema_type == "object":
        assert isinstance(instance, dict), f"{path} must be an object"
        for key in schema.get("required", []):
            assert key in instance, f"{path}.{key} is required"
        if schema.get("additionalProperties") is False:
            unexpected = set(instance) - set(schema.get("properties", {}))
            assert not unexpected, f"{path} has unexpected properties: {sorted(unexpected)}"
        for key, value in instance.items():
            child = schema.get("properties", {}).get(key)
            if child is not None:
                assert_schema(value, child, f"{path}.{key}")
    elif schema_type == "array":
        assert isinstance(instance, list), f"{path} must be an array"
        for index, value in enumerate(instance):
            assert_schema(value, schema["items"], f"{path}[{index}]")
    elif schema_type == "string":
        assert isinstance(instance, str), f"{path} must be a string"
        assert len(instance) >= schema.get("minLength", 0), f"{path} is too short"
        if "pattern" in schema:
            assert re.fullmatch(schema["pattern"], instance), (
                f"{path} must match {schema['pattern']!r}"
            )
    elif schema_type == "boolean":
        assert isinstance(instance, bool), f"{path} must be a boolean"
    elif schema_type == "integer":
        assert isinstance(instance, int) and not isinstance(instance, bool), (
            f"{path} must be an integer"
        )
    elif schema_type == "null":
        assert instance is None, f"{path} must be null"


__all__ = ["assert_schema"]
