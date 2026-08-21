"""Unit tests for the named-key channel."""

import os

from agy_remote.keys import KEY_SEQUENCES, TMUX_KEY_NAMES, is_known_key
from agy_remote.pty_runner import PtySupervisor


def _press(key: str) -> bytes:
    read_fd, write_fd = os.pipe()
    sup = PtySupervisor()
    sup.master_fd = write_fd
    try:
        sup.send_key(key)
        os.write(write_fd, b"<end>")
        return os.read(read_fd, 4096).replace(b"<end>", b"")
    finally:
        os.close(read_fd)
        os.close(write_fd)


def test_shift_tab_cycles_the_agent_mode():
    """Shift+Tab is CSI Z; agy cycles default -> accept-edits -> plan on it."""
    assert _press("shift_tab") == b"\x1b[Z"


def test_arrow_keys_and_escape_drive_agy_panels():
    assert _press("up") == b"\x1b[A"
    assert _press("down") == b"\x1b[B"
    assert _press("escape") == b"\x1b"


def test_unknown_keys_are_refused_and_send_nothing():
    """The pty runs a live agent; only names from the allowlist reach it."""
    assert _press("rm -rf /") == b""
    assert _press("\x1b]0;pwn\x07") == b""
    assert is_known_key("shift_tab") is True
    assert is_known_key("meta_x") is False


def test_send_key_reports_whether_it_pressed_anything():
    sup = PtySupervisor()
    assert sup.send_key("shift_tab") is False  # no pty attached
    read_fd, write_fd = os.pipe()
    try:
        sup.master_fd = write_fd
        assert sup.send_key("shift_tab") is True
        assert sup.send_key("nope") is False
    finally:
        os.close(read_fd)
        os.close(write_fd)


def test_both_runners_speak_the_same_key_names():
    """A key the PTY path accepts must also work under --tmux, and vice versa."""
    assert set(KEY_SEQUENCES) == set(TMUX_KEY_NAMES)
