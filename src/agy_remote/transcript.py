"""Turning agy's raw transcript records into something worth reading on a phone.

agy writes for its own consumption: a prompt is wrapped in `<USER_REQUEST>`
alongside metadata blocks it feeds back to the model, and every tool argument is
stored as a JSON string inside a JSON object. Rendered literally, a one-line
question fills the whole phone screen with plumbing and `run_command` shows
`"\\"du -hd 1\\""`.
"""

from __future__ import annotations

import json
import re
from typing import Any

#: The block holding what the user actually typed.
_REQUEST_BLOCK = re.compile(r"<USER_REQUEST>(.*?)</USER_REQUEST>", re.DOTALL)

#: A leftover opening tag, when agy truncated the record mid-envelope.
_OPEN_REQUEST = re.compile(r"</?USER_REQUEST>")

#: Blocks agy adds for the model's benefit: the local time, a settings change.
#: Matched by shape (an all-caps tag) rather than by name, so a new one added
#: upstream does not start leaking onto the screen.
_PLUMBING_BLOCK = re.compile(r"<([A-Z][A-Z0-9_]{2,})>.*?</\1>\s*", re.DOTALL)


def clean_user_content(content: str | None) -> str:
    """Return what the user actually said, without agy's envelope.

    Prose is left exactly as written -- only all-caps tag blocks are touched, so
    `a < b` and `<div>` survive.
    """
    if not content:
        return ""

    match = _REQUEST_BLOCK.search(content)
    if match:
        return match.group(1).strip()

    stripped = _PLUMBING_BLOCK.sub("", content)
    return _OPEN_REQUEST.sub("", stripped).strip()


def normalize_tool_calls(tool_calls: list[Any]) -> list[Any]:
    """Decode arguments agy stored as JSON strings, leaving everything else alone.

    Only string results are unwrapped: `"5000"` is an argument the tool receives
    as text, and turning it into a number would be inventing a type agy never
    used. Question arguments are structured payloads and are decoded into
    real lists/dicts so the client can render questions directly.
    """
    normalized = []
    for call in tool_calls:
        if not isinstance(call, dict) or not isinstance(call.get("args"), dict):
            normalized.append(call)
            continue

        call_name = call.get("name") or (
            call.get("function", {}) if isinstance(call.get("function"), dict) else {}
        ).get("name")
        new_args = {}
        for key, value in call["args"].items():
            if key == "questions" or call_name == "ask_question":
                new_args[key] = _decode_structured(value)
            else:
                new_args[key] = _decode(value)

        normalized.append({**call, "args": new_args})
    return normalized


def _decode_structured(value: Any) -> Any:
    if not isinstance(value, str):
        return value

    cur: Any = value
    while isinstance(cur, str):
        try:
            decoded = json.loads(cur)
        except (ValueError, TypeError):
            break
        cur = decoded
    return cur


def _decode(value: Any) -> Any:
    if not isinstance(value, str):
        return value

    try:
        decoded = json.loads(value)
    except (ValueError, TypeError):
        return value

    return decoded if isinstance(decoded, str) else value


#: Steps agy writes for the model's benefit, not the user's. Every session opens
#: with a CHECKPOINT announcing that "earlier parts of this conversation have
#: been truncated" -- boilerplate whose request list holds only this session's
#: own first prompt, and which instructs the model not to acknowledge it. Shown
#: as conversation, it made every fresh session look like a continuation.
_SCAFFOLDING_TYPES = frozenset({"CHECKPOINT", "SYSTEM_MESSAGE"})


def is_scaffolding(step_type: str | None, source: str | None = None) -> bool:
    """Whether a step is agy talking to itself rather than to the user."""
    return (step_type or "") in _SCAFFOLDING_TYPES
