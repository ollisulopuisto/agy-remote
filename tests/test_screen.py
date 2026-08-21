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


# agy's real status bar, as it appears at the bottom of the pty: a left hint,
# then right-aligned fields separated by runs of spaces.
STATUS_BAR = "? for shortcuts{gap}{mode}Gemini 3.7 Flash   medium   1 task(s)  /tasks"


def _bar(mode: str = "") -> bytes:
    field = f"{mode}   " if mode else ""
    return STATUS_BAR.format(gap=" " * 20, mode=field).encode()


def test_execution_mode_is_read_off_the_status_bar():
    """Shift+Tab is otherwise fired blind: nothing else reports the result."""
    mirror = TerminalMirror(rows=3, cols=120)
    mirror.feed(b"\x1b[3;1H" + _bar("accept-edits"))
    assert mirror.snapshot()["mode"] == "accept-edits"

    mirror.feed(b"\x1b[2J\x1b[3;1H" + _bar("plan"))
    assert mirror.snapshot()["mode"] == "plan"


def test_default_mode_shows_no_field_and_is_reported_as_none():
    mirror = TerminalMirror(rows=3, cols=120)
    mirror.feed(b"\x1b[3;1H" + _bar())
    assert mirror.snapshot()["mode"] is None


def test_scrollback_does_not_override_the_status_bar():
    """agy announces a mode change in the conversation, and that line stays put.

    Reading the mode from anywhere but the bar means the announcement of a mode
    you have since left keeps winning.
    """
    mirror = TerminalMirror(rows=3, cols=120)
    mirror.feed(b"\x1b[1;1HAccept-edits mode: file edits auto-approved (shift+tab to cycle)\r\n")
    mirror.feed(b"\x1b[3;1H" + _bar("plan"))

    assert mirror.snapshot()["mode"] == "plan"


def test_prose_mentioning_a_mode_is_not_a_mode():
    mirror = TerminalMirror(rows=3, cols=120)
    mirror.feed(b"\x1b[3;1Hlet me plan the accept-edits rollout before we continue")
    assert mirror.snapshot()["mode"] is None


def test_resize_keeps_the_mirror_matching_the_pty():
    mirror = TerminalMirror(rows=4, cols=20)
    mirror.resize(10, 40)

    snapshot = mirror.snapshot()
    assert len(snapshot["lines"]) == 10
    assert snapshot["rows"] == 10
    assert snapshot["cols"] == 40


def test_the_mode_is_read_when_the_bar_packs_it_next_to_the_model():
    """Real status bars, captured from agy 2.1.238 while cycling Shift+Tab.

    The bar is not an API and its shape moved: the mode used to sit in its own
    space-separated column, and now shares one with the model, separated by a
    middle dot. Splitting on spaces alone yields the field
    "accept-edits · Gemini 3.7 Flash · medium", which matches nothing -- so the
    phone's badge stayed empty through every press.
    """
    from agy_remote.screen import parse_mode

    assert parse_mode(["? for shortcuts                         accept-edits · Gemini 3.7 Flash · medium"]) == (
        "accept-edits"
    )
    assert parse_mode(["? for shortcuts                   \t        plan · Gemini 3.7 Flash · medium"]) == "plan"
    # Default mode prints no field at all, and must not be guessed from the rest.
    assert parse_mode(["? for shortcuts                   \t               Gemini 3.7 Flash · medium"]) is None
