"""Agent backends: the agent-specific half of session management.

The server, the encryption, the push manager and the PWA speak one normalized
event vocabulary (`init`, `step_added`, `step_updated`, `approval_request`,
`session_switched`, ...). What differs per agent is where the steps come from
and how a decision travels back:

- **agy** writes `transcript.jsonl` files in its brain directory, which we
  tail, and blocks a `PreToolUse` hook process until the phone answers.
- **opencode** runs a first-class HTTP server: an SSE event stream carries
  messages, parts and permission requests, and a permission answer is a REST
  call. No hooks, no file formats.

A backend is constructed from the config and handed the manager on every call
(rather than holding a back-reference) so a manager can be built with any
backend, including fakes in tests.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from .config import RemoteConfig
from .models import ConversationSummary, TranscriptStep
from .transcript import clean_user_content, is_scaffolding, normalize_tool_calls

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .session_manager import SessionManager

logger = logging.getLogger("agy_remote.backends")


class AgentBackend(Protocol):
    """The interface a session manager drives, per agent."""

    name: str

    async def start(self, mgr: SessionManager) -> None:
        """Called from `SessionManager.start()`. May spawn tasks."""

    async def stop(self) -> None:
        """Called from `SessionManager.stop()`. Release tasks and connections."""

    async def tick(self, mgr: SessionManager) -> None:
        """One iteration of the manager's watch loop: stream new steps.

        The generic parts of the loop (terminal broadcast, expired-client
        sweep) live in the manager; only this is agent-specific.
        """

    def on_switch(self, conversation_id: str | None) -> None:
        """The manager switched the active conversation; reset per-view state."""

    def list_conversations(self, mgr: SessionManager) -> list[ConversationSummary]:
        """All known conversations, newest first."""

    def get_latest_conversation_id(self) -> str | None:
        """The most recently updated conversation, cheaply (no content reads)."""

    def get_transcript_path(self, conversation_id: str) -> Path | None:
        """Where the conversation lives on disk, or None (API-backed agents)."""

    def is_known_conversation(self, conversation_id: str) -> bool:
        """Whether an id resolves to a real conversation (switch guard)."""

    async def load_steps(self, mgr: SessionManager, conversation_id: str) -> list[TranscriptStep]:
        """Full step history for a conversation (initial load on switch)."""

    async def load_conversation(self, mgr: SessionManager, conversation_id: str) -> dict[str, Any] | None:
        """Detail payload for an *inactive* conversation, or None if unknown."""

    def summary_of(self, mgr: SessionManager, conversation_id: str | None) -> dict[str, Any] | None:
        """One conversation's summary as clients need it to name a session."""

    async def send_prompt(self, mgr: SessionManager, prompt: str, conversation_id: str | None = None) -> str:
        """Deliver a prompt; returns how it was delivered.

        `conversation_id` is the session the sender was looking at. The active
        session moves on its own now (the phone follows the desktop), so a
        prompt that does not say where it came from can land somewhere its
        author will never see it.
        """

    async def deliver_resolution(self, mgr: SessionManager, approval: dict[str, Any], payload: dict[str, Any]) -> None:
        """Carry the phone's decision to the agent (agy: nothing, the blocked
        hook call carries it; opencode: a REST reply)."""


def make_backend(config: RemoteConfig) -> AgentBackend:
    """The backend for the agent named in the config."""
    if config.agent == "opencode":
        from .opencode_backend import OpencodeBackend

        return OpencodeBackend(config)
    return AgyBackend(config)


# ---------------------------------------------------------------------------
# agy
# ---------------------------------------------------------------------------


