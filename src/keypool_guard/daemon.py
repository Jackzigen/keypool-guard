"""Daemon mode: probe on a schedule, publish the healthy subpool.

Each round probes every key of every provider concurrently, folds the
results into the pool, then publishes:

- ``output_file`` (default ``keypool.healthy.json``): the healthy
  subpool, ready to be consumed by a gateway (litellm / one-api / your
  own rotation script). This file contains raw key values — keep it
  private (written with 0600).
- ``state_file``: masked key state for restart continuity.

Optionally serves the same healthy-subpool JSON at ``GET /healthy``.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional

from .config import GuardConfig
from .pool import KeyPool
from .prober import probe_provider
from .state import load_state, save_state

logger = logging.getLogger("keypool-guard")


class HealthEndpoint:
    """Minimal stdlib HTTP server exposing the latest healthy subpool.

    GET /healthy  -> healthy-subpool JSON (200) or 503 before first round
    GET /healthz  -> "ok" (200) once the endpoint is up
    """

    def __init__(self, host: str, port: int):
        outer = self
        self._payload: Optional[dict] = None
        self._lock = threading.Lock()

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802 (stdlib API)
                if self.path == "/healthz":
                    self._send(200, b"ok\n", "text/plain")
                elif self.path == "/healthy":
                    with outer._lock:
                        payload = outer._payload
                    if payload is None:
                        self._send(
                            503,
                            b'{"error": "no probe round completed yet"}\n',
                            "application/json",
                        )
                    else:
                        self._send(
                            200,
                            (json.dumps(payload, indent=2) + "\n").encode(),
                            "application/json",
                        )
                else:
                    self._send(404, b"not found\n", "text/plain")

            def _send(self, status: int, body: bytes, ctype: str) -> None:
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, fmt: str, *args) -> None:  # silence
                logger.debug("endpoint: " + fmt, *args)

        self._httpd = ThreadingHTTPServer((host, port), Handler)
        self.port = self._httpd.server_address[1]
        self._thread: Optional[threading.Thread] = None

    def publish(self, payload: dict) -> None:
        with self._lock:
            self._payload = payload

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._httpd.serve_forever,
            name="keypool-guard-endpoint",
            daemon=True,
        )
        self._thread.start()
        logger.info("health endpoint listening on :%d", self.port)

    def stop(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()


class GuardDaemon:
    """Probe scheduler owning the pool, state file and output file."""

    def __init__(self, config: GuardConfig, pool: Optional[KeyPool] = None):
        self.config = config
        self.pool = pool if pool is not None else KeyPool(config)
        previous = load_state(config.state_file)
        if previous is not None:
            self.pool.restore_json(previous)
        self.rounds = 0

    async def round(self) -> dict:
        """Run one full probe round, then persist state + output files."""
        for provider in self.config.providers:
            results = await probe_provider(provider)
            for result in results:
                self.pool.apply(provider, result)
        self.rounds += 1

        subpool = self.pool.healthy_subpool()
        output_path = self.config.output_file
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(subpool, f, indent=2)
            f.write("\n")
        try:
            os.chmod(output_path, 0o600)
        except OSError:
            pass
        save_state(self.config.state_file, self.pool.to_json())

        healthy = sum(p["healthy"] for p in subpool["providers"])
        total = sum(p["total"] for p in subpool["providers"])
        logger.info(
            "round %d done: %d/%d keys healthy; wrote %s",
            self.rounds,
            healthy,
            total,
            output_path,
        )
        return subpool

    async def run_forever(self) -> None:
        """Probe every ``interval_seconds`` until SIGINT/SIGTERM."""
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        installed = False
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, stop.set)
                installed = True
            except (NotImplementedError, RuntimeError):
                pass  # e.g. Windows / non-main loop

        endpoint: Optional[HealthEndpoint] = None
        if self.config.server.enabled:
            endpoint = HealthEndpoint(self.config.server.host, self.config.server.port)
            endpoint.start()

        try:
            while not stop.is_set():
                await self.round()
                try:
                    await asyncio.wait_for(
                        stop.wait(), timeout=self.config.interval_seconds
                    )
                except asyncio.TimeoutError:
                    pass
        finally:
            if endpoint is not None:
                endpoint.stop()
            if not installed:
                pass  # signal handlers unavailable; caller handles Ctrl-C
