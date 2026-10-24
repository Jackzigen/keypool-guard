"""Shared fixtures: a real local HTTP server mimicking an OpenAI-compatible
chat API.

Every prober/daemon/CLI test goes over a real socket to 127.0.0.1 — the
probing path is never patched or stubbed. Behaviors are programmable per
Bearer token so tests can flip a key between healthy / 401 / 403 / quota /
429 / 5xx / slow, and the server records requests for assertions.
"""
from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

HEALTHY_BODY = {
    "id": "cmpl-mock",
    "object": "chat.completion",
    "choices": [
        {"index": 0, "message": {"role": "assistant", "content": "!"}, "finish_reason": "stop"}
    ],
    "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
}


def error_body(message: str, code: str = "mock_error") -> dict:
    return {"error": {"message": message, "type": code, "code": code}}


class QuietHTTPServer(ThreadingHTTPServer):
    """ThreadingHTTPServer that stays silent when a probe client gives up."""

    daemon_threads = True

    def handle_error(self, request, client_address) -> None:
        import sys

        exc = sys.exc_info()[1]
        if isinstance(exc, (BrokenPipeError, ConnectionResetError, TimeoutError)):
            return  # probe timed out client-side; nothing to report
        super().handle_error(request, client_address)


class MockKeyServer:
    """Programmable OpenAI-compatible mock server on an ephemeral port."""

    def __init__(self):
        self.behaviors = {}  # bearer token -> dict(status, body, delay)
        self.requests = []  # list of dicts: path, auth, payload
        self.active = 0
        self.max_active = 0
        self._lock = threading.Lock()
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_POST(self) -> None:  # noqa: N802 (stdlib API)
                length = int(self.headers.get("Content-Length", "0"))
                raw = self.rfile.read(length) if length else b"{}"
                auth = self.headers.get("Authorization", "")
                token = auth[len("Bearer "):] if auth.startswith("Bearer ") else None
                with outer._lock:
                    outer.requests.append(
                        {
                            "path": self.path,
                            "token": token,
                            "payload": json.loads(raw or b"{}"),
                        }
                    )
                    outer.active += 1
                    outer.max_active = max(outer.max_active, outer.active)
                try:
                    behavior = outer.behaviors.get(token, {"status": 401})
                    if behavior.get("delay"):
                        time.sleep(behavior["delay"])
                    status = behavior.get("status", 401)
                    body = behavior.get("body", error_body("unknown key"))
                    payload = (
                        json.dumps(body).encode()
                        if isinstance(body, dict)
                        else str(body).encode()
                    )
                    self.send_response(status)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                finally:
                    with outer._lock:
                        outer.active -= 1

            def log_message(self, fmt: str, *args) -> None:  # silence
                pass

        self._httpd = QuietHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self._httpd.server_address[1]
        self._thread: threading.Thread | None = None

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}/v1"

    def set(self, token: str, status: int = 200, body: dict | None = None, delay: float = 0.0) -> None:
        """Program the response a given Bearer token will receive."""
        self.behaviors[token] = {
            "status": status,
            "body": body if body is not None else HEALTHY_BODY,
            "delay": delay,
        }

    def start(self) -> None:
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()

    def hit_count(self, token: str) -> int:
        return sum(1 for r in self.requests if r["token"] == token)


@pytest.fixture
def mock_server():
    server = MockKeyServer()
    server.start()
    yield server
    server.stop()


@pytest.fixture
def write_config(tmp_path):
    """Factory writing a keypool-guard YAML config aimed at the mock server."""

    def _write(
        base_url: str,
        keys: list,
        name: str = "mock",
        model: str = "gpt-4o-mini",
        interval: float = 60.0,
        restore_after_successes: int = 3,
        max_concurrency: int = 8,
        timeout_seconds: float = 15.0,
        server: dict | None = None,
        filename: str = "pool.yaml",
        extra: str = "",
        probe_extra: str = "",
    ) -> str:
        key_lines = []
        for entry in keys:
            if isinstance(entry, str):
                key_lines.append(f"      - value: {entry}")
            else:
                # dict with value/name — must span two lines; a single-line
                # "value: x, name: y" is invalid YAML (nested mapping).
                key_lines.append(f"      - value: {entry['value']}")
                if entry.get("name"):
                    key_lines.append(f"        name: {entry['name']}")
        yaml = (
            f"interval_seconds: {interval}\n"
            f"state_file: {tmp_path / 'state.json'}\n"
            f"output_file: {tmp_path / 'healthy.json'}\n"
        )
        if server is not None:
            yaml += (
                f"server:\n"
                f"  enabled: {str(server.get('enabled', False)).lower()}\n"
                f"  host: {server.get('host', '127.0.0.1')}\n"
                f"  port: {server.get('port', 8390)}\n"
            )
        yaml += (
            "providers:\n"
            f"  - name: {name}\n"
            f"    base_url: {base_url}\n"
            f"    model: {model}\n"
            "    probe:\n"
            f"      timeout_seconds: {timeout_seconds}\n"
            f"      max_concurrency: {max_concurrency}\n"
            f"      restore_after_successes: {restore_after_successes}\n"
            + probe_extra
            + "    keys:\n" + "\n".join(key_lines) + "\n" + extra
        )
        path = tmp_path / filename
        path.write_text(yaml, encoding="utf-8")
        return str(path)

    return _write
