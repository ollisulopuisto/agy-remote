"""Working-tree diff for the phone.

opencode's PWA shows the session's git state as a per-file diff viewer with
addition/deletion counts. This module gives agy-remote the same view: run
`git diff HEAD` in the session's workdir (staged and unstaged in one pass)
and split the unified text into per-file chunks the PWA can render with its
own escape-first helpers.
"""

from __future__ import annotations

import re

_DIFF_GIT_RE = re.compile(r"^diff --git (?P<a>.+?) b/(?P<b>.+)$")

#: The most raw diff text one response carries. A session that reformatted a
#: vendored bundle can produce megabytes; the phone wants the shape, not all
#: of it. The cap lands on the joined text, so `truncated` is honest.
MAX_DIFF_BYTES = 256 * 1024


def split_git_diff(diff_text: str) -> list[dict[str, object]]:
    """Split unified `git diff` output into per-file records.

    Each record carries the path (the b/ side), addition and deletion counts
    (hunk bodies only -- headers, `---`/`+++` and context lines count as
    neither), and the raw unified chunk for that one file, ready for a
    client-side renderer. Binary and rename-only entries come through with
    zero counts and whatever git printed for them.
    """
    files: list[dict[str, object]] = []
    current: dict[str, object] | None = None

    for line in diff_text.splitlines():
        match = _DIFF_GIT_RE.match(line)
        if match:
            current = {"path": match.group("b"), "additions": 0, "deletions": 0, "diff": [line]}
            files.append(current)
            continue
        if current is None:
            continue
        lines: list[str] = current["diff"]  # type: ignore[assignment]
        lines.append(line)
        if line.startswith("+") and not line.startswith("+++"):
            current["additions"] = int(current["additions"]) + 1  # type: ignore[arg-type]
        elif line.startswith("-") and not line.startswith("---"):
            current["deletions"] = int(current["deletions"]) + 1  # type: ignore[arg-type]

    out: list[dict[str, object]] = []
    for entry in files:
        chunk_lines: list[str] = entry.pop("diff")  # type: ignore[arg-type]
        entry["diff"] = "\n".join(chunk_lines)
        out.append(entry)
    return out
