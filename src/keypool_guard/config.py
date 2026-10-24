"""YAML configuration loading and validation.

Config shape (see ``keypool-guard init`` for a full example):

    interval_seconds: 60
    state_file: keypool_state.json
    output_file: keypool.healthy.json
    server:
      enabled: false
      host: 127.0.0.1
      port: 8390
    providers:
      - name: openai
        base_url: https://api.openai.com/v1
        model: gpt-4o-mini
        probe:
          timeout_seconds: 15
          max_concurrency: 8
          restore_after_successes: 3
        keys:
          - value: ${OPENAI_KEY_1}
          - value: sk-plain-key
            name: backup

``${VAR}`` references are expanded from the environment so keys never
have to sit in the YAML file itself.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List

import yaml


class ConfigError(ValueError):
    """Invalid configuration. The message is safe to show to users."""


_ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _interpolate_env(value: str, origin: str) -> str:
    def repl(match: "re.Match[str]") -> str:
        var = match.group(1)
        resolved = os.environ.get(var)
        if resolved is None:
            raise ConfigError(
                f"{origin}: environment variable '{var}' is not set"
            )
        return resolved

    return _ENV_PATTERN.sub(repl, value)


@dataclass
class ProbeConfig:
    timeout_seconds: float = 15.0
    max_concurrency: int = 8
    # Hysteresis: a quarantined key must pass this many consecutive
    # probes before it is readmitted to the healthy pool.
    restore_after_successes: int = 3


@dataclass
class KeyConfig:
    value: str
    name: str = ""  # optional label; defaults to a masked form of value


@dataclass
class ProviderConfig:
    name: str
    base_url: str  # OpenAI-compatible root, e.g. https://api.openai.com/v1
    model: str  # cheap model used for the 1-token probe request
    keys: List[KeyConfig]
    probe: ProbeConfig = field(default_factory=ProbeConfig)

    @property
    def endpoint(self) -> str:
        return self.base_url.rstrip("/") + "/chat/completions"


@dataclass
class ServerConfig:
    enabled: bool = False
    host: str = "127.0.0.1"
    port: int = 8390


@dataclass
class GuardConfig:
    interval_seconds: float = 60.0
    state_file: str = "keypool_state.json"
    output_file: str = "keypool.healthy.json"
    providers: List[ProviderConfig] = field(default_factory=list)
    server: ServerConfig = field(default_factory=ServerConfig)


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ConfigError(message)


def _parse_probe(raw: Any, origin: str) -> ProbeConfig:
    probe = ProbeConfig()
    if raw is None:
        return probe
    _require(isinstance(raw, dict), f"{origin}: probe must be a mapping")
    if "timeout_seconds" in raw:
        _require(
            isinstance(raw["timeout_seconds"], (int, float))
            and raw["timeout_seconds"] > 0,
            f"{origin}: probe.timeout_seconds must be a positive number",
        )
        probe.timeout_seconds = float(raw["timeout_seconds"])
    if "max_concurrency" in raw:
        _require(
            isinstance(raw["max_concurrency"], int) and raw["max_concurrency"] >= 1,
            f"{origin}: probe.max_concurrency must be an integer >= 1",
        )
        probe.max_concurrency = raw["max_concurrency"]
    if "restore_after_successes" in raw:
        _require(
            isinstance(raw["restore_after_successes"], int)
            and raw["restore_after_successes"] >= 1,
            f"{origin}: probe.restore_after_successes must be an integer >= 1",
        )
        probe.restore_after_successes = raw["restore_after_successes"]
    return probe


def _parse_keys(raw: Any, origin: str) -> List[KeyConfig]:
    _require(
        isinstance(raw, list) and len(raw) >= 1,
        f"{origin}: keys must be a non-empty list",
    )
    keys: List[KeyConfig] = []
    seen: set = set()
    labels: set = set()
    for i, item in enumerate(raw):
        korigin = f"{origin}: keys[{i}]"
        if isinstance(item, str):
            value, name = item, ""
        elif isinstance(item, dict):
            _require("value" in item, f"{korigin}: missing 'value'")
            value = item["value"]
            _require(
                isinstance(value, str) and value.strip() != "",
                f"{korigin}: 'value' must be a non-empty string",
            )
            name = item.get("name", "")
            _require(
                isinstance(name, str),
                f"{korigin}: 'name' must be a string",
            )
        else:
            raise ConfigError(f"{korigin}: must be a string or a mapping")
        value = _interpolate_env(value, korigin).strip()
        _require(value != "", f"{korigin}: key resolves to an empty string")
        _require(
            value not in seen,
            f"{origin}: duplicate key at index {i}",
        )
        seen.add(value)
        # Explicit labels must be unique per provider: pool state is keyed
        # by (provider, label), so two keys named "main" would silently
        # overwrite each other's state.
        if name:
            _require(
                name not in labels,
                f"{origin}: duplicate key name '{name}' "
                f"(keys[{i}] clashes with an earlier key)",
            )
            labels.add(name)
        keys.append(KeyConfig(value=value, name=name))
    return keys


def _parse_provider(raw: Any, index: int) -> ProviderConfig:
    origin = f"providers[{index}]"
    _require(isinstance(raw, dict), f"{origin}: must be a mapping")
    _require(
        isinstance(raw.get("name"), str) and raw["name"].strip() != "",
        f"{origin}: 'name' must be a non-empty string",
    )
    _require(
        isinstance(raw.get("base_url"), str)
        and raw["base_url"].startswith(("http://", "https://")),
        f"{origin}: 'base_url' must start with http:// or https://",
    )
    _require(
        isinstance(raw.get("model"), str) and raw["model"].strip() != "",
        f"{origin}: 'model' must be a non-empty string",
    )
    return ProviderConfig(
        name=raw["name"].strip(),
        base_url=_interpolate_env(raw["base_url"], origin).rstrip("/"),
        model=raw["model"].strip(),
        keys=_parse_keys(raw.get("keys"), origin),
        probe=_parse_probe(raw.get("probe"), origin),
    )


def load_config(path: str) -> GuardConfig:
    """Load and validate a YAML config file."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f)
    except FileNotFoundError:
        raise ConfigError(f"config file not found: {path}")
    except yaml.YAMLError as exc:
        raise ConfigError(f"invalid YAML in {path}: {exc}")

    _require(isinstance(raw, dict), "config root must be a mapping")
    _require(
        isinstance(raw.get("providers"), list) and len(raw["providers"]) >= 1,
        "config must define a non-empty 'providers' list",
    )

    providers = [_parse_provider(p, i) for i, p in enumerate(raw["providers"])]

    names = [p.name for p in providers]
    _require(
        len(names) == len(set(names)),
        f"duplicate provider names: {sorted(n for n in names if names.count(n) > 1)}",
    )

    config = GuardConfig(providers=providers)

    if "interval_seconds" in raw:
        _require(
            isinstance(raw["interval_seconds"], (int, float))
            and raw["interval_seconds"] > 0,
            "interval_seconds must be a positive number",
        )
        config.interval_seconds = float(raw["interval_seconds"])
    if "state_file" in raw:
        _require(
            isinstance(raw["state_file"], str) and raw["state_file"] != "",
            "state_file must be a non-empty string",
        )
        config.state_file = raw["state_file"]
    if "output_file" in raw:
        _require(
            isinstance(raw["output_file"], str) and raw["output_file"] != "",
            "output_file must be a non-empty string",
        )
        config.output_file = raw["output_file"]

    server_raw = raw.get("server")
    if server_raw is not None:
        _require(
            isinstance(server_raw, dict),
            "server must be a mapping",
        )
        if "enabled" in server_raw:
            config.server.enabled = bool(server_raw["enabled"])
        if "host" in server_raw:
            _require(
                isinstance(server_raw["host"], str) and server_raw["host"] != "",
                "server.host must be a non-empty string",
            )
            config.server.host = server_raw["host"]
        if "port" in server_raw:
            _require(
                isinstance(server_raw["port"], int) and 0 <= server_raw["port"] < 65536,
                "server.port must be an integer in [0, 65536)",
            )
            config.server.port = server_raw["port"]

    return config


EXAMPLE_CONFIG = """\
# keypool-guard example configuration.
# Probe = one minimal chat completion (max_tokens: 1) per key per round.

interval_seconds: 60          # seconds between probe rounds (daemon mode)
state_file: keypool_state.json
output_file: keypool.healthy.json

# Optional HTTP endpoint serving the healthy subpool (GET /healthy).
server:
  enabled: false
  host: 127.0.0.1
  port: 8390

providers:
  - name: openai
    base_url: https://api.openai.com/v1
    model: gpt-4o-mini        # cheap model; the probe costs ~1 token
    probe:
      timeout_seconds: 15
      max_concurrency: 8      # parallel probes per provider
      restore_after_successes: 3   # hysteresis: N consecutive healthy
                                   # probes before a quarantined key is
                                   # readmitted (anti-flapping)
    keys:
      - value: ${OPENAI_KEY_MAIN}   # expanded from the environment
        name: main
      - value: sk-example-backup-key
        name: backup

  - name: deepseek
    base_url: https://api.deepseek.com/v1
    model: deepseek-chat
    keys:
      - value: ${DEEPSEEK_KEY}
"""
