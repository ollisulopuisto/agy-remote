"""The named keys the phone is allowed to press in the supervised terminal.

agy's TUI is driven by more than text: Shift+Tab cycles the execution mode
(`default` -> `accept-edits` -> `plan`), Esc closes a panel or halts a stream,
and the arrow keys drive every selection list `/model`, `/permissions` and
`/resume` put on screen. None of that can be expressed as a line of text
followed by Enter, which is all `inject_input` can send.

Only named keys are accepted, never raw bytes off the network: the pty is
wired to a live agent session, so arbitrary control sequences would be
arbitrary code execution wearing a hat.
"""

from __future__ import annotations

#: Name -> the bytes a real terminal sends for that key.
KEY_SEQUENCES: dict[str, bytes] = {
    "enter": b"\r",
    "escape": b"\x1b",
    "tab": b"\t",
    "shift_tab": b"\x1b[Z",  # CSI Z, the standard "back tab"
    "up": b"\x1b[A",
    "down": b"\x1b[B",
    "right": b"\x1b[C",
    "left": b"\x1b[D",
    "page_up": b"\x1b[5~",
    "page_down": b"\x1b[6~",
    "backspace": b"\x7f",
    "interrupt": b"\x03",  # Ctrl+C
    "ctrl_c": b"\x03",  # Ctrl+C
    "ctrl_z": b"\x1a",  # Ctrl+Z
    "suspend": b"\x1a",  # Ctrl+Z
    "yes": b"y",
    "no": b"n",
}

#: The same keys, spelled the way `tmux send-keys` spells them.
TMUX_KEY_NAMES: dict[str, str] = {
    "enter": "Enter",
    "escape": "Escape",
    "tab": "Tab",
    "shift_tab": "BTab",
    "up": "Up",
    "down": "Down",
    "right": "Right",
    "left": "Left",
    "page_up": "PageUp",
    "page_down": "PageDown",
    "backspace": "BSpace",
    "interrupt": "C-c",
    "ctrl_c": "C-c",
    "ctrl_z": "C-z",
    "suspend": "C-z",
    "yes": "y",
    "no": "n",
}


def is_known_key(key: str) -> bool:
    """Whether `key` is a key the phone may press."""
    return key in KEY_SEQUENCES
