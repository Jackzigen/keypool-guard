"""Local JSON state persistence (atomic write, 0600 permissions)."""
from __future__ import annotations

import json
import os
import time
from typing import Optional


def save_state(path: str, providers: dict) -> None:
    """Atomically write the pool state to ``path`` as JSON.

    Writes to a temp file in the same directory, chmod 0600, then
    ``os.replace`` so a crash never leaves a half-written state file.
    """
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(
            {"saved_at": time.time(), "providers": providers},
            f,
            indent=2,
        )
        f.flush()
        os.fsync(f.fileno())
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def load_state(path: str) -> Optional[dict]:
    """Load the ``providers`` dict from a state file.

    Returns ``None`` when the file is missing or unreadable — a corrupt
    state file never prevents startup; the pool just starts fresh.
    """
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    providers = data.get("providers")
    return providers if isinstance(providers, dict) else None
