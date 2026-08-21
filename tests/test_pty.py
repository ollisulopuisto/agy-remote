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
