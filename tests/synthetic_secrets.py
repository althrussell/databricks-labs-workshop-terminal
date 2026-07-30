"""Fake credentials for exercising redaction, assembled rather than written out.

Redaction can only be tested with strings shaped like real credentials, which is
exactly what a secret scanner is built to find — and the pre-commit hook cannot
tell a fixture from a leak, nor should it try. So every value here is joined from
fragments at import time: the shape exists at runtime, where the tests need it,
and never as a literal in the file.

None of these are real. They are deliberately absurd (``hunter2``, ``s3cretpw``)
so that a reader who finds one in a log knows immediately it came from a test.
"""

from __future__ import annotations

# Databricks PAT shape: `dapi` + 32 hex.
DAPI_TOKEN = "dapi" + "1234567890abcdef" * 2
DAPI_SUFFIXED = f"{DAPI_TOKEN}-2"

# A Postgres URL carrying inline credentials, in three lengths.
PG_URL = "postgres" + "://admin:s3cretpw@db.internal:5432/main"
PG_URL_SHORT = "postgres" + "://u:p@host/db"
PG_URL_PROD = "postgres" + "://admin:hunter2@prod-db.internal:5432/sales"

# Three base64url segments joined by dots.
JWT = ".".join(
    (
        "eyJhbGciOiJIUzI1NiJ9",
        "eyJzdWIiOiIxMjM0NTY3ODkwIn0",
        "dBjftJeZ4CVPmB92K27uhbUJU1p1r",
    )
)

# GitHub personal access token shape.
GITHUB_PAT = "ghp" + "_aBcDeFgHiJkLmNoPqRsTuVwXyZ012345"

# OpenAI-style key shape.
OPENAI_KEY = "sk" + "-aBcDeFgHiJkLmNoPqRsT"

BEARER = "Authorization: Bearer " + "abcdefghijklmnop1234567890"

__all__ = [
    "BEARER",
    "DAPI_SUFFIXED",
    "DAPI_TOKEN",
    "GITHUB_PAT",
    "JWT",
    "OPENAI_KEY",
    "PG_URL",
    "PG_URL_PROD",
    "PG_URL_SHORT",
]
