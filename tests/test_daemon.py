"""Daemon tests: rounds, output files, health endpoint, restart continuity.

All probing hits the real mock server over HTTP.
"""
import asyncio
import json
import urllib.error
import urllib.request

import pytest

from keypool_guard.config import load_config
from keypool_guard.daemon import GuardDaemon, HealthEndpoint


def run(coro):
    return asyncio.run(coro)


def test_round_writes_healthy_subpool_and_state(mock_server, write_config, tmp_path):
    mock_server.set("sk-ok1", status=200)
    mock_server.set("sk-ok2", status=200)
    mock_server.set("sk-dead", status=401, body={"error": {"message": "bad key"}})
    config_path = write_config(
        mock_server.base_url,
        [{"value": "sk-ok1", "name": "one"}, {"value": "sk-ok2", "name": "two"}, {"value": "sk-dead", "name": "dead"}],
        restore_after_successes=3,
    )
    daemon = GuardDaemon(load_config(config_path))
    subpool = run(daemon.round())

    assert subpool["providers"][0]["healthy"] == 2
    assert subpool["providers"][0]["keys"] == ["sk-ok1", "sk-ok2"]
    assert subpool["providers"][0]["total"] == 3

    output = json.loads((tmp_path / "healthy.json").read_text())
    assert output["providers"][0]["keys"] == ["sk-ok1", "sk-ok2"]

    state = json.loads((tmp_path / "state.json").read_text())
    state_blob = json.dumps(state)
    assert "sk-dead" not in state_blob  # no raw keys in the state file
    key_states = {k["key_name"]: k for k in state["providers"]["mock"]["keys"]}
    assert key_states["dead"]["healthy"] is False
    assert key_states["dead"]["cls"] == "invalid"
    assert key_states["one"]["healthy"] is True


def test_quarantined_key_returns_after_n_consecutive_rounds(
    mock_server, write_config, tmp_path
):
    mock_server.set("sk-flaky", status=500, body={"error": {"message": "boom"}})
    config_path = write_config(
        mock_server.base_url,
        ["sk-flaky"],
        restore_after_successes=2,
    )
    config = load_config(config_path)
    daemon = GuardDaemon(config)

    run(daemon.round())
    assert json.loads((tmp_path / "healthy.json").read_text())["providers"][0]["keys"] == []

    mock_server.set("sk-flaky", status=200)  # key recovered upstream
    run(daemon.round())
    # one healthy round is not enough (hysteresis = 2)
    assert json.loads((tmp_path / "healthy.json").read_text())["providers"][0]["keys"] == []

    run(daemon.round())
    output = json.loads((tmp_path / "healthy.json").read_text())
    assert output["providers"][0]["keys"] == ["sk-flaky"]


def test_restart_preserves_quarantine(mock_server, write_config, tmp_path):
    mock_server.set("sk-dead", status=403, body={"error": {"message": "banned"}})
    config_path = write_config(mock_server.base_url, ["sk-dead"])
    config = load_config(config_path)

    first = GuardDaemon(config)
    run(first.round())

    # A brand-new daemon (simulated restart) must not re-admit the key.
    second = GuardDaemon(config)
    assert second.pool.healthy_keys("mock") == []
    # ...and it stays out until the key really passes hysteresis.
    mock_server.set("sk-dead", status=200)
    run(second.round())
    assert second.pool.healthy_keys("mock") == []


def test_probe_round_hits_every_key(mock_server, write_config):
    keys = [f"sk-{i}" for i in range(5)]
    for key in keys:
        mock_server.set(key, status=200)
    config_path = write_config(mock_server.base_url, keys)
    daemon = GuardDaemon(load_config(config_path))
    run(daemon.round())
    for key in keys:
        assert mock_server.hit_count(key) == 1


def test_health_endpoint_serves_subpool(mock_server, write_config, tmp_path):
    mock_server.set("sk-ok", status=200)
    mock_server.set("sk-bad", status=401, body={"error": {"message": "no"}})
    config_path = write_config(
        mock_server.base_url,
        ["sk-ok", "sk-bad"],
        server={"enabled": True, "host": "127.0.0.1", "port": 0},
    )
    config = load_config(config_path)
    endpoint = HealthEndpoint(config.server.host, 0)  # port 0 = ephemeral
    endpoint.start()
    try:
        # Before any round: 503
        with pytest.raises(urllib.error.HTTPError) as excinfo:
            urllib.request.urlopen(
                f"http://127.0.0.1:{endpoint.port}/healthy", timeout=5
            )
        assert excinfo.value.code == 503

        daemon = GuardDaemon(config)
        subpool = run(daemon.round())
        endpoint.publish(subpool)

        with urllib.request.urlopen(
            f"http://127.0.0.1:{endpoint.port}/healthy", timeout=5
        ) as resp:
            assert resp.status == 200
            body = json.loads(resp.read())
        assert body["providers"][0]["keys"] == ["sk-ok"]
        assert body["providers"][0]["healthy"] == 1

        with urllib.request.urlopen(
            f"http://127.0.0.1:{endpoint.port}/healthz", timeout=5
        ) as resp:
            assert resp.status == 200

        with pytest.raises(urllib.error.HTTPError) as excinfo:
            urllib.request.urlopen(
                f"http://127.0.0.1:{endpoint.port}/nope", timeout=5
            )
        assert excinfo.value.code == 404
    finally:
        endpoint.stop()
