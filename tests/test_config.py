"""Tests for YAML config loading, validation, and env interpolation."""
import pytest

from keypool_guard.config import ConfigError, load_config


def test_full_config_roundtrip(write_config):
    path = write_config(
        "http://127.0.0.1:1/v1",
        ["sk-a", {"value": "sk-b", "name": "backup"}],
        interval=30,
        restore_after_successes=2,
        max_concurrency=4,
        timeout_seconds=5,
    )
    config = load_config(path)
    assert config.interval_seconds == 30
    assert config.output_file.endswith("healthy.json")
    provider = config.providers[0]
    assert provider.name == "mock"
    assert provider.endpoint == "http://127.0.0.1:1/v1/chat/completions"
    assert provider.probe.timeout_seconds == 5
    assert provider.probe.max_concurrency == 4
    assert provider.probe.restore_after_successes == 2
    assert [k.value for k in provider.keys] == ["sk-a", "sk-b"]
    assert provider.keys[1].name == "backup"


def test_defaults(write_config):
    path = write_config("https://x.example/v1", ["sk-a"])
    config = load_config(path)
    assert config.interval_seconds == 60.0
    assert config.state_file.endswith("state.json")
    assert config.server.enabled is False
    assert config.server.port == 8390
    assert config.providers[0].probe.timeout_seconds == 15.0
    assert config.providers[0].probe.restore_after_successes == 3


def test_env_interpolation(monkeypatch, write_config):
    monkeypatch.setenv("MY_SECRET_KEY", "sk-from-env")
    path = write_config("https://x.example/v1", ["${MY_SECRET_KEY}"])
    config = load_config(path)
    assert config.providers[0].keys[0].value == "sk-from-env"


def test_env_interpolation_missing_var(monkeypatch, write_config):
    monkeypatch.delenv("MISSING_VAR", raising=False)
    path = write_config("https://x.example/v1", ["${MISSING_VAR}"])
    with pytest.raises(ConfigError, match="MISSING_VAR"):
        load_config(path)


def test_missing_file(tmp_path):
    with pytest.raises(ConfigError, match="not found"):
        load_config(str(tmp_path / "nope.yaml"))


def test_invalid_yaml(tmp_path):
    bad = tmp_path / "bad.yaml"
    bad.write_text("providers: [oops\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="invalid YAML"):
        load_config(str(bad))


@pytest.mark.parametrize(
    "kwargs,match",
    [
        ({"keys": []}, "non-empty"),
        ({"name": ""}, "'name' must be a non-empty string"),
        ({"name": "a b c"}, None),  # spaces are allowed in names
    ],
)
def test_provider_validation(write_config, kwargs, match):
    # "keys" arrives as a kwarg but the factory takes it positionally.
    keys = kwargs.pop("keys", ["sk-a"])
    if match is None:
        load_config(write_config("https://x.example/v1", keys, **kwargs))
        return
    with pytest.raises(ConfigError, match=match):
        load_config(write_config("https://x.example/v1", keys, **kwargs))


def test_bad_base_url(write_config):
    path = write_config("ftp://x.example", ["sk-a"])
    with pytest.raises(ConfigError, match="base_url"):
        load_config(path)


def test_no_providers(tmp_path):
    path = tmp_path / "empty.yaml"
    path.write_text("providers: []\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="providers"):
        load_config(str(path))


def test_duplicate_provider(write_config):
    path = write_config("https://x.example/v1", ["sk-a"], extra=(
        "  - name: mock\n"
        "    base_url: https://x.example/v1\n"
        "    model: m\n"
        "    keys:\n"
        "      - value: sk-c\n"
    ))
    with pytest.raises(ConfigError, match="duplicate provider"):
        load_config(path)


def test_duplicate_key_same_provider(write_config):
    path = write_config("https://x.example/v1", ["sk-a", "sk-a"])
    with pytest.raises(ConfigError, match="duplicate key"):
        load_config(path)


@pytest.mark.parametrize(
    "field,bad",
    [
        ("timeout_seconds", 0),
        ("max_concurrency", 0),
        ("restore_after_successes", 0),
        ("restore_after_successes", 1.5),
    ],
)
def test_bad_probe_values(write_config, field, bad):
    path = write_config(
        "https://x.example/v1",
        ["sk-a"],
        probe_extra=f"      {field}: {bad}\n",
    )
    with pytest.raises(ConfigError, match=field):
        load_config(path)


def test_bad_interval(write_config):
    path = write_config("https://x.example/v1", ["sk-a"], interval=0)
    with pytest.raises(ConfigError, match="interval_seconds"):
        load_config(path)


def test_server_section(write_config):
    path = write_config(
        "https://x.example/v1",
        ["sk-a"],
        server={"enabled": True, "host": "0.0.0.0", "port": 9000},
    )
    config = load_config(path)
    assert config.server.enabled is True
    assert config.server.host == "0.0.0.0"
    assert config.server.port == 9000


def test_bad_port(write_config):
    path = write_config(
        "https://x.example/v1",
        ["sk-a"],
        server={"enabled": True, "port": 99999},
    )
    with pytest.raises(ConfigError, match="server.port"):
        load_config(path)
