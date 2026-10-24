"""State file persistence: atomic write, permissions, corrupt tolerance."""
import json
import os
import stat

from keypool_guard.state import load_state, save_state


def test_save_and_load_roundtrip(tmp_path):
    path = str(tmp_path / "state.json")
    providers = {"mock": {"keys": [{"key_name": "a", "healthy": True}]}}
    save_state(path, providers)
    assert load_state(path) == providers


def test_save_is_atomic_no_tmp_leftover(tmp_path):
    path = str(tmp_path / "state.json")
    save_state(path, {"mock": {}})
    assert not os.path.exists(path + ".tmp")
    assert json.loads(open(path).read())["providers"] == {"mock": {}}


def test_saved_file_permissions_0600(tmp_path):
    path = str(tmp_path / "state.json")
    save_state(path, {"mock": {}})
    mode = stat.S_IMODE(os.stat(path).st_mode)
    assert mode == 0o600


def test_load_missing_returns_none(tmp_path):
    assert load_state(str(tmp_path / "nope.json")) is None


def test_load_corrupt_returns_none(tmp_path):
    path = tmp_path / "corrupt.json"
    path.write_text("{not json", encoding="utf-8")
    assert load_state(str(path)) is None


def test_load_wrong_shape_returns_none(tmp_path):
    path = tmp_path / "wrong.json"
    path.write_text('{"providers": [1, 2]}', encoding="utf-8")  # not a dict
    assert load_state(str(path)) is None


def test_save_creates_missing_directory(tmp_path):
    path = str(tmp_path / "deep" / "nested" / "state.json")
    save_state(path, {"mock": {}})
    assert load_state(path) == {"mock": {}}
