"""Prompt queue: follow-ups typed mid-turn wait instead of interleaving.

opencode's composer queues follow-up prompts while the agent works and shows
them as cancelable chips; agy-remote typed straight into the TUI, so a prompt
sent while agy was mid-turn landed in the input box of a running stream.
"""

import time
from pathlib import Path

import pytest

from agy_remote.config import RemoteConfig
from agy_remote.models import SessionRecord
from agy_remote.session_manager import SessionManager


def _record(conversation_id: str = "conv-1") -> SessionRecord:
    return SessionRecord(id="s1", tmux_name="agy-remote-test", conversation_id=conversation_id)


class _FakeWebSocket:
    def __init__(self):
        self.sent = []

    async def send_json(self, data):
        self.sent.append(data)


class _FakeSupervisor:
    """Records what would be typed into agy's pane."""

    def __init__(self):
        self.injected = []

    def inject_input(self, text):
        self.injected.append(text)
        return True


class _RecordingBackend:
    """A backend that delivers through the supervisor, like the real one."""

    name = "agy"

    def __init__(self):
        self.prompts: list[tuple[str, str | None]] = []

    async def send_prompt(self, mgr, prompt, conversation_id=None):
        self.prompts.append((prompt, conversation_id))
        sup = mgr.get_supervisor(conversation_id)
        if sup is not None and hasattr(sup, "inject_input"):
            sup.inject_input(prompt)
            return "pty"
        return "broadcast"


def _mgr(tmp_path: Path) -> tuple[SessionManager, _RecordingBackend]:
    cfg = RemoteConfig(brain_dir=tmp_path, auth_token="token", e2ee_enabled=False)
    backend = _RecordingBackend()
    mgr = SessionManager(cfg, backend=backend)
    return mgr, backend


def _recently_active(mgr: SessionManager, conversation_id: str) -> None:
    """Mark the conversation as streaming a step right now."""
    mgr.note_conversation_activity(conversation_id)


# ---------------------------------------------------------------------------
# Busy detection
# ---------------------------------------------------------------------------


def test_a_conversation_that_just_streamed_is_busy(tmp_path: Path):
    mgr, _ = _mgr(tmp_path)
    _recently_active(mgr, "conv-1")
    assert mgr.is_conversation_busy("conv-1") is True


def test_a_quiet_conversation_is_not_busy(tmp_path: Path):
    mgr, _ = _mgr(tmp_path)
    assert mgr.is_conversation_busy("conv-1") is False


def test_activity_older_than_the_window_is_not_busy(tmp_path: Path):
    mgr, _ = _mgr(tmp_path)
    mgr._last_activity["conv-1"] = time.monotonic() - mgr.busy_window_seconds - 1
    assert mgr.is_conversation_busy("conv-1") is False


@pytest.mark.asyncio
async def test_a_pending_approval_keeps_the_session_busy(tmp_path: Path):
    """A held tool call is a paused turn: a follow-up typed then must queue."""
    mgr, _ = _mgr(tmp_path)
    mgr._pending_approvals["app-1"] = {
        "id": "app-1",
        "conversation_id": "conv-1",
        "status": "pending",
    }
    assert mgr.is_conversation_busy("conv-1") is True
    mgr._pending_approvals["app-1"]["status"] = "allowed"
    assert mgr.is_conversation_busy("conv-1") is False


def test_an_answered_approval_for_another_session_does_not_leak_busy(tmp_path: Path):
    mgr, _ = _mgr(tmp_path)
    mgr._pending_approvals["app-1"] = {
        "id": "app-1",
        "conversation_id": "conv-2",
        "status": "pending",
    }
    assert mgr.is_conversation_busy("conv-1") is False


# ---------------------------------------------------------------------------
# Submitting: queue vs straight through
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_prompt_mid_turn_is_queued_not_injected(tmp_path: Path):
    mgr, backend = _mgr(tmp_path)
    ws = _FakeWebSocket()
    mgr._connected_clients.add(ws)
    sup = _FakeSupervisor()
    mgr.register_session(
        _record(),
        supervisor=sup,
    )
    _recently_active(mgr, "conv-1")

    result = await mgr.submit_prompt("follow up please", "conv-1")

    assert result["status"] == "queued"
    assert sup.injected == [], "a queued prompt must not be typed into a running turn"
    assert backend.prompts == []
    events = [frame["event"] for frame in ws.sent]
    assert "prompt_queued" in events
    queued = next(f for f in ws.sent if f["event"] == "prompt_queued")
    assert queued["data"]["prompt"] == "follow up please"
    assert queued["data"]["conversation_id"] == "conv-1"
    assert queued["data"]["id"]


@pytest.mark.asyncio
async def test_an_idle_prompt_goes_straight_through(tmp_path: Path):
    mgr, backend = _mgr(tmp_path)
    ws = _FakeWebSocket()
    mgr._connected_clients.add(ws)
    sup = _FakeSupervisor()
    mgr.register_session(
        _record(),
        supervisor=sup,
    )

    result = await mgr.submit_prompt("hello", "conv-1")

    assert result["status"] == "ok"
    assert result["delivered_via"] == "pty"
    assert sup.injected == ["hello"]
    assert any(f["event"] == "prompt_sent" for f in ws.sent)


