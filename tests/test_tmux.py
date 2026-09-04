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


def test_start_or_attach_creates_session_with_vsusp_disabled(monkeypatch):
    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)

        class Res:
            returncode = 1 if "has-session" in cmd else 0

        return Res()

    monkeypatch.setattr("subprocess.run", fake_run)
    monkeypatch.setattr(TmuxSupervisor, "_attach_session", lambda self: 0)

    sup = TmuxSupervisor(session_name="test-vsusp", cmd=["agy", "--fast"])
    ret = sup.start_or_attach()

    assert ret == 0
    # Verify new-session disables VSUSP and starts agy with SIGTSTP ignored
    new_session_cmd = next(c for c in calls if "new-session" in c)
    assert "stty susp undef 2>/dev/null; trap '' TSTP 2>/dev/null; exec agy --fast" in new_session_cmd
    # Verify focus-events is enabled
    focus_events_cmd = next(c for c in calls if "set-option" in c and "focus-events" in c)
    assert "focus-events" in focus_events_cmd
    assert "on" in focus_events_cmd


def test_start_detached_enables_focus_events(monkeypatch):
    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)

        class Res:
            returncode = 1 if "has-session" in cmd else 0

        return Res()

    monkeypatch.setattr("subprocess.run", fake_run)

    sup = TmuxSupervisor(session_name="test-detached-focus", cmd=["agy"])
    assert sup.start_detached() is True

    focus_events_cmd = next(c for c in calls if "set-option" in c and "focus-events" in c)
    assert "focus-events" in focus_events_cmd
    assert "on" in focus_events_cmd


def test_is_pane_active_and_visible(monkeypatch):
    from agy_remote.tmux_runner import is_pane_active_and_visible

    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)

        class Res:
            returncode = 0
            stdout = "1:1:1\n"

        return Res()

    monkeypatch.setattr("agy_remote.tmux_runner.is_tmux_available", lambda: True)
    monkeypatch.setattr("agy_remote.tmux_runner.subprocess.run", fake_run)

    # Active and attached
    assert is_pane_active_and_visible("%10") is True

    # Inactive window in attached session
    def fake_inactive(cmd, **kwargs):
        class Res:
            returncode = 0
            stdout = "1:0:1\n"

        return Res()

    monkeypatch.setattr("agy_remote.tmux_runner.subprocess.run", fake_inactive)
    assert is_pane_active_and_visible("%10") is False

    # Detached session
    def fake_detached(cmd, **kwargs):
        class Res:
            returncode = 0
            stdout = "0:1:1\n"

        return Res()

    monkeypatch.setattr("agy_remote.tmux_runner.subprocess.run", fake_detached)
    assert is_pane_active_and_visible("%10") is False
