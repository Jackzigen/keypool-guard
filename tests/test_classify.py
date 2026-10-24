"""Unit tests for HTTP status -> failure-class mapping."""
import pytest

from keypool_guard.classify import (
    BROKEN_CLASSES,
    TRANSIENT_CLASSES,
    FailureClass,
    classify,
    describe,
)


@pytest.mark.parametrize(
    "status,expected",
    [
        (200, FailureClass.HEALTHY),
        (201, FailureClass.HEALTHY),
        (204, FailureClass.HEALTHY),
        (401, FailureClass.INVALID),
        (403, FailureClass.FORBIDDEN),
        (400, FailureClass.INSUFFICIENT_QUOTA),
        (402, FailureClass.INSUFFICIENT_QUOTA),
        (429, FailureClass.RATE_LIMITED),
        (408, FailureClass.SERVER_ERROR),
        (500, FailureClass.SERVER_ERROR),
        (502, FailureClass.SERVER_ERROR),
        (503, FailureClass.SERVER_ERROR),
        (404, FailureClass.UNKNOWN),
        (418, FailureClass.UNKNOWN),
    ],
)
def test_status_mapping(status, expected):
    assert classify(status, None) is expected


def test_none_status_is_network_error():
    assert classify(None, None) is FailureClass.NETWORK_ERROR


def test_429_with_quota_body_is_quota_exhaustion():
    # OpenAI reports exhausted quota as 429 with insufficient_quota —
    # that key will not recover by waiting, so it must be BROKEN.
    body = '{"error": {"code": "insufficient_quota"}}'
    assert classify(429, body) is FailureClass.INSUFFICIENT_QUOTA


def test_429_plain_body_is_rate_limit():
    body = '{"error": {"message": "too many requests"}}'
    assert classify(429, body) is FailureClass.RATE_LIMITED


def test_broken_and_transient_sets():
    assert FailureClass.INVALID in BROKEN_CLASSES
    assert FailureClass.FORBIDDEN in BROKEN_CLASSES
    assert FailureClass.INSUFFICIENT_QUOTA in BROKEN_CLASSES
    assert FailureClass.RATE_LIMITED in TRANSIENT_CLASSES
    assert FailureClass.SERVER_ERROR in TRANSIENT_CLASSES
    assert FailureClass.NETWORK_ERROR in TRANSIENT_CLASSES
    assert not (BROKEN_CLASSES & TRANSIENT_CLASSES)


@pytest.mark.parametrize(
    "cls,status,snippet",
    [
        (FailureClass.HEALTHY, 200, "ok"),
        (FailureClass.INVALID, 401, "invalid key (HTTP 401)"),
        (FailureClass.FORBIDDEN, 403, "banned (HTTP 403)"),
        (FailureClass.INSUFFICIENT_QUOTA, 400, "insufficient quota (HTTP 400)"),
        (FailureClass.RATE_LIMITED, 429, "rate limited (HTTP 429)"),
        (FailureClass.NETWORK_ERROR, None, "timeout / unreachable"),
        (FailureClass.SERVER_ERROR, 503, "server error (HTTP 503)"),
        (FailureClass.UNKNOWN, 418, "unexpected response (HTTP 418)"),
    ],
)
def test_describe(cls, status, snippet):
    assert describe(cls, status) == snippet
