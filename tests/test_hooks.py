"""Unit tests for hooks module."""

import json
from pathlib import Path

import pytest

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

    monkeypatch.setattr(hooks_mod, "live_runtime_state", lambda: None)
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
        "live_runtime_state",
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


def test_hook_health_accepts_a_working_install(tmp_path: Path, monkeypatch):
    import shlex

    from agy_remote import hooks as hooks_mod

    install_hooks_config(tmp_path)
    hooks_file = tmp_path / "hooks.json"
    command = json.loads(hooks_file.read_text())["remote-approval"]["PreToolUse"][0]["hooks"][0]["command"]
    # Pin parity to whatever the resolved binary actually reports, so the test
    # stays green while checkout and tool install drift in real life.
    reported = hooks_mod._hook_binary_version(shlex.split(command)[0])
    monkeypatch.setattr(hooks_mod, "_RUNNING_VERSION", reported)
    status, _detail = hooks_mod.hook_health(config_dir=tmp_path)
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


# ---------------------------------------------------------------------------
# A supervised launch with --dangerously-skip-permissions must actually skip.
# The flag turns off agy's built-in checks, but PreToolUse hooks still fire --
# without a marker from the launching server the hook re-implements the very
# gate the user asked to remove.
# ---------------------------------------------------------------------------


def _run_hook(monkeypatch, stdin_payload: str = '{"toolCall": {"name": "run_command"}}') -> str:
    """Run the hook with stdin mocked; returns what it printed for agy."""
    import io

    monkeypatch.setattr("sys.stdin", io.StringIO(stdin_payload))
    import contextlib

    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        from agy_remote.hooks import run_pre_tool_hook

        run_pre_tool_hook()
    return out.getvalue()


def test_a_skip_permissions_launch_allows_every_tool_call_without_the_server(monkeypatch):
    """The marker comes from the supervised launch, so no server round trip --
    and no prompt -- is needed. The endpoint would 401 or hang on nothing."""
    import agy_remote.hooks as hooks_mod

    monkeypatch.setenv("AGY_REMOTE_SKIP_PERMISSIONS", "1")

    def explode(*a, **kw):
        raise AssertionError("the hook must not contact the server under skip-permissions")

    monkeypatch.setattr(hooks_mod.urllib.request, "urlopen", explode)

    decision = json.loads(_run_hook(monkeypatch))
    assert decision["decision"] == "allow"


@pytest.mark.parametrize("value", ["0", "", "false", "yes-but-typo"])
def test_without_the_marker_the_hook_does_not_auto_allow(monkeypatch, value):
    """Only an explicit marker from the supervised launch skips; a hand-started
    agy or a stale env must keep the approval flow intact."""
    import agy_remote.hooks as hooks_mod

    if value:
        monkeypatch.setenv("AGY_REMOTE_SKIP_PERMISSIONS", value)
    else:
        monkeypatch.delenv("AGY_REMOTE_SKIP_PERMISSIONS", raising=False)

    monkeypatch.setattr(hooks_mod, "_ancestor_skip_permissions", lambda: False)

    def unreachable(req, timeout=0):
        raise hooks_mod.urllib.error.URLError("connection refused")

    monkeypatch.setattr(hooks_mod.urllib.request, "urlopen", unreachable)

    decision = json.loads(_run_hook(monkeypatch))
    assert decision["decision"] == "ask"


def test_ancestor_skip_permissions_allows_tool_without_env_marker(monkeypatch):
    """When agy is run by hand with --dangerously-skip-permissions, the hook detects it in ancestor args."""
    import agy_remote.hooks as hooks_mod

    monkeypatch.delenv("AGY_REMOTE_SKIP_PERMISSIONS", raising=False)
    monkeypatch.setattr(hooks_mod, "_ancestor_skip_permissions", lambda: True)

    def explode(*a, **kw):
        raise AssertionError("the hook must not contact the server when ancestor skipped permissions")

    monkeypatch.setattr(hooks_mod.urllib.request, "urlopen", explode)

    decision = json.loads(_run_hook(monkeypatch))
    assert decision["decision"] == "allow"


