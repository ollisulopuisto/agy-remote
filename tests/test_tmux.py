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
