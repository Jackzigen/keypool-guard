"""Prober tests against a real local HTTP server.

No patching: every test sends real requests over real sockets to the
stdlib mock server from conftest.py.
"""
import asyncio
import time

import httpx
import pytest

from keypool_guard.classify import FailureClass
from keypool_guard.config import ProviderConfig, KeyConfig, ProbeConfig
from keypool_guard.prober import mask_key, probe_key, probe_provider

from conftest import HEALTHY_BODY, error_body


def make_provider(base_url: str, keys, **probe_overrides) -> ProviderConfig:
    return ProviderConfig(
        name="mock",
        base_url=base_url,
        model="gpt-4o-mini",
        keys=[KeyConfig(value=k) for k in keys],
        probe=ProbeConfig(**{"timeout_seconds": 15.0, **probe_overrides}),
    )


def run(coro):
    return asyncio.run(coro)


def test_mask_key_never_reveals_full_value():
    masked = mask_key("sk-verylongsecretkey123456")
    assert "verylongsecretkey123456" not in masked
    assert "*" in mask_key("short")  # short keys use the *** form
    # uniqueness: two different unnamed keys must never share a label
    assert mask_key("sk-aaa111") != mask_key("sk-bbb222")


@pytest.mark.parametrize(
    "status,body,expected_cls",
    [
        (200, HEALTHY_BODY, FailureClass.HEALTHY),
        (401, error_body("invalid api key"), FailureClass.INVALID),
        (403, error_body("key banned"), FailureClass.FORBIDDEN),
        (400, error_body("insufficient balance"), FailureClass.INSUFFICIENT_QUOTA),
        (402, error_body("payment required"), FailureClass.INSUFFICIENT_QUOTA),
        (429, error_body("too many requests"), FailureClass.RATE_LIMITED),
        (429, {"error": {"code": "insufficient_quota"}}, FailureClass.INSUFFICIENT_QUOTA),
        (500, error_body("boom"), FailureClass.SERVER_ERROR),
        (503, error_body("unavailable"), FailureClass.SERVER_ERROR),
    ],
)
def test_probe_real_http_statuses(mock_server, status, body, expected_cls):
    mock_server.set("sk-live", status=status, body=body)
    provider = make_provider(mock_server.base_url, ["sk-live"])
    result = run(probe_provider(provider))[0]
    assert result.cls is expected_cls
    assert result.http_status == status
    assert result.latency_ms >= 0.0
    assert "sk-live" not in result.error  # raw key never leaks into details


def test_probe_sends_minimal_real_request(mock_server):
    mock_server.set("sk-live")
    provider = make_provider(mock_server.base_url, ["sk-live"])
    run(probe_provider(provider))
    request = mock_server.requests[0]
    assert request["path"] == "/v1/chat/completions"
    assert request["token"] == "sk-live"  # correct Authorization header
    assert request["payload"]["model"] == "gpt-4o-mini"
    assert request["payload"]["max_tokens"] == 1
    assert request["payload"]["stream"] is False
    assert request["payload"]["messages"] == [
        {"role": "user", "content": "ping"}
    ]


def test_probe_timeout_is_network_error(mock_server):
    # Server sleeps 2s; client gives up after 0.3s — a real timeout.
    mock_server.set("sk-slow", status=200, delay=2.0)
    provider = make_provider(
        mock_server.base_url, ["sk-slow"], timeout_seconds=0.3
    )
    result = run(probe_provider(provider))[0]
    assert result.cls is FailureClass.NETWORK_ERROR
    assert result.http_status is None


def test_probe_connect_error_is_network_error():
    # Nothing listens on this port — a real connection refusal.
    provider = make_provider("http://127.0.0.1:1/v1", ["sk-x"])
    result = run(probe_provider(provider, timeout_seconds=2.0))[0]
    assert result.cls is FailureClass.NETWORK_ERROR
    assert result.http_status is None


def test_unknown_key_defaults_to_401(mock_server):
    provider = make_provider(mock_server.base_url, ["sk-not-programmed"])
    result = run(probe_provider(provider))[0]
    assert result.cls is FailureClass.INVALID
    assert result.http_status == 401


def test_concurrency_bounded_by_semaphore(mock_server):
    keys = [f"sk-par-{i}" for i in range(6)]
    for key in keys:
        mock_server.set(key, status=200, delay=0.3)
    provider = make_provider(
        mock_server.base_url, keys, max_concurrency=2
    )
    started = time.monotonic()
    results = run(probe_provider(provider))
    elapsed = time.monotonic() - started

    assert all(r.cls is FailureClass.HEALTHY for r in results)
    # Never more than 2 requests in flight at the server...
    assert mock_server.max_active <= 2
    # ...but at least 2 concurrent (proves parallelism actually happens).
    assert mock_server.max_active >= 2
    # 6 keys x 0.3s serially would be >= 1.8s; two lanes should land
    # near 0.9s. Generous ceiling still proves concurrency happened.
    assert elapsed < 1.7


def test_latency_recorded(mock_server):
    mock_server.set("sk-live", status=200, delay=0.1)
    provider = make_provider(mock_server.base_url, ["sk-live"])
    result = run(probe_provider(provider))[0]
    assert result.latency_ms >= 100.0


def test_key_label_prefers_name(mock_server):
    provider = ProviderConfig(
        name="mock",
        base_url=mock_server.base_url,
        model="gpt-4o-mini",
        keys=[KeyConfig(value="sk-abc12345678", name="primary")],
        probe=ProbeConfig(),
    )
    mock_server.set("sk-abc12345678")
    client = httpx.AsyncClient(timeout=15.0)
    result = run(probe_key(client, provider, provider.keys[0]))
    assert result.key_name == "primary"
