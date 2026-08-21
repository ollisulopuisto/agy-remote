"""Unit tests for session manager and transcript reader."""

import asyncio
import json
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
