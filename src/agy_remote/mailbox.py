"""The agent-to-agent mailbox: one append-only JSONL inbox file per session.

Files rather than pipes: a running agent's stdin is its pty, owned by tmux,
so a FIFO cannot be spliced in mid-session -- and a FIFO as a mailbox is worse
than a file anyway (it blocks readers, holds no history, dies with a crash).
A plain JSONL file is the proven `transcript.jsonl` pattern: append-only,
durable, inspectable, no open file descriptors, survives restarts.

Sending is an ordinary bash tool call (`agy-msg to <target> "text"`), so it
passes the existing PreToolUse approval gate: the phone shows the command and
the human permits the channel once. That is the safety spine.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import uuid
from datetime import UTC, datetime
from pathlib import Path

#: One message is one line a server tails and an agent appends with bash. Past
#: this the line stops being a message and becomes a log dump, so the cap is a
#: readability rule as much as a loop-protection one.
MAX_MESSAGE_BYTES = 4096

#: A mailbox target becomes a filename. Alnum, dash and underscore only, so a
#: `../etc/passwd` target cannot reach the filesystem: dots and slashes are
#: simply not in the alphabet.
_TARGET_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


class MailboxError(ValueError):
    """A message that must not reach the inbox, with a reason the sender can read."""


def mailbox_dir() -> Path:
    """Where inboxes live.

    Sits next to the brain directory, under a private `agy-remote` subtree, so
    a relocated setup or a test can point `AGY_REMOTE_MAILBOX_DIR` elsewhere
    without touching the CLI's default.
    """
    env = os.environ.get("AGY_REMOTE_MAILBOX_DIR")
    if env:
        return Path(env).expanduser()
    return Path.home() / ".gemini" / "antigravity-cli" / "agy-remote" / "mailbox"


def validate_target(target: str) -> str:
    """A mailbox filename, not a path.

    The alphabet alone blocks traversal, but a target of all dashes or a bare
    `.` is a file nobody can name, so require at least one alphanumeric too.
    """
    if not target or not _TARGET_RE.match(target) or not any(c.isalnum() for c in target):
        raise MailboxError(f"invalid target {target!r}: session names use letters, digits, '-' and '_' only")
    return target


def sender_id() -> str:
    """Who is sending.

    The server exports `AGY_REMOTE_SESSION_ID` into every agy it spawns, and
    that flows into the tools agy launches, so an agent signs its own message
    without being told who it is. A hand-started agent has no such parent: its
    tmux pane id is the next best answer, and `unknown` is the honest last one.
    """
    return os.environ.get("AGY_REMOTE_SESSION_ID") or os.environ.get("TMUX_PANE") or "unknown"


def send_message(target: str, text: str, from_session: str | None = None) -> Path:
    """Append one message to the target's inbox; return the inbox path.

    Validation happens before anything touches the filesystem: an oversized or
    malformed message must fail the same way whether the inbox exists or not.
    """
    validate_target(target)
    if not isinstance(text, str):
        raise MailboxError("message text must be a string")
    if "\x00" in text:
        raise MailboxError("message contains a NUL byte")

    encoded = text.encode("utf-8")
    if not encoded.strip():
        raise MailboxError("empty message")
    if len(encoded) > MAX_MESSAGE_BYTES:
        raise MailboxError(f"message is {len(encoded)} bytes; the cap is {MAX_MESSAGE_BYTES}")

    directory = mailbox_dir()
    directory.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        directory.chmod(0o700)

    message = {
        "id": uuid.uuid4().hex,
        "from": from_session or sender_id(),
        "to": target,
        "text": text,
        "ts": datetime.now(UTC).isoformat(),
    }
    inbox = directory / f"{target}.jsonl"
    with open(inbox, "a", encoding="utf-8") as f:
        f.write(json.dumps(message, ensure_ascii=False) + "\n")
    with contextlib.suppress(OSError):
        os.chmod(inbox, 0o600)
    return inbox
