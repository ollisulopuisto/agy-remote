"""A second agy-remote on the same host must fail loudly, not silently.

The web server runs on a daemon thread, and uvicorn answers a failed bind with
`sys.exit(1)` -- which kills only that thread and is swallowed by `threading`.
The launch then carried on to print a QR nobody could use, attach to the *first*
instance's tmux session, and leave two terminals driving one agy.
"""

from __future__ import annotations

import socket
import sys
from pathlib import Path

import pytest
from click.testing import CliRunner

import agy_remote.cli  # noqa: F401
from agy_remote import config as config_mod
from agy_remote.config import RemoteConfig

cli_mod = sys.modules["agy_remote.cli"]


@pytest.fixture
def busy_port() -> int:
    """A port with a live listener on loopback, as a first instance leaves it."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    sock.listen(8)
    try:
        yield sock.getsockname()[1]
    finally:
        sock.close()


def _stub_launch(monkeypatch, tmp_path: Path, port: int) -> dict[str, list]:
    """Neuter everything a launch does after the port check, and record it."""
    seen: dict[str, list] = {"banner": [], "supervisor": [], "app": []}

    def fake_config(**kw) -> RemoteConfig:
        return RemoteConfig(
            brain_dir=tmp_path,
            host="127.0.0.1",
            port=port,
            auth_token="secret123",
            tailscale_ip=None,
            tailscale_bin=None,
            lan_ip="127.0.0.1",
        )

    monkeypatch.setattr(cli_mod, "get_config", fake_config)
    monkeypatch.setattr(cli_mod, "_setup_tls", lambda cfg, tls: None)
    monkeypatch.setattr(cli_mod, "_warn_if_hooks_unwired", lambda: None)
    monkeypatch.setattr(cli_mod, "print_banner", lambda cfg, mode="": seen["banner"].append(mode))
    monkeypatch.setattr(cli_mod, "create_app", lambda cfg: seen["app"].append(cfg) or object())
    monkeypatch.setattr(config_mod, "RUNTIME_STATE_FILE", tmp_path / "runtime.json")

    class FakeSupervisor:
        def __init__(self, *a, **kw) -> None:
            seen["supervisor"].append(kw)

        def start_sync(self) -> int:
            return 0

    monkeypatch.setattr(cli_mod, "PtySupervisor", FakeSupervisor)
    monkeypatch.setattr(cli_mod, "TmuxSupervisor", FakeSupervisor)
    return seen


def test_run_refuses_a_port_already_in_use(monkeypatch, tmp_path: Path, busy_port: int):
    seen = _stub_launch(monkeypatch, tmp_path, busy_port)
    res = CliRunner().invoke(cli_mod.cli, ["run", "--host", "127.0.0.1", "-p", str(busy_port)], input="n\n")

    assert res.exit_code == 2, res.output
    assert "already" in res.output.lower()
    assert str(busy_port) in res.output
    # No QR, no agy: a launch that cannot serve must not start a session.
    assert seen["banner"] == []
    assert seen["supervisor"] == []


def test_serve_refuses_a_port_already_in_use(monkeypatch, tmp_path: Path, busy_port: int):
    seen = _stub_launch(monkeypatch, tmp_path, busy_port)
    res = CliRunner().invoke(cli_mod.cli, ["serve", "--host", "127.0.0.1", "-p", str(busy_port)], input="n\n")

    assert res.exit_code == 2, res.output
    assert seen["banner"] == []


def test_run_accepts_alternate_port_when_prompted(monkeypatch, tmp_path: Path, busy_port: int):
    seen = _stub_launch(monkeypatch, tmp_path, busy_port)
    monkeypatch.setattr(cli_mod, "_serve_in_background_or_exit", lambda cfg, app: None)
    monkeypatch.setattr(cli_mod, "wait_for_keypress_or_timeout", lambda *a, **kw: False)

    # Pressing Enter directly adopts the new port because default is True [Y/n]
    res = CliRunner().invoke(
        cli_mod.cli,
        ["run", "--host", "127.0.0.1", "-p", str(busy_port)],
        input="\n",
    )

    assert res.exit_code == 0, res.output
    assert "starting new instance on port" in res.output.lower()
    assert len(seen["banner"]) == 1


@pytest.mark.filterwarnings("ignore::pytest.PytestUnhandledThreadExceptionWarning")
def test_run_aborts_when_the_server_thread_dies_after_the_check(monkeypatch, tmp_path: Path):
    """The port can be stolen between the check and the bind, and other bind
    failures never reach the check at all. The launch must notice either way."""
    free = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    free.bind(("127.0.0.1", 0))
    port = free.getsockname()[1]
    free.close()

    seen = _stub_launch(monkeypatch, tmp_path, port)

    class DyingServer:
        started = False

        def __init__(self, config) -> None:
            pass

        def run(self) -> None:
            raise SystemExit(1)  # what uvicorn does on a failed bind

    monkeypatch.setattr(cli_mod.uvicorn, "Server", DyingServer)
    monkeypatch.setattr(cli_mod.uvicorn, "Config", lambda *a, **kw: None)

    res = CliRunner().invoke(cli_mod.cli, ["run", "--host", "127.0.0.1", "-p", str(port)])

    assert res.exit_code == 2, res.output
    assert "server" in res.output.lower()
    assert seen["supervisor"] == []


def test_tmux_session_name_is_per_port(monkeypatch, tmp_path: Path):
    """Two instances on different ports must not share one tmux session."""
    from agy_remote.tmux_runner import session_name_for_port

    assert session_name_for_port(8765) == "agy-remote"
    assert session_name_for_port(8766) == "agy-remote-8766"


def test_runtime_state_is_not_stolen_from_a_live_server(monkeypatch, tmp_path: Path):
    """The PreToolUse hook reads one file. A second instance on another port
    overwrote it, silently redirecting the first server's approvals."""
    import json
    import os

    state_file = tmp_path / "runtime.json"
    monkeypatch.setattr(config_mod, "RUNTIME_STATE_FILE", state_file)
    state_file.write_text(json.dumps({"auth_token": "first", "port": 8765, "pid": 424242}))
    monkeypatch.setattr(config_mod, "_pid_alive", lambda pid: pid == 424242)

    cfg = RemoteConfig(
        brain_dir=tmp_path,
        port=8766,
        auth_token="second",
        tailscale_ip=None,
        tailscale_bin=None,
        lan_ip="127.0.0.1",
    )
    assert config_mod.write_runtime_state(cfg) is None
    assert json.loads(state_file.read_text())["auth_token"] == "first"

    # A dead owner is just a stale file: the new server takes it over.
    monkeypatch.setattr(config_mod, "_pid_alive", lambda pid: False)
    assert config_mod.write_runtime_state(cfg) == state_file
    written = json.loads(state_file.read_text())
    assert written["auth_token"] == "second"
    assert written["pid"] == os.getpid()


