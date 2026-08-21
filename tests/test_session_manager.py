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


# ---------------------------------------------------------------------------
# Starting a new agy session must move the phone's view to it. Without this the
# phone silently keeps rendering a hours-old conversation while the desktop
# works in the new one.
# ---------------------------------------------------------------------------


def _write_conversation(brain_dir: Path, conv_id: str, first_message: str, mtime: float) -> Path:
    log = brain_dir / conv_id / ".system_generated" / "logs" / "transcript.jsonl"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text(
        json.dumps({"step_index": 0, "type": "USER_INPUT", "source": "USER_INPUT", "content": first_message}) + "\n",
        encoding="utf-8",
    )
    os.utime(log, (mtime, mtime))
    return log


@pytest.mark.asyncio
async def test_watcher_follows_a_newly_started_conversation(tmp_path: Path):
    now = time.time()
    _write_conversation(tmp_path, "old-conv", "yesterday's work", now - 3600)

    cfg = RemoteConfig(brain_dir=tmp_path, auth_token="token")
    mgr = SessionManager(cfg)
    await mgr.switch_conversation("old-conv")
    assert mgr.active_conversation_id == "old-conv"

    # agy is launched and writes a brand new conversation.
    _write_conversation(tmp_path, "new-conv", "today's work", now)

    await mgr.follow_latest_conversation()
    assert mgr.active_conversation_id == "new-conv"


@pytest.mark.asyncio
async def test_a_conversation_the_user_picked_is_not_yanked_away(tmp_path: Path):
    """Browsing an old session on the phone must survive a new session starting."""
    now = time.time()
    _write_conversation(tmp_path, "old-conv", "yesterday's work", now - 3600)
    _write_conversation(tmp_path, "current-conv", "today's work", now)

    cfg = RemoteConfig(brain_dir=tmp_path, auth_token="token")
    mgr = SessionManager(cfg)
    await mgr.switch_conversation("old-conv", pin=True)

    _write_conversation(tmp_path, "newest-conv", "even newer", now + 60)
    await mgr.follow_latest_conversation()

    assert mgr.active_conversation_id == "old-conv"


@pytest.mark.asyncio
async def test_selecting_the_newest_conversation_resumes_following(tmp_path: Path):
    now = time.time()
    _write_conversation(tmp_path, "old-conv", "yesterday's work", now - 3600)
    _write_conversation(tmp_path, "current-conv", "today's work", now)

    cfg = RemoteConfig(brain_dir=tmp_path, auth_token="token")
    mgr = SessionManager(cfg)
    await mgr.switch_conversation("old-conv", pin=True)
    await mgr.switch_conversation("current-conv", pin=True)

    _write_conversation(tmp_path, "newest-conv", "even newer", now + 60)
    await mgr.follow_latest_conversation()

    assert mgr.active_conversation_id == "newest-conv"
