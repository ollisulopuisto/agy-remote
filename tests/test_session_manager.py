"""Unit tests for session manager and transcript reader."""

import asyncio
import json
import os
import time
from pathlib import Path

import pytest

from agy_remote.config import RemoteConfig
from agy_remote.models import ApprovalResponseRequest
from agy_remote.session_manager import SessionManager


@pytest.mark.asyncio
async def test_session_manager_list_and_read(tmp_path: Path):
    # Setup mock conversation folder in tmp_path
    conv_id = "test-conv-uuid-123"
    conv_dir = tmp_path / conv_id / ".system_generated" / "logs"
    conv_dir.mkdir(parents=True)
    log_file = conv_dir / "transcript.jsonl"

    sample_lines = [
        {
            "step_index": 0,
            "type": "USER_INPUT",
            "source": "USER_INPUT",
            "content": "Hello agy",
        },
        {
            "step_index": 1,
            "type": "PLANNER_RESPONSE",
            "source": "MODEL",
            "thinking": "Thinking about response",
            "content": "Hello user! How can I help?",
            "tool_calls": [{"name": "run_command", "args": {"CommandLine": "ls"}}],
        },
    ]

    with open(log_file, "w", encoding="utf-8") as f:
        for line in sample_lines:
            f.write(json.dumps(line) + "\n")

    cfg = RemoteConfig(brain_dir=tmp_path, auth_token="token")
    mgr = SessionManager(cfg)

    convs = mgr.list_conversations()
    assert len(convs) == 1
    assert convs[0].id == conv_id
    assert convs[0].step_count == 2
    assert convs[0].title == "Hello agy"

    # Switch conversation and read
    await mgr.switch_conversation(conv_id)
    assert mgr.active_conversation_id == conv_id
    assert len(mgr.active_steps) == 2
    assert mgr.active_steps[0].content == "Hello agy"
    assert mgr.active_steps[1].thinking == "Thinking about response"


@pytest.mark.asyncio
async def test_approval_flow(tmp_path: Path):
    cfg = RemoteConfig(brain_dir=tmp_path, auth_token="token")
    mgr = SessionManager(cfg)
    mgr.active_conversation_id = "conv-1"

    # Simulate approval request
    approval_task = asyncio.create_task(
        mgr.request_approval(
            approval_id="app-1",
            conversation_id="conv-1",
            tool_name="run_command",
            args={"CommandLine": "rm -rf /tmp/test"},
        )
    )

    # Let the event loop cycle
    await asyncio.sleep(0.01)

    pending = mgr.get_active_pending_approvals()
    assert len(pending) == 1
    assert pending[0]["id"] == "app-1"

    # Resolve approval from mobile
    resolved = await mgr.resolve_approval(
        "app-1",
        ApprovalResponseRequest(decision="allow"),
    )
    assert resolved is True

    result = await approval_task
    assert result["decision"] == "allow"


# ---------------------------------------------------------------------------
# The watcher loop re-parsed every transcript 3x/second.
# ---------------------------------------------------------------------------


def _make_conv(brain: Path, name: str, lines: int = 3) -> Path:
    d = brain / name / ".system_generated" / "logs"
    d.mkdir(parents=True)
    log = d / "transcript.jsonl"
    log.write_text(
        "".join(json.dumps({"step_index": i, "type": "USER_INPUT", "content": f"m{i}"}) + "\n" for i in range(lines))
    )
    return log


def test_latest_conversation_lookup_parses_nothing(tmp_path: Path):
    """Finding the newest conversation only needs mtimes, not file contents.

    _watch_loop called this every 0.3s; parsing every transcript to answer it
    kept a core busy continuously on a real brain directory.
    """
    for name in ("conv-a", "conv-b", "conv-c"):
        _make_conv(tmp_path, name)

    cfg = RemoteConfig(brain_dir=tmp_path, e2ee_enabled=False)
    mgr = SessionManager(cfg)

    assert mgr.get_latest_conversation_id() is not None
    assert mgr.parse_count == 0, f"parsed {mgr.parse_count} transcripts just to find the newest"


def test_unchanged_transcripts_are_not_reparsed(tmp_path: Path):
    """A second listing must reuse cached summaries for untouched files."""
    for name in ("conv-a", "conv-b"):
        _make_conv(tmp_path, name)

    cfg = RemoteConfig(brain_dir=tmp_path, e2ee_enabled=False)
    mgr = SessionManager(cfg)

    mgr.list_conversations()
    first = mgr.parse_count
    assert first == 2

    mgr.list_conversations()
    assert mgr.parse_count == first, "re-parsed unchanged transcripts"


def test_modified_transcript_is_reparsed(tmp_path: Path):
    """A changed file must invalidate its cache entry."""
    log = _make_conv(tmp_path, "conv-a")
    cfg = RemoteConfig(brain_dir=tmp_path, e2ee_enabled=False)
    mgr = SessionManager(cfg)

    mgr.list_conversations()
    before = mgr.parse_count

    with open(log, "a", encoding="utf-8") as f:
        f.write(json.dumps({"step_index": 9, "type": "USER_INPUT", "content": "new"}) + "\n")
    os.utime(log, (time.time() + 5, time.time() + 5))

    summaries = mgr.list_conversations()
    assert mgr.parse_count == before + 1
    assert summaries[0].step_count == 4
