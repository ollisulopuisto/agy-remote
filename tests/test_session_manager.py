"""Unit tests for session manager and transcript reader."""

import asyncio
import json
import os
import time
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from agy_remote.config import RemoteConfig
from agy_remote.models import ApprovalResponseRequest, SessionRecord
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
    os.utime(brain_dir / conv_id, (mtime, mtime))
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


@pytest.mark.asyncio
async def test_multiple_concurrent_conversations_do_not_cause_flapping(tmp_path: Path):
    """Activity in an existing session must not yank the view away from another active session."""
    now = time.time()
    log_a = _write_conversation(tmp_path, "conv-a", "task A", now - 100)
    log_b = _write_conversation(tmp_path, "conv-b", "task B", now)

    cfg = RemoteConfig(brain_dir=tmp_path, auth_token="token")
    mgr = SessionManager(cfg)
    await mgr.switch_conversation("conv-b")
    assert mgr.active_conversation_id == "conv-b"

    # conv-a receives a new step, bumping its mtime past conv-b
    with open(log_a, "a", encoding="utf-8") as f:
        f.write(json.dumps({"step_index": 1, "type": "USER_INPUT", "source": "USER_INPUT", "content": "more A"}) + "\n")
    os.utime(log_a, (now + 50, now + 50))

    # The watcher must NOT switch to conv-a: conv-a is an existing session, not a newly started one.
    switched = await mgr.follow_latest_conversation()
    assert not switched
    assert mgr.active_conversation_id == "conv-b"

    # conv-b receives a new step
    with open(log_b, "a", encoding="utf-8") as f:
        f.write(json.dumps({"step_index": 1, "type": "USER_INPUT", "source": "USER_INPUT", "content": "more B"}) + "\n")
    os.utime(log_b, (now + 60, now + 60))

    switched = await mgr.follow_latest_conversation()
    assert not switched
    assert mgr.active_conversation_id == "conv-b"

    # A brand new session is launched
    _write_conversation(tmp_path, "conv-c", "brand new C", now + 100)
    switched = await mgr.follow_latest_conversation()
    assert switched
    assert mgr.active_conversation_id == "conv-c"


@pytest.mark.asyncio
async def test_send_prompt_to_different_conversation_does_not_type_into_wrong_supervisor(tmp_path: Path, monkeypatch):
    """Prompt sent to an inactive conversation must not type into the active supervisor."""
    from agy_remote import pty_runner

    typed_prompts = []

    class MockPty:
        running = True

        def inject_input(self, text: str) -> bool:
            typed_prompts.append(text)
            return True

    mock_pty = MockPty()
    monkeypatch.setattr(pty_runner, "get_pty_supervisor", lambda: mock_pty)

    cfg = RemoteConfig(brain_dir=tmp_path, auth_token="token")
    mgr = SessionManager(cfg)
    mgr.active_conversation_id = "conv-1"

    # Prompt sent to matching active conversation -> delivered to pty
    res1 = await mgr.backend.send_prompt(mgr, "do this", conversation_id="conv-1")
    assert res1 == "pty"
    assert typed_prompts == ["do this"]

    # Prompt sent to a different conversation -> broadcasted, not typed into conv-1's pty
    res2 = await mgr.backend.send_prompt(mgr, "do that", conversation_id="conv-2")
    assert res2 == "broadcast"
    assert typed_prompts == ["do this"]  # did not receive "do that"


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
async def test_rename_overrides_titles_and_survives_a_restart(tmp_path: Path):
    """A user-chosen name wins over the derived one, and outlives the server.

    Titles are derived from transcripts, so two sessions that started the
    same way read identically in the drawer. The override is applied after
    the backend builds its summaries -- whatever the agent names things, the
    human's name is what the phone shows -- and persists in a small JSON
    store so a server restart does not take the names with it.
    """
    conv_id = "rename-me"
    log = tmp_path / conv_id / ".system_generated" / "logs" / "transcript.jsonl"
    log.parent.mkdir(parents=True)
    log.write_text(
        json.dumps({"step_index": 0, "type": "USER_INPUT", "source": "USER_INPUT", "content": "Hello agy"}) + "\n",
        encoding="utf-8",
    )

    cfg = RemoteConfig(brain_dir=tmp_path, auth_token="token")
    store = tmp_path / "titles.json"
    mgr = SessionManager(cfg, title_store=store)

    renamed = mgr.rename_conversation(conv_id, "My own name")
    assert renamed is not None
    assert renamed["title"] == "My own name"

    # The drawer list and the switch snapshot both name it the user's way.
    assert mgr.list_conversations()[0].title == "My own name"
    await mgr.switch_conversation(conv_id)
    assert mgr._summary_of(conv_id)["title"] == "My own name"

    # Unknown conversations have nothing to rename.
    assert mgr.rename_conversation("nope", "x") is None

    # A restart keeps the names the user chose.
    fresh = SessionManager(cfg, title_store=store)
    assert fresh.list_conversations()[0].title == "My own name"


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
async def test_zombie_sockets_of_one_device_count_once(tmp_path: Path):
    """The badge counts devices, not sockets.

    iOS suspends the PWA, drops its socket without a close frame, and the
    reload reconnects -- and a reconnect race can leave several sockets open
    for the same phone. Every socket the client keeps pinging is legitimately
    alive, so no amount of server-side reaping explains a badge that says
    twenty devices where one exists. What cannot lie is a per-device identity:
    the same device id on ten sockets is one phone.
    """
    cfg = RemoteConfig(brain_dir=tmp_path, auth_token="token", e2ee_enabled=False)
    mgr = SessionManager(cfg)

    phone_a = _FakeWebSocket()
    phone_a_zombie = _FakeWebSocket()
    phone_b = _FakeWebSocket()
    await mgr.register_client(phone_a, device_id="phone-a")
    await mgr.register_client(phone_a_zombie, device_id="phone-a")
    await mgr.register_client(phone_b, device_id="phone-b")

    peers = [f for f in phone_a.sent if f["event"] == "peers"]
    assert peers[-1]["data"]["count"] == 2, "three sockets from two devices must announce two devices"

    # A zombie reaped must not change the device count.
    mgr.unregister_client(phone_a_zombie)
    await mgr.announce_peers()
    assert [f for f in phone_a.sent if f["event"] == "peers"][-1]["data"]["count"] == 2


