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

    base_url, token = hooks_mod.resolve_server_endpoint()
    assert base_url == "http://127.0.0.1:8765"
    assert token == ""


def test_hook_uses_https_when_the_server_serves_tls(monkeypatch):
    """Regression: the hook posted http:// to an HTTPS port and always failed.

    The connection was refused, the hook fell back to "ask", and no approval
    ever reached the phone. The certificate is issued for the MagicDNS name,
    so that is the address the hook must use.
    """
    import agy_remote.hooks as hooks_mod

    monkeypatch.setattr(
        hooks_mod,
        "read_runtime_state",
        lambda: {
            "auth_token": "tok",
            "port": 8766,
            "base_url": "https://mac-studio.example.ts.net:8766",
        },
    )
    base_url, token = hooks_mod.resolve_server_endpoint()
    assert base_url == "https://mac-studio.example.ts.net:8766"
    assert token == "tok"


def test_config_local_base_url_tracks_tls(tmp_path):
    from agy_remote.config import RemoteConfig

    plain = RemoteConfig(brain_dir=tmp_path, port=8765)
    assert plain.local_base_url == "http://127.0.0.1:8765"

    cert, key = tmp_path / "c", tmp_path / "k"
    cert.write_text("x")
    key.write_text("y")
    secure = RemoteConfig(
        brain_dir=tmp_path,
        port=8766,
        tls_cert=cert,
        tls_key=key,
        tailscale_dns_name="mac-studio.example.ts.net",
    )
    assert secure.local_base_url == "https://mac-studio.example.ts.net:8766"


# ---------------------------------------------------------------------------
# Approvals must not fail silently: `run` should be able to tell whether the
# hook is actually wired on THIS machine before promising remote approvals.
# ---------------------------------------------------------------------------


def _write_hooks_file(tmp_path: Path, command: str) -> Path:
    hooks_file = tmp_path / "hooks.json"
    hooks_file.write_text(
        json.dumps(
            {"remote-approval": {"PreToolUse": [{"matcher": "*", "hooks": [{"type": "command", "command": command}]}]}}
        )
    )
    return hooks_file


def test_hook_health_reports_missing_config(tmp_path: Path):
    from agy_remote.hooks import hook_health

    status, _detail = hook_health(config_dir=tmp_path)
    assert status == "missing"


def test_hook_health_reports_a_stale_binary_path(tmp_path: Path):
    """hooks.json carries an absolute path from install time; a moved checkout,
    a recreated venv, or a config synced from another machine leaves it
    pointing at nothing. agy then quietly falls back to asking in the TUI and
    the phone never sees the approval."""
    from agy_remote.hooks import hook_health

    _write_hooks_file(tmp_path, "/nonexistent/venv/bin/agy-remote hook-pre-tool")
    status, detail = hook_health(config_dir=tmp_path)
    assert status == "broken"
    assert "/nonexistent/venv/bin/agy-remote" in detail


def test_hook_health_accepts_a_working_install(tmp_path: Path):
    from agy_remote.hooks import hook_health

    install_hooks_config(tmp_path)
    status, _detail = hook_health(config_dir=tmp_path)
    assert status == "ok"


def test_hook_health_reports_config_without_our_entry(tmp_path: Path):
    from agy_remote.hooks import hook_health

    (tmp_path / "hooks.json").write_text(json.dumps({"other-plugin": {}}))
    status, _detail = hook_health(config_dir=tmp_path)
    assert status == "missing"


# ---------------------------------------------------------------------------
# setup-hooks must not pin the hook to uv's ephemeral cache. Under `uvx
# agy-remote`, argv[0] AND sys.executable live in ~/.cache/uv/environments-v2/,
# which `uv cache clean` deletes -- quietly breaking approvals until the next
# setup-hooks. (`python -m` is no escape: the interpreter is in the same env.)
# ---------------------------------------------------------------------------


def _fake_cache_argv(monkeypatch, tmp_path: Path) -> Path:
    import sys

    cache = tmp_path / "uv-cache"
    ephemeral = cache / "environments-v2" / "agy-remote-abc123" / "bin" / "agy-remote"
    ephemeral.parent.mkdir(parents=True)
    ephemeral.write_text("#!/bin/sh\n")
    ephemeral.chmod(0o755)
    monkeypatch.setenv("UV_CACHE_DIR", str(cache))
    monkeypatch.setattr(sys, "argv", [str(ephemeral)])
    return tmp_path


def test_a_stable_tool_install_beats_the_ephemeral_cache_binary(monkeypatch, tmp_path: Path):
    from agy_remote.hooks import resolve_hook_command

    _fake_cache_argv(monkeypatch, tmp_path)
    tool_bin = tmp_path / "uv-tools" / "agy-remote" / "bin" / "agy-remote"
    tool_bin.parent.mkdir(parents=True)
    tool_bin.write_text("#!/bin/sh\n")
    tool_bin.chmod(0o755)
    monkeypatch.setenv("UV_TOOL_DIR", str(tmp_path / "uv-tools"))

    command = resolve_hook_command()
    assert command == f"{tool_bin} hook-pre-tool"


def test_without_a_stable_install_the_hook_goes_through_uvx(monkeypatch, tmp_path: Path):
    """uvx re-resolves per call, so the hook self-heals after `uv cache clean`
    instead of pointing at a deleted directory."""

    from agy_remote import hooks as hooks_mod
    from agy_remote.hooks import resolve_hook_command

    _fake_cache_argv(monkeypatch, tmp_path)
    monkeypatch.setenv("UV_TOOL_DIR", str(tmp_path / "no-tools"))
    uvx = tmp_path / "bin" / "uvx"
    uvx.parent.mkdir(parents=True)
    uvx.write_text("#!/bin/sh\n")
    uvx.chmod(0o755)
    monkeypatch.setattr(
        hooks_mod.shutil,
        "which",
        lambda name: str(uvx) if name == "uvx" else None,
    )

    command = resolve_hook_command()
    assert command == f"{uvx} agy-remote hook-pre-tool"


def test_a_stable_argv0_is_used_directly_as_before(monkeypatch, tmp_path: Path):
    import sys

    from agy_remote.hooks import resolve_hook_command

    stable = tmp_path / "project" / ".venv" / "bin" / "agy-remote"
    stable.parent.mkdir(parents=True)
    stable.write_text("#!/bin/sh\n")
    stable.chmod(0o755)
    monkeypatch.setenv("UV_CACHE_DIR", str(tmp_path / "uv-cache"))
    monkeypatch.setattr(sys, "argv", [str(stable)])

    assert resolve_hook_command() == f"{stable} hook-pre-tool"