def test_run_recognizes_the_skip_permissions_flag_in_its_passthrough_args():
    from agy_remote.cli import _wants_skip_permissions

    assert _wants_skip_permissions(["--dangerously-skip-permissions"])
    assert _wants_skip_permissions(["--model", "x", "--dangerously-skip-permissions"])
    assert _wants_skip_permissions(["--dangerously-skip-permissions=true"])
    assert not _wants_skip_permissions([])
    assert not _wants_skip_permissions(["--model", "x"])
    # A different flag that merely contains the words must not match.
    assert not _wants_skip_permissions(["--dangerously-skip-permissions-nothing"])


# ---------------------------------------------------------------------------
# A stale copy must be impossible to miss. The uv tool install drifts behind
# the checkout it was installed from, and a mixed pair -- new run with an old
# hook, or the reverse -- breaks the approval protocol silently.
# ---------------------------------------------------------------------------


def _fake_hook_binary(tmp_path: Path, output: str, name: str = "agy-remote") -> str:

    binary = tmp_path / "bin" / name
    binary.parent.mkdir(exist_ok=True)
    binary.write_text(f"#!/bin/sh\ncat <<'EOF'\n{output}\nEOF\n")
    binary.chmod(0o755)
    return str(binary)


def test_a_hook_binary_from_another_build_is_stale(monkeypatch, tmp_path: Path):
    from agy_remote import hooks as hooks_mod

    binary = _fake_hook_binary(tmp_path, "agy-remote v0.0.0.1")
    monkeypatch.setattr(hooks_mod, "_RUNNING_VERSION", "26.08.30.102")
    _write_hooks_file(tmp_path, f"{binary} hook-pre-tool")

    status, detail = hooks_mod.hook_health(config_dir=tmp_path)

    assert status == "stale"
    assert "0.0.0.1" in detail and "26.08.30.102" in detail


def test_a_hook_binary_that_reports_no_version_is_stale(monkeypatch, tmp_path: Path):
    from agy_remote import hooks as hooks_mod

    binary = _fake_hook_binary(tmp_path, "something else entirely")
    monkeypatch.setattr(hooks_mod, "_RUNNING_VERSION", "26.08.30.102")
    _write_hooks_file(tmp_path, f"{binary} hook-pre-tool")

    status, detail = hooks_mod.hook_health(config_dir=tmp_path)

    assert status == "stale"
    assert "no version" in detail


def test_a_matching_hook_binary_is_ok(monkeypatch, tmp_path: Path):
    from agy_remote import hooks as hooks_mod

    binary = _fake_hook_binary(tmp_path, "agy-remote v26.08.30.102")
    monkeypatch.setattr(hooks_mod, "_RUNNING_VERSION", "26.08.30.102")
    _write_hooks_file(tmp_path, f"{binary} hook-pre-tool")

    status, _detail = hooks_mod.hook_health(config_dir=tmp_path)

    assert status == "ok"


@pytest.mark.parametrize("command_form", ["uvx", "module"])
def test_the_version_check_skips_forms_that_cannot_answer_cheaply(monkeypatch, tmp_path: Path, command_form):
    """`uvx` resolves the package over the network and `python -m --version`
    prints the interpreter's version -- neither may run on every tool call's
    health check, and neither may be mistaken for a stale binary."""
    import sys

    from agy_remote import hooks as hooks_mod

    monkeypatch.setattr(hooks_mod, "_RUNNING_VERSION", "26.08.30.102")
    if command_form == "uvx":
        # A uvx that would fail loudly if spawned: the check must not run it.
        fake_uvx = _fake_hook_binary(tmp_path, "", name="uvx")
        command = f"{fake_uvx} agy-remote hook-pre-tool"
    else:
        command = f"{sys.executable} -m agy_remote.cli hook-pre-tool"
    _write_hooks_file(tmp_path, command)

    status, _detail = hooks_mod.hook_health(config_dir=tmp_path)

    assert status == "ok"


