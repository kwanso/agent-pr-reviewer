"""Error classification and handling for the review agent.

Categorizes LLM and API errors into actionable types with retry strategies.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class ErrorType(str, Enum):
    """Classified error types for consistent handling."""

    QUOTA_EXHAUSTED = "quota_exhausted"  # 429, quota, rate_limited, resource_exhausted
    SERVICE_UNAVAILABLE = "service_unavailable"  # UNAVAILABLE, overloaded
    TIMEOUT = "timeout"  # Timeout, deadline exceeded
    AUTH_FAILED = "auth_failed"  # 401, 403, invalid token
    INVALID_REQUEST = "invalid_request"  # 400, bad input
    NOT_FOUND = "not_found"  # 404, resource missing
    SERVER_ERROR = "server_error"  # 5xx
    UNKNOWN = "unknown"


@dataclass
class ClassifiedError:
    """Classified error with retry recommendation."""

    error_type: ErrorType
    original_exception: Exception
    message: str
    retriable: bool  # Should we retry?
    retry_after_s: float = 30.0  # Suggested delay


def classify_llm_error(exc: Exception) -> ClassifiedError:
    """Classify LLM errors into actionable types.

    Args:
        exc: Exception from LLM call

    Returns:
        ClassifiedError with type and retry recommendation
    """
    msg = str(exc).lower()

    # Quota errors
    if any(
        kw in msg
        for kw in ("429", "quota", "rate", "resource_exhausted", "quota exceeded")
    ):
        retry_delay = _extract_retry_delay(str(exc)) + 2  # +2s safety margin
        return ClassifiedError(
            error_type=ErrorType.QUOTA_EXHAUSTED,
            original_exception=exc,
            message=f"API quota exhausted; retry after {retry_delay:.1f}s",
            retriable=True,
            retry_after_s=retry_delay,
        )

    # Service unavailable
    if any(
        kw in msg
        for kw in (
            "unavailable",
            "overloaded",
            "service unavailable",
            "too many requests",
        )
    ):
        return ClassifiedError(
            error_type=ErrorType.SERVICE_UNAVAILABLE,
            original_exception=exc,
            message="Service temporarily unavailable; should retry",
            retriable=True,
            retry_after_s=60.0,
        )

    # Timeout
    if any(kw in msg for kw in ("timeout", "deadline exceeded", "timed out")):
        return ClassifiedError(
            error_type=ErrorType.TIMEOUT,
            original_exception=exc,
            message="Request timeout; may retry with longer timeout",
            retriable=True,
            retry_after_s=5.0,
        )

    # Auth errors
    if any(
        kw in msg
        for kw in ("401", "403", "unauthorized", "forbidden", "invalid api key")
    ):
        return ClassifiedError(
            error_type=ErrorType.AUTH_FAILED,
            original_exception=exc,
            message="Authentication failed; check credentials",
            retriable=False,
        )

    # Invalid request
    if any(kw in msg for kw in ("400", "bad request", "invalid", "malformed")):
        return ClassifiedError(
            error_type=ErrorType.INVALID_REQUEST,
            original_exception=exc,
            message="Invalid request; check input format",
            retriable=False,
        )

    # Not found
    if any(kw in msg for kw in ("404", "not found", "does not exist")):
        return ClassifiedError(
            error_type=ErrorType.NOT_FOUND,
            original_exception=exc,
            message="Resource not found",
            retriable=False,
        )

    # Server error
    if any(kw in msg for kw in ("500", "502", "503", "504", "server error")):
        return ClassifiedError(
            error_type=ErrorType.SERVER_ERROR,
            original_exception=exc,
            message="Server error; may retry",
            retriable=True,
            retry_after_s=30.0,
        )

    # Unknown
    return ClassifiedError(
        error_type=ErrorType.UNKNOWN,
        original_exception=exc,
        message=f"Unknown error: {msg[:100]}",
        retriable=False,
    )


def _extract_retry_delay(error_msg: str) -> float:
    """Extract retry-after delay from error message in seconds.

    Looks for patterns like:
    - "Retry in 30 seconds"
    - "Delay: '120'"
    - "retry_after: 60"
    """
    import re

    patterns = [
        r"retry\s*(?:in|after)\s*(\d+)\s*(?:seconds?|s)?",
        r"['\"]?delay['\"]?\s*:\s*['\"]?(\d+)",
        r"retry_after['\"]?\s*:\s*(\d+)",
    ]

    for pattern in patterns:
        match = re.search(pattern, error_msg, re.IGNORECASE)
        if match:
            try:
                return float(match.group(1))
            except (ValueError, IndexError):
                continue

    return 30.0  # Default 30s
