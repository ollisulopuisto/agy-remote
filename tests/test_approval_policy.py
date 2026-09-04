"""Tests for granular approval policies (ask_all, auto_reads, auto_all)."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agy_remote.config import RemoteConfig
from agy_remote.hooks import is_read_only_tool
from agy_remote.server import create_app
from agy_remote.session_manager import SessionManager


def test_is_read_only_tool():
    assert is_read_only_tool("view_file") is True
    assert is_read_only_tool("grep_search") is True
    assert is_read_only_tool("find_by_name") is True
    assert is_read_only_tool("list_dir") is True
    assert is_read_only_tool("read_url_content") is True
    assert is_read_only_tool("read_browser_page") is True
    assert is_read_only_tool("search_web") is True

    # Mutating tools must be False
    assert is_read_only_tool("run_command") is False
    assert is_read_only_tool("write_to_file") is False
    assert is_read_only_tool("replace_file_content") is False

    # Dialogue gate is never read-only
    assert is_read_only_tool("ask_question") is False

    # Action-dependent tools
    assert is_read_only_tool("manage_task", {"Action": "list"}) is True
    assert is_read_only_tool("manage_task", {"Action": "status"}) is True
    assert is_read_only_tool("manage_task", {"Action": "kill"}) is False
    assert is_read_only_tool("manage_task", {"Action": "send_input"}) is False


@pytest.mark.asyncio
async def test_session_manager_approval_policy_auto_all(tmp_path: Path):
    cfg = RemoteConfig(brain_dir=tmp_path, auth_token="tok", e2ee_enabled=False)
    mgr = SessionManager(cfg)
    mgr.set_approval_policy("auto_all")
    assert mgr.get_approval_policy() == "auto_all"

    # auto_all allows mutations without needing clients or waiting
    res = await mgr.request_approval("ap-1", "conv-1", "run_command", {"CommandLine": "rm -rf /"})
    assert res["decision"] == "allow"
    assert "auto_all" in res["reason"]

    # auto_all never auto-answers ask_question (dialogue gate)
    # With no clients connected, it falls back to local ask
    res_q = await mgr.request_approval("ap-2", "conv-1", "ask_question", {})
    assert res_q["decision"] == "ask"


@pytest.mark.asyncio
async def test_session_manager_approval_policy_auto_reads(tmp_path: Path):
    cfg = RemoteConfig(brain_dir=tmp_path, auth_token="tok", e2ee_enabled=False)
    mgr = SessionManager(cfg)
    mgr.set_approval_policy("auto_reads")

    # Read-only tool is auto-allowed
    res_read = await mgr.request_approval("ap-1", "conv-1", "view_file", {"AbsolutePath": "/foo"})
    assert res_read["decision"] == "allow"
    assert "auto-accepted read-only" in res_read["reason"]

    # Mutating tool is NOT auto-allowed (falls back to local ask when no client connected)
    res_write = await mgr.request_approval("ap-2", "conv-1", "run_command", {"CommandLine": "git status"})
    assert res_write["decision"] == "ask"


@pytest.mark.asyncio
async def test_session_manager_per_conversation_policy(tmp_path: Path):
    cfg = RemoteConfig(brain_dir=tmp_path, auth_token="tok", e2ee_enabled=False)
    mgr = SessionManager(cfg)
    mgr.set_approval_policy("ask_all")
    mgr.set_approval_policy("auto_reads", conversation_id="conv-auto")

    assert mgr.get_approval_policy("conv-other") == "ask_all"
    assert mgr.get_approval_policy("conv-auto") == "auto_reads"


def test_rest_api_approval_policy(tmp_path: Path):
    cfg = RemoteConfig(brain_dir=tmp_path, auth_token="tok", enable_auth=True, e2ee_enabled=False)
    app = create_app(cfg)
    client = TestClient(app)

    # Initial policy
    resp = client.get("/api/approvals/policy", headers={"X-Auth-Token": "tok"})
    assert resp.status_code == 200
    assert resp.json()["policy"] == "ask_all"

    # Set policy
    resp = client.post(
        "/api/approvals/policy",
        json={"policy": "auto_reads"},
        headers={"X-Auth-Token": "tok"},
    )
    assert resp.status_code == 200
    assert resp.json()["policy"] == "auto_reads"

    # Verify updated
    resp = client.get("/api/approvals/policy", headers={"X-Auth-Token": "tok"})
    assert resp.json()["policy"] == "auto_reads"

    # Invalid policy rejected
    resp = client.post(
        "/api/approvals/policy",
        json={"policy": "invalid_policy_name"},
        headers={"X-Auth-Token": "tok"},
    )
    assert resp.status_code == 400