def test_the_hook_advertises_its_version_to_the_server(monkeypatch):
    """So a mixed install is visible while running, not only at startup."""
    import io

    from agy_remote import hooks as hooks_mod

    captured: dict = {}

    class FakeResponse:
        def read(self):
            return b'{"decision": "ask"}'

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(req, timeout=0):
        captured["version"] = req.headers.get("X-agy-remote-version")
        return FakeResponse()

    monkeypatch.setattr(hooks_mod.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setenv("AGY_REMOTE_URL", "http://127.0.0.1:9999")
    monkeypatch.setattr("sys.stdin", io.StringIO('{"toolCall": {"name": "run_command"}}'))

    import contextlib

    with contextlib.redirect_stdout(io.StringIO()):
        hooks_mod.run_pre_tool_hook()

    assert captured["version"] == hooks_mod.__version__


def test_hook_transmits_tmux_pane_in_payload_and_header(monkeypatch):
    """PreToolUse hook captures $TMUX_PANE and sends it to the server."""
    import contextlib
    import io
    import json

    import agy_remote.hooks as hooks_mod

    captured = {}

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def read(self):
            return b'{"decision": "allow"}'

    def fake_urlopen(req, timeout=0):
        captured["header_pane"] = req.headers.get("X-tmux-pane")
        captured["body"] = json.loads(req.data.decode("utf-8"))
        return FakeResponse()

    monkeypatch.setattr(hooks_mod.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setenv("AGY_REMOTE_URL", "http://127.0.0.1:9999")
    monkeypatch.setenv("TMUX_PANE", "%77")
    monkeypatch.setattr("sys.stdin", io.StringIO('{"toolCall": {"name": "run_command"}}'))

    with contextlib.redirect_stdout(io.StringIO()):
        hooks_mod.run_pre_tool_hook()

    assert captured["header_pane"] == "%77"
    assert captured["body"]["tmux_pane"] == "%77"


def test_run_refuses_to_start_with_a_mixed_hook_build(monkeypatch):
    """The exact failure seen in the wild: `run` upgraded, the hook binary not
    (or the reverse). Coherence matters more than a quick start."""
    import sys

    cli_mod = sys.modules["agy_remote.cli"]
    monkeypatch.setattr(cli_mod, "hook_health", lambda: ("stale", "old binary reports v0.0.0.1"))
    with pytest.raises(SystemExit):
        cli_mod._ensure_hooks_wiring(allow_stale_hook=False)


def test_run_can_be_forced_past_a_mixed_hook_build(monkeypatch):
    import sys

    cli_mod = sys.modules["agy_remote.cli"]
    monkeypatch.setattr(cli_mod, "hook_health", lambda: ("stale", "old binary reports v0.0.0.1"))
    assert cli_mod._ensure_hooks_wiring(allow_stale_hook=True) == "stale"


def test_the_timeouts_are_nested_so_the_server_answers_first():
    """Whoever gives up first decides what the user sees.

    agy kills its hook at 300s. The hook waited 310s for a reply and the server
    waited 300s before denying, so the outermost layer always won: agy killed
    the process at the same instant the server made up its mind, and the user
    got `signal: killed` instead of "approval timed out on mobile remote".

    They have to nest strictly inward: server < hook < agy.
    """
    import inspect

    from agy_remote import hooks as hooks_mod
    from agy_remote.session_manager import SessionManager

    agy_kills_at = 300  # the "timeout" written into hooks.json

    server_waits = inspect.signature(SessionManager.await_approval).parameters["timeout"].default
    hook_waits = hooks_mod.HOOK_RESPONSE_TIMEOUT

    assert server_waits < hook_waits < agy_kills_at, f"server {server_waits}s, hook {hook_waits}s, agy {agy_kills_at}s"
    # And with enough room that a slow reply is reported, not truncated.
    assert hook_waits - server_waits >= 10
    assert agy_kills_at - hook_waits >= 10
