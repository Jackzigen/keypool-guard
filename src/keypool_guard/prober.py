"""Active probing engine.

For every key in a provider pool, sends one minimal real request —
an OpenAI-compatible chat completion with ``max_tokens: 1`` — and
classifies the outcome. Probes run concurrently, bounded by a
per-provider semaphore.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import List, Optional

import httpx

from .classify import FailureClass, classify, describe
from .config import KeyConfig, ProviderConfig


def mask_key(key: str) -> str:
    """Mask a key for display: never return the full value.

    A short hash suffix keeps labels unique when two unnamed keys share
    a prefix/suffix — the pool keys state by label, so ``sk-aaa…x12`` and
    ``sk-bbb…x12`` must not collide into one slot.
    """
    import hashlib

    digest = hashlib.sha256(key.encode()).hexdigest()[:6]
    if len(key) <= 8:
        return f"{key[:2]}***-{digest}"
    return f"{key[:6]}...{key[-4:]}-{digest}"


def key_label(key: KeyConfig) -> str:
    return key.name if key.name else mask_key(key.value)


@dataclass
class ProbeResult:
    key_name: str  # label or masked key — never the raw key
    cls: FailureClass
    http_status: Optional[int]
    latency_ms: float
    error: str = ""  # short human detail, safe to print
    body_snippet: str = ""  # first ~120 chars of an error body


def _probe_payload(provider: ProviderConfig) -> dict:
    return {
        "model": provider.model,
        "messages": [{"role": "user", "content": "ping"}],
        "max_tokens": 1,
        "stream": False,
    }


async def probe_key(
    client: httpx.AsyncClient,
    provider: ProviderConfig,
    key: KeyConfig,
) -> ProbeResult:
    """Send one minimal real request with ``key`` and classify the result."""
    name = key_label(key)
    started = time.monotonic()
    try:
        resp = await client.post(
            provider.endpoint,
            headers={"Authorization": f"Bearer {key.value}"},
            json=_probe_payload(provider),
        )
    except (httpx.TimeoutException, httpx.TransportError) as exc:
        latency = (time.monotonic() - started) * 1000.0
        kind = type(exc).__name__
        return ProbeResult(
            key_name=name,
            cls=FailureClass.NETWORK_ERROR,
            http_status=None,
            latency_ms=latency,
            error=f"network error ({kind})",
        )

    latency = (time.monotonic() - started) * 1000.0
    cls = classify(resp.status_code, resp.text)
    body_snippet = ""
    if cls is not FailureClass.HEALTHY:
        body_snippet = resp.text[:120].replace("\n", " ")
    return ProbeResult(
        key_name=name,
        cls=cls,
        http_status=resp.status_code,
        latency_ms=latency,
        error=describe(cls, resp.status_code),
        body_snippet=body_snippet,
    )


async def probe_provider(
    provider: ProviderConfig,
    timeout_seconds: Optional[float] = None,
    max_concurrency: Optional[int] = None,
) -> List[ProbeResult]:
    """Probe every key of a provider concurrently (bounded by a semaphore)."""
    timeout = (
        provider.probe.timeout_seconds
        if timeout_seconds is None
        else timeout_seconds
    )
    concurrency = (
        provider.probe.max_concurrency
        if max_concurrency is None
        else max_concurrency
    )
    semaphore = asyncio.Semaphore(concurrency)

    async with httpx.AsyncClient(timeout=timeout) as client:

        async def run(key: KeyConfig) -> ProbeResult:
            async with semaphore:
                return await probe_key(client, provider, key)

        return list(
            await asyncio.gather(*(run(key) for key in provider.keys))
        )
