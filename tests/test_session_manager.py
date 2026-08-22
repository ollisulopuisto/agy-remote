"""Unit tests for session manager and transcript reader."""

import asyncio
import json
import os
import time
from pathlib import Path
from unittest.mock import AsyncMock

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
    # Someone has to be looking at this session, or the hook is answered
    # locally instead of held -- see test_an_approval_nobody_can_see_is_not_held.
    mgr._connected_clients.add(_FakeWebSocket())

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


# ---------------------------------------------------------------------------
# The terminal mirror: agy's panels and its execution mode live only on the
# screen, so without this the phone drives them blind.
# ---------------------------------------------------------------------------


class _FakeWebSocket:
    def __init__(self):
        self.sent = []

    async def send_json(self, data):
        self.sent.append(data)


class _FakeSupervisor:
    """A supervisor that hands out pty output the way PtySupervisor does."""

    def __init__(self, rows=24, cols=80):
        self.running = True
        self.rows = rows
        self.cols = cols
        self._listeners = []

    def add_output_listener(self, callback):
        self._listeners.append(callback)

    def emit(self, data: bytes):
        for callback in self._listeners:
            callback(data)


@pytest.mark.asyncio
async def test_terminal_output_is_mirrored_and_broadcast(tmp_path: Path):
    cfg = RemoteConfig(brain_dir=tmp_path, auth_token="token", e2ee_enabled=False)
    mgr = SessionManager(cfg)
    ws = _FakeWebSocket()
    mgr._connected_clients.add(ws)

    supervisor = _FakeSupervisor(rows=4, cols=30)
    mgr.attach_terminal(supervisor)

    supervisor.emit(b"\x1b[2J\x1b[Hchoose a model\r\n")
    await mgr.broadcast_terminal()

    assert len(ws.sent) == 1
    assert ws.sent[0]["event"] == "terminal_screen"
    assert any("choose a model" in line for line in ws.sent[0]["data"]["lines"])


@pytest.mark.asyncio
async def test_an_unchanged_screen_is_not_rebroadcast(tmp_path: Path):
    """The watcher ticks several times a second; a still screen must cost nothing."""
    cfg = RemoteConfig(brain_dir=tmp_path, auth_token="token", e2ee_enabled=False)
    mgr = SessionManager(cfg)
    ws = _FakeWebSocket()
    mgr._connected_clients.add(ws)

    supervisor = _FakeSupervisor()
    mgr.attach_terminal(supervisor)
    supervisor.emit(b"hello")

    await mgr.broadcast_terminal()
    await mgr.broadcast_terminal()
    await mgr.broadcast_terminal()

    assert len(ws.sent) == 1


@pytest.mark.asyncio
async def test_no_terminal_attached_broadcasts_nothing(tmp_path: Path):
    """Watcher mode supervises no session, so there is no screen to mirror."""
    cfg = RemoteConfig(brain_dir=tmp_path, auth_token="token", e2ee_enabled=False)
    mgr = SessionManager(cfg)
    ws = _FakeWebSocket()
    mgr._connected_clients.add(ws)

    await mgr.broadcast_terminal()
    assert ws.sent == []


@pytest.mark.asyncio
async def test_the_mirror_matches_the_pty_size(tmp_path: Path):
    cfg = RemoteConfig(brain_dir=tmp_path, auth_token="token", e2ee_enabled=False)
    mgr = SessionManager(cfg)

    mgr.attach_terminal(_FakeSupervisor(rows=12, cols=45))
    assert mgr.terminal is not None
    assert (mgr.terminal.rows, mgr.terminal.cols) == (12, 45)


@pytest.mark.asyncio
async def test_the_envelope_is_stripped_before_it_reaches_a_client(tmp_path: Path):
    """agy wraps a prompt in <USER_REQUEST> plus metadata; the phone showed it all."""
    conv_id = "enveloped"
    log = tmp_path / conv_id / ".system_generated" / "logs" / "transcript.jsonl"
    log.parent.mkdir(parents=True)
    log.write_text(
        json.dumps(
            {
                "step_index": 0,
                "type": "USER_INPUT",
                "source": "USER_EXPLICIT",
                "content": (
                    "<USER_REQUEST>\nClean up my disk\n</USER_REQUEST>\n"
                    "<ADDITIONAL_METADATA>\nThe current local time is: now.\n</ADDITIONAL_METADATA>"
                ),
            }
        )
        + "\n"
        + json.dumps(
            {
                "step_index": 1,
                "type": "PLANNER_RESPONSE",
                "source": "MODEL",
                "tool_calls": [{"name": "run_command", "args": {"CommandLine": '"df -h"'}}],
            }
        )
        + "\n",
        encoding="utf-8",
    )

    cfg = RemoteConfig(brain_dir=tmp_path, auth_token="token")
    mgr = SessionManager(cfg)
    await mgr.switch_conversation(conv_id)

    assert mgr.active_steps[0].content == "Clean up my disk"
    assert mgr.active_steps[1].tool_calls[0]["args"]["CommandLine"] == "df -h"
    assert mgr.list_conversations()[0].title == "Clean up my disk"


