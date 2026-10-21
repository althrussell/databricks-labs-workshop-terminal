"""Bounded, payload-free classification of retained capture I/O failures.

SDK exception classes do not always retain an HTTP status. In that case it is
reported as unknown, rather than inferred from the exception's text or class.
"""

from __future__ import annotations

from typing import Any

import requests


def storage_failure(error: Exception, *, operation: str, attempts: int) -> dict[str, Any]:
    """Inspect only typed exception metadata; never retain messages or URLs."""
    from databricks.sdk.errors import (
        BadRequest, Cancelled, DataLoss, DeadlineExceeded, InternalError, NotFound,
        NotImplemented, PermissionDenied, ResourceConflict, TemporarilyUnavailable,
        TooManyRequests, Unauthenticated,
    )

    status = None
    timeout = "none"
    retryable = False
    terminal = False
    seen: set[int] = set()
    current: BaseException | None = error
    for _ in range(8):
        if current is None or id(current) in seen:
            break
        seen.add(id(current))
        candidates = [getattr(current, "status_code", None)]
        response = getattr(current, "response", None)
        candidates.append(getattr(response, "status_code", None))
        kwargs = getattr(current, "kwargs", None)
        if isinstance(kwargs, dict):
            candidates.append(kwargs.get("status_code"))
        if status is None:
            # Keep the outermost request's explicit status. An older nested
            # HTTP failure must not turn its 403 into a retryable 503.
            for candidate in candidates:
                if type(candidate) is int and 100 <= candidate <= 599:
                    status = candidate
                    break
        if isinstance(current, (
            BadRequest, Cancelled, DataLoss, NotFound, NotImplemented,
            PermissionDenied, ResourceConflict, Unauthenticated,
        )):
            # A current terminal SDK response can omit its native status. Do
            # not borrow an older response/timeout from its exception chain.
            terminal = True
            break
        if isinstance(current, requests.exceptions.ReadTimeout):
            timeout = "read"
        elif isinstance(current, requests.exceptions.ConnectTimeout):
            timeout = "connect"
        elif isinstance(current, DeadlineExceeded):
            timeout = "deadline"
        elif isinstance(current, (TimeoutError, requests.exceptions.Timeout)):
            timeout = "timeout"
        if isinstance(current, (
            TimeoutError, ConnectionError, requests.exceptions.Timeout,
            requests.exceptions.ConnectionError, DeadlineExceeded,
            TemporarilyUnavailable, TooManyRequests,
        )) or (isinstance(current, InternalError) and not isinstance(current, DataLoss)):
            retryable = True
        current = current.__cause__ or current.__context__
    if status is not None:
        # An explicit nontransient response overrides a nested socket error.
        retryable = status in {408, 429, 500, 502, 503, 504}
    if terminal:
        retryable = False
    return {
        "operation": operation,
        "http_status": status,
        "timeout": timeout,
        "attempts": attempts,
        "retryable": retryable,
    }