@pytest.mark.asyncio
async def test_sockets_without_a_device_id_each_count_as_one_device(tmp_path: Path):
    """Old clients (and tests) send no device id; they still count, once each."""
    cfg = RemoteConfig(brain_dir=tmp_path, auth_token="token", e2ee_enabled=False)
    mgr = SessionManager(cfg)

    identified = _FakeWebSocket()
    anonymous = _FakeWebSocket()
    await mgr.register_client(identified, device_id="phone-a")
    await mgr.register_client(anonymous)

    peers = [f for f in identified.sent if f["event"] == "peers"]
    assert peers[-1]["data"]["count"] == 2


@pytest.mark.asyncio
async def test_an_approval_while_a_phone_watches_also_surfaces_in_the_tui(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    """A permission gate must be visible where agy runs, not only on the phone.

    With no client connected the hook answers "ask" and agy prompts in its own
    terminal, exactly as if the hook did not exist. With a client connected the
    hook holds -- and the desktop terminal went silent: agy sat frozen on a
    question only the phone could see. Whoever was sitting at the terminal had
    no idea anything was waiting.
    """
    cfg = RemoteConfig(brain_dir=tmp_path, auth_token="token", e2ee_enabled=False)
    mgr = SessionManager(cfg)
    ws = _FakeWebSocket()
    await mgr.register_client(ws)

    spawned = []

    class _FakePopen:
        def __init__(self, cmd, env=None, **kwargs):
            spawned.append((cmd, env))

    monkeypatch.setattr("agy_remote.session_manager.subprocess.Popen", _FakePopen)
    monkeypatch.setattr(mgr, "is_pane_visible", lambda target: True)

    class _TmuxSupervisor:
        session_name = "agy-remote-8090"
        target = "agy-remote-8090:0.0"

    mgr._supervisors["conv-1"] = _TmuxSupervisor()
    await mgr.register_approval("ap-1", "conv-1", "bash", {"command": "rm -rf /"})

    assert spawned, "the tmux pane showed nothing while the hook held"
    cmd, env = spawned[0]
    assert "tui-approve" in " ".join(cmd)
    assert "AGY_REMOTE_APPROVAL_ID=ap-1" in " ".join(cmd)
    assert env["AGY_REMOTE_APPROVAL_ID"] == "ap-1"
    assert env["AGY_REMOTE_TOKEN"] == "token"

    # When the pane's window is inactive, popup MUST NOT spawn to avoid stealing focus
    spawned.clear()
    monkeypatch.setattr(mgr, "is_pane_visible", lambda target: False)
    await mgr.register_approval("ap-inactive", "conv-1", "bash", {"command": "ls"})
    assert not any("display-popup" in cmd for cmd, _ in spawned), (
        "popup must not open when target pane window is inactive"
    )

    # Origin pane passed explicitly from PreToolUse hook is targeted directly
    spawned.clear()
    monkeypatch.setattr(mgr, "is_pane_visible", lambda target: True)
    await mgr.register_approval("ap-origin", "conv-origin", "bash", {"command": "pwd"}, origin_pane="%42")
    popup_cmds = [cmd for cmd, _ in spawned if "display-popup" in cmd]
    assert popup_cmds, "popup must spawn for active origin pane"
    cmd = popup_cmds[0]
    idx = cmd.index("-t")
    assert cmd[idx + 1] == "%42", "popup must target exact originating pane"

    # A session without tmux (a server-owned pty) cannot host a popup; the
    # console still has to hear something, so the terminal bell rings.
    spawned.clear()

    class _PtySupervisor:
        running = True

    mgr._supervisors["conv-2"] = _PtySupervisor()
    await mgr.register_approval("ap-2", "conv-2", "bash", {"command": "ls"})
    assert not spawned, "a pty session must not be handed a tmux popup"
    assert "\a" in capsys.readouterr().out, "the console was not rung"


@pytest.mark.asyncio
async def test_a_pending_approval_can_be_polled_for_the_tui(tmp_path: Path):
    """The desktop popup needs to read what it is offering to approve."""
    cfg = RemoteConfig(brain_dir=tmp_path, auth_token="token", e2ee_enabled=False)
    mgr = SessionManager(cfg)

    assert mgr.pending_approval("nope") is None

    await mgr.register_approval("ap-1", "conv-1", "bash", {"command": "ls"})
    info = mgr.pending_approval("ap-1")
    assert info is not None
    assert info["tool_name"] == "bash"
    assert info["status"] == "pending"


@pytest.mark.asyncio
async def test_an_approval_nobody_can_see_is_not_held(tmp_path: Path):
    """A server must not hold an agy hostage for a banner nobody was shown.

    The PreToolUse hook blocks the agy that fired it until this returns. That
    is the point when a phone is watching *that* session -- wait, however long
    it takes. Every other case can only end one way: agy kills the hook after
    300s and the tool call fails.

    Two of them, both real:
      - no client connected (an always-on server's normal state),
      - a socket still open with nothing behind it.

    A client watching a *different* session is no longer one of them: those
    approvals are broadcast with the session that raised them, so whoever is
    connected can answer.

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

    # 2. A phone watching another session *is* asked now: the banner names the
    #    session that raised it, so it can be answered from anywhere. See
    #    test_an_approval_from_another_session_reaches_the_phone_named.
    mgr._connected_clients.add(_FakeWebSocket())
    mgr.active_conversation_id = "on-screen"
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(
            mgr.request_approval(
                approval_id="a2", conversation_id="some-other-session", tool_name="run_command", args={}
            ),
            timeout=0.5,
        )

    # 3. A socket that is open with nothing behind it.
    class _DeadSocket:
        async def send_json(self, data) -> None:  # noqa: ANN001
            raise ConnectionResetError("phone went to sleep")

    mgr._connected_clients.clear()
    mgr._connected_clients.add(_DeadSocket())
    mgr.active_conversation_id = "c1"
    dead = await ask("a3", "c1")
    assert dead["decision"] == "ask", dead
    # Nothing is left pending for it: a banner nobody received must not
    # reappear later as one somebody has to dismiss. (a2 is still pending on
    # purpose -- its agy is still blocked, and the phone can still answer it.)
    assert [a["id"] for a in mgr.get_active_pending_approvals()] == ["a2"]

    # 4. Someone is genuinely looking at this session: wait, as designed.
    mgr._connected_clients.clear()
    mgr._connected_clients.add(_FakeWebSocket())
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(
            mgr.request_approval(approval_id="a4", conversation_id="c1", tool_name="run_command", args={}),
            timeout=0.5,
        )


@pytest.mark.asyncio
async def test_an_approval_from_another_session_reaches_the_phone_named(tmp_path: Path):
    """Every session's approvals reach the phone; each says which it came from.

    Hiding another session's banner was the honest fix for an unattributed one:
    a bare `bash` request drawn into the transcript on screen reads as
    belonging to the work in front of you. But hiding it meant nobody could
    answer, so the hook was held for a banner that was never drawn -- and then
    answered locally to avoid the hang, which left "approve anything from
    anywhere" simply not working.

    Attribution is the fix. Broadcast every approval, and say whose it is.
    """
    cfg = RemoteConfig(brain_dir=tmp_path, auth_token="token", e2ee_enabled=False)
    mgr = SessionManager(cfg)
    mgr.active_conversation_id = "on-screen"
    ws = _FakeWebSocket()
    mgr._connected_clients.add(ws)

    # Anyone connected can be asked, whichever session is asking.
    assert mgr.can_hold_approval("elsewhere") is True
    assert mgr.can_hold_approval("on-screen") is True

    await mgr.register_approval(
        approval_id="a1",
        conversation_id="elsewhere",
        tool_name="run_command",
        args={"CommandLine": "rm -rf /"},
    )

    asked = [f for f in ws.sent if f.get("event") == "approval_request"]
    assert asked, f"another session's approval never reached the phone: {[f['event'] for f in ws.sent]}"
    assert asked[-1]["data"]["conversation_id"] == "elsewhere"

    # A client that reconnects must still find it, not just one that was
    # listening at the time.
    fresh = _FakeWebSocket()
    await mgr.register_client(fresh)
    snapshot = next(f for f in fresh.sent if f["event"] == "init")["data"]
    pending = snapshot["pending_approvals"]
    assert [a["conversation_id"] for a in pending] == ["elsewhere"], pending


@pytest.mark.asyncio
async def test_an_approval_says_which_session_it_came_from_by_name(tmp_path: Path):
    """A conversation id is not something anyone can recognise at 2am.

    The banner has to be answerable on its own: "rm -rf /" from
    `fe67ae68-b3b6-4918` tells you nothing about which of four terminals is
    waiting, and the phone cannot look the name up for a session it has never
    displayed.
    """
    conv_id = "conv-titled"
    log_dir = tmp_path / conv_id / ".system_generated" / "logs"
    log_dir.mkdir(parents=True)
    with open(log_dir / "transcript.jsonl", "w", encoding="utf-8") as f:
        step = {"step_index": 0, "type": "USER_INPUT", "source": "USER_INPUT", "content": "Fix the footer"}
        f.write(json.dumps(step) + "\n")

    cfg = RemoteConfig(brain_dir=tmp_path, auth_token="token", e2ee_enabled=False)
    mgr = SessionManager(cfg)
    ws = _FakeWebSocket()
    mgr._connected_clients.add(ws)

    await mgr.register_approval(
        approval_id="a1",
        conversation_id=conv_id,
        tool_name="run_command",
        args={"CommandLine": "rm -rf /"},
    )

    asked = next(f for f in ws.sent if f.get("event") == "approval_request")["data"]
    assert asked["conversation_title"] == "Fix the footer", asked


@pytest.mark.asyncio
async def test_answered_approvals_do_not_accumulate_forever(tmp_path: Path):
    """A server that runs for weeks keeps every approval it ever handled.

    Entries are marked allowed or denied and left in the dict. One a minute is
    half a million a year, each holding its tool arguments -- a command line, a
    file path, sometimes a whole diff. Nothing reads them once answered; the
    futures they carry are already resolved.
    """
    cfg = RemoteConfig(brain_dir=tmp_path, auth_token="token", e2ee_enabled=False)
    mgr = SessionManager(cfg)
    mgr.active_conversation_id = "c1"
    mgr._connected_clients.add(_FakeWebSocket())

    for i in range(60):
        await mgr.register_approval(
            approval_id=f"a{i}",
            conversation_id="c1",
            tool_name="run_command",
            args={"CommandLine": f"echo {i}"},
        )
        await mgr.resolve_approval(f"a{i}", ApprovalResponseRequest(decision="allow"))

    assert mgr.get_active_pending_approvals() == []
    assert len(mgr._pending_approvals) <= 32, f"kept {len(mgr._pending_approvals)} answered approvals"

    # The most recent answers survive: a client resolving one twice, or a late
    # `approval_resolved` arriving, must still find it rather than 404.
    assert "a59" in mgr._pending_approvals


# ---------------------------------------------------------------------------
# Zombie clients: iOS suspends the page mid-connection, kills the socket
# without a close frame, and the reload on return arrives as a brand-new
# connection. The old one reads open from the server side -- writes buffer
# successfully into a dead peer -- so the count in `peers` claimed six devices
# where two existed, and every sleep/reload cycle ratcheted it further.
# Liveness has to be judged by what the client last *said*, not by whether
# the socket is open.
# ---------------------------------------------------------------------------


class _ReapableWebSocket:
    def __init__(self):
        self.sent = []
        self.closed = []

    async def send_json(self, data):
        self.sent.append(data)

    async def close(self, code: int = 1000):
        self.closed.append(code)


@pytest.mark.asyncio
async def test_a_silent_client_is_reaped_and_the_count_corrects(tmp_path: Path):
    cfg = RemoteConfig(brain_dir=tmp_path, auth_token="token", e2ee_enabled=False)
    mgr = SessionManager(cfg)
    live = _ReapableWebSocket()
    zombie = _ReapableWebSocket()
    await mgr.register_client(live)
    await mgr.register_client(zombie)
    assert len(mgr._connected_clients) == 2

    # The heartbeats from one of them stopped -- Safari slept the page and the
    # socket was never closed -- while the other keeps talking.
    now = time.monotonic()
    mgr._client_last_seen[zombie] = now - 120.0
    mgr._client_last_seen[live] = now - 5.0

    removed = await mgr.reap_stale_clients()

    assert removed == 1
    assert zombie not in mgr._connected_clients
    assert live in mgr._connected_clients
    assert zombie.closed, "a reaped client is told why its socket went away"
    peers = [f for f in live.sent if f["event"] == "peers"]
    assert peers and peers[-1]["data"]["count"] == 1, "the survivors hear the corrected count"


@pytest.mark.asyncio
async def test_any_inbound_frame_counts_as_liveness(tmp_path: Path):
    cfg = RemoteConfig(brain_dir=tmp_path, auth_token="token", e2ee_enabled=False)
    mgr = SessionManager(cfg)
    ws = _ReapableWebSocket()
    await mgr.register_client(ws)
    mgr._client_last_seen[ws] = time.monotonic() - 120.0

    mgr.note_client_activity(ws)
    await mgr.reap_stale_clients()

    assert ws in mgr._connected_clients


@pytest.mark.asyncio
async def test_an_unstamped_client_is_not_reaped_on_sight(tmp_path: Path):
    """A client with no liveness stamp yet gets the benefit of the doubt.

    Reaping on first sight would drop every connection registered before the
    liveness tracking existed (and every test that adds a socket directly) --
    the stamp is recorded fresh on the first sweep, and the *next* sweep
    judges it.
    """
    cfg = RemoteConfig(brain_dir=tmp_path, auth_token="token", e2ee_enabled=False)
    mgr = SessionManager(cfg)
    ws = _ReapableWebSocket()
    mgr._connected_clients.add(ws)

    assert await mgr.reap_stale_clients() == 0
    assert ws in mgr._connected_clients

    # Enough silence after the first stamp, and it goes.
    stamp = mgr._client_last_seen[ws]
    mgr._client_last_seen[ws] = stamp - 120.0
    assert await mgr.reap_stale_clients() == 1
    assert ws not in mgr._connected_clients


# ---------------------------------------------------------------------------
# Host files for the phone: the transcript names files as
# [file:///abs/path], and the server runs on the machine that has them. A
# read endpoint is only safe if it refuses everything outside the sanctioned
# roots -- the registered sessions' workdirs and the projects root -- so a
# crafted reference can never read /etc/passwd or the operator's home dir.
# ---------------------------------------------------------------------------


@pytest.fixture
def workdir_mgr(tmp_path: Path) -> SessionManager:
    cfg = RemoteConfig(brain_dir=tmp_path / "brain", auth_token="token", e2ee_enabled=False)
    mgr = SessionManager(cfg)
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "classifier.ts").write_text("export const tiers = ['TRIAL'];\n")
    mgr.register_session(SessionRecord(id="s1", conversation_id="c1", workdir=str(repo)))
    return mgr


def test_host_file_reads_inside_a_registered_workdir(workdir_mgr: SessionManager, tmp_path: Path):
    result = workdir_mgr.read_host_file(f"{tmp_path}/repo/src/classifier.ts")
    assert result["name"] == "classifier.ts"
    assert result["content"] == "export const tiers = ['TRIAL'];\n"
    assert result["truncated"] is False
    assert result["path"].endswith("src/classifier.ts")


def test_file_uri_form_is_accepted(workdir_mgr: SessionManager, tmp_path: Path):
    result = workdir_mgr.read_host_file(f"file://{tmp_path}/repo/src/classifier.ts")
    assert result["name"] == "classifier.ts"


def test_host_file_refuses_paths_outside_every_root(workdir_mgr: SessionManager):
    with pytest.raises(PermissionError):
        workdir_mgr.read_host_file("/etc/hosts")


def test_host_file_refuses_traversal(workdir_mgr: SessionManager, tmp_path: Path):
    with pytest.raises(PermissionError):
        workdir_mgr.read_host_file(f"{tmp_path}/repo/src/../../../etc/hosts")


def test_host_file_refuses_symlink_escape(workdir_mgr: SessionManager, tmp_path: Path):
    secret = tmp_path / "secret.txt"
    secret.write_text("token")
    (tmp_path / "repo" / "src" / "link.ts").symlink_to(secret)
    with pytest.raises(PermissionError):
        workdir_mgr.read_host_file(str(tmp_path / "repo" / "src" / "link.ts"))


def test_host_file_reports_missing_files(workdir_mgr: SessionManager, tmp_path: Path):
    with pytest.raises(FileNotFoundError):
        workdir_mgr.read_host_file(f"{tmp_path}/repo/src/nope.ts")


def test_host_file_refuses_binary(workdir_mgr: SessionManager, tmp_path: Path):
    (tmp_path / "repo" / "src" / "blob.bin").write_bytes(b"\x00\x01\x02binary")
    with pytest.raises(ValueError):
        workdir_mgr.read_host_file(f"{tmp_path}/repo/src/blob.bin")


def test_host_file_truncates_large_files(workdir_mgr: SessionManager, tmp_path: Path):
    big = tmp_path / "repo" / "src" / "big.log"
    big.write_text("x" * (256 * 1024))
    result = workdir_mgr.read_host_file(str(big))
    assert result["truncated"] is True
    assert len(result["content"]) < 256 * 1024


def test_host_file_refuses_directories(workdir_mgr: SessionManager, tmp_path: Path):
    with pytest.raises(ValueError):
        workdir_mgr.read_host_file(f"{tmp_path}/repo/src")


def test_projects_root_is_also_allowed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """The supervised session's own repo lives under the projects root."""
    monkeypatch.setenv("AGY_REMOTE_PROJECTS_DIR", str(tmp_path / "projects"))
    root = tmp_path / "projects" / "harness"
    root.mkdir(parents=True)
    (root / "runner.ts").write_text("ready\n")

    cfg = RemoteConfig(brain_dir=tmp_path / "brain", auth_token="token", e2ee_enabled=False)
    mgr = SessionManager(cfg)
    result = mgr.read_host_file(str(root / "runner.ts"))
    assert result["content"] == "ready\n"

    # A sibling of the projects root is still nobody's business.
    with pytest.raises(PermissionError):
        mgr.read_host_file(str(tmp_path / "brain" / "transcript.jsonl"))


@pytest.mark.asyncio
async def test_rename_in_transcript_updates_conversation_title(tmp_path: Path):
    """When a user renames a session in the terminal via /rename, the summary reflects it."""
    log = _write_conversation(tmp_path, "conv-rename", "first question", time.time())
    with open(log, "a", encoding="utf-8") as f:
        f.write(
            json.dumps(
                {"step_index": 1, "type": "USER_INPUT", "source": "USER_INPUT", "content": "/rename Refactored Backend"}
            )
            + "\n"
        )

    cfg = RemoteConfig(brain_dir=tmp_path, auth_token="token", e2ee_enabled=False)
    mgr = SessionManager(cfg)
    summaries = mgr.list_conversations()
    conv = next(s for s in summaries if s.id == "conv-rename")
    assert conv.title == "Refactored Backend"