@pytest.mark.asyncio
async def test_steps_carry_whether_they_are_scaffolding(tmp_path: Path):
    conv_id = "with-checkpoint"
    log = tmp_path / conv_id / ".system_generated" / "logs" / "transcript.jsonl"
    log.parent.mkdir(parents=True)
    log.write_text(
        json.dumps({"step_index": 0, "type": "USER_INPUT", "source": "USER_EXPLICIT", "content": "hi"})
        + "\n"
        + json.dumps({"step_index": 1, "type": "CHECKPOINT", "source": "SYSTEM", "content": "{{ CHECKPOINT 0 }}"})
        + "\n",
        encoding="utf-8",
    )

    mgr = SessionManager(RemoteConfig(brain_dir=tmp_path, auth_token="token"))
    await mgr.switch_conversation(conv_id)

    assert mgr.active_steps[0].scaffolding is False
    assert mgr.active_steps[1].scaffolding is True


@pytest.mark.asyncio
async def test_a_switch_carries_the_conversation_it_switched_to(tmp_path: Path):
    """The phone cannot tell a new session from the old one without its identity."""
    conv_id = "fresh-session"
    log = tmp_path / conv_id / ".system_generated" / "logs" / "transcript.jsonl"
    log.parent.mkdir(parents=True)
    log.write_text(
        json.dumps({"step_index": 0, "type": "USER_INPUT", "source": "USER_EXPLICIT", "content": "hello"}) + "\n",
        encoding="utf-8",
    )

    mgr = SessionManager(RemoteConfig(brain_dir=tmp_path, auth_token="token", e2ee_enabled=False))
    ws = _FakeWebSocket()
    mgr._connected_clients.add(ws)

    await mgr.switch_conversation(conv_id)

    conversation = ws.sent[0]["data"]["conversation"]
    assert conversation["id"] == conv_id
    assert conversation["title"] == "hello"
    assert conversation["created_at"]


# ---------------------------------------------------------------------------
# Expiry must also end sessions that were connected before the deadline.
# ---------------------------------------------------------------------------


class _ClosableWebSocket:
    def __init__(self):
        self.sent = []
        self.closed_with = None

    async def send_json(self, data):
        self.sent.append(data)

    async def close(self, code=1000):
        self.closed_with = code


@pytest.mark.asyncio
async def test_live_connections_are_closed_when_the_pairing_expires(tmp_path: Path):
    """token_ok only refuses *new* connections; a socket opened before the
    deadline would otherwise stream transcripts and accept prompts forever."""
    from datetime import UTC, datetime, timedelta

    cfg = RemoteConfig(brain_dir=tmp_path, auth_token="token", e2ee_enabled=False)
    cfg.credentials_expire_at = datetime.now(UTC) - timedelta(seconds=1)
    mgr = SessionManager(cfg)
    ws = _ClosableWebSocket()
    mgr._connected_clients.add(ws)

    closed = await mgr.disconnect_expired_clients()

    assert closed == 1
    assert ws.closed_with == 1008  # policy violation
    assert ws not in mgr._connected_clients


@pytest.mark.asyncio
async def test_live_connections_survive_while_the_pairing_is_valid(tmp_path: Path):
    from datetime import UTC, datetime, timedelta

    cfg = RemoteConfig(brain_dir=tmp_path, auth_token="token", e2ee_enabled=False)
    cfg.credentials_expire_at = datetime.now(UTC) + timedelta(days=5)
    mgr = SessionManager(cfg)
    ws = _ClosableWebSocket()
    mgr._connected_clients.add(ws)

    assert await mgr.disconnect_expired_clients() == 0
    assert ws.closed_with is None
    assert ws in mgr._connected_clients


@pytest.mark.asyncio
async def test_no_deadline_means_no_disconnects(tmp_path: Path):
    cfg = RemoteConfig(brain_dir=tmp_path, auth_token="token", e2ee_enabled=False)
    mgr = SessionManager(cfg)
    ws = _ClosableWebSocket()
    mgr._connected_clients.add(ws)

    assert await mgr.disconnect_expired_clients() == 0
    assert ws in mgr._connected_clients