# ---------------------------------------------------------------------------
# Delivery when the turn ends
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_queued_prompt_is_delivered_once_the_turn_goes_quiet(tmp_path: Path):
    mgr, backend = _mgr(tmp_path)
    ws = _FakeWebSocket()
    mgr._connected_clients.add(ws)
    sup = _FakeSupervisor()
    mgr.register_session(
        _record(),
        supervisor=sup,
    )
    await mgr.queue_prompt("the follow-up", "conv-1")

    # The turn ends: no steps for longer than the busy window, no pending approval.
    mgr._last_activity["conv-1"] = time.monotonic() - mgr.busy_window_seconds - 1
    delivered = await mgr.deliver_due_prompts()

    assert delivered == 1
    assert sup.injected == ["the follow-up"]
    assert backend.prompts == [("the follow-up", "conv-1")]
    events = {f["event"] for f in ws.sent}
    assert "prompt_delivered" in events
    delivered_frame = next(f for f in ws.sent if f["event"] == "prompt_delivered")
    assert delivered_frame["data"]["id"]
    assert delivered_frame["data"]["prompt"] == "the follow-up"


@pytest.mark.asyncio
async def test_the_queue_is_fifo_and_delivers_one_head_per_poll(tmp_path: Path):
    mgr, backend = _mgr(tmp_path)
    sup = _FakeSupervisor()
    mgr.register_session(
        _record(),
        supervisor=sup,
    )
    await mgr.queue_prompt("one", "conv-1")
    second = await mgr.queue_prompt("two", "conv-1")
    mgr._last_activity["conv-1"] = time.monotonic() - mgr.busy_window_seconds - 1

    await mgr.deliver_due_prompts()

    assert sup.injected == ["one"], "the head of the queue delivers first"
    remaining = mgr.queued_prompts("conv-1")
    assert [e["id"] for e in remaining] == [second["prompt_id"]]


@pytest.mark.asyncio
async def test_a_busy_session_holds_its_queue(tmp_path: Path):
    mgr, backend = _mgr(tmp_path)
    sup = _FakeSupervisor()
    mgr.register_session(
        _record(),
        supervisor=sup,
    )
    await mgr.queue_prompt("still working", "conv-1")
    _recently_active(mgr, "conv-1")

    await mgr.deliver_due_prompts()

    assert sup.injected == []
    assert len(mgr.queued_prompts("conv-1")) == 1


@pytest.mark.asyncio
async def test_a_queue_without_a_supervisor_is_held_not_dropped(tmp_path: Path):
    """No pane to type into: the mail waits, exactly like held mailbox mail."""
    mgr, backend = _mgr(tmp_path)
    await mgr.queue_prompt("nobody home", "conv-1")
    mgr._last_activity["conv-1"] = time.monotonic() - mgr.busy_window_seconds - 1

    delivered = await mgr.deliver_due_prompts()

    assert delivered == 0
    assert len(mgr.queued_prompts("conv-1")) == 1
    assert backend.prompts == []


# ---------------------------------------------------------------------------
# Cancelling
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_queued_prompt_can_be_cancelled(tmp_path: Path):
    mgr, _ = _mgr(tmp_path)
    ws = _FakeWebSocket()
    mgr._connected_clients.add(ws)
    queued = await mgr.queue_prompt("changed my mind", "conv-1")
    prompt_id = queued["prompt_id"]

    assert await mgr.cancel_queued_prompt(prompt_id) is True
    assert mgr.queued_prompts("conv-1") == []
    cancelled = next(f for f in ws.sent if f["event"] == "prompt_cancelled")
    assert cancelled["data"]["id"] == prompt_id


@pytest.mark.asyncio
async def test_cancelling_an_unknown_id_says_so(tmp_path: Path):
    mgr, _ = _mgr(tmp_path)
    assert await mgr.cancel_queued_prompt("nope") is False


# ---------------------------------------------------------------------------
# REST surface: the fallback path queues with the same semantics
# ---------------------------------------------------------------------------


def test_the_prompt_endpoint_queues_when_busy(tmp_path: Path, monkeypatch):
    from fastapi.testclient import TestClient

    from agy_remote.server import create_app

    cfg = RemoteConfig(brain_dir=tmp_path, auth_token="secret123", enable_auth=True, e2ee_enabled=False)
    app = create_app(cfg)
    client = TestClient(app)

    class _BusyBackend:
        name = "agy"

        async def send_prompt(self, mgr, prompt, conversation_id=None):
            return "pty"

    mgr = app.state.session_manager
    mgr.backend = _BusyBackend()
    sup = _FakeSupervisor()
    mgr.register_session(
        _record(),
        supervisor=sup,
    )
    mgr.note_conversation_activity("conv-1")

    resp = client.post(
        "/api/prompt",
        json={"prompt": "queued via rest", "conversation_id": "conv-1"},
        headers={"X-Auth-Token": "secret123"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "queued"
    assert sup.injected == []
