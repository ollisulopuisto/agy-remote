"""Unit tests for multi-session core registry (Phase 1.1)."""

from datetime import datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agy_remote.config import RemoteConfig
from agy_remote.models import SessionRecord
from agy_remote.screen import TerminalMirror
from agy_remote.server import create_app
from agy_remote.session_manager import SessionManager
from agy_remote.tmux_runner import (
    TmuxSupervisor,
    get_tmux_supervisor,
    register_tmux_supervisor,
)


class FakeSupervisor:
    def __init__(self, name: str):
        self.name = name
        self.injected_prompts: list[str] = []
        self.pressed_keys: list[str] = []
        self.running = True

    def inject_input(self, text: str) -> bool:
        self.injected_prompts.append(text)
        return True

    def send_key(self, key: str) -> bool:
        self.pressed_keys.append(key)
        return True

    def has_session(self) -> bool:
        return self.running


def test_session_record_model(tmp_path: Path):
    rec = SessionRecord(
        id="session-alpha",
        tmux_name="agy-remote-alpha",
        pane_target="agy-remote-alpha:0.0",
        workdir=tmp_path / "alpha",
        conversation_id="conv-alpha-123",
        busy=False,
    )
    assert rec.id == "session-alpha"
    assert rec.tmux_name == "agy-remote-alpha"
    assert rec.pane_target == "agy-remote-alpha:0.0"
    assert rec.workdir == tmp_path / "alpha"
    assert rec.conversation_id == "conv-alpha-123"
    assert rec.busy is False
    assert isinstance(rec.created_at, datetime)
    assert isinstance(rec.last_activity_at, datetime)


@pytest.mark.asyncio
async def test_session_manager_registry_operations(tmp_path: Path):
    cfg = RemoteConfig(brain_dir=tmp_path, auth_token="tok")
    mgr = SessionManager(cfg)

    sup1 = FakeSupervisor("sup1")
    mirror1 = TerminalMirror(rows=24, cols=80)
    rec1 = SessionRecord(
        id="sess-1",
        tmux_name="agy-remote-1",
        conversation_id="conv-1",
        workdir=tmp_path / "proj1",
    )

    sup2 = FakeSupervisor("sup2")
    mirror2 = TerminalMirror(rows=24, cols=80)
    rec2 = SessionRecord(
        id="sess-2",
        tmux_name="agy-remote-2",
        conversation_id="conv-2",
        workdir=tmp_path / "proj2",
    )

    # Register sessions
    mgr.register_session(rec1, supervisor=sup1, mirror=mirror1)
    mgr.register_session(rec2, supervisor=sup2, mirror=mirror2)

    assert len(mgr.list_sessions()) == 2
    assert mgr.get_session("sess-1") == rec1
    assert mgr.get_session("sess-2") == rec2
    assert mgr.get_session_by_conversation("conv-1") == rec1
    assert mgr.get_session_by_conversation("conv-2") == rec2

    # Supervised lookups
    assert mgr.get_supervisor("sess-1") == sup1
    assert mgr.get_supervisor("conv-2") == sup2
    assert mgr.get_screen_mirror("sess-1") == mirror1
    assert mgr.get_screen_mirror("conv-2") == mirror2

    # Remove session
    mgr.remove_session("sess-1")
    assert len(mgr.list_sessions()) == 1
    assert mgr.get_session("sess-1") is None
    assert mgr.get_supervisor("sess-1") is None


@pytest.mark.asyncio
async def test_multi_session_prompt_routing(tmp_path: Path):
    cfg = RemoteConfig(brain_dir=tmp_path, auth_token="tok")
    mgr = SessionManager(cfg)

    sup1 = FakeSupervisor("sup1")
    rec1 = SessionRecord(id="sess-1", conversation_id="conv-1")
    mgr.register_session(rec1, supervisor=sup1)

    sup2 = FakeSupervisor("sup2")
    rec2 = SessionRecord(id="sess-2", conversation_id="conv-2")
    mgr.register_session(rec2, supervisor=sup2)

    # Send prompt targeting conv-1
    res1 = await mgr.backend.send_prompt(mgr, "prompt for conv 1", conversation_id="conv-1")
    assert res1 in ("tmux", "pty")
    assert sup1.injected_prompts == ["prompt for conv 1"]
    assert sup2.injected_prompts == []

    # Send prompt targeting conv-2
    res2 = await mgr.backend.send_prompt(mgr, "prompt for conv 2", conversation_id="conv-2")
    assert res2 in ("tmux", "pty")
    assert sup1.injected_prompts == ["prompt for conv 1"]
    assert sup2.injected_prompts == ["prompt for conv 2"]


def test_multi_session_key_and_screen_api(tmp_path: Path):
    cfg = RemoteConfig(brain_dir=tmp_path, auth_token="secret", enable_auth=True)
    app = create_app(cfg)
    client = TestClient(app)
    mgr = app.state.session_manager

    sup1 = FakeSupervisor("sup1")
    mirror1 = TerminalMirror(rows=24, cols=80)
    mirror1.feed(b"SCREEN 1 CONTENT\n")
    rec1 = SessionRecord(id="sess-1", conversation_id="conv-1")
    mgr.register_session(rec1, supervisor=sup1, mirror=mirror1)

    sup2 = FakeSupervisor("sup2")
    mirror2 = TerminalMirror(rows=24, cols=80)
    mirror2.feed(b"SCREEN 2 CONTENT\n")
    rec2 = SessionRecord(id="sess-2", conversation_id="conv-2")
    mgr.register_session(rec2, supervisor=sup2, mirror=mirror2)

    headers = {"X-Auth-Token": "secret"}

    # Target key press to conv-2
    resp = client.post(
        "/api/key",
        json={"key": "escape", "conversation_id": "conv-2"},
        headers=headers,
    )
    assert resp.status_code == 200
    assert sup2.pressed_keys == ["escape"]
    assert sup1.pressed_keys == []

    # Screen query targeting conv-1
    resp_s1 = client.get("/api/screen?conversation_id=conv-1", headers=headers)
    assert resp_s1.status_code == 200
    assert "SCREEN 1 CONTENT" in "".join(resp_s1.json()["terminal"]["lines"])

    # Screen query targeting conv-2
    resp_s2 = client.get("/api/screen?conversation_id=conv-2", headers=headers)
    assert resp_s2.status_code == 200
    assert "SCREEN 2 CONTENT" in "".join(resp_s2.json()["terminal"]["lines"])


def test_tmux_registry_multiple_supervisors():
    sup_a = TmuxSupervisor(session_name="agy-remote-a")
    sup_b = TmuxSupervisor(session_name="agy-remote-b")

    register_tmux_supervisor(sup_a)
    register_tmux_supervisor(sup_b)

    assert get_tmux_supervisor("agy-remote-a") == sup_a
    assert get_tmux_supervisor("agy-remote-b") == sup_b
