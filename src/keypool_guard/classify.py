"""Failure classification for probe results.

Maps an HTTP status code (plus error-body hints) to a small set of
failure classes the pool logic acts on:

    401                  -> INVALID            (key revoked / wrong)
    403                  -> FORBIDDEN          (key banned)
    400 / 402            -> INSUFFICIENT_QUOTA (out of credit / arrears)
    429                  -> RATE_LIMITED (or INSUFFICIENT_QUOTA when the
                            body says the quota is exhausted)
    5xx / 408            -> SERVER_ERROR
    timeout / conn error -> NETWORK_ERROR
"""
from __future__ import annotations

from enum import Enum


class FailureClass(str, Enum):
    HEALTHY = "healthy"
    INVALID = "invalid"                        # 401
    FORBIDDEN = "forbidden"                    # 403
    INSUFFICIENT_QUOTA = "insufficient_quota"  # 400 / 402
    RATE_LIMITED = "rate_limited"              # 429 (throttled, key alive)
    SERVER_ERROR = "server_error"              # 5xx, upstream trouble
    NETWORK_ERROR = "network_error"            # timeout / connect failure
    UNKNOWN = "unknown"                        # anything else


#: Classes that mean the key itself is broken (revoked, banned, or broke).
BROKEN_CLASSES = frozenset(
    {
        FailureClass.INVALID,
        FailureClass.FORBIDDEN,
        FailureClass.INSUFFICIENT_QUOTA,
    }
)

#: Transient classes — the key is fine but unavailable right now.
TRANSIENT_CLASSES = frozenset(
    {
        FailureClass.RATE_LIMITED,
        FailureClass.SERVER_ERROR,
        FailureClass.NETWORK_ERROR,
    }
)

#: Lowercased substrings providers put in 429/400 bodies when the real
#: problem is an exhausted balance rather than throttling.
QUOTA_HINTS = (
    "insufficient quota",
    "insufficient_quota",
    "quota exceeded",
    "exceeded your current quota",
    "billing_hard_limit_reached",
    "billing",
    "arrears",
    "balance",
    "欠费",
    "余额不足",
)


def classify(status_code: int | None, body: str | None) -> FailureClass:
    """Classify one probe response.

    ``status_code`` of ``None`` means the request never got a response
    (timeout or connection error).
    """
    if status_code is None:
        return FailureClass.NETWORK_ERROR
    if 200 <= status_code < 300:
        return FailureClass.HEALTHY
    if status_code == 401:
        return FailureClass.INVALID
    if status_code == 403:
        return FailureClass.FORBIDDEN
    if status_code in (400, 402):
        return FailureClass.INSUFFICIENT_QUOTA
    if status_code == 429:
        if body and any(hint in body.lower() for hint in QUOTA_HINTS):
            # OpenAI-style exhausted quota arrives as 429; it will not
            # recover by waiting, so treat it as a broken key.
            return FailureClass.INSUFFICIENT_QUOTA
        return FailureClass.RATE_LIMITED
    if status_code == 408 or 500 <= status_code < 600:
        return FailureClass.SERVER_ERROR
    return FailureClass.UNKNOWN


def describe(cls: FailureClass, status_code: int | None) -> str:
    """Short human-readable detail for reports (no secrets, no body)."""
    if cls is FailureClass.HEALTHY:
        return "ok"
    if cls is FailureClass.NETWORK_ERROR:
        return "timeout / unreachable"
    label = {
        FailureClass.INVALID: "invalid key",
        FailureClass.FORBIDDEN: "banned",
        FailureClass.INSUFFICIENT_QUOTA: "insufficient quota",
        FailureClass.RATE_LIMITED: "rate limited",
        FailureClass.SERVER_ERROR: "server error",
        FailureClass.UNKNOWN: "unexpected response",
    }[cls]
    if status_code is not None:
        return f"{label} (HTTP {status_code})"
    return label
