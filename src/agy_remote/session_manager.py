"""Session management, log tailing, and live state synchronization.

The manager owns the agent-agnostic half: the WebSocket fan-out, the E2EE
sealing, the pending-approval state machine, the terminal mirror and the
watcher loop. Everything agent-specific (where steps come from, how a prompt
or a decision travels to the CLI) lives in a backend, see `backends.py`.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import WebSocket

from .backends import AgentBackend, make_backend
from .config import RemoteConfig, get_config
from .crypto import ReplayGuard, decode_key, encrypt_payload
from .mailbox import format_envelope, mailbox_dir, parse_message_line
from .models import (
    ApprovalResponseRequest,
    ConversationSummary,
    SessionRecord,
    TranscriptStep,
)
from .screen import TerminalMirror

logger = logging.getLogger("agy_remote.session")


class SessionManager:
    """Manages active agent conversations and real-time streaming."""

    def __init__(self, config: RemoteConfig | None = None, backend: AgentBackend | None = None) -> None:
        self.config = config or get_config()
        self.backend = backend or make_backend(self.config)
        self.active_conversation_id: str | None = None
        #: The conversation ID belonging to the supervised agy process for this server.
        self.supervised_conversation_id: str | None = None
        #: Track whichever conversation is newest, until the user picks one.
        self.follow_latest: bool = True
        self.active_steps: list[TranscriptStep] = []
        self._connected_clients: set[WebSocket] = set()
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
        #: Multi-session registry (Phase 1.1)
        self._sessions: dict[str, SessionRecord] = {}
        self._supervisors: dict[str, Any] = {}
        self._terminal_mirrors: dict[str, TerminalMirror] = {}
        #: Byte offset already delivered from each session's inbox. A session
        #: is first seen at its inbox's current end: delivery is for what
        #: agents write while we are watching, and replaying a prompt an agent
        #: already handled would start a conversation the human never sent.
        self._inbox_pos: dict[str, int] = {}

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
        """All known conversations, newest first."""
        return self.backend.list_conversations(self)

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
                },
            }
        )
        return True

    def _summary_of(self, conversation_id: str | None) -> dict[str, Any] | None:
        """The summary for one conversation, as clients need it to name a session."""
        return self.backend.summary_of(self, conversation_id)

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

    async def register_client(self, websocket: WebSocket) -> None:
        """Register a new WebSocket client and send initial snapshot."""
        if self.ensure_session is not None and not self._connected_clients:
            # Only for the first arrival: later ones join what is already
            # running rather than each starting an agent of their own.
            try:
                await self.ensure_session()
            except Exception as e:
                logger.warning("Could not start a session for the arriving client: %s", e)

        self._connected_clients.add(websocket)
        # Send full snapshot of current state
        init_data = {
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
            },
        }
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
        """
        await self.broadcast({"event": "peers", "data": {"count": len(self._connected_clients)}})

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
        self._client_focus.pop(websocket, None)

    async def broadcast(self, payload: dict[str, Any]) -> None:
        """Send JSON payload to all active WebSocket clients."""
        if not self._connected_clients:
            return

        # Seal once and reuse: every client shares the same pre-shared key.
        envelope = self.seal(payload)

        to_remove = set()
        for ws in self._connected_clients:
            try:
                await ws.send_json(envelope)
            except Exception:
                to_remove.add(ws)

        for ws in to_remove:
            self._connected_clients.discard(ws)

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

    def poll_inboxes(self) -> None:
        """Deliver new mailbox messages to the sessions they are addressed to.

        Called from the watch loop. Each registered session has one inbox
        (`<session-id>.jsonl` under the mailbox dir); every line appended
        since the last poll is wrapped in an envelope and typed into that
        session through the normal prompt path, so it reads in the transcript
        like any other instruction -- except the envelope says who sent it.

        A line without its trailing newline is a write in flight: it is held
        for the next poll. A target with no live supervisor is held the same
        way; its mail is not lost, only waited for.
        """
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
            envelopes: list[str] = []
            for line in complete.decode("utf-8", "replace").splitlines():
                message = parse_message_line(line)
                if message is None:
                    logger.debug("Dropping undeliverable mailbox line for %s: %.120s", session_id, line)
                    continue
                envelopes.append(format_envelope(message, self._mailbox_name(message.get("from"))))

            # Advance past torn lines even when there is nothing deliverable,
            # or the offset would sit behind garbage forever.
            if not envelopes:
                self._inbox_pos[session_id] = pos + len(complete)
                continue

            # The target may stop taking mail (its session died since we looked, or
            # tmux refused). A batch cut off mid-way would read to the
            # receiving agent as its colleague's words stopping: if any
            # envelope does not land, the whole batch is held for the next
            # poll, and an earlier line may be resent rather than a later
            # one be lost.
            landed = True
            for envelope in envelopes:
                if not sup.inject_input(envelope):
                    landed = False
                    logger.warning("Mailbox delivery to %s refused; holding the batch for retry", session_id)
                    break
            if not landed:
                continue
            self._inbox_pos[session_id] = pos + len(complete)

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

                # Agent-to-agent mail: deliver what the inboxes received
                self.poll_inboxes()

                # Mirror the terminal, for the panels the transcript never sees
                await self.broadcast_terminal()

                # End sessions whose pairing has expired mid-connection
                await self.disconnect_expired_clients()

                await asyncio.sleep(0.3)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.debug("Exception in watch loop: %s", e)
                await asyncio.sleep(1.0)

    # -------------------------------------------------------------------------
    # Tool Approvals / Permissions Handling
    # -------------------------------------------------------------------------
    async def register_approval(
        self,
        approval_id: str,
        conversation_id: str,
        tool_name: str,
        args: dict[str, Any],
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
        self._pending_approvals[approval_id] = approval_data
        self._forget_old_answers()

        # Broadcast approval request to phone
        await self.broadcast({"event": "approval_request", "data": approval_data})
        return approval_data

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
            return {"decision": "deny", "reason": "Unknown approval."}

        try:
            # Long enough to walk to the phone, short enough to answer first.
            res = await asyncio.wait_for(fut, timeout=timeout)
            return res
        except TimeoutError:
            self._pending_approvals[approval_id]["status"] = "denied"
            return {
                "decision": "deny",
                "reason": "Approval timed out on mobile remote.",
            }
        finally:
            self._approval_futures.pop(approval_id, None)

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
    ) -> dict[str, Any]:
        """Register a pending approval and wait for the user's response.

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

        await self.register_approval(approval_id, conversation_id, tool_name, args)

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
