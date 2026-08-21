"""A server-side mirror of the supervised terminal.

The PWA renders `transcript.jsonl`, which holds the conversation and nothing
else. Everything agy draws transiently -- the `/model` picker, the
`/permissions` list, autocomplete, the status bar carrying the execution mode --
exists only on the terminal screen, so from the phone those panels are invisible
and every key aimed at them is fired blind.

The supervisor already has the bytes; it just wrote them to its own stdout and
forgot them. Feeding them through a terminal emulator here, rather than shipping
an emulator to the phone, keeps the PWA free of third-party scripts and turns a
stream of escape sequences into a small grid of text that any client can render.
"""

from __future__ import annotations

import re
import threading
from typing import Any

import pyte

#: How agy spells each execution mode in the status bar. The bar is not an API,
#: so an unrecognised one reports no mode rather than a wrong one.
_MODE_FIELDS: dict[str, str] = {
    "accept-edits": "accept-edits",
    "accept edits": "accept-edits",
    "plan": "plan",
    "plan mode": "plan",
}

#: Status-bar fields are separated by runs of spaces.
_FIELD_SPLIT = re.compile(r"\s{2,}")


class TerminalMirror:
    """An emulated screen fed from the pty, snapshotted as plain text.

    Bytes arrive on the supervisor's thread and snapshots are taken on the
    server's event loop, so every mutation is under a lock.
    """

    def __init__(self, rows: int = 24, cols: int = 80) -> None:
        self._lock = threading.Lock()
        self._screen = pyte.Screen(cols, rows)
        self._stream = pyte.ByteStream(self._screen)
        self._dirty = False

    @property
    def rows(self) -> int:
        return self._screen.lines

    @property
    def cols(self) -> int:
        return self._screen.columns

    def feed(self, data: bytes) -> None:
        """Apply raw pty output to the screen."""
        if not data:
            return
        with self._lock:
            self._stream.feed(data)
            self._dirty = True

    def resize(self, rows: int, cols: int) -> None:
        """Match a pty that changed size; a mismatch wraps every line wrongly."""
        if rows <= 0 or cols <= 0:
            return
        with self._lock:
            self._screen.resize(rows, cols)
            self._dirty = True

    def snapshot(self) -> dict[str, Any]:
        """The current screen as plain text, with the cursor and parsed mode."""
        with self._lock:
            lines = list(self._screen.display)
            cursor = {"x": self._screen.cursor.x, "y": self._screen.cursor.y}
            rows, cols = self._screen.lines, self._screen.columns
            self._dirty = False

        return {
            "lines": lines,
            "cursor": cursor,
            "rows": rows,
            "cols": cols,
            "mode": parse_mode(lines),
        }

    def take_dirty_snapshot(self) -> dict[str, Any] | None:
        """A snapshot only if the screen changed, so a still screen costs nothing."""
        with self._lock:
            if not self._dirty:
                return None
        return self.snapshot()


def parse_mode(lines: list[str]) -> str | None:
    """Read agy's execution mode out of the status bar, or None if absent.

    Shift+Tab cycles `default` -> `accept-edits` -> `plan`, and the status bar
    is the only report of the result, so without this the phone toggles blind.

    Only the bar is read, never the conversation above it. agy announces a mode
    change as a line of text -- "Accept-edits mode: file edits auto-approved" --
    and that line stays on screen after you have cycled past that mode, so
    anything scanning the whole screen keeps reporting a mode you already left.
    `default` prints no field at all and is reported as no mode.
    """
    status_bar = next((line for line in reversed(lines) if line.strip()), "")

    for field in _FIELD_SPLIT.split(status_bar.strip()):
        mode = _MODE_FIELDS.get(field.strip().lower())
        if mode:
            return mode
    return None
