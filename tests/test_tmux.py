"""Unit tests for tmux supervisor module."""

from agy_remote.tmux_runner import TmuxSupervisor, is_tmux_available


def test_tmux_availability_and_config():
    available = is_tmux_available()
    assert isinstance(available, bool)

    sup = TmuxSupervisor(session_name="test-session", cmd=["agy", "--verbose"])
    assert sup.session_name == "test-session"
    assert sup.cmd == ["agy", "--verbose"]


def test_pairing_is_acknowledged_before_the_screen_is_taken_over():
    """The QR must survive until scanned.

    `tmux attach-session` replaces the whole terminal with tmux's screen, so
    everything printed before the attach -- the banner and the QR code --
    vanishes the moment agy appears. PTY mode never had this problem because
    its output scrolls under the QR instead of replacing it.
    """
    from agy_remote.cli import attach_tmux_after_pairing

    calls: list[str] = []

    class FakeSupervisor:
        def start_or_attach(self) -> int:
            calls.append("attach")
            return 0

    exit_code = attach_tmux_after_pairing(FakeSupervisor(), pause=lambda: calls.append("pause"))

    assert calls == ["pause", "attach"], calls
    assert exit_code == 0


def test_wait_for_keypress_or_timeout_zero():
    from agy_remote.cli import wait_for_keypress_or_timeout

    # Timeout 0 returns False immediately without blocking
    assert wait_for_keypress_or_timeout(0) is False


def test_wait_for_keypress_or_timeout_on_input(monkeypatch):
    import io
    import sys

    from agy_remote.cli import wait_for_keypress_or_timeout

    fake_stdin = io.StringIO("x\n")
    fake_stdin.fileno = lambda: 0
    fake_stdin.isatty = lambda: True

    monkeypatch.setattr(sys, "stdin", fake_stdin)
    monkeypatch.setattr("select.select", lambda r, w, x, t: ([sys.stdin], [], []))
    monkeypatch.setattr("termios.tcgetattr", lambda fd: [])
    monkeypatch.setattr("termios.tcsetattr", lambda fd, opt, attr: None)
    monkeypatch.setattr("tty.setcbreak", lambda fd: None)

    assert wait_for_keypress_or_timeout(30) is True


def test_wait_for_keypress_or_timeout_on_expiry(monkeypatch):
    import io
    import sys

    from agy_remote.cli import wait_for_keypress_or_timeout

    fake_stdin = io.StringIO("")
    fake_stdin.fileno = lambda: 0
    fake_stdin.isatty = lambda: True

    monkeypatch.setattr(sys, "stdin", fake_stdin)
    monkeypatch.setattr("select.select", lambda r, w, x, t: ([], [], []))
    monkeypatch.setattr("termios.tcgetattr", lambda fd: [])
    monkeypatch.setattr("termios.tcsetattr", lambda fd, opt, attr: None)
    monkeypatch.setattr("tty.setcbreak", lambda fd: None)

    # 1 second timeout expiring without input
    assert wait_for_keypress_or_timeout(0.01) is False


def test_attach_tmux_with_timeout(monkeypatch):
    import sys

    import agy_remote.cli  # noqa: F401

    cli_mod = sys.modules["agy_remote.cli"]

    calls: list[str] = []

    class FakeSupervisor:
        def start_or_attach(self) -> int:
            calls.append("attach")
            return 0

    monkeypatch.setattr(
        cli_mod,
        "wait_for_keypress_or_timeout",
        lambda timeout_seconds=30, **kw: calls.append(f"timeout_{timeout_seconds}"),
    )
    exit_code = cli_mod.attach_tmux_after_pairing(FakeSupervisor(), timeout=15)

    assert calls == ["timeout_15", "attach"]
    assert exit_code == 0


def test_cli_run_help_has_qr_timeout():
    from click.testing import CliRunner

    from agy_remote.cli import cli

    runner = CliRunner()
    res = runner.invoke(cli, ["run", "--help"])
    assert "--qr-timeout" in res.output
    assert "--pairing-timeout" in res.output
