"""Unit tests for hooks module."""

import json
from pathlib import Path

from agy_remote.hooks import install_hooks_config


def test_install_hooks_config(tmp_path: Path):
    hooks_file = install_hooks_config(tmp_path)
    assert hooks_file.exists()

    with open(hooks_file, encoding="utf-8") as f:
        data = json.load(f)

    assert "remote-approval" in data
    assert "PreToolUse" in data["remote-approval"]
    assert data["remote-approval"]["PreToolUse"][0]["matcher"] == "*"


# ---------------------------------------------------------------------------
# The installed hook must actually be executable, and must be cheap to run.
# ---------------------------------------------------------------------------


def test_installed_hook_command_is_an_absolute_executable(tmp_path):
    """`agy-remote` lives in a project venv that is not on PATH.

    Writing the bare name meant agy's `sh -c` could not find it, so every
    approval silently failed to launch.
    """
    import json as _json
    import shlex
    from pathlib import Path as _Path

    from agy_remote.hooks import install_hooks_config

    hooks_file = install_hooks_config(tmp_path)
    data = _json.loads(hooks_file.read_text())
    command = data["remote-approval"]["PreToolUse"][0]["hooks"][0]["command"]

    executable = shlex.split(command)[0]
    assert _Path(executable).is_absolute(), f"hook command is not absolute: {command}"
    assert _Path(executable).exists(), f"hook executable does not exist: {executable}"
    assert command.endswith("hook-pre-tool")


def test_hook_fallback_does_no_network_detection(monkeypatch):
    """With no server published the hook must fail fast, not probe interfaces.

    get_config() shells out to `tailscale ip` and `ifconfig`, costing ~2s on
    every single tool call agy makes.
    """
    import agy_remote.hooks as hooks_mod

    monkeypatch.setattr(hooks_mod, "read_runtime_state", lambda: None)
    monkeypatch.delenv("AGY_REMOTE_PORT", raising=False)

    # hooks.py must not import the expensive config builder at all.
    assert not hasattr(hooks_mod, "get_config"), "hooks.py still reaches for get_config()"

    port, token = hooks_mod.resolve_server_endpoint()
    assert port == 8765
    assert token == ""