class AgyBackend:
    """Antigravity CLI: brain-dir transcript tailing plus PreToolUse hooks."""

    name = "agy"

    def __init__(self, config: RemoteConfig) -> None:
        self.brain_dir = config.brain_dir
        #: Read offset for the active conversation's transcript.
        self._last_file_pos = 0
        #: Parsed summaries keyed by transcript path, invalidated on
        #: (mtime, size). Without this the watcher re-read every transcript
        #: several times a second.
        self._summary_cache: dict[Path, tuple[float, int, ConversationSummary]] = {}
        #: Number of transcripts actually parsed; asserted on in tests.
        self.parse_count = 0

    async def start(self, mgr: SessionManager) -> None:
        self.brain_dir.mkdir(parents=True, exist_ok=True)

    async def stop(self) -> None:
        pass

    async def tick(self, mgr: SessionManager) -> None:
        """Follow a newer conversation, then tail the active one's transcript."""
        await mgr.follow_latest_conversation()

        if not mgr.active_conversation_id:
            return
        path = self.get_transcript_path(mgr.active_conversation_id)
        if not path or not path.exists():
            return

        try:
            stat = path.stat()
        except OSError:
            return
        if stat.st_size <= self._last_file_pos:
            return

        new_steps, self._last_file_pos = self._read_file(path, self._last_file_pos)
        for step in new_steps:
            await mgr.broadcast(
                {
                    "event": "step_added",
                    "data": {
                        "conversation_id": mgr.active_conversation_id,
                        "step": step.model_dump(),
                    },
                }
            )

    def on_switch(self, conversation_id: str | None) -> None:
        self._last_file_pos = 0

    # -- discovery ----------------------------------------------------------

    def _iter_transcripts(self) -> list[tuple[str, Path]]:
        """List (conversation_id, transcript path) pairs using stat only."""
        if not self.brain_dir.exists():
            return []

        found: list[tuple[str, Path]] = []
        for path in self.brain_dir.iterdir():
            if not path.is_dir():
                continue
            log_path = path / ".system_generated" / "logs" / "transcript.jsonl"
            if not log_path.exists():
                log_path = path / "transcript.jsonl"
                if not log_path.exists():
                    continue
            found.append((path.name, log_path))
        return found

    def _summarize(self, mgr: SessionManager, conversation_id: str, log_path: Path) -> ConversationSummary | None:
        """Parse one transcript into a summary, reusing the cache when possible."""
        try:
            stat = log_path.stat()
        except OSError:
            return None

        cached = self._summary_cache.get(log_path)
        if cached and cached[0] == stat.st_mtime and cached[1] == stat.st_size:
            summary = cached[2].model_copy()
        else:
            try:
                summary = self._parse_transcript(conversation_id, log_path, stat)
            except Exception as e:
                logger.debug("Failed reading conversation %s: %s", conversation_id, e)
                return None
            self._summary_cache[log_path] = (stat.st_mtime, stat.st_size, summary.model_copy())

        # These change without the file changing, so refresh them every time.
        summary.is_active = conversation_id == mgr.active_conversation_id
        summary.has_pending_approval = any(
            a.get("conversation_id") == conversation_id and a.get("status") == "pending"
            for a in mgr._pending_approvals.values()
        )
        return summary

    def _parse_transcript(self, conversation_id: str, log_path: Path, stat: os.stat_result) -> ConversationSummary:
        """Read a transcript end to end and build its summary."""
        self.parse_count += 1
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
                except Exception:
                    continue
                step_type = obj.get("type", "")
                content = obj.get("content") or ""
                if step_type == "USER_INPUT":
                    # The drawer titles every conversation from this; unwrapped,
                    # every one of them reads "<USER_REQUEST>".
                    content = clean_user_content(content)
                    if not first_prompt:
                        first_prompt = content[:100]
                    last_prompt = content[:100]
                elif step_type == "PLANNER_RESPONSE" and content:
                    last_response = content[:150]

        return ConversationSummary(
            id=conversation_id,
            title=first_prompt or f"Session {conversation_id[:8]}",
            created_at=datetime.fromtimestamp(stat.st_ctime),
            updated_at=datetime.fromtimestamp(stat.st_mtime),
            step_count=step_count,
            last_user_message=last_prompt,
            last_model_response=last_response,
        )

    def list_conversations(self, mgr: SessionManager) -> list[ConversationSummary]:
        """Scan brain_dir and return a sorted list of conversation summaries."""
        summaries = [
            summary
            for conversation_id, log_path in self._iter_transcripts()
            if (summary := self._summarize(mgr, conversation_id, log_path)) is not None
        ]
        summaries.sort(key=lambda s: s.updated_at or datetime.min, reverse=True)
        return summaries

    def get_latest_conversation_id(self) -> str | None:
        """Find the most recently updated conversation ID from mtimes alone.

        The watcher loop asks this several times a second, so it must not read
        file contents: parsing every transcript here kept a core busy full time
        on a brain directory of any real size.
        """
        newest_id: str | None = None
        newest_mtime = float("-inf")
        for conversation_id, log_path in self._iter_transcripts():
            try:
                mtime = log_path.stat().st_mtime
            except OSError:
                continue
            if mtime > newest_mtime:
                newest_mtime = mtime
                newest_id = conversation_id
        return newest_id

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

    def is_known_conversation(self, conversation_id: str) -> bool:
        return self.get_transcript_path(conversation_id) is not None

    # -- steps ---------------------------------------------------------------

    def _read_file(self, path: Path, from_pos: int) -> tuple[list[TranscriptStep], int]:
        """Read steps from `from_pos` (0 = start). Returns (steps, new_pos)."""
        new_steps: list[TranscriptStep] = []
        index = 0
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                if from_pos:
                    f.seek(from_pos)

                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        data = json.loads(line)
                        source = data.get("source", "UNKNOWN")
                        step_type = data.get("type", "UNKNOWN")
                        content = data.get("content")
                        # Only a user's own words carry agy's envelope; model
                        # output must reach the phone exactly as written.
                        if step_type == "USER_INPUT" or source in ("USER_INPUT", "USER_EXPLICIT"):
                            content = clean_user_content(content)

                        step = TranscriptStep(
                            step_index=data.get("step_index", index),
                            source=source,
                            type=step_type,
                            status=data.get("status", "DONE"),
                            created_at=data.get("created_at"),
                            content=content,
                            thinking=data.get("thinking"),
                            tool_calls=normalize_tool_calls(data.get("tool_calls") or []),
                            truncated_fields=data.get("truncated_fields") or [],
                            scaffolding=is_scaffolding(step_type, source),
                        )
                        new_steps.append(step)
                        index += 1
                    except Exception as err:
                        logger.debug("Skipping unparseable transcript line: %s", err)

                pos = f.tell()
        except Exception as e:
            logger.debug("Error reading transcript file %s: %s", path, e)
            pos = from_pos

        return new_steps, pos

    async def load_steps(self, mgr: SessionManager, conversation_id: str) -> list[TranscriptStep]:
        path = self.get_transcript_path(conversation_id)
        if not path or not path.exists():
            return []
        steps, self._last_file_pos = self._read_file(path, 0)
        return steps

    async def load_conversation(self, mgr: SessionManager, conversation_id: str) -> dict[str, Any] | None:
        path = self.get_transcript_path(conversation_id)
        if not path or not path.exists():
            return None
        steps, _ = self._read_file(path, 0)
        return {
            "id": conversation_id,
            "steps": [s.model_dump() for s in steps],
            "pending_approvals": [],
        }

    def summary_of(self, mgr: SessionManager, conversation_id: str | None) -> dict[str, Any] | None:
        """The summary for one conversation, as clients need it to name a session."""
        if not conversation_id:
            return None

        log_path = self.get_transcript_path(conversation_id)
        if not log_path or not log_path.exists():
            return None

        summary = self._summarize(mgr, conversation_id, log_path)
        return summary.model_dump(mode="json") if summary else None

    # -- interaction ---------------------------------------------------------

    async def send_prompt(self, mgr: SessionManager, prompt: str, conversation_id: str | None = None) -> str:
        """Type into whichever supervisor is live; fall back to a broadcast.

        agy drives one session per supervisor, so there is nowhere else the
        typing could go and `conversation_id` is accepted but unused.
        """
        from .pty_runner import get_pty_supervisor
        from .tmux_runner import get_tmux_supervisor

        tmux = get_tmux_supervisor()
        if tmux and tmux.has_session():
            tmux.inject_input(prompt)
            return "tmux"

        pty = get_pty_supervisor()
        if pty and pty.running:
            pty.inject_input(prompt)
            return "pty"

        return "broadcast"

    async def deliver_resolution(self, mgr: SessionManager, approval: dict[str, Any], payload: dict[str, Any]) -> None:
        # The decision travels back to agy through the still-blocked hook call;
        # the manager's approval future carries it, so there is nothing to do.
        pass
