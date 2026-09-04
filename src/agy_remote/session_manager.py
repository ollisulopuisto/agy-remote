"""Session management, log tailing, and live state synchronization.

The manager owns the agent-agnostic half: the WebSocket fan-out, the E2EE
sealing, the pending-approval state machine, the terminal mirror and the
watcher loop. Everything agent-specific (where steps come from, how a prompt
or a decision travels to the CLI) lives in a backend, see `backends.py`.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import shlex
import subprocess
import sys
import time
import uuid
from collections.abc import Awaitable, Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import WebSocket

from .backends import AgentBackend, make_backend, parse_ask_question_args
from .config import RUNTIME_STATE_FILE, RemoteConfig, get_config
from .crypto import ReplayGuard, decode_key, encrypt_payload
from .loop_guard import LoopGuard
from .mailbox import format_envelope, mailbox_dir, parse_message_line
from .meta_agy import MetaAgyClient
from .models import (
    AgentRecord,
    ApprovalResponseRequest,
    ConversationSummary,
    SessionRecord,
    TranscriptStep,
)
from .screen import TerminalMirror
from .spawner import projects_root

logger = logging.getLogger("agy_remote.session")

#: A client that has said nothing for this long is gone, whatever its socket
#: claims. The PWA heartbeats every 15 s, so this tolerates three missed beats;
#: a suspended iOS Safari kills its socket without a close frame, and a write
#: into the dead peer buffers successfully -- only silence reveals it.
STALE_CLIENT_SECONDS = 45.0

#: The most of a file /api/file will put in one JSON response. A transcript
#: reference can name a build log; the phone wants the top of it, not all of it.
MAX_FILE_BYTES = 128 * 1024

#: Where the user's session names live. Derived titles come and go with the
#: transcripts; a name the operator typed should outlive the server.
DEFAULT_TITLE_STORE = RUNTIME_STATE_FILE.parent / "agy-remote-titles.json"

#: How long after a session's last transcript step it still counts as busy.
#: The PWA's own drawer uses the same window (`busyWindowMs`), so the chip and
#: the dot cannot disagree. A tool that runs in silence for longer than this
#: looks like a finished turn; that is the honest failure mode of a
#: quiescence heuristic, and typing one prompt into a still-running turn is
#: what every terminal agent absorbs anyway.
BUSY_WINDOW_SECONDS = 30.0

#: How long one `git diff` may take before the phone gets an error instead.
GIT_DIFF_TIMEOUT_SECONDS = 10.0


class SessionManager:
    """Manages active agent conversations and real-time streaming."""

    def __init__(
        self,
        config: RemoteConfig | None = None,
        backend: AgentBackend | None = None,
        push_manager: Any = None,
        title_store: Path | None = None,
        meta_agy_client: MetaAgyClient | None = None,
    ) -> None:
        self.config = config or get_config()
        self.backend = backend or make_backend(self.config)
        self.push_manager = push_manager
        self.meta_agy = meta_agy_client or MetaAgyClient(
            base_url=self.config.meta_agy_url,
            token=self.config.meta_agy_token,
        )
        self._meta_jobs: dict[str, AgentRecord] = {}
        self._meta_job_outputs: dict[str, str] = {}
        self._last_meta_poll: float = 0.0
        self.active_conversation_id: str | None = None
        #: The conversation ID belonging to the supervised agy process for this server.
        self.supervised_conversation_id: str | None = None
        #: Track whichever conversation is newest, until the user picks one.
        self.follow_latest: bool = True
        self.active_steps: list[TranscriptStep] = []
        self._connected_clients: set[WebSocket] = set()
        #: The last time each client said anything the server could hear.
        #: A socket reads open long after its peer is gone; this, not the
        #: socket state, is what the reaper judges liveness by.
        self._client_last_seen: dict[WebSocket, float] = {}
        #: The per-device identity each socket claims, when it sent one. The
        #: peer count is devices, not sockets: reconnect races and suspended
        #: reload zombies can hold several open sockets for one phone, and all
        #: of them ping, so counting sockets said "20 devices" where one sat.
        self._client_devices: dict[WebSocket, str | None] = {}
        #: Called before the first client's snapshot when the server is
        #: listening with nothing behind it. An always-on server has no agy
        #: until someone wants one; this is where one appears.
        self.ensure_session: Callable[[], Awaitable[None]] | None = None
        self._pending_approvals: dict[str, dict[str, Any]] = {}
        #: How many answered approvals to keep. A late `approval_resolved`, or
        #: a second tap on a banner, has to find its approval rather than a
        #: 404 -- but a server that runs for weeks must not hoard every command
        #: line it ever relayed.
        self._answered_history = 32
        self._approval_futures: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._watcher_task: asyncio.Task[None] | None = None
        #: Mirror of the supervised terminal, when a session is supervised.
        self.terminal: TerminalMirror | None = None
        self._running: bool = False
        #: Track active focus status per connected WebSocket client
        self._client_focus: dict[WebSocket, dict[str, Any]] = {}
        #: Event sequencing and replay buffer for transient mobile reconnects (Phase 3)
        self._event_seq: int = 0
        self._outbox: list[tuple[int, dict[str, Any]]] = []
        self._outbox_max_size: int = 500
        #: Multi-session registry (Phase 1.1)
        self._sessions: dict[str, SessionRecord] = {}
        self._supervisors: dict[str, Any] = {}
        self._conversation_panes: dict[str, str] = {}
        self._terminal_mirrors: dict[str, TerminalMirror] = {}
        #: Byte offset already delivered from each session's inbox. A session
        #: is first seen at its inbox's current end: delivery is for what
        #: agents write while we are watching, and replaying a prompt an agent
        #: already handled would start a conversation the human never sent.
        self._inbox_pos: dict[str, int] = {}
        #: Loop protection for the agent-to-agent mailbox: a sliding rate
        #: window per pair plus a ping-pong latch that pauses a pair (and
        #: raises an alert) when two agents run in circles without a human.
        self.loop_guard = LoopGuard(
            rate_limit=self.config.mailbox_rate_limit,
            window_seconds=self.config.mailbox_rate_window_seconds,
            ping_pong_limit=self.config.mailbox_ping_pong_limit,
        )
        #: Titles the user typed, keyed by conversation id. The backend derives
        #: titles from transcripts -- two sessions started the same way read
        #: identically in the drawer -- so a name the operator chose is stored
        #: here and applied after every summary the backend builds.
        self.title_store = Path(title_store) if title_store else DEFAULT_TITLE_STORE
        self._title_overrides: dict[str, str] = {}
        self._load_title_overrides()
        #: The last time each conversation streamed a step (`time.monotonic()`).
        #: A prompt arriving while a session is mid-turn must not be typed into
        #: a running stream -- it queues, like opencode's follow-up dock.
        self._last_activity: dict[str, float] = {}
        #: Per-conversation FIFO of prompts waiting for the agent's turn to
        #: end. Head delivers first; cancel removes by id.
        self._prompt_queues: dict[str, list[dict[str, Any]]] = {}
        #: Overridable in tests; production uses the module default.
        self.busy_window_seconds: float = BUSY_WINDOW_SECONDS

        # Key material for sealing every frame we put on the wire. Derived once
        # so a malformed key fails loudly at startup rather than per-message.
        self._key_bytes: bytes | None = None
        if self.config.e2ee_enabled:
            self._key_bytes = decode_key(self.config.e2ee_key)
        #: Nonce cache for envelopes arriving *from* clients.
        self.replay_guard = ReplayGuard()

    @property
    def parse_count(self) -> int:
        """Transcripts parsed by the backend; asserted on in tests."""
        return getattr(self.backend, "parse_count", 0)

    def seal(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Wrap an outbound payload in an AES-GCM envelope when E2EE is on.

        Every server-to-client frame goes through here. Transcript content,
        tool arguments and diffs are the most sensitive data this app handles,
        so none of it may reach the socket in cleartext.
        """
        if self._key_bytes is None:
            return payload
        return encrypt_payload(payload, self._key_bytes)

    async def send_to(self, websocket: WebSocket, payload: dict[str, Any]) -> None:
        """Seal and deliver a single payload to one client."""
        await websocket.send_json(self.seal(payload))

    async def start(self) -> None:
        """Start the session manager and background watcher loop."""
        self._running = True
        await self.backend.start(self)
        # Find the latest conversation
        latest = self.get_latest_conversation_id()
        if latest:
            await self.switch_conversation(latest)
        with contextlib.suppress(Exception):
            await self.poll_meta_agy()
        self._watcher_task = asyncio.create_task(self._watch_loop())

    async def stop(self) -> None:
        """Stop watcher task and clean up."""
        self._running = False
        if self._watcher_task:
            self._watcher_task.cancel()
            import contextlib

            with contextlib.suppress(asyncio.CancelledError):
                await self._watcher_task
        await self.backend.stop()

    def list_conversations(self) -> list[ConversationSummary]:
        """All known conversations, newest first, under the user's names."""
        summaries = self.backend.list_conversations(self)
        for summary in summaries:
            if summary.id in self._title_overrides:
                summary.title = self._title_overrides[summary.id]
        return summaries

    def _load_title_overrides(self) -> None:
        """Read the stored names back, tolerating a missing or damaged file."""
        try:
            data = json.loads(self.title_store.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except Exception as e:  # noqa: BLE001 - a bad store must not kill startup
            logger.warning("Could not read the session titles store %s: %s", self.title_store, e)
            return
        if isinstance(data, dict):
            self._title_overrides = {str(k): str(v) for k, v in data.items() if str(v).strip()}

    def _save_title_overrides(self) -> None:
        """Persist the names, owner-only like the rest of the runtime state."""
        try:
            self.title_store.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(self.title_store, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(self._title_overrides, f, indent=2)
        except Exception as e:  # noqa: BLE001 - a failed save must not fail the rename
            logger.warning("Could not save the session titles store %s: %s", self.title_store, e)

    def rename_conversation(self, conversation_id: str, title: str) -> dict[str, Any] | None:
        """Record the user's name for a conversation and return its summary.

        None means there was nothing to rename: an unknown conversation, or a
        title that is only whitespace. The stored name is what every later
        summary reports -- derived titles carry on changing underneath, but the
        phone shows the name its operator chose.
        """
        title = title.strip()
        if not title or not self.backend.is_known_conversation(conversation_id):
            return None

        self._title_overrides[conversation_id] = title
        self._save_title_overrides()

        summary = self._summary_of(conversation_id)
        if summary is None:
            summary = {"id": conversation_id, "title": title}
        summary["title"] = title
        return summary

    def get_latest_conversation_id(self) -> str | None:
        """The most recently updated conversation ID, cheaply."""
        return self.backend.get_latest_conversation_id()

    def get_newest_conversation_id(self) -> str | None:
        """The most recently created conversation ID, cheaply."""
        if hasattr(self.backend, "get_newest_conversation_id"):
            return self.backend.get_newest_conversation_id()
        return self.backend.get_latest_conversation_id()

    def get_transcript_path(self, conversation_id: str) -> Path | None:
        """Where the conversation lives on disk, or None for API-backed agents."""
        return self.backend.get_transcript_path(conversation_id)

    async def switch_conversation(self, conversation_id: str, pin: bool = False) -> bool:
        """Switch active conversation to the specified ID and load steps.

        `pin` marks the choice as the user's own, made from the phone. Picking
        an older session then stops the watcher from dragging the view forward
        the moment a new one appears; picking the newest resumes following.
        """
        if pin:
            newest_id = self.get_newest_conversation_id() or self.get_latest_conversation_id()
            self.follow_latest = conversation_id == newest_id

        self.active_conversation_id = conversation_id
        self.active_steps = []
        self.backend.on_switch(conversation_id)
        self.active_steps = await self.backend.load_steps(self, conversation_id)

        await self.broadcast(
            {
                "event": "session_switched",
                "data": {
                    "conversation_id": conversation_id,
                    # Which session this is, so a client can say so rather than
                    # letting a new one look like more of the last one.
                    "conversation": self._summary_of(conversation_id),
                    "conversations": [c.model_dump(mode="json") for c in self.list_conversations()],
                    "steps": [step.model_dump() for step in self.active_steps],
                    "pending_approvals": self.get_active_pending_approvals(),
                    "usage": self.get_usage_hud(conversation_id),
                },
            }
        )
        return True

    def _summary_of(self, conversation_id: str | None) -> dict[str, Any] | None:
        """The summary for one conversation, as clients need it to name a session."""
        summary = self.backend.summary_of(self, conversation_id)
        if summary and conversation_id in self._title_overrides:
            summary["title"] = self._title_overrides[conversation_id]
        return summary

    def _forget_old_answers(self) -> None:
        """Drop the oldest answered approvals, keeping the recent ones.

        Nothing reads an approval once it is answered -- its future is already
        resolved -- but a client can tap a banner twice, or a resolution can
        cross with one already in flight, and both should find it rather than a
        404. Recent history is enough for that; the rest is a slow leak of
        command lines and file paths in a process that never restarts.
        """
        answered = [aid for aid, app in self._pending_approvals.items() if app.get("status") != "pending"]
        for aid in answered[: max(0, len(answered) - self._answered_history)]:
            self._pending_approvals.pop(aid, None)

    def get_active_pending_approvals(self) -> list[dict[str, Any]]:
        """Every approval still waiting, from whichever session raised it.

        A client that reconnects has to find the ones it missed. Filtering
        these to the session on screen meant an agy in another window sat
        blocked on a request the phone could not see, and a reconnect did not
        recover it -- the approval was simply lost until it timed out.
        """
        return [app for app in self._pending_approvals.values() if app.get("status") == "pending"]

    def _init_data(self) -> dict[str, Any]:
        """The snapshot a new client receives on connect, as a plain dict.

        Kept in one place so a client arriving mid-session (and these tests)
        see exactly what the socket will hand over -- including the
        agent-to-agent traffic counters, so a phone that pairs late is not
        blind to a loop that is already running.
        """
        return {
            "event": "init",
            "data": {
                # Which agent CLI is behind this server; the PWA adapts its
                # quick actions and approval buttons to it.
                "agent": self.backend.name,
                "active_conversation_id": self.active_conversation_id,
                "steps": [step.model_dump() for step in self.active_steps],
                "conversations": [c.model_dump(mode="json") for c in self.list_conversations()],
                "pending_approvals": self.get_active_pending_approvals(),
                "conversation": self._summary_of(self.active_conversation_id),
                # A client that connects mid-panel must see the panel, not wait
                # for the next redraw that may never come.
                "terminal": self.terminal.snapshot() if self.terminal else None,
                # Which agent-to-agent pairs are chattering, looping, or muted.
                "agent_traffic": self.agent_traffic(),
                # Unified agent list (native Antigravity sessions + meta-AGY worker jobs)
                "agents": [a.model_dump(mode="json") for a in self.list_cached_agents()],
                # Monotonic event sequence id for replay
                "last_seq": self._event_seq,
                # Context, token & cost HUD data
                "usage": self.get_usage_hud(),
            },
        }

    async def register_client(self, websocket: WebSocket, device_id: str | None = None) -> None:
        """Register a new WebSocket client and send initial snapshot."""
        if self.ensure_session is not None and not self._connected_clients:
            # Only for the first arrival: later ones join what is already
            # running rather than each starting an agent of their own.
            try:
                await self.ensure_session()
            except Exception as e:
                logger.warning("Could not start a session for the arriving client: %s", e)

        self._connected_clients.add(websocket)
        self._client_last_seen[websocket] = time.monotonic()
        self._client_devices[websocket] = device_id
        # Send full snapshot of current state
        init_data = self._init_data()
        try:
            await websocket.send_json(self.seal(init_data))
        except Exception as e:
            logger.debug("Failed sending init payload to websocket: %s", e)

        await self.announce_peers()

    async def announce_peers(self) -> None:
        """Tell every client how many are connected.

        Access here is all-or-nothing: every client holds the same host-wide
        token, so there is no per-device identity to audit afterwards and no
        way to revoke one device without revoking them all. That makes an
        unexpected connection the only observable sign that the pairing URL has
        escaped -- and it is only observable if somebody says so.

        The count is devices, not sockets. A phone that reloads mid-suspension
        can leave several sockets open behind it -- each one legitimately
        pinged by its own client-side heartbeat -- and a socket count turned
        the badge into a ratchet. Sockets that claim no device id (an old
        client, a test) still count, one apiece.
        """
        devices = {d for d in self._client_devices.values() if d}
        anonymous = sum(1 for d in self._client_devices.values() if not d)
        await self.broadcast({"event": "peers", "data": {"count": len(devices) + anonymous}})

    def set_client_focus(self, websocket: WebSocket, focused: bool, conversation_id: str | None = None) -> None:
        """Track whether a client window is actively focused and which conversation it is viewing."""
        if not focused:
            self._client_focus.pop(websocket, None)
        else:
            self._client_focus[websocket] = {"focused": True, "conversation_id": conversation_id}

    def is_client_focused(self, conversation_id: str | None = None) -> bool:
        """Check if any connected client has active focus on the app (optionally on conversation_id)."""
        if not self._client_focus:
            return False
        if conversation_id is None or conversation_id == "default":
            return any(info.get("focused") for info in self._client_focus.values())
        return any(
            info.get("focused")
            and (info.get("conversation_id") is None or info.get("conversation_id") == conversation_id)
            for info in self._client_focus.values()
        )

    def unregister_client(self, websocket: WebSocket) -> None:
        """Remove a disconnected WebSocket client."""
        self._connected_clients.discard(websocket)
        self._client_last_seen.pop(websocket, None)
        self._client_devices.pop(websocket, None)
        self._client_focus.pop(websocket, None)

    def note_client_activity(self, websocket: WebSocket) -> None:
        """Record that this client just said something the server heard.

        Every inbound frame proves liveness -- not only the heartbeat -- so a
        chatty client is never reaped mid-conversation.
        """
        self._client_last_seen[websocket] = time.monotonic()

    async def reap_stale_clients(self) -> int:
        """Drop clients whose heartbeats stopped, and correct the peer count.

        iOS suspends a backgrounded PWA and kills its socket without a close
        frame; the reload on return arrives as a brand-new connection while the
        old one still reads open here, and writes into the dead peer buffer
        successfully rather than failing. Judging by socket state alone let the
        device count ratchet upward with every sleep/reload cycle -- the badge
        said six devices where two existed.

        A client with no stamp yet (registered before liveness tracking, or a
        socket added directly in tests) is stamped fresh on the first sweep and
        judged on the next one.
        """
        now = time.monotonic()
        stale: list[WebSocket] = []
        for ws in list(self._connected_clients):
            last = self._client_last_seen.get(ws)
            if last is None:
                self._client_last_seen[ws] = now
                continue
            if now - last > STALE_CLIENT_SECONDS:
                stale.append(ws)

        for ws in stale:
            self._connected_clients.discard(ws)
            self._client_last_seen.pop(ws, None)
            self._client_devices.pop(ws, None)
            self._client_focus.pop(ws, None)
            try:
                await ws.close(code=1000)  # normal closure: the server moved on
            except Exception as e:  # noqa: BLE001 - the socket was the problem
                logger.debug("Closing reaped client failed: %s", e)

        if stale:
            logger.info("Reaped %d silent client(s): no frame for %.0fs", len(stale), STALE_CLIENT_SECONDS)
            # The count is the alarm; the survivors must hear the correction.
            await self.announce_peers()
        return len(stale)

    async def broadcast(self, payload: dict[str, Any]) -> None:
        """Send JSON payload to all active WebSocket clients, recording in the replay outbox."""
        self._event_seq += 1
        seq_payload = {**payload, "seq": self._event_seq}

        self._outbox.append((self._event_seq, seq_payload))
        if len(self._outbox) > self._outbox_max_size:
            self._outbox.pop(0)

        if not self._connected_clients:
            return

        envelope = self.seal(seq_payload)

        to_remove = set()
        for ws in self._connected_clients:
            try:
                await ws.send_json(envelope)
            except Exception:
                to_remove.add(ws)

        for ws in to_remove:
            self._connected_clients.discard(ws)

    async def replay_since(self, websocket: WebSocket, since_seq: int) -> bool:
        """Replay missed events from the outbox to a newly reconnected client.

        Returns True if all missed events were successfully replayed, or False
        if the client has fallen behind the bounded outbox buffer (meaning
        the client must perform a full state re-sync via `init`).
        """
        if not self._outbox:
            return True
        earliest_seq = self._outbox[0][0]
        if since_seq < earliest_seq - 1:
            return False

        for seq, evt in self._outbox:
            if seq > since_seq:
                await self.send_to(websocket, evt)
        return True

    def get_usage_hud(self, conversation_id: str | None = None) -> dict[str, Any]:
        """Get context window tokens, cost, model, and execution mode HUD data."""
        conv_id = conversation_id or self.active_conversation_id
        result: dict[str, Any] = {
            "conversation_id": conv_id,
            "agent": self.backend.name,
            "model": None,
            "mode": None,
            "context_tokens": None,
            "context_limit": None,
            "context_percent": None,
            "cost": None,
            "total_steps": len(self.active_steps) if conv_id == self.active_conversation_id else 0,
        }

        mirror = self.get_screen_mirror(conv_id)
        if mirror:
            snap = mirror.snapshot()
            usage = snap.get("usage") or {}
            result.update({k: v for k, v in usage.items() if v is not None})
            if snap.get("mode"):
                result["mode"] = snap.get("mode")

        if not result.get("model") and conv_id and conv_id in self._meta_jobs:
            job = self._meta_jobs[conv_id]
            result["model"] = job.model or job.provider
            result["provider"] = job.provider

        return result

    def register_session(
        self,
        record: SessionRecord,
        supervisor: Any = None,
        mirror: TerminalMirror | None = None,
    ) -> None:
        """Register a session and its supervisor/mirror in the multi-session registry."""
        self._sessions[record.id] = record
        if supervisor is not None:
            self._supervisors[record.id] = supervisor
        if mirror is not None:
            self._terminal_mirrors[record.id] = mirror

        # If this session carries a conversation_id and none is active/supervised, adopt it
        if record.conversation_id:
            if not self.supervised_conversation_id:
                self.supervised_conversation_id = record.conversation_id
            if not self.active_conversation_id:
                self.active_conversation_id = record.conversation_id

    def unregister_session(self, session_id: str) -> None:
        """Remove a session from the multi-session registry."""
        self._sessions.pop(session_id, None)
        self._supervisors.pop(session_id, None)
        self._terminal_mirrors.pop(session_id, None)
        self._inbox_pos.pop(session_id, None)

    def remove_session(self, session_id: str) -> None:
        """Alias for unregister_session."""
        self.unregister_session(session_id)

    def list_sessions(self) -> list[SessionRecord]:
        """All currently registered supervised sessions."""
        return list(self._sessions.values())

    def get_session(self, key: str | None = None) -> SessionRecord | None:
        """Find a session record by id, conversation_id, or tmux_name."""
        if not key:
            if self.active_conversation_id and self.active_conversation_id in self._sessions:
                return self._sessions[self.active_conversation_id]
            for s in self._sessions.values():
                if s.conversation_id == self.active_conversation_id:
                    return s
            return next(iter(self._sessions.values()), None)

        if key in self._sessions:
            return self._sessions[key]
        for s in self._sessions.values():
            if s.conversation_id == key or s.tmux_name == key:
                return s
        return None

    def get_session_by_conversation(self, conversation_id: str) -> SessionRecord | None:
        """Find a session record by its Antigravity conversation ID."""
        return self.get_session(conversation_id)

    # -------------------------------------------------------------------------
    # Generalized Agent & Meta-AGY Integration
    # -------------------------------------------------------------------------

    def _antigravity_agent_records(self) -> list[AgentRecord]:
        """Represent native Antigravity sessions and conversations as generalized AgentRecord."""
        records: list[AgentRecord] = []
        pending_approvals = self.get_active_pending_approvals()
        pending_by_conv: dict[str, int] = {}
        for p in pending_approvals:
            cid = p.get("conversation_id")
            if cid:
                pending_by_conv[cid] = pending_by_conv.get(cid, 0) + 1

        conversations = self.list_conversations()
        seen_convs = set()

        for conv in conversations:
            seen_convs.add(conv.id)
            sess = self.get_session_by_conversation(conv.id)
            workdir = str(sess.workdir) if (sess and sess.workdir) else None
            project_name = Path(workdir).name if workdir else conv.title

            if pending_by_conv.get(conv.id, 0) > 0:
                status = "needs_attention"
            elif self.is_conversation_busy(conv.id):
                status = "running"
            else:
                status = "completed"

            model = None
            if self.terminal:
                snap = self.terminal.snapshot()
                mode = snap.get("mode")
                if mode:
                    model = mode

            records.append(
                AgentRecord(
                    agent_id=conv.id,
                    backend="antigravity",
                    provider="antigravity",
                    model=model,
                    project=project_name,
                    workspace=workdir,
                    current_task=conv.title,
                    status=status,
                    started_at=conv.created_at,
                    last_activity=conv.updated_at,
                )
            )

        for sid, sess in self._sessions.items():
            if sess.conversation_id and sess.conversation_id in seen_convs:
                continue
            workdir = str(sess.workdir) if sess.workdir else None
            project_name = Path(workdir).name if workdir else sess.id
            status = "running" if sess.busy else "completed"
            records.append(
                AgentRecord(
                    agent_id=sess.conversation_id or sid,
                    backend="antigravity",
                    provider="antigravity",
                    model=None,
                    project=project_name,
                    workspace=workdir,
                    current_task=f"Session {sess.tmux_name or sid}",
                    status=status,
                    started_at=sess.created_at,
                    last_activity=sess.last_activity_at,
                )
            )

        return records

    def _sort_agent_records(self, records: list[AgentRecord]) -> list[AgentRecord]:
        """Sort agents: attention-needed & running first, then completed and failed."""
        status_priority = {
            "needs_attention": 0,
            "running": 1,
            "completed": 2,
            "failed": 3,
            "cancelled": 4,
        }

        def sort_key(rec: AgentRecord):
            prio = status_priority.get(rec.status, 5)
            ts = 0.0
            t_val = rec.last_activity or rec.started_at
            if isinstance(t_val, datetime):
                ts = t_val.timestamp()
            elif isinstance(t_val, str):
                try:
                    ts = datetime.fromisoformat(t_val).timestamp()
                except Exception:
                    ts = 0.0
            return (prio, -ts)

        return sorted(records, key=sort_key)

    def list_cached_agents(self) -> list[AgentRecord]:
        """Combined list of native and meta-AGY agents from local cache."""
        all_records = self._antigravity_agent_records() + list(self._meta_jobs.values())
        return self._sort_agent_records(all_records)

    async def list_agents(self) -> list[AgentRecord]:
        """Fresh list of all agents, polling meta-AGY if reachable."""
        await self.poll_meta_agy()
        return self.list_cached_agents()

    async def get_agent(self, agent_id: str) -> AgentRecord | None:
        """Find an agent record by ID across meta-AGY and native sessions."""
        if agent_id in self._meta_jobs:
            return self._meta_jobs[agent_id]
        try:
            job = await self.meta_agy.get_job(agent_id)
            if job:
                self._meta_jobs[job.agent_id] = job
                return job
        except Exception:
            pass

        for agent in self._antigravity_agent_records():
            if agent.agent_id == agent_id:
                return agent
        return None

    async def get_agent_output(self, agent_id: str, offset: int = 0) -> tuple[str, int]:
        """Fetch incremental output for an agent. Returns (content, next_offset)."""
        agent = await self.get_agent(agent_id)
        if agent and agent.backend == "meta-agy":
            content, next_offset = await self.meta_agy.get_output(agent_id, offset)
            prev_out = self._meta_job_outputs.get(agent_id, "")
            if offset == 0:
                self._meta_job_outputs[agent_id] = content
            elif content:
                self._meta_job_outputs[agent_id] = prev_out + content
            return content, next_offset

        # For native antigravity sessions:
        if self.terminal:
            snap = self.terminal.snapshot()
            full_text = "\n".join(snap.get("lines", []))
            encoded = full_text.encode("utf-8")
            if offset >= len(encoded):
                return "", len(encoded)
            slice_bytes = encoded[offset:]
            return slice_bytes.decode("utf-8", errors="replace"), len(encoded)
        return "", offset

    async def submit_meta_job(
        self,
        project: str,
        task: str,
        provider: str = "gemini",
        model: str | None = None,
        context: str | None = None,
    ) -> AgentRecord:
        """Submit a structured job to meta-AGY."""
        job = await self.meta_agy.submit_job(
            project=project,
            task=task,
            provider=provider,
            model=model,
            context=context,
        )
        self._meta_jobs[job.agent_id] = job
        if self.push_manager:
            self.push_manager.send_notification(
                f"Agent Started: [{provider}] {project}",
                task,
                data={"agent_id": job.agent_id, "backend": "meta-agy", "status": "running"},
            )
        await self.broadcast(
            {
                "event": "agent_updated",
                "data": {"agent": job.model_dump(mode="json")},
            }
        )
        return job

    async def cancel_agent(self, agent_id: str) -> bool:
        """Stop or cancel an agent."""
        agent = await self.get_agent(agent_id)
        if agent and agent.backend == "meta-agy":
            success = await self.meta_agy.cancel_job(agent_id)
            if success and agent_id in self._meta_jobs:
                updated_job = self._meta_jobs[agent_id].model_copy(update={"status": "cancelled"})
                self._meta_jobs[agent_id] = updated_job
                if self.push_manager:
                    self.push_manager.send_notification(
                        f"Agent Cancelled: [{updated_job.provider}] {updated_job.project or agent_id}",
                        "Task was cancelled by operator.",
                        data={"agent_id": agent_id, "backend": "meta-agy", "status": "cancelled"},
                    )
                await self.broadcast(
                    {
                        "event": "agent_updated",
                        "data": {"agent": updated_job.model_dump(mode="json")},
                    }
                )
            return success

        # Native session stop
        from .keys import send_key_to_supervisor

        supervisor = self.get_supervisor(agent_id)
        if supervisor:
            send_key_to_supervisor(supervisor, "escape")
            return True
        return False

    async def retry_agent(self, agent_id: str) -> AgentRecord:
        """Retry a failed or completed meta-AGY agent job."""
        job = await self.meta_agy.retry_job(agent_id)
        self._meta_jobs[job.agent_id] = job
        if self.push_manager:
            self.push_manager.send_notification(
                f"Agent Retried: [{job.provider}] {job.project or agent_id}",
                job.current_task or "Retrying task",
                data={"agent_id": job.agent_id, "backend": "meta-agy", "status": job.status},
            )
        await self.broadcast(
            {
                "event": "agent_updated",
                "data": {"agent": job.model_dump(mode="json")},
            }
        )
        return job

    async def poll_meta_agy(self) -> list[AgentRecord]:
        """Poll meta-AGY for updates, detect status transitions, and notify."""
        try:
            jobs = await self.meta_agy.list_jobs()
        except Exception as exc:
            logger.debug("Failed polling meta-AGY: %s", exc)
            return list(self._meta_jobs.values())

        updated = False
        for job in jobs:
            prev = self._meta_jobs.get(job.agent_id)
            if prev is not None and prev.status != job.status:
                updated = True
                if self.push_manager and not self.is_client_focused(job.agent_id):
                    if job.status == "completed":
                        self.push_manager.send_notification(
                            f"Agent Completed: [{job.provider}] {job.project or 'Job'}",
                            job.current_task or "Task finished successfully.",
                            data={"agent_id": job.agent_id, "backend": "meta-agy", "status": "completed"},
                        )
                    elif job.status == "failed":
                        self.push_manager.send_notification(
                            f"Agent Failed: [{job.provider}] {job.project or 'Job'}",
                            job.current_task or "Job execution failed.",
                            data={"agent_id": job.agent_id, "backend": "meta-agy", "status": "failed"},
                        )
                    elif job.status == "needs_attention":
                        self.push_manager.send_notification(
                            f"Agent Needs Attention: [{job.provider}] {job.project or 'Job'}",
                            job.current_task or "Operator attention required.",
                            data={"agent_id": job.agent_id, "backend": "meta-agy", "status": "needs_attention"},
                        )
            elif prev is None:
                updated = True

            self._meta_jobs[job.agent_id] = job

        if updated:
            await self.broadcast(
                {
                    "event": "agent_updated",
                    "data": {"agents": [a.model_dump(mode="json") for a in self.list_cached_agents()]},
                }
            )
        return list(self._meta_jobs.values())

    def get_supervisor(self, key: str | None = None) -> Any | None:
        """Find supervisor for session key, conversation_id, or active session."""
        session = self.get_session(key)
        if session and session.id in self._supervisors:
            return self._supervisors[session.id]

        if key and key in self._supervisors:
            return self._supervisors[key]

        # Do not fall back to active global supervisor if targeting a different conversation
        if key is not None and key != self.active_conversation_id and key != self.supervised_conversation_id:
            return None

        from .pty_runner import get_pty_supervisor
        from .tmux_runner import get_tmux_supervisor

        tmux = get_tmux_supervisor(session.tmux_name if session else None)
        if tmux and tmux.has_session():
            return tmux

        pty = get_pty_supervisor()
        if pty and pty.running:
            return pty

        return None

    def get_screen_mirror(self, key: str | None = None) -> TerminalMirror | None:
        """Find TerminalMirror for session key, conversation_id, or active session."""
        session = self.get_session(key)
        if session and session.id in self._terminal_mirrors:
            return self._terminal_mirrors[session.id]

        if key and key in self._terminal_mirrors:
            return self._terminal_mirrors[key]

        if key is not None and key != self.active_conversation_id and key != self.supervised_conversation_id:
            return None

        return self.terminal

    def _mailbox_name(self, from_id: str | None) -> str:
        """The sender's name for an envelope: a known session's name, else the raw id."""
        if not from_id:
            return "unknown"
        session = self._sessions.get(from_id)
        if session and session.tmux_name:
            return session.tmux_name
        return from_id

    # ---------------------------------------------------------------------
    # Agent-to-agent loop protection (W2, item 2.3)
    # ---------------------------------------------------------------------
    def agent_traffic(self) -> list[dict[str, object]]:
        """Every mailbox pair the guard knows, for the PWA and the API."""
        return self.loop_guard.pair_stats()

    def mute_mailbox_pair(self, a: str, b: str) -> None:
        """Deliberately go dark on one pair until a human unmutes it."""
        self.loop_guard.mute_pair(a, b)

    def unmute_mailbox_pair(self, a: str, b: str) -> None:
        """Reopen a muted pair; its held mail starts flowing again."""
        self.loop_guard.unmute_pair(a, b)

    def note_human_prompt(self, key: str | None) -> None:
        """A human's own words reached a session: the loop record resets.

        `key` is what a prompt carries -- a conversation id, a tmux name, or a
        session id -- and is resolved to the registry's id, which is what the
        mailbox keys its pairs by. The ping-pong latch and counter clear; the
        rate window does not, so breaking a loop never widens the pipe.
        """
        if not key:
            return
        record = self.get_session(key)
        if record is not None:
            self.loop_guard.note_human_prompt(record.id)

    def _raise_loop_alert(self, from_id: str, to_id: str) -> None:
        """Push the "agents are looping" alert, once, when a pair latches.

        The latch is the safety valve: the server has stopped feeding two
        agents that are talking past each other, and the human needs to know
        now, not when the inbox file grows. Without a push manager (tests, a
        server started before push was wired) the WebSocket event still goes
        out; the lock-screen alert is best-effort on top of it.
        """
        logger.warning("Mailbox loop between %s and %s: delivery paused until a human speaks", from_id, to_id)
        if self.push_manager is None:
            return
        try:
            self.push_manager.send_notification(
                title="Agents are looping",
                body=(
                    f"{from_id} and {to_id} kept replying to each other. "
                    "Their mailbox is paused until you send one of them a prompt."
                ),
                data={"type": "agent_loop", "from": from_id, "to": to_id},
            )
        except Exception as e:  # noqa: BLE001 - an alert that fails must not stop the loop guard
            logger.debug("Loop alert push failed: %s", e)

    def poll_inboxes(self) -> tuple[list[tuple[str, str]], int]:
        """Deliver new mailbox messages to the sessions they are addressed to.

        Called from the watch loop. Each registered session has one inbox
        (`<session-id>.jsonl` under the mailbox dir); every line appended
        since the last poll is wrapped in an envelope and typed into that
        session through the normal prompt path, so it reads in the transcript
        like any other instruction -- except the envelope says who sent it.

        Returns the pairs that tripped the loop latch during this poll, and
        how many messages were delivered, so the watch loop can raise the
        alert exactly once and tell the phones traffic moved.

        A line without its trailing newline is a write in flight: it is held
        for the next poll. A target with no live supervisor is held the same
        way; its mail is not lost, only waited for. The loop guard adds a
        third kind of hold: a line the guard will not admit (muted, looping,
        or rate-limited) is kept in the inbox and re-admitted on a later poll
        -- the hold ends when a human breaks the loop, unmutes, or the rate
        window slides. Lines already delivered in the batch advance the
        offset, so a target that dies mid-batch is not re-typed its own mail.
        """
        looped: list[tuple[str, str]] = []
        delivered = 0
        for session_id in list(self._sessions):
            inbox = mailbox_dir() / f"{session_id}.jsonl"
            try:
                size = inbox.stat().st_size
            except OSError:
                continue

            pos = self._inbox_pos.get(session_id)
            if pos is None:
                self._inbox_pos[session_id] = size
                continue
            if size < pos:
                # The file shrank under us (a manual edit or a rotation): the
                # old offset is nonsense, so resync rather than read garbage.
                self._inbox_pos[session_id] = size
                continue
            if size == pos:
                continue

            sup = self.get_supervisor(session_id)
            if sup is None or not hasattr(sup, "inject_input"):
                continue  # held: delivered once the target is live

            try:
                with open(inbox, "rb") as f:
                    f.seek(pos)
                    data = f.read(size - pos)
            except OSError:
                continue

            last_nl = data.rfind(b"\n")
            if last_nl == -1:
                continue  # a write in flight; the rest arrives next poll

            complete = data[: last_nl + 1]
            new_pos = pos
            for raw in complete.splitlines(keepends=True):
                line = raw.decode("utf-8", "replace")
                message = parse_message_line(line)
                if message is None:
                    # Garbage or a stray hand-edit: drop it and advance, or
                    # the offset would sit behind it forever.
                    logger.debug("Dropping undeliverable mailbox line for %s: %.120s", session_id, line)
                    new_pos += len(raw)
                    continue

                from_id = str(message.get("from") or "unknown")
                admission = self.loop_guard.admit(from_id, session_id)
                if not admission.deliver:
                    logger.info(
                        "Holding mailbox mail for %s: %s from %s",
                        session_id,
                        admission.reason,
                        from_id,
                    )
                    break  # held: re-admitted on a later poll

                envelope = format_envelope(message, self._mailbox_name(message.get("from")))
                # The target may stop taking mail mid-batch (its session died
                # since we looked, or tmux refused). What did not land stays in
                # the inbox; what did is not re-sent on the next poll.
                if not sup.inject_input(envelope):
                    logger.warning("Mailbox delivery to %s refused; holding the rest for retry", session_id)
                    break
                if self.loop_guard.commit(from_id, session_id):
                    looped.append((from_id, session_id))
                delivered += 1
                new_pos += len(raw)

            self._inbox_pos[session_id] = new_pos

        return looped, delivered

    # -------------------------------------------------------------------------
    # Prompt queue: follow-ups typed mid-turn wait, like opencode's dock
    # -------------------------------------------------------------------------

    def note_conversation_activity(self, conversation_id: str | None) -> None:
        """Record that a conversation streamed a step right now.

        The backend's tick calls this whenever new transcript steps arrive, so
        quiescence -- the busy window lapsing -- means the turn ended. Prompts
        the server itself injected deliberately do *not* count: a fast turn
        that ends inside the window would otherwise keep the session busy
        against its own follow-up.
        """
        if conversation_id:
            self._last_activity[conversation_id] = time.monotonic()

    def is_conversation_busy(self, conversation_id: str | None) -> bool:
        """Whether the agent in this conversation is mid-turn.

        Two signals, either of which is enough: a pending tool approval (the
        turn is paused, not finished) or transcript steps inside the busy
        window (the turn is streaming). A conversation with no activity on
        record is never busy -- an idle session must accept a prompt exactly
        as before the queue existed.
        """
        if not conversation_id:
            return False
        if any(
            app.get("conversation_id") == conversation_id
            for app in self._pending_approvals.values()
            if app.get("status") == "pending"
        ):
            return True
        last = self._last_activity.get(conversation_id)
        return last is not None and (time.monotonic() - last) < self.busy_window_seconds

    def queued_prompts(self, conversation_id: str) -> list[dict[str, Any]]:
        """The FIFO waiting for this conversation, oldest first."""
        return list(self._prompt_queues.get(conversation_id, []))

    async def queue_prompt(self, prompt: str, conversation_id: str) -> dict[str, Any]:
        """Hold a prompt until the conversation's turn ends.

        Broadcasts `prompt_queued` so every connected phone draws the chip;
        the id is what a cancel tap carries back.
        """
        entry = {
            "id": uuid.uuid4().hex[:12],
            "prompt": prompt,
            "conversation_id": conversation_id,
            "queued_at": datetime.now().isoformat(),
        }
        self._prompt_queues.setdefault(conversation_id, []).append(entry)
        await self.broadcast({"event": "prompt_queued", "data": dict(entry)})
        return {"status": "queued", "prompt_id": entry["id"], "conversation_id": conversation_id}

    async def cancel_queued_prompt(self, prompt_id: str) -> bool:
        """Remove a queued prompt by id, announcing it so the chip comes down."""
        for conversation_id, queue in self._prompt_queues.items():
            for index, entry in enumerate(queue):
                if entry["id"] == prompt_id:
                    queue.pop(index)
                    await self.broadcast(
                        {"event": "prompt_cancelled", "data": {"id": prompt_id, "conversation_id": conversation_id}}
                    )
                    return True
        return False

    async def submit_prompt(self, prompt: str, conversation_id: str | None = None) -> dict[str, Any]:
        """The one door every user prompt goes through, WS or REST.

        Idle conversations get exactly the old behavior -- backend delivery,
        a `prompt_sent` broadcast, the same response shape. A conversation
        mid-turn with a live supervisor queues instead: typing into a running
        stream used to drop the text into agy's input box behind the agent's
        back, and nothing told the phone.
        """
        target = conversation_id or self.active_conversation_id
        if target and self.is_conversation_busy(target) and self.get_supervisor(target) is not None:
            return await self.queue_prompt(prompt, target)

        delivered_via = await self.backend.send_prompt(self, prompt, conversation_id)
        await self.broadcast(
            {
                "event": "prompt_sent",
                "data": {
                    "prompt": prompt,
                    "conversation_id": conversation_id or self.active_conversation_id,
                    "delivered_via": delivered_via,
                },
            }
        )
        return {"status": "ok", "delivered_via": delivered_via}

    async def deliver_due_prompts(self) -> int:
        """Deliver queued prompts whose turns have ended; called per watch tick.

        At most one head per conversation per tick, and only when the
        conversation is no longer busy *and* a supervisor exists to type
        into -- a queue without a pane is held, not dropped, exactly like
        mailbox mail whose target died. Delivery is announced once, with the
        prompt included, so the phone renders the row and takes the chip down
        from the same event.
        """
        delivered = 0
        for conversation_id in list(self._prompt_queues):
            queue = self._prompt_queues.get(conversation_id)
            if not queue:
                continue
            if self.is_conversation_busy(conversation_id):
                continue
            if self.get_supervisor(conversation_id) is None:
                continue
            entry = queue.pop(0)
            delivered_via = await self.backend.send_prompt(self, entry["prompt"], conversation_id)
            await self.broadcast(
                {
                    "event": "prompt_delivered",
                    "data": {
                        "id": entry["id"],
                        "prompt": entry["prompt"],
                        "conversation_id": conversation_id,
                        "delivered_via": delivered_via,
                    },
                }
            )
            delivered += 1
        return delivered

    def attach_terminal(self, supervisor: Any) -> None:
        """Mirror a supervised session's screen for clients that cannot see it.

        agy draws its pickers, its confirmations and its execution mode on the
        terminal and never writes them to the transcript, so a phone holding
        only the transcript is pressing keys at a screen it cannot see.
        """
        if supervisor is None or not hasattr(supervisor, "add_output_listener"):
            self.terminal = None
            return
        rows = getattr(supervisor, "rows", 24)
        cols = getattr(supervisor, "cols", 80)
        if not isinstance(rows, int):
            rows = 24
        if not isinstance(cols, int):
            cols = 80
        mirror = TerminalMirror(rows=rows, cols=cols)
        supervisor.add_output_listener(mirror.feed)
        self.terminal = mirror

    def attach_screen(self, mirror: Any) -> None:
        """Mirror a screen we cannot listen to, only read.

        A supervisor we started hands us its bytes (`attach_terminal`). A
        session adopted from tmux has no such stream -- its terminal belongs to
        whoever attached to it -- so the mirror reads the pane back instead,
        and is handed over ready-made.
        """
        self.terminal = mirror

    async def broadcast_terminal(self) -> bool:
        """Push the screen to clients, but only when it actually changed."""
        if self.terminal is None:
            return False

        snapshot = self.terminal.take_dirty_snapshot()
        if snapshot is None:
            return False

        await self.broadcast({"event": "terminal_screen", "data": snapshot})
        return True

    async def bind_supervised_conversation(self, conversation_id: str, session_id: str | None = None) -> None:
        """Bind this manager to its supervised agy conversation."""
        if not conversation_id:
            return

        if session_id and session_id in self._sessions:
            self._sessions[session_id].conversation_id = conversation_id
        elif self._sessions:
            for s in self._sessions.values():
                if not s.conversation_id:
                    s.conversation_id = conversation_id
                    break

        if self.supervised_conversation_id == conversation_id:
            return
        logger.info("Bound supervised session to conversation %s", conversation_id)
        self.supervised_conversation_id = conversation_id
        if self.active_conversation_id != conversation_id:
            await self.switch_conversation(conversation_id)

    async def follow_latest_conversation(self) -> bool:
        """Move the view to the newest conversation, unless the user pinned one or a session is supervised.

        Launching agy starts a new conversation, and the phone has no way to
        know: it kept rendering whatever session was newest when the server
        booted, hours stale, while the desktop worked in the new one. The old
        guard only ever switched when nothing at all was active, so in practice
        it never fired after startup.
        """
        if not self.follow_latest or self.supervised_conversation_id is not None:
            return False

        newest_id = self.get_newest_conversation_id()
        if not newest_id or newest_id == self.active_conversation_id:
            return False

        await self.switch_conversation(newest_id)
        return True

    # -------------------------------------------------------------------------
    # Host files for the phone
    # -------------------------------------------------------------------------

    def _file_roots(self) -> list[Path]:
        """The directories a file reference may name, resolved.

        The registered sessions' workdirs plus the projects root: the places
        the operator's agents are sanctioned to work. Anything else -- the
        operator's home, the brain dir, /etc -- is refused by `read_host_file`.
        """
        roots: list[Path] = []
        for record in self._sessions.values():
            if record.workdir:
                roots.append(Path(str(record.workdir)).resolve())
        roots.append(projects_root().resolve())
        return roots

    def working_tree_diff(self, key: str | None = None) -> dict[str, Any]:
        """The session's git working-tree state, for the phone's diff pane.

        Runs `git diff HEAD` in the session's registered workdir -- staged and
        unstaged in one pass, like opencode's working-tree viewer -- and hands
        back per-file chunks with addition/deletion counts. The workdir comes
        from the session registry, never from the request, and must sit inside
        the sanctioned roots; the request only *names* a session.

        Raises LookupError when the named session has no workdir, ValueError
        when the directory is not a git repository (and on git failure or
        timeout), PermissionError when the workdir somehow left the roots.
        """
        from .gitdiff import MAX_DIFF_BYTES, split_git_diff

        session = self.get_session(key)
        if session is None or not session.workdir:
            raise LookupError("no supervised session workdir for this conversation")

        resolved = Path(str(session.workdir)).resolve()
        roots = self._file_roots()
        if not any(resolved == root or root in resolved.parents for root in roots):
            raise PermissionError(f"outside the sanctioned project directories: {resolved}")

        try:
            proc = subprocess.run(
                ["git", "diff", "HEAD", "--"],
                cwd=resolved,
                capture_output=True,
                text=True,
                timeout=GIT_DIFF_TIMEOUT_SECONDS,
                check=False,
            )
        except subprocess.TimeoutExpired as e:
            raise ValueError(f"git diff timed out after {GIT_DIFF_TIMEOUT_SECONDS:.0f}s") from e
        except FileNotFoundError as e:
            raise ValueError("git is not available on this host") from e

        if proc.returncode != 0:
            stderr = proc.stderr.strip()
            if "not a git repository" in stderr.lower():
                raise ValueError(f"not a git repository: {resolved.name}")
            raise ValueError(f"git diff failed: {stderr[:200]}")

        stdout = proc.stdout
        # `git diff HEAD` cannot see untracked files -- the ones an agent has
        # just written and not staged. opencode's viewer shows them, and they
        # are usually the most interesting work in the tree, so collect them
        # as synthesized new-file chunks. Binary content is named, not shipped.
        listing = subprocess.run(
            ["git", "ls-files", "--others", "--exclude-standard"],
            cwd=resolved,
            capture_output=True,
            text=True,
            timeout=GIT_DIFF_TIMEOUT_SECONDS,
            check=False,
        )
        if listing.returncode == 0:
            for name in listing.stdout.splitlines():
                if not name or ".." in Path(name).parts:
                    continue
                path = resolved / name
                try:
                    if not path.is_file():
                        continue
                    data = path.read_bytes()[:65536]
                except OSError:
                    continue
                if b"\x00" in data:
                    stdout += (
                        f"\ndiff --git a/{name} b/{name}\n"
                        "new file mode 100644\n"
                        f"Binary files /dev/null and b/{name} differ\n"
                    )
                    continue
                lines = data.decode("utf-8", "replace").splitlines()
                shown = min(len(lines), 1000)
                body = "".join(f"+{line}\n" for line in lines[:shown])
                stdout += (
                    f"\ndiff --git a/{name} b/{name}\n"
                    "new file mode 100644\n"
                    f"--- /dev/null\n+++ b/{name}\n"
                    f"@@ -0,0 +1,{shown} @@\n{body}"
                )

        truncated = len(stdout.encode("utf-8", "replace")) > MAX_DIFF_BYTES
        if truncated:
            stdout = stdout[:MAX_DIFF_BYTES]
        files = split_git_diff(stdout)
        return {
            "workdir": str(resolved),
            "clean": not files,
            "files": files,
            "truncated": truncated,
        }

    def list_host_dir(self, raw_path: str | None, key: str | None = None) -> dict[str, Any]:
        """List one directory of the session's project, for the phone's file tree.

        With no path this is the registered session's workdir -- the tree's
        root, so the request never has to know where the project lives. Every
        path is resolved and must land inside the sanctioned roots, exactly
        like `read_host_file`; the listing itself is safe to hand over, and a
        symlinked entry that escapes the roots is still stopped at open time
        by the viewer's own check. Entries are directories first, then
        case-insensitive by name, each carrying its absolute path so the
        client never builds one.

        Raises LookupError when the named session has no workdir,
        PermissionError outside the roots, FileNotFoundError for a missing
        path, ValueError when the path is not a directory.
        """
        session = self.get_session(key)
        if session is None or not session.workdir:
            raise LookupError("no supervised session workdir for this conversation")

        requested = (raw_path or "").strip() or str(session.workdir)
        resolved = Path(requested).resolve()
        roots = self._file_roots()
        if not any(resolved == root or root in resolved.parents for root in roots):
            raise PermissionError(f"outside the sanctioned project directories: {requested}")

        if not resolved.exists():
            raise FileNotFoundError(resolved.name)
        if not resolved.is_dir():
            raise ValueError(f"not a directory: {resolved.name}")

        entries: list[dict[str, Any]] = []
        for child in resolved.iterdir():
            try:
                is_dir = child.is_dir()
                size = 0 if is_dir else child.stat().st_size
            except OSError:
                continue  # raced a delete, or permission denied: not listable
            entries.append({"name": child.name, "type": "dir" if is_dir else "file", "size": size, "path": str(child)})
        entries.sort(key=lambda e: (e["type"] != "dir", str(e["name"]).lower()))

        return {"path": str(resolved), "name": resolved.name, "entries": entries}

    def read_host_file(self, raw_path: str) -> dict[str, Any]:
        """Read a file the transcript named, for the phone to display.

        Accepts the `[file:///abs/path]` form the agent writes and bare
        absolute paths. The path is resolved (which collapses `..` and follows
        symlinks) and must land inside a sanctioned root, so a crafted
        reference cannot read a file the agent could not have. Binary files and
        directories are refused; large files are truncated, not streamed.

        Raises PermissionError outside the roots, FileNotFoundError for a
        missing file, ValueError for a binary file or a directory.
        """
        requested = raw_path.strip()
        if requested.startswith("file://"):
            requested = requested[len("file://") :]
            # `file://host/path` names another machine's share; only the
            # empty-host form, `file:///path`, refers to this one.
            if not requested.startswith("/"):
                raise PermissionError(f"not an absolute file reference: {raw_path!r}")

        resolved = Path(requested).resolve()
        if not any(resolved == root or root in resolved.parents for root in self._file_roots()):
            raise PermissionError(f"outside the sanctioned project directories: {raw_path!r}")

        if not resolved.exists():
            raise FileNotFoundError(resolved.name)
        if not resolved.is_file():
            raise ValueError(f"not a regular file: {resolved.name}")

        with open(resolved, "rb") as fh:
            data = fh.read(MAX_FILE_BYTES + 1)
        # A NUL in the first kilobytes is the classic binary tell; decoding
        # one into the phone's <pre> produces garbage either way.
        if b"\x00" in data[:8192]:
            raise ValueError(f"binary file: {resolved.name}")

        truncated = len(data) > MAX_FILE_BYTES
        content = data[:MAX_FILE_BYTES].decode("utf-8", errors="replace")
        return {
            "path": str(resolved),
            "name": resolved.name,
            "content": content,
            "truncated": truncated,
            "size": resolved.stat().st_size,
        }

    async def disconnect_expired_clients(self) -> int:
        """Close live connections once the pairing deadline passes.

        `token_ok` refuses new connections after expiry, but a WebSocket
        authenticated before the deadline holds its socket open -- streaming
        the transcript and accepting prompts on a credential that is no longer
        valid. The watcher sweeps them out; the client's reconnect is then
        refused at the door.
        """
        if not self.config.pairing_expired() or not self._connected_clients:
            return 0

        expired = list(self._connected_clients)
        self._connected_clients.clear()
        for websocket in expired:
            try:
                await websocket.close(code=1008)  # policy violation
            except Exception as e:  # noqa: BLE001 - a dead socket is already what we wanted
                logger.debug("Closing expired client failed: %s", e)

        logger.info("Closed %d connection(s): pairing expired", len(expired))
        return len(expired)

    async def _watch_loop(self) -> None:
        """Continuous loop keeping the phone in step with the agent.

        The agent-specific half (follow a newer conversation, stream new
        steps) is the backend's `tick`; the rest is shared by every agent.
        """
        while self._running:
            try:
                await self.backend.tick(self)

                # Agent-to-agent mail: deliver what the inboxes received, and
                # report what the loop guard latched or moved.
                looped, delivered = self.poll_inboxes()
                if looped:
                    for from_id, to_id in looped:
                        self._raise_loop_alert(from_id, to_id)
                if looped or delivered:
                    await self.broadcast(
                        {
                            "event": "agent_traffic",
                            "data": {
                                # Every pair the guard knows; the PWA draws
                                # per-row counters and the header badge from it.
                                "pairs": self.agent_traffic(),
                                # The pairs that just latched this poll: the
                                # PWA says so, and the human sees the alert.
                                "looped": [{"from": a, "to": b} for a, b in looped],
                            },
                        }
                    )

                # Mirror the terminal, for the panels the transcript never sees
                await self.broadcast_terminal()

                # Queued follow-ups whose turns have ended go in now
                await self.deliver_due_prompts()

                # End sessions whose pairing has expired mid-connection
                await self.disconnect_expired_clients()

                # Drop clients that went silent -- a suspended Safari never
                # sends the close frame, so the socket alone proves nothing.
                await self.reap_stale_clients()

                # Poll meta-AGY jobs periodically
                now = time.monotonic()
                if now - self._last_meta_poll >= self.config.meta_agy_poll_interval:
                    self._last_meta_poll = now
                    await self.poll_meta_agy()

                await asyncio.sleep(0.3)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.debug("Exception in watch loop: %s", e)
                await asyncio.sleep(1.0)

    # -------------------------------------------------------------------------
    # Tool Approvals / Permissions Handling
    # -------------------------------------------------------------------------
    def is_pane_visible(self, target: str) -> bool:
        """Check if target pane is in the active window of an attached client."""
        from .tmux_runner import is_pane_active_and_visible

        return is_pane_active_and_visible(target)

    async def register_approval(
        self,
        approval_id: str,
        conversation_id: str,
        tool_name: str,
        args: dict[str, Any],
        origin_pane: str | None = None,
    ) -> dict[str, Any]:
        """Register a pending approval and broadcast it to the phone.

        Non-blocking: agy's hook endpoint awaits the answer separately
        (`await_approval`); a backend whose agent must be told the outcome does
        that in `deliver_resolution`.

        Every session's approvals are broadcast, each carrying the session
        that raised it. Hiding another session's was the honest fix for a bare
        `bash` request drawn into the transcript on screen -- it read as
        belonging to the work in front of you -- but it also meant nobody could
        answer it, so the hook was held for a banner that was never drawn. The
        client names the session instead.
        """
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[dict[str, Any]] = loop.create_future()
        self._approval_futures[approval_id] = fut

        if origin_pane:
            self._conversation_panes[conversation_id] = origin_pane

        # Name the session, not just its id. "rm -rf /" from
        # `fe67ae68-b3b6-4918` says nothing about which of four terminals is
        # waiting, and a phone cannot look up a name for a session it has never
        # displayed.
        summary = self._summary_of(conversation_id) or {}
        approval_data = {
            "id": approval_id,
            "conversation_id": conversation_id,
            "conversation_title": summary.get("title") or conversation_id[:8],
            "tool_name": tool_name,
            "args": args,
            "created_at": datetime.now().isoformat(),
            "status": "pending",
        }
        if origin_pane:
            approval_data["origin_pane"] = origin_pane
        if tool_name == "ask_question":
            parsed_questions = parse_ask_question_args(args)
            if parsed_questions:
                approval_data["questions"] = parsed_questions
        self._pending_approvals[approval_id] = approval_data
        self._forget_old_answers()

        # Broadcast approval request to phone
        await self.broadcast({"event": "approval_request", "data": approval_data})
        self._surface_in_tui(conversation_id, approval_id, tool_name, args, origin_pane=origin_pane)
        return approval_data

    def pending_approval(self, approval_id: str) -> dict[str, Any] | None:
        """The pending approval's data, for a desktop that wants to see what
        it is being asked before it answers. Answered or unknown ids are None.
        """
        info = self._pending_approvals.get(approval_id)
        if info is None or info.get("status") != "pending":
            return None
        return info

    def _surface_in_tui(
        self,
        conversation_id: str,
        approval_id: str,
        tool_name: str,
        args: dict[str, Any],
        origin_pane: str | None = None,
    ) -> None:
        """Mirror a held approval into the terminal agy runs in.

        With no phone connected the hook answers "ask" and agy prompts in its
        own TUI, so the desktop sees the question. With a phone connected the
        hook holds and the TUI went silent: agy froze on a question visible
        only on the phone. This puts the question back in front of whoever is
        sitting at the terminal -- and lets them answer it there, first answer
        winning as everywhere else.

        tmux sessions get an overlay popup running `agy-remote tui-approve`
        (drawn by tmux itself, so agy's screen is never touched). A server-
        owned pty has no window system to overlay; the console bell rings.
        """
        target = origin_pane or self._conversation_panes.get(conversation_id)
        session_name: str | None = None

        supervisor = self.get_supervisor(conversation_id)
        if supervisor is not None:
            session_name = getattr(supervisor, "session_name", None)
            if not target:
                target = getattr(supervisor, "target", None) or session_name
            if not target and not session_name:
                # No tmux pane to draw on: ring the bell and leave the deciding
                # to the phone.
                try:
                    sys.stdout.write("\a")
                    sys.stdout.flush()
                except Exception as e:  # noqa: BLE001 - a closed console is not fatal
                    logger.debug("Could not ring the console bell: %s", e)
                return

        if not target and not session_name:
            logger.debug(
                "No target tmux pane known for approval %s in conversation %s; deciding on phone",
                approval_id,
                conversation_id,
            )
            return

        final_target = target or session_name or ""

        # CRITICAL: Prevent capturing keyboard activity in a different window.
        # tmux display-popup displays over the attached client's currently active window.
        # If the target pane's window is not active, display-popup will pop up over
        # whatever window the user is currently working in (e.g. editor or zsh) and
        # steal keystrokes. We only open popups if the target pane's window is active.
        if not self.is_pane_visible(final_target):
            logger.info(
                "Skipping TUI popup for approval %s: pane %s is in an inactive window; deciding on phone",
                approval_id,
                final_target,
            )
            with contextlib.suppress(Exception):
                subprocess.run(
                    ["tmux", "set-window-option", "-t", final_target, "monitor-activity", "on"],
                    capture_output=True,
                    check=False,
                )
            return

        env = {
            "AGY_REMOTE_URL": self.config.local_base_url,
            "AGY_REMOTE_TOKEN": self.config.auth_token,
            "AGY_REMOTE_APPROVAL_ID": approval_id,
            "AGY_REMOTE_TOOL_NAME": tool_name,
            "AGY_REMOTE_TOOL_ARGS": json.dumps(args, default=str)[:2048],
        }
        # The tmux server executes popup commands in its own daemon environment,
        # ignoring the subprocess.Popen env passed to the tmux client. We must
        # explicitly export these environment variables in the shell command
        # string and use the active Python interpreter so venvs/uv tool paths work.
        env_exports = " ".join(f"{k}={shlex.quote(str(v))}" for k, v in env.items())
        popup_cmd = f"exec env {env_exports} {shlex.quote(sys.executable)} -m agy_remote.cli tui-approve"
        cmd = [
            "tmux",
            "display-popup",
            "-t",
            final_target,
            "-w",
            "80%",
            "-h",
            "12",
            "-E",
            popup_cmd,
        ]
        try:
            subprocess.Popen(  # noqa: S603 - fixed argv, tmux is the point
                cmd,
                env={**os.environ, **env},
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except Exception as e:  # noqa: BLE001 - the phone still has the banner
            logger.warning("Could not surface approval %s in the tmux pane: %s", approval_id, e)

    async def await_approval(self, approval_id: str, timeout: float = 240.0) -> dict[str, Any]:
        """Wait for the phone's answer to a registered approval.

        Used by agy, whose hook process blocks until this returns. A backend
        whose permission simply stays open until someone answers -- in the
        terminal or on the phone -- never calls this.

        This is the innermost of three timeouts and must be the shortest:
        agy kills the hook at 300s and the hook gives up on the socket at 270s,
        so deciding at 300s meant agy killed the process at the same instant --
        `signal: killed`, rather than a denial anyone could read.
        """
        fut = self._approval_futures.get(approval_id)
        if fut is None:
            return {"decision": "deny", "reason": "unknown approval"}

        try:
            return await asyncio.wait_for(fut, timeout=timeout)
        except TimeoutError:
            self._pending_approvals.pop(approval_id, None)
            self._approval_futures.pop(approval_id, None)
            return {
                "decision": "deny",
                "reason": "approval timed out waiting for response",
            }

    def can_hold_approval(self, conversation_id: str) -> bool:
        """Whether an approval for this session would reach a person.

        Every session's approvals are broadcast, each naming the session that
        raised it, so any connected client can answer any of them. What this
        rules out is holding a hook when nobody is there at all: agy would wait
        out its own timeout and kill the call.
        """
        return bool(self._connected_clients)

    async def request_approval(
        self,
        approval_id: str,
        conversation_id: str,
        tool_name: str,
        args: dict[str, Any],
        origin_pane: str | None = None,
    ) -> dict[str, Any]:
        """Request remote approval from connected clients (PWA/phones).

        The agy PreToolUse hook path: the hook process blocks on this call and
        returns whatever the phone decides (or a timeout denial) to the CLI.
        With no client connected there is nobody who could ever answer, and
        waiting can only end one way: agy kills the hook after its own timeout
        and the tool call fails. A server that runs all day makes that the
        normal state rather than a rare one -- every tool call in every
        hand-started agy stalling for five minutes -- so the answer has to be
        immediate. "ask" hands the decision back to agy, which prompts in its
        own terminal exactly as it would with no hook installed.
        """
        if not self.can_hold_approval(conversation_id):
            logger.info("Approval for %s answered locally: no phone is watching this session", tool_name)
            return {
                "decision": "ask",
                "reason": "agy-remote: no phone watching this session, asking here instead",
            }

        await self.register_approval(approval_id, conversation_id, tool_name, args, origin_pane=origin_pane)

        # `broadcast` prunes clients whose send failed, so an open socket with
        # nothing behind it -- a phone that slept, a laptop that closed -- is
        # discovered exactly here. Waiting on it would hang agy just as surely
        # as having no client at all.
        if not self._connected_clients:
            self._pending_approvals.pop(approval_id, None)
            logger.info("Approval for %s answered locally: the connection was dead", tool_name)
            return {
                "decision": "ask",
                "reason": "agy-remote: no phone connected, asking here instead",
            }

        return await self.await_approval(approval_id)

    async def resolve_approval(
        self,
        approval_id: str,
        req: ApprovalResponseRequest,
        source: str = "phone",
    ) -> bool:
        """Resolve a pending tool approval.

        `source` is "phone" for a tap on the PWA (the decision must then be
        carried to the agent) and "agent" for a resolution that happened on the
        agent's own side (the TUI answered), which only needs the phone's
        banner cleared.
        """
        if approval_id not in self._pending_approvals:
            return False

        app = self._pending_approvals[approval_id]
        app["status"] = "allowed" if req.decision in ("allow", "always") else "denied"
        app["reason"] = req.reason
        self._forget_old_answers()

        response_payload: dict[str, Any] = {
            "decision": req.decision,
            "reason": req.reason or "",
        }
        if req.overwrite_args:
            response_payload["overwrite"] = req.overwrite_args

        fut = self._approval_futures.get(approval_id)
        if fut and not fut.done():
            fut.set_result(response_payload)

        if source == "phone":
            await self.backend.deliver_resolution(self, app, response_payload)

        # Broadcast resolution
        await self.broadcast(
            {
                "event": "approval_resolved",
                "data": {
                    "id": approval_id,
                    "status": app["status"],
                    "decision": req.decision,
                },
            }
        )
        return True


session_manager_instance: SessionManager | None = None


def get_session_manager() -> SessionManager:
    """Get global session manager singleton."""
    global session_manager_instance
    if session_manager_instance is None:
        session_manager_instance = SessionManager()
    return session_manager_instance
