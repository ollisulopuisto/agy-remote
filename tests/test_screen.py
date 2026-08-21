"""Unit tests for the server-side terminal mirror."""

from agy_remote.screen import TerminalMirror


def test_plain_output_lands_on_the_screen():
    mirror = TerminalMirror(rows=4, cols=20)
    mirror.feed(b"hello\r\nworld\r\n")

    lines = mirror.snapshot()["lines"]
    assert lines[0].strip() == "hello"
    assert lines[1].strip() == "world"


def test_cursor_addressing_is_resolved_not_shown():
    """A TUI repaints by moving the cursor; the phone must see the result."""
    mirror = TerminalMirror(rows=4, cols=20)
    mirror.feed(b"first\r\nsecond\r\n")
    mirror.feed(b"\x1b[1;1H")  # home
    mirror.feed(b"FIRST")

    lines = mirror.snapshot()["lines"]
    assert lines[0].strip() == "FIRST"
    assert "\x1b" not in "".join(lines)


def test_clear_screen_clears():
    mirror = TerminalMirror(rows=4, cols=20)
    mirror.feed(b"noise everywhere\r\n")
    mirror.feed(b"\x1b[2J\x1b[H")

    assert "".join(mirror.snapshot()["lines"]).strip() == ""


def test_snapshot_reports_the_cursor():
    mirror = TerminalMirror(rows=4, cols=20)
    mirror.feed(b"abc")

    cursor = mirror.snapshot()["cursor"]
    assert cursor == {"x": 3, "y": 0}


def test_take_dirty_snapshot_only_returns_changes():
    """The mirror is polled; unchanged screens must not be broadcast."""
    mirror = TerminalMirror(rows=4, cols=20)
    assert mirror.take_dirty_snapshot() is None

    mirror.feed(b"something")
    assert mirror.take_dirty_snapshot() is not None
    assert mirror.take_dirty_snapshot() is None


def test_execution_mode_is_read_off_the_status_bar():
    """Shift+Tab is otherwise fired blind: nothing reports the resulting mode."""
    mirror = TerminalMirror(rows=4, cols=40)
    mirror.feed(b"? for shortcuts        accept-edits on\r\n")
    assert mirror.snapshot()["mode"] == "accept-edits"

    mirror.feed(b"\x1b[2J\x1b[H? for shortcuts        plan mode on\r\n")
    assert mirror.snapshot()["mode"] == "plan"

    mirror.feed(b"\x1b[2J\x1b[H? for shortcuts\r\n")
    assert mirror.snapshot()["mode"] is None


def test_resize_keeps_the_mirror_matching_the_pty():
    mirror = TerminalMirror(rows=4, cols=20)
    mirror.resize(10, 40)

    snapshot = mirror.snapshot()
    assert len(snapshot["lines"]) == 10
    assert snapshot["rows"] == 10
    assert snapshot["cols"] == 40
