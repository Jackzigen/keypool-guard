"""Pool state machine tests: quarantine, hysteresis backfill, persistence."""
import pytest

from keypool_guard.classify import FailureClass
from keypool_guard.config import GuardConfig, KeyConfig, ProbeConfig, ProviderConfig
from keypool_guard.pool import KeyPool
from keypool_guard.prober import ProbeResult
from keypool_guard.state import load_state, save_state


def build_config(restore_after_successes: int = 3) -> GuardConfig:
    return GuardConfig(
        state_file="/tmp/unused-state.json",
        output_file="/tmp/unused-output.json",
        providers=[
            ProviderConfig(
                name="mock",
                base_url="http://127.0.0.1:1/v1",
                model="m",
                keys=[
                    KeyConfig(value="sk-good", name="good"),
                    KeyConfig(value="sk-flaky", name="flaky"),
                    KeyConfig(value="sk-bad", name="bad"),
                ],
                probe=ProbeConfig(restore_after_successes=restore_after_successes),
            )
        ],
    )


def result(key_name: str, cls: FailureClass, status=None) -> ProbeResult:
    return ProbeResult(
        key_name=key_name, cls=cls, http_status=status, latency_ms=12.0
    )


def apply_named(pool, config, key_name, cls, status=None):
    return pool.apply(
        config.providers[0], result(key_name, cls, status)
    )


def test_all_keys_start_healthy():
    pool = KeyPool(build_config())
    assert pool.healthy_keys("mock") == ["sk-good", "sk-flaky", "sk-bad"]


def test_failure_quarantines_immediately():
    config = build_config()
    pool = KeyPool(config)
    state = apply_named(pool, config, "flaky", FailureClass.INVALID, 401)
    assert state.healthy is False
    assert state.cls == "invalid"
    assert state.quarantined_at is not None
    assert pool.healthy_keys("mock") == ["sk-good", "sk-bad"]


def test_transient_failure_also_leaves_pool():
    config = build_config()
    pool = KeyPool(config)
    apply_named(pool, config, "flaky", FailureClass.RATE_LIMITED, 429)
    assert "sk-flaky" not in pool.healthy_keys("mock")


def test_hysteresis_requires_n_consecutive_successes():
    config = build_config(restore_after_successes=3)
    pool = KeyPool(config)
    apply_named(pool, config, "flaky", FailureClass.SERVER_ERROR, 500)
    assert pool.state("mock", "flaky").healthy is False

    apply_named(pool, config, "flaky", FailureClass.HEALTHY, 200)
    apply_named(pool, config, "flaky", FailureClass.HEALTHY, 200)
    # two healthy probes — still not enough
    assert pool.state("mock", "flaky").healthy is False
    assert pool.state("mock", "flaky").consecutive_successes == 2

    apply_named(pool, config, "flaky", FailureClass.HEALTHY, 200)
    state = pool.state("mock", "flaky")
    assert state.healthy is True
    assert state.quarantined_at is None
    assert state.consecutive_successes == 0  # streak reset after readmission
    assert "sk-flaky" in pool.healthy_keys("mock")


def test_failure_resets_recovery_streak():
    config = build_config(restore_after_successes=2)
    pool = KeyPool(config)
    apply_named(pool, config, "flaky", FailureClass.FORBIDDEN, 403)

    apply_named(pool, config, "flaky", FailureClass.HEALTHY, 200)
    apply_named(pool, config, "flaky", FailureClass.RATE_LIMITED, 429)  # streak reset
    apply_named(pool, config, "flaky", FailureClass.HEALTHY, 200)
    assert pool.state("mock", "flaky").healthy is False  # only 1 consecutive
    apply_named(pool, config, "flaky", FailureClass.HEALTHY, 200)
    assert pool.state("mock", "flaky").healthy is True


def test_healthy_key_failing_goes_out_and_counts():
    config = build_config()
    pool = KeyPool(config)
    state = apply_named(pool, config, "good", FailureClass.NETWORK_ERROR)
    assert state.healthy is False
    assert state.total_failures == 1
    assert state.consecutive_failures == 1


def test_snapshot_and_to_json_never_contain_raw_keys():
    config = build_config()
    pool = KeyPool(config)
    apply_named(pool, config, "bad", FailureClass.INVALID, 401)
    import json

    blob = json.dumps(pool.to_json())
    assert "sk-good" not in blob
    assert "sk-flaky" not in blob
    assert "sk-bad" not in blob


def test_state_persistence_roundtrip(tmp_path):
    config = build_config()
    pool = KeyPool(config)
    apply_named(pool, config, "bad", FailureClass.INVALID, 401)
    apply_named(pool, config, "flaky", FailureClass.RATE_LIMITED, 429)
    state_path = str(tmp_path / "state.json")
    save_state(state_path, pool.to_json())

    restarted = KeyPool(config)
    restarted.restore_json(load_state(state_path))
    assert restarted.state("mock", "bad").healthy is False
    assert restarted.state("mock", "bad").cls == "invalid"
    assert restarted.state("mock", "flaky").healthy is False
    assert restarted.state("mock", "good").healthy is True
    assert "sk-bad" not in restarted.healthy_keys("mock")


def test_restore_ignores_unknown_providers_and_keys(tmp_path):
    config = build_config()
    pool = KeyPool(config)
    pool.restore_json(
        {
            "ghost-provider": {"keys": [{"key_name": "x", "healthy": False}]},
            "mock": {
                "keys": [
                    {"key_name": "ghost-key", "healthy": False},
                    {"key_name": "bad", "healthy": False, "cls": "forbidden"},
                ]
            },
        }
    )
    assert pool.state("mock", "good").healthy is True  # untouched
    assert pool.state("mock", "bad").healthy is False  # restored
    assert pool.state("mock", "bad").cls == "forbidden"
    with pytest.raises(KeyError):
        pool.state("ghost-provider", "x")


def test_restore_tolerates_corrupt_entries():
    config = build_config()
    pool = KeyPool(config)
    pool.restore_json(
        {"mock": {"keys": [{"key_name": "bad", "healthy": "not-a-bool",
                            "total_probes": "NaN"}]}}
    )
    # corrupt entry left the fresh state intact
    assert pool.state("mock", "bad").healthy is True


def test_healthy_subpool_output():
    config = build_config()
    pool = KeyPool(config)
    apply_named(pool, config, "bad", FailureClass.INSUFFICIENT_QUOTA, 400)
    doc = pool.healthy_subpool()
    provider = doc["providers"][0]
    assert provider["name"] == "mock"
    assert provider["healthy"] == 2
    assert provider["total"] == 3
    assert provider["keys"] == ["sk-good", "sk-flaky"]
    assert doc["updated_at"].endswith("Z")