def test_a_foreign_listener_is_not_reported_as_an_agy_remote(monkeypatch, tmp_path: Path, busy_port: int):
    """Any process can hold the port; only a live agy-remote may be named one.

    A stray `python -m http.server 8765` produced "An agy-remote is already
    running on this host" and sent the user to `tmux attach -t agy-remote`,
    which does not exist -- so the one useful fact, that something *else* owns
    the port, was the one thing the message did not say.
    """
    seen = _stub_launch(monkeypatch, tmp_path, busy_port)  # no runtime state -> no live owner
    res = CliRunner().invoke(cli_mod.cli, ["run", "--host", "127.0.0.1", "-p", str(busy_port)], input="n\n")

    assert res.exit_code == 2, res.output
    out = res.output.lower()
    assert "already in use" in out
    assert "an agy-remote is already running" not in out
    assert "tmux attach" not in out
    assert "agy-remote qr" not in out
    # Name the way to find the actual holder.
    assert "lsof" in out
    assert seen["banner"] == []
    assert seen["supervisor"] == []


def test_a_live_agy_remote_on_the_port_still_gets_its_guidance(monkeypatch, tmp_path: Path, busy_port: int):
    """The check must not throw away the message that is right when it is right."""
    import json

    seen = _stub_launch(monkeypatch, tmp_path, busy_port)
    state_file = tmp_path / "runtime.json"
    state_file.write_text(json.dumps({"auth_token": "first", "port": busy_port, "pid": 424242}))
    monkeypatch.setattr(config_mod, "_pid_alive", lambda pid: pid == 424242)

    res = CliRunner().invoke(cli_mod.cli, ["run", "--host", "127.0.0.1", "-p", str(busy_port)], input="n\n")

    assert res.exit_code == 2, res.output
    out = res.output.lower()
    assert "an agy-remote is already running" in out
    assert "tmux attach" in out
    assert "424242" in out
    assert seen["supervisor"] == []
