"""Session management, log tailing, and live state synchronization.

The manager owns the agent-agnostic half: the WebSocket fan-out, the E2EE
sealing, the pending-approval state machine, the terminal mirror and the
watcher loop. Everything agent-specific (where steps come from, how a prompt
or a decision travels to the CLI) lives in a backend, see `backends.py`.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import WebSocket

from .backends import AgentBackend, make_backend
from .config import RemoteConfig, get_config
from .crypto import ReplayGuard, decode_key, encrypt_payload
from .models import (
    ApprovalResponseRequest,
    ConversationSummary,
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
        #: Track whichever conversation is newest, until the user picks one.
        self.follow_latest: bool = True
        self.active_steps: list[TranscriptStep] = []
        self._connected_clients: set[WebSocket] = set()
        self._pending_approvals: dict[str, dict[str, Any]] = {}
        self._approval_futures: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._watcher_task: asyncio.Task[None] | None = None
        #: Mirror of the supervised terminal, when a session is supervised.
        self.terminal: TerminalMirror | None = None
        self._running: bool = False

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
            self.follow_latest = conversation_id == self.get_latest_conversation_id()

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
                    "steps": [step.model_dump() for step in self.active_steps],
                    "pending_approvals": self.get_active_pending_approvals(),
                },
            }
        )
        return True

    def _summary_of(self, conversation_id: str | None) -> dict[str, Any] | None:
        """The summary for one conversation, as clients need it to name a session."""
        return self.backend.summary_of(self, conversation_id)

    def get_active_pending_approvals(self) -> list[dict[str, Any]]:
        """Return pending approvals for current active conversation."""
        if not self.active_conversation_id:
            return []
        return [
            app
            for app in self._pending_approvals.values()
            if app.get("conversation_id") == self.active_conversation_id and app.get("status") == "pending"
        ]

    async def register_client(self, websocket: WebSocket) -> None:
        """Register a new WebSocket client and send initial snapshot."""
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

    def unregister_client(self, websocket: WebSocket) -> None:
        """Remove a disconnected WebSocket client."""
        self._connected_clients.discard(websocket)

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

    async def broadcast_terminal(self) -> bool:
        """Push the screen to clients, but only when it actually changed."""
        if self.terminal is None:
            return False

        snapshot = self.terminal.take_dirty_snapshot()
        if snapshot is None:
            return False

        await self.broadcast({"event": "terminal_screen", "data": snapshot})
        return True

    async def follow_latest_conversation(self) -> bool:
        """Move the view to the newest conversation, unless the user pinned one.

        Launching agy starts a new conversation, and the phone has no way to
        know: it kept rendering whatever session was newest when the server
        booted, hours stale, while the desktop worked in the new one. The old
        guard only ever switched when nothing at all was active, so in practice
        it never fired after startup.
        """
        if not self.follow_latest:
            return False

        latest_id = self.get_latest_conversation_id()
        if not latest_id or latest_id == self.active_conversation_id:
            return False

        await self.switch_conversation(latest_id)
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
        (`await_approval`), while opencode's permission arrives as an event
        whose answer travels back as a REST call (`deliver_resolution`).
        """
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[dict[str, Any]] = loop.create_future()
        self._approval_futures[approval_id] = fut

        approval_data = {
            "id": approval_id,
            "conversation_id": conversation_id,
            "tool_name": tool_name,
            "args": args,
            "created_at": datetime.now().isoformat(),
            "status": "pending",
        }
        self._pending_approvals[approval_id] = approval_data

        # Broadcast approval request to phone
        await self.broadcast(
            {
                "event": "approval_request",
                "data": approval_data,
            }
        )
        return approval_data

    async def await_approval(self, approval_id: str, timeout: float = 300.0) -> dict[str, Any]:
        """Wait for the phone's answer to a registered approval.

        Used by agy, whose hook process blocks until this returns. opencode
        never waits: its permission simply stays open until someone answers,
        in the TUI or on the phone.
        """
        fut = self._approval_futures.get(approval_id)
        if fut is None:
            return {"decision": "deny", "reason": "Unknown approval."}

        try:
            # Wait up to 5 minutes for approval from mobile
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
        """
        await self.register_approval(approval_id, conversation_id, tool_name, args)
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
