"""Session management, log tailing, and live state synchronization."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import WebSocket

from .config import RemoteConfig, get_config
from .models import (
    ApprovalResponseRequest,
    ConversationSummary,
    TranscriptStep,
)

logger = logging.getLogger("agy_remote.session")


class SessionManager:
    """Manages active Antigravity CLI conversations and real-time streaming."""

    def __init__(self, config: RemoteConfig | None = None) -> None:
        self.config = config or get_config()
        self.brain_dir = self.config.brain_dir
        self.active_conversation_id: str | None = None
        self.active_steps: list[TranscriptStep] = []
        self._last_file_pos: int = 0
        self._connected_clients: set[WebSocket] = set()
        self._pending_approvals: dict[str, dict[str, Any]] = {}
        self._approval_futures: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._watcher_task: asyncio.Task[None] | None = None
        self._running: bool = False

    async def start(self) -> None:
        """Start the session manager and background watcher loop."""
        self._running = True
        self.brain_dir.mkdir(parents=True, exist_ok=True)
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

    def list_conversations(self) -> list[ConversationSummary]:
        """Scan brain_dir and return a sorted list of conversation summaries."""
        if not self.brain_dir.exists():
            return []

        summaries: list[ConversationSummary] = []
        for path in self.brain_dir.iterdir():
            if not path.is_dir():
                continue
            # Check for transcript log
            log_path = path / ".system_generated" / "logs" / "transcript.jsonl"
            if not log_path.exists():
                # Some might have transcript directly or under subdirs
                alt_path = path / "transcript.jsonl"
                if alt_path.exists():
                    log_path = alt_path
                else:
                    continue

            try:
                stat = log_path.stat()
                updated_at = datetime.fromtimestamp(stat.st_mtime)
                created_at = datetime.fromtimestamp(stat.st_ctime)

                # Quick sample first user prompt and step count
                step_count = 0
                first_prompt: str | None = None
                last_prompt: str | None = None
                last_response: str | None = None

                with open(log_path, encoding="utf-8", errors="replace") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        step_count += 1
                        try:
                            obj = json.loads(line)
                            step_type = obj.get("type", "")
                            content = obj.get("content") or ""
                            if step_type == "USER_INPUT":
                                if not first_prompt:
                                    first_prompt = content[:100]
                                last_prompt = content[:100]
                            elif step_type == "PLANNER_RESPONSE":
                                if content:
                                    last_response = content[:150]
                        except Exception:
                            continue

                title = first_prompt or f"Session {path.name[:8]}"
                has_pending = any(
                    a.get("conversation_id") == path.name and a.get("status") == "pending"
                    for a in self._pending_approvals.values()
                )

                summaries.append(
                    ConversationSummary(
                        id=path.name,
                        title=title,
                        created_at=created_at,
                        updated_at=updated_at,
                        step_count=step_count,
                        last_user_message=last_prompt,
                        last_model_response=last_response,
                        is_active=(path.name == self.active_conversation_id),
                        has_pending_approval=has_pending,
                    )
                )
            except Exception as e:
                logger.debug("Failed reading conversation %s: %s", path.name, e)

        # Sort newest first
        summaries.sort(
            key=lambda s: s.updated_at or datetime.min,
            reverse=True,
        )
        return summaries

    def get_latest_conversation_id(self) -> str | None:
        """Find the most recently updated conversation ID."""
        convs = self.list_conversations()
        return convs[0].id if convs else None

    def get_transcript_path(self, conversation_id: str) -> Path | None:
        """Resolve the path to transcript.jsonl for a conversation with traversal protection."""
        if not conversation_id or not all(c.isalnum() or c in "-_" for c in conversation_id):
            return None

        primary = (self.brain_dir / conversation_id / ".system_generated" / "logs" / "transcript.jsonl").resolve()
        try:
            if not primary.is_relative_to(self.brain_dir.resolve()):
                return None
        except (ValueError, RuntimeError):
            return None

        if primary.exists():
            return primary

        fallback = (self.brain_dir / conversation_id / "transcript.jsonl").resolve()
        try:
            if fallback.is_relative_to(self.brain_dir.resolve()) and fallback.exists():
                return fallback
        except (ValueError, RuntimeError):
            return None

        return None

    async def switch_conversation(self, conversation_id: str) -> bool:
        """Switch active conversation to the specified ID and load steps."""
        self.active_conversation_id = conversation_id
        self.active_steps = []
        self._last_file_pos = 0

        transcript_path = self.get_transcript_path(conversation_id)
        if transcript_path and transcript_path.exists():
            await self._read_new_steps(transcript_path, initial=True)

        await self.broadcast(
            {
                "event": "session_switched",
                "data": {
                    "conversation_id": conversation_id,
                    "steps": [step.model_dump() for step in self.active_steps],
                    "pending_approvals": self.get_active_pending_approvals(),
                },
            }
        )
        return True

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
                "active_conversation_id": self.active_conversation_id,
                "steps": [step.model_dump() for step in self.active_steps],
                "conversations": [c.model_dump(mode="json") for c in self.list_conversations()],
                "pending_approvals": self.get_active_pending_approvals(),
            },
        }
        try:
            await websocket.send_json(init_data)
        except Exception as e:
            logger.debug("Failed sending init payload to websocket: %s", e)

    def unregister_client(self, websocket: WebSocket) -> None:
        """Remove a disconnected WebSocket client."""
        self._connected_clients.discard(websocket)

    async def broadcast(self, payload: dict[str, Any]) -> None:
        """Send JSON payload to all active WebSocket clients."""
        if not self._connected_clients:
            return

        to_remove = set()
        for ws in self._connected_clients:
            try:
                await ws.send_json(payload)
            except Exception:
                to_remove.add(ws)

        for ws in to_remove:
            self._connected_clients.discard(ws)

    async def _read_new_steps(self, path: Path, initial: bool = False) -> list[TranscriptStep]:
        """Read appended lines from transcript.jsonl."""
        new_steps: list[TranscriptStep] = []
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                if not initial:
                    f.seek(self._last_file_pos)

                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        data = json.loads(line)
                        step = TranscriptStep(
                            step_index=data.get("step_index", len(self.active_steps) + len(new_steps)),
                            source=data.get("source", "UNKNOWN"),
                            type=data.get("type", "UNKNOWN"),
                            status=data.get("status", "DONE"),
                            created_at=data.get("created_at"),
                            content=data.get("content"),
                            thinking=data.get("thinking"),
                            tool_calls=data.get("tool_calls") or [],
                            truncated_fields=data.get("truncated_fields") or [],
                        )
                        new_steps.append(step)
                    except Exception as err:
                        logger.debug("Skipping unparseable transcript line: %s", err)

                self._last_file_pos = f.tell()
        except Exception as e:
            logger.debug("Error reading transcript file %s: %s", path, e)

        if new_steps:
            self.active_steps.extend(new_steps)

        return new_steps

    async def _watch_loop(self) -> None:
        """Continuous polling/tailing loop for the active conversation log."""
        while self._running:
            try:
                # 1. Check if a newer conversation was started
                latest_id = self.get_latest_conversation_id()
                if latest_id and latest_id != self.active_conversation_id and not self.active_conversation_id:
                    # Switch automatically if active session is empty or user just launched agy
                    await self.switch_conversation(latest_id)

                # 2. Tail the active conversation log
                if self.active_conversation_id:
                    transcript_path = self.get_transcript_path(self.active_conversation_id)
                    if transcript_path and transcript_path.exists():
                        stat = transcript_path.stat()
                        if stat.st_size > self._last_file_pos:
                            new_steps = await self._read_new_steps(transcript_path)
                            if new_steps:
                                for step in new_steps:
                                    await self.broadcast(
                                        {
                                            "event": "step_added",
                                            "data": {
                                                "conversation_id": self.active_conversation_id,
                                                "step": step.model_dump(),
                                            },
                                        }
                                    )

                await asyncio.sleep(0.3)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.debug("Exception in watch loop: %s", e)
                await asyncio.sleep(1.0)

    # -------------------------------------------------------------------------
    # Tool Approvals / Permissions Handling
    # -------------------------------------------------------------------------
    async def request_approval(
        self,
        approval_id: str,
        conversation_id: str,
        tool_name: str,
        args: dict[str, Any],
    ) -> dict[str, Any]:
        """Register a pending approval from PreToolUse hook and wait for user response."""
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

        try:
            # Wait up to 5 minutes for approval from mobile
            res = await asyncio.wait_for(fut, timeout=300.0)
            return res
        except TimeoutError:
            self._pending_approvals[approval_id]["status"] = "denied"
            return {
                "decision": "deny",
                "reason": "Approval timed out on mobile remote.",
            }
        finally:
            self._approval_futures.pop(approval_id, None)

    async def resolve_approval(
        self,
        approval_id: str,
        req: ApprovalResponseRequest,
    ) -> bool:
        """Resolve a pending tool approval from the mobile UI."""
        if approval_id not in self._pending_approvals:
            return False

        app = self._pending_approvals[approval_id]
        app["status"] = "allowed" if req.decision == "allow" else "denied"
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
