"""Unit tests for the PTY supervisor module."""

import os

from agy_remote.pty_runner import PtySupervisor, get_pty_supervisor, set_pty_supervisor


def _capture_injection(text: str) -> bytes:
    """Inject `text` into a fake master fd and return the bytes written to it."""
    read_fd, write_fd = os.pipe()
    sup = PtySupervisor()
    sup.master_fd = write_fd
    try:
        sup.inject_input(text)
        return os.read(read_fd, 4096)
    finally:
        os.close(read_fd)
        os.close(write_fd)


def test_inject_input_submits_with_carriage_return():
    """A prompt from the phone must arrive as a real Enter keypress.

    Terminal UIs run the tty in raw mode, where Enter is CR (0x0D). Sending LF
    (0x0A) is Ctrl-J, which most TUIs treat as "insert a line break" -- the
    prompt lands in agy's input box and just sits there, unsent.
    """
    assert _capture_injection("hello") == b"hello\r"


def test_inject_input_does_not_double_up_existing_newline():
    assert _capture_injection("hello\n") == b"hello\r"
    assert _capture_injection("hello\r\n") == b"hello\r"


def test_inject_input_never_emits_line_feed():
    assert b"\n" not in _capture_injection("hello")


def test_inject_input_without_pty_is_a_noop():
    PtySupervisor().inject_input("hello")


def test_supervisor_registry_roundtrip():
    sup = PtySupervisor(cmd=["agy", "--verbose"])
    set_pty_supervisor(sup)
    assert get_pty_supervisor() is sup
    assert sup.cmd == ["agy", "--verbose"]


def test_output_listeners_receive_what_the_cli_writes():
    """The mirror can only exist if the supervisor hands out its pty output."""
    sup = PtySupervisor()
    seen: list[bytes] = []
    sup.add_output_listener(seen.append)

    sup._emit_output(b"\x1b[2Jchoose a model")

    assert seen == [b"\x1b[2Jchoose a model"]


def test_a_broken_listener_does_not_kill_the_session():
    """The listener runs on the pty read loop; a raise there would drop agy."""
    sup = PtySupervisor()
    survivor: list[bytes] = []

    def explode(_data: bytes) -> None:
        raise RuntimeError("listener is broken")

    sup.add_output_listener(explode)
    sup.add_output_listener(survivor.append)

    sup._emit_output(b"still fine")

    assert survivor == [b"still fine"]


def test_window_size_is_recorded_for_the_mirror():
    """A mirror sized differently from the pty wraps every line wrongly."""
    sup = PtySupervisor()
    sup.set_window_size(30, 100)
    assert (sup.rows, sup.cols) == (30, 100)


def test_the_child_gets_a_controlling_terminal():
    """Without one, Ctrl+C and Ctrl+Z do nothing at all.

    The interrupt and suspend characters are not forwarded as bytes: the pty's
    line discipline turns them into SIGINT and SIGTSTP and sends them to the
    foreground process group *of that terminal*. A child that called setsid()
    without claiming the pty as its controlling terminal has no such group, so
    the keystrokes are swallowed and the session cannot be interrupted.
    """
    import pty as pty_mod

    master_fd, slave_fd = pty_mod.openpty()
    read_fd, write_fd = os.pipe()

    pid = os.fork()
    if pid == 0:  # child
        try:
            os.close(master_fd)
            os.close(read_fd)
            PtySupervisor()._become_session_leader(slave_fd)
            # A controlling terminal is exactly what /dev/tty resolves to.
            fd = os.open("/dev/tty", os.O_RDWR)
            has_foreground_group = os.tcgetpgrp(fd) == os.getpgrp()
            os.write(write_fd, b"ok" if has_foreground_group else b"no-foreground-group")
        except Exception as e:  # noqa: BLE001 - reported to the parent, not raised
            os.write(write_fd, f"failed: {e}".encode())
        finally:
            os._exit(0)

    os.close(write_fd)
    os.close(slave_fd)
    try:
        result = os.read(read_fd, 256)
        os.waitpid(pid, 0)
    finally:
        os.close(read_fd)
        os.close(master_fd)

    assert result == b"ok", result.decode()


def test_ctrl_z_does_not_suspend_child_session():
    """Ctrl-Z should deliver byte input rather than generating SIGTSTP.

    Without job control, SIGTSTP freezes agy with no shell to run `fg`.
    """
    import pty as pty_mod
    import time

    master_fd, slave_fd = pty_mod.openpty()
    read_fd, write_fd = os.pipe()

    pid = os.fork()
    if pid == 0:  # child
        try:
            os.close(master_fd)
            os.close(read_fd)
            PtySupervisor()._become_session_leader(slave_fd)
            os.dup2(slave_fd, 0)
            os.dup2(slave_fd, 1)
            os.dup2(slave_fd, 2)
            os.close(slave_fd)
            # Read bytes from stdin (canonical mode waits for newline)
            data = os.read(0, 10)
            os.write(write_fd, b"received:" + data)
        except Exception as e:  # noqa: BLE001
            os.write(write_fd, f"failed: {e}".encode())
        finally:
            os._exit(0)

    os.close(write_fd)
    os.close(slave_fd)
    try:
        time.sleep(0.1)
        # Send Ctrl-Z (\x1a) followed by \n so readline completes in canonical mode
        os.write(master_fd, b"\x1a\n")
        result = os.read(read_fd, 256)
        wpid, _ = os.waitpid(pid, 0)
        assert wpid == pid
    finally:
        os.close(read_fd)
        os.close(master_fd)

    assert result == b"received:\x1a\n", result.decode()


def test_pty_mode_respects_qr_timeout(monkeypatch):
    import sys
    from unittest.mock import MagicMock

    from click.testing import CliRunner

    import agy_remote.cli as _  # noqa: F401

    cli_mod = sys.modules["agy_remote.cli"]
    calls: list[str] = []

    monkeypatch.setattr(
        cli_mod,
        "wait_for_keypress_or_timeout",
        lambda timeout_seconds=30, **kw: calls.append(f"timeout_{timeout_seconds}"),
    )
    mock_sup = MagicMock()
    mock_sup.start_sync.return_value = 0
    monkeypatch.setattr(cli_mod, "set_pty_supervisor", lambda sup: None)
    monkeypatch.setattr(cli_mod, "PtySupervisor", lambda *a, **kw: mock_sup)
    monkeypatch.setattr(cli_mod, "_serve_in_background_or_exit", lambda *a, **kw: None)
    monkeypatch.setattr(cli_mod, "_preflight_port_or_exit", lambda *a, **kw: None)
    monkeypatch.setattr(cli_mod, "_setup_tls", lambda *a, **kw: None)
    monkeypatch.setattr(cli_mod, "_guard_or_exit", lambda *a, **kw: None)

    runner = CliRunner()
    res = runner.invoke(cli_mod.cli, ["run", "--qr-timeout", "20"])
    assert res.exit_code == 0, res.output
    assert "timeout_20.0" in calls
    mock_sup.start_sync.assert_called_once()

