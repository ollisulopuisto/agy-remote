"""Unit tests for tmux supervisor module."""

from agy_remote.tmux_runner import TmuxSupervisor, is_tmux_available


def test_tmux_availability_and_config():
    available = is_tmux_available()
    assert isinstance(available, bool)

    sup = TmuxSupervisor(session_name="test-session", cmd=["agy", "--verbose"])
    assert sup.session_name == "test-session"
    assert sup.cmd == ["agy", "--verbose"]
