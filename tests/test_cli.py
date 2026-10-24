"""CLI tests: check / daemon --once / init, exit codes, key masking."""
import json

import pytest

from keypool_guard import __version__
from keypool_guard.cli import main


def test_check_all_healthy_exit_0(mock_server, write_config, capsys):
    mock_server.set("sk-ok1", status=200)
    mock_server.set("sk-ok2", status=200)
    path = write_config(
        mock_server.base_url,
        [{"value": "sk-ok1", "name": "one"}, "sk-ok2"],
    )
    assert main(["check", "-c", path, "--no-color"]) == 0
    out = capsys.readouterr().out
    assert "mock" in out
    assert "one" in out
    assert "healthy 2/2 keys" in out
    assert "sk-ok1" not in out  # raw key never printed


def test_check_with_bad_key_exit_1(mock_server, write_config, capsys):
    mock_server.set("sk-ok", status=200)
    mock_server.set("sk-dead", status=401, body={"error": {"message": "revoked"}})
    path = write_config(mock_server.base_url, ["sk-ok", "sk-dead"])
    assert main(["check", "-c", path, "--no-color"]) == 1
    out = capsys.readouterr().out
    assert "invalid" in out
    assert "healthy 1/2 keys" in out


def test_check_config_error_exit_2(tmp_path, capsys):
    missing = tmp_path / "missing.yaml"
    assert main(["check", "-c", str(missing)]) == 2
    assert "config error" in capsys.readouterr().err


def test_check_colors_by_default(mock_server, write_config, capsys):
    mock_server.set("sk-ok", status=200)
    path = write_config(mock_server.base_url, ["sk-ok"])
    assert main(["check", "-c", path]) == 0
    assert "\x1b[" in capsys.readouterr().out


def test_daemon_once_writes_output(mock_server, write_config, tmp_path, capsys):
    mock_server.set("sk-ok", status=200)
    mock_server.set("sk-bad", status=429, body={"error": {"message": "slow down"}})
    path = write_config(
        mock_server.base_url, ["sk-ok", "sk-bad"], restore_after_successes=1
    )
    assert main(["daemon", "-c", path, "--once"]) == 0
    output = json.loads((tmp_path / "healthy.json").read_text())
    assert output["providers"][0]["keys"] == ["sk-ok"]
    assert (tmp_path / "state.json").exists()


def test_daemon_once_config_error_exit_2(tmp_path):
    assert main(["daemon", "-c", str(tmp_path / "nope.yaml"), "--once"]) == 2


def test_init_prints_example_config(capsys):
    assert main(["init"]) == 0
    out = capsys.readouterr().out
    assert "providers:" in out
    assert "restore_after_successes" in out
    assert "interval_seconds" in out


def test_version_flag(capsys):
    with pytest.raises(SystemExit) as excinfo:
        main(["--version"])
    assert excinfo.value.code == 0
    assert __version__ in capsys.readouterr().out
