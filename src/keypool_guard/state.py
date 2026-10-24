"""Local JSON state persistence (atomic write, 0600 permissions)."""
from __future__ import annotations

import json
import os
import time
from typing import Optional


def write_private_json(path: str, payload: dict) -> None:
    """Atomically write ``payload`` as JSON to ``path`` with 0600 perms.

    The temp file is created with 0600 from the first instant (mode is
    applied by ``os.open`` at creation, not fixed up afterwards), so a
    file holding raw key values is never briefly world-readable. The
    write goes to a temp file in the same directory, is fsynced, then
    atomically renamed — a crash never leaves a half-written file.
    """
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except OSError:
        try:
            os.unlink(tmp)  # don't litter on failure
        except OSError:
            pass
        raise


def save_state(path: str, providers: dict) -> None:
    """Atomically write the pool state to ``path`` as JSON (0600)."""
    write_private_json(path, {"saved_at": time.time(), "providers": providers})


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
