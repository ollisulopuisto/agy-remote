"""Tests for process hang/stall watchdog and emergency session control endpoints."""

import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agy_remote.config import RemoteConfig
from agy_remote.models import SessionRecord
from agy_remote.server import create_app
from agy_remote.session_manager import SessionManager


class _MockSupervisor:
    def __init__(self):
        self.sent_keys = []
        self.killed = False

    def send_key(self, key: str) -> bool:
        self.sent_keys.append(key)
        return True

    def kill(self) -> bool:
        self.killed = True
        return True


class _MockWebSocket:
    def __init__(self):
        self.sent = []

    async def send_json(self, data):
        self.sent.append(data)


@pytest.mark.asyncio
async def test_watchdog_detects_stall(tmp_path: Path):
    cfg = RemoteConfig(brain_dir=tmp_path, auth_token="tok", e2ee_enabled=False)
    mgr = SessionManager(cfg)
    ws = _MockWebSocket()
    mgr._connected_clients.add(ws)

    rec = SessionRecord(id="s1", conversation_id="conv-1")
    sup = _MockSupervisor()
    mgr.register_session(rec, supervisor=sup)

    # Session is busy, but silence exceeds threshold
    mgr._last_activity["conv-1"] = time.monotonic()
    mgr._last_output_time["conv-1"] = time.monotonic() - 200.0

    stalled = await mgr.check_stalled_sessions(threshold_seconds=180.0)
    assert "conv-1" in stalled
    assert mgr.is_session_stalled("conv-1") is True

    # Broadcast event sent
    events = [f["event"] for f in ws.sent]
    assert "session_stalled" in events

    # Output arriving clears the stall
    mgr.note_output("conv-1")
    assert mgr.is_session_stalled("conv-1") is False


@pytest.mark.asyncio
async def test_watchdog_ignores_pending_approvals(tmp_path: Path):
    """A session waiting on a human tool approval is paused, not hung."""
    cfg = RemoteConfig(brain_dir=tmp_path, auth_token="tok", e2ee_enabled=False)
    mgr = SessionManager(cfg)
    rec = SessionRecord(id="s1", conversation_id="conv-1")
    mgr.register_session(rec)

    # Mark as busy with pending approval
    mgr._last_activity["conv-1"] = time.monotonic()
    mgr._last_output_time["conv-1"] = time.monotonic() - 300.0
    mgr._pending_approvals["app-1"] = {
        "id": "app-1",
        "conversation_id": "conv-1",
        "status": "pending",
    }

    stalled = await mgr.check_stalled_sessions(threshold_seconds=180.0)
    assert stalled == []
    assert mgr.is_session_stalled("conv-1") is False


@pytest.mark.asyncio
async def test_session_interrupt_and_kill(tmp_path: Path):
    cfg = RemoteConfig(brain_dir=tmp_path, auth_token="tok", e2ee_enabled=False)
    mgr = SessionManager(cfg)
    rec = SessionRecord(id="s1", conversation_id="conv-1")
    sup = _MockSupervisor()
    mgr.register_session(rec, supervisor=sup)

    assert mgr.interrupt_session("conv-1") is True
    assert "interrupt" in sup.sent_keys

    assert mgr.kill_session("conv-1") is True
    assert sup.killed is True


def test_rest_api_interrupt_kill_reorder(tmp_path: Path):
    cfg = RemoteConfig(brain_dir=tmp_path, auth_token="tok", enable_auth=True, e2ee_enabled=False)
    app = create_app(cfg)
    client = TestClient(app)

    mgr: SessionManager = app.state.session_manager
    rec = SessionRecord(id="s1", conversation_id="conv-1")
    sup = _MockSupervisor()
    mgr.register_session(rec, supervisor=sup)

    # Test interrupt endpoint
    resp = client.post("/api/sessions/conv-1/interrupt", headers={"X-Auth-Token": "tok"})
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"
    assert "interrupt" in sup.sent_keys

    # Test kill endpoint
    resp = client.post("/api/sessions/conv-1/kill", headers={"X-Auth-Token": "tok"})
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"
    assert sup.killed is True

    # Test queue reorder endpoint
    mgr._prompt_queues["conv-1"] = [
        {"id": "p1", "prompt": "first"},
        {"id": "p2", "prompt": "second"},
    ]
    resp = client.post(
        "/api/sessions/conv-1/queue/reorder",
        json={"ordered_ids": ["p2", "p1"]},
        headers={"X-Auth-Token": "tok"},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"
    assert [p["id"] for p in mgr.queued_prompts("conv-1")] == ["p2", "p1"]