@pytest.mark.asyncio
async def test_steps_that_arrive_while_watching_stay_in_the_view(tmp_path: Path):
    """A step broadcast once is not a step the next client can see.

    `tick` tails the transcript and broadcasts each new step, but left
    `active_steps` holding whatever was on disk at the last switch. Everything
    after that lived only in the frames already sent: reconnect, reload the
    PWA, or ask `/api/conversations/<active>` and the answer was a transcript
    that stopped mid-conversation -- with the agent still working in it.
    """
    conv_id = "conv-live"
    log_dir = tmp_path / conv_id / ".system_generated" / "logs"
    log_dir.mkdir(parents=True)
    log_file = log_dir / "transcript.jsonl"

    def append(**step) -> None:
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(step) + "\n")

    append(step_index=0, type="USER_INPUT", source="USER_INPUT", content="say ATTACHED")

    cfg = RemoteConfig(brain_dir=tmp_path, auth_token="token")
    mgr = SessionManager(cfg)
    mgr.broadcast = AsyncMock()
    await mgr.switch_conversation(conv_id)
    assert len(mgr.active_steps) == 1

    # The agent answers while the phone is connected.
    append(step_index=1, type="PLANNER_RESPONSE", source="MODEL", content="ATTACHED")
    await mgr.backend.tick(mgr)

    assert [s.content for s in mgr.active_steps] == ["say ATTACHED", "ATTACHED"]
    broadcast = [c.args[0] for c in mgr.broadcast.call_args_list if c.args[0].get("event") == "step_added"]
    assert broadcast and broadcast[-1]["data"]["step"]["content"] == "ATTACHED"

    # Tailing again with nothing new must not duplicate it.
    await mgr.backend.tick(mgr)
    assert len(mgr.active_steps) == 2


@pytest.mark.asyncio
async def test_a_second_device_connecting_is_announced(tmp_path: Path):
    """Access is all-or-nothing, so the connection count is the only alarm.

    Every client holds the same host-wide token: there is no per-device
    identity to audit afterwards and no way to revoke one device without
    revoking them all. A connection nobody expected is therefore the single
    observable sign that the pairing URL has escaped -- and nothing announced
    one, so a second device could watch and type unnoticed.
    """
    # Frames are sealed by default; read them in the clear here so the test is
    # about who is connected, not about the envelope.
    cfg = RemoteConfig(brain_dir=tmp_path, auth_token="token", e2ee_enabled=False)
    mgr = SessionManager(cfg)

    first = _FakeWebSocket()
    await mgr.register_client(first)
    # The count comes with the snapshot, so a client knows from the start
    # whether it is alone.
    assert [f["event"] for f in first.sent] == ["init", "peers"]
    assert first.sent[-1]["data"]["count"] == 1

    second = _FakeWebSocket()
    await mgr.register_client(second)

    peers = [f for f in first.sent if f["event"] == "peers"]
    assert peers, f"a second device connected unannounced: {[f['event'] for f in first.sent]}"
    assert peers[-1]["data"]["count"] == 2
    # The one that just arrived is told too, so both agree on what is connected.
    assert [f["event"] for f in second.sent][-1] == "peers"

    # And the alarm clears when it leaves rather than lingering.
    mgr.unregister_client(second)
    await mgr.announce_peers()
    assert [f for f in first.sent if f["event"] == "peers"][-1]["data"]["count"] == 1


@pytest.mark.asyncio
async def test_an_approval_nobody_can_see_is_not_held(tmp_path: Path):
    """A server must not hold an agy hostage for a banner nobody was shown.

    The PreToolUse hook blocks the agy that fired it until this returns. That
    is the point when a phone is watching *that* session -- wait, however long
    it takes. Every other case can only end one way: agy kills the hook after
    300s and the tool call fails.

    Three of them, all real:
      - no client connected (an always-on server's normal state),
      - a client watching a different session, whose banner is deliberately
        never drawn for this one,
      - a socket still open with nothing behind it.

    Each now answers immediately with "ask", which hands the decision back to
    agy: it prompts in its own terminal exactly as it would with no hook.
    """
    cfg = RemoteConfig(brain_dir=tmp_path, auth_token="token", e2ee_enabled=False)
    mgr = SessionManager(cfg)

    async def ask(approval_id: str, conversation_id: str) -> dict:
        return await asyncio.wait_for(
            mgr.request_approval(
                approval_id=approval_id,
                conversation_id=conversation_id,
                tool_name="run_command",
                args={"CommandLine": "grep -r @podpuri.com ."},
            ),
            timeout=5,
        )

    # 1. Nobody connected at all.
    lonely = await ask("a1", "c1")
    assert lonely["decision"] == "ask", lonely
    assert mgr.get_active_pending_approvals() == []

    # 2. A phone is connected, but watching another session. Its banner is only
    #    ever drawn for the active conversation, so this one would be invisible.
    mgr._connected_clients.add(_FakeWebSocket())
    mgr.active_conversation_id = "on-screen"
    elsewhere = await ask("a2", "some-other-session")
    assert elsewhere["decision"] == "ask", elsewhere

    # 3. A socket that is open with nothing behind it.
    class _DeadSocket:
        async def send_json(self, data) -> None:  # noqa: ANN001
            raise ConnectionResetError("phone went to sleep")

    mgr._connected_clients.clear()
    mgr._connected_clients.add(_DeadSocket())
    mgr.active_conversation_id = "c1"
    dead = await ask("a3", "c1")
    assert dead["decision"] == "ask", dead
    assert mgr.get_active_pending_approvals() == []

    # 4. Someone is genuinely looking at this session: wait, as designed.
    mgr._connected_clients.clear()
    mgr._connected_clients.add(_FakeWebSocket())
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(
            mgr.request_approval(approval_id="a4", conversation_id="c1", tool_name="run_command", args={}),
            timeout=0.5,
        )
