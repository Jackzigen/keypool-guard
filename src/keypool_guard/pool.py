"""Key pool state machine: quarantine bad keys, readmit with hysteresis.

Rules:
- Any non-healthy probe immediately removes a key from the healthy pool
  (quarantine). Broken keys (401/403/quota) and transient trouble
  (429/5xx/network) both leave the pool; the reason is recorded.
- A quarantined key is readmitted only after N *consecutive* healthy
  probes (``probe.restore_after_successes``). Any failure resets the
  streak — this is the hysteresis that prevents flapping.
- Key states are keyed by (provider, label) and never store raw keys.
"""
from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from typing import Dict, List, Optional

from .classify import FailureClass
from .config import GuardConfig, KeyConfig, ProviderConfig
from .prober import ProbeResult, key_label


@dataclass
class KeyState:
    provider: str
    key_name: str  # label or masked key — never the raw key
    healthy: bool = True
    cls: str = FailureClass.HEALTHY.value
    consecutive_successes: int = 0
    consecutive_failures: int = 0
    quarantined_at: Optional[float] = None
    last_checked: Optional[float] = None
    last_latency_ms: Optional[float] = None
    last_error: str = ""
    total_probes: int = 0
    total_failures: int = 0


@dataclass
class ProviderSnapshot:
    name: str
    base_url: str
    model: str
    keys: List[KeyState] = field(default_factory=list)

    @property
    def healthy_count(self) -> int:
        return sum(1 for k in self.keys if k.healthy)

    @property
    def total_count(self) -> int:
        return len(self.keys)


class KeyPool:
    """Holds per-key state for all configured providers."""

    def __init__(self, config: GuardConfig):
        self._config = config
        self._states: Dict[tuple, KeyState] = {}
        self._raw: Dict[tuple, str] = {}  # (provider, label) -> raw key
        for provider in config.providers:
            for key in provider.keys:
                label = key_label(key)
                ident = (provider.name, label)
                self._states[ident] = KeyState(provider.name, label)
                self._raw[ident] = key.value

    # -- state updates ----------------------------------------------------

    def apply(self, provider: ProviderConfig, result: ProbeResult) -> KeyState:
        """Fold one probe result into the pool state and return the state."""
        ident = (provider.name, result.key_name)
        state = self._states[ident]
        state.total_probes += 1
        state.last_checked = time.time()
        state.last_latency_ms = result.latency_ms

        if result.cls is FailureClass.HEALTHY:
            state.consecutive_successes += 1
            state.consecutive_failures = 0
            state.cls = result.cls.value
            state.last_error = ""
            threshold = provider.probe.restore_after_successes
            if not state.healthy and state.consecutive_successes >= threshold:
                # Hysteresis satisfied — readmit and reset the streak.
                state.healthy = True
                state.quarantined_at = None
                state.consecutive_successes = 0
        else:
            state.total_failures += 1
            state.consecutive_failures += 1
            state.consecutive_successes = 0
            state.cls = result.cls.value
            state.last_error = result.error
            if state.healthy:
                state.healthy = False
                state.quarantined_at = time.time()
        return state

    # -- queries -----------------------------------------------------------

    def state(self, provider_name: str, key_name: str) -> KeyState:
        return self._states[(provider_name, key_name)]

    def provider(self, name: str) -> ProviderConfig:
        for provider in self._config.providers:
            if provider.name == name:
                return provider
        raise KeyError(name)

    def healthy_keys(self, provider_name: str) -> List[str]:
        """Raw key values currently fit for the gateway."""
        return [
            self._raw[(provider_name, state.key_name)]
            for state in self._states.values()
            if state.provider == provider_name and state.healthy
        ]

    def snapshot(self) -> Dict[str, ProviderSnapshot]:
        """Per-provider state snapshot (no raw keys) for report/persistence."""
        result: Dict[str, ProviderSnapshot] = {}
        for provider in self._config.providers:
            result[provider.name] = ProviderSnapshot(
                name=provider.name,
                base_url=provider.base_url,
                model=provider.model,
                keys=[
                    self._states[(provider.name, key_label(k))]
                    for k in provider.keys
                ],
            )
        return result

    # -- persistence -------------------------------------------------------

    def to_json(self) -> dict:
        """Serialize state for the state file (no raw keys)."""
        return {
            name: {
                "base_url": snap.base_url,
                "model": snap.model,
                "keys": [asdict(k) for k in snap.keys],
            }
            for name, snap in self.snapshot().items()
        }

    def restore_json(self, data: dict) -> None:
        """Merge a previously saved state file into this pool.

        Only (provider, key_name) pairs that exist in the current config
        are restored; everything else is ignored, so editing the config
        never breaks a restart.
        """
        if not isinstance(data, dict):
            return
        for provider_name, provider_data in data.items():
            if provider_name not in self.snapshot():
                continue
            for key_data in provider_data.get("keys", []):
                ident = (provider_name, key_data.get("key_name", ""))
                state = self._states.get(ident)
                if state is None:
                    continue
                try:
                    state.healthy = bool(key_data["healthy"])
                    state.cls = str(
                        key_data.get("cls", FailureClass.UNKNOWN.value)
                    )
                    state.consecutive_successes = int(
                        key_data.get("consecutive_successes", 0)
                    )
                    state.consecutive_failures = int(
                        key_data.get("consecutive_failures", 0)
                    )
                    state.quarantined_at = key_data.get("quarantined_at")
                    state.last_checked = key_data.get("last_checked")
                    state.last_latency_ms = key_data.get("last_latency_ms")
                    state.last_error = str(key_data.get("last_error", "") or "")
                    state.total_probes = int(key_data.get("total_probes", 0))
                    state.total_failures = int(
                        key_data.get("total_failures", 0)
                    )
                except (KeyError, TypeError, ValueError):
                    continue  # corrupt entry — keep the fresh state

    # -- daemon output -----------------------------------------------------

    def healthy_subpool(self) -> dict:
        """The healthy-subpool document consumed by gateways.

        This is the only serialization that contains raw key values.
        """
        providers = []
        for provider in self._config.providers:
            keys = self.healthy_keys(provider.name)
            providers.append(
                {
                    "name": provider.name,
                    "base_url": provider.base_url,
                    "model": provider.model,
                    "healthy": len(keys),
                    "total": len(provider.keys),
                    "keys": keys,
                }
            )
        return {
            "updated_at": time.strftime(
                "%Y-%m-%dT%H:%M:%SZ", time.gmtime()
            ),
            "providers": providers,
        }
