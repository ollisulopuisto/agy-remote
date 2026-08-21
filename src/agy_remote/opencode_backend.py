"""opencode backend: the SSE event stream plus its REST API.

Where agy writes private file formats and blocks a hook process, opencode runs
a first-class HTTP server (embedded in the TUI, or `opencode serve`) that
publishes an SSE event stream and answers REST calls. This backend consumes
`GET /event`, maps opencode's messages and parts onto the normalized step
model, turns `permission.updated` events into phone approvals, and carries the
phone's answer back with `POST /session/:id/permissions/:id`.

The server is always reached on loopback: the phone-facing side (token, E2EE,
TLS) stays agy-remote's, and opencode's own port never leaves this machine.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import httpx

from .config import RemoteConfig
from .models import ApprovalResponseRequest, ConversationSummary, TranscriptStep

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .session_manager import SessionManager

logger = logging.getLogger("agy_remote.opencode")

#: opencode tool states -> the step status vocabulary the PWA understands.
_TOOL_STATUS = {
    "pending": "PENDING",
    "running": "RUNNING",
    "completed": "DONE",
    "error": "ERROR",
}

#: The phone's decision -> opencode's permission response vocabulary.
_RESPONSE = {
    "allow": "once",
    "always": "always",
    "deny": "reject",
    "force_ask": "reject",
}


def _dt(ms: int | float | str | None) -> datetime | None:
    """opencode's epoch-milliseconds or ISO string -> datetime."""
    if not ms:
        return None
    if isinstance(ms, datetime):
        return ms if ms.tzinfo else ms.replace(tzinfo=UTC)
    if isinstance(ms, str):
        if ms.isdigit():
            ms = int(ms)
        else:
            try:
                parsed = datetime.fromisoformat(ms)
                return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
            except ValueError:
                return None
    try:
        return datetime.fromtimestamp(int(ms) / 1000, tz=UTC)
    except (OverflowError, OSError, ValueError, TypeError):
        return None


def _iso(ms: int | float | str | None) -> str | None:
    """opencode's epoch-milliseconds -> the ISO string steps carry."""
    dt = _dt(ms)
    return dt.isoformat() if dt else None


class OpencodeBackend:
    """opencode: SSE-driven steps, REST-driven prompts and permissions."""

    name = "opencode"

    def __init__(self, config: RemoteConfig) -> None:
        if not config.opencode_port:
            raise ValueError("opencode backend needs an opencode_port (the loopback port of the opencode server)")
        self.base_url = f"http://127.0.0.1:{config.opencode_port}"
        self._client: httpx.AsyncClient | None = None
        self._sse_task: asyncio.Task[None] | None = None
        self._running = False
        #: Known sessions, id -> summary. Refreshed on connect and on events.
        self._sessions: dict[str, ConversationSummary] = {}
        #: Live steps of the active session, message id -> step. The manager's
        #: `active_steps` holds the same objects, so a mutation here is the
        #: update the phone receives.
        self._steps: dict[str, TranscriptStep] = {}
        #: Raw parts of the active session, message id -> (part id -> part).
        #: Rebuilding a step from its parts keeps streamed text, reasoning and
        #: tool state consistent no matter the order events arrive in.
        self._parts: dict[str, dict[str, dict[str, Any]]] = {}
        #: opencode permission id -> our approval id, and back.
        self._approval_ids: dict[str, str] = {}
        self._oc_ids: dict[str, str] = {}

    # -- lifecycle -----------------------------------------------------------

    async def start(self, mgr: SessionManager) -> None:
        self._running = True
        self._client = httpx.AsyncClient(base_url=self.base_url, timeout=10.0)
        await self._refresh_sessions()
        self._sse_task = asyncio.create_task(self._consume_events(mgr))

    async def stop(self) -> None:
        self._running = False
        if self._sse_task:
            self._sse_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._sse_task
            self._sse_task = None
        if self._client:
            await self._client.aclose()
            self._client = None

    async def tick(self, mgr: SessionManager) -> None:
        # The SSE stream drives everything; the manager's loop only needs the
        # shared duties (terminal mirror, expired clients), which it does itself.
        pass

    def on_switch(self, conversation_id: str | None) -> None:
        # The live step map belongs to one session at a time; load_steps
        # repopulates it for the newly active one.
        self._steps = {}
        self._parts = {}

    # -- SSE ------------------------------------------------------------------

    async def _consume_events(self, mgr: SessionManager) -> None:
        """Follow `GET /event`, reconnecting with backoff while we are alive.

        The opencode server may start after us (`run` launches both in one go)
        or restart under us, so a dropped stream is routine, not an error.
        """
        backoff = 1.0
        while self._running and self._client:
            try:
                async with self._client.stream("GET", "/event") as resp:
                    backoff = 1.0
                    async for line in resp.aiter_lines():
                        event = self._parse_sse_line(line)
                        if event is None:
                            continue
                        try:
                            await self._handle_event(mgr, event)
                        except Exception as e:
                            logger.debug("Dropping opencode event %s: %s", event.get("type"), e)
            except asyncio.CancelledError:
                return
            except Exception as e:
                if not self._running:
                    return
                logger.debug("opencode event stream down (%s); retrying in %.0fs", e, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 15.0)

    @staticmethod
    def _parse_sse_line(line: str) -> dict[str, Any] | None:
        """One SSE line -> the event object, or None.

        The stream is `data: {"id","type","properties"}` per event. Anything
        else (comments, event:/id: lines, blanks) is not an event.
        """
        line = line.strip()
        if not line.startswith("data:"):
            return None
        try:
            event = json.loads(line[len("data:") :].strip())
        except (json.JSONDecodeError, ValueError):
            return None
        return event if isinstance(event, dict) and event.get("type") else None

    async def _handle_event(self, mgr: SessionManager, event: dict[str, Any]) -> None:
        etype = event.get("type")
        props = event.get("properties") or {}

        if etype == "server.connected":
            # (Re)connected: the session list may have changed while we were
            # dark, so rebuild it before trusting follow-latest.
            await self._refresh_sessions()
        elif etype in ("session.created", "session.updated"):
            info = props.get("info") or props
            before = self._sessions.get((info.get("info") or info).get("id") or "")
            previous_title = before.title if before else None
            self._upsert_session(info)
            sid_now = (info.get("info") or info).get("id")
            if (
                etype == "session.updated"
                and sid_now
                and sid_now == mgr.active_conversation_id
                and self._sessions[sid_now].title != previous_title
            ):
                # opencode titles a session from its first exchange. The header
                # read a title only at `init` or on a switch, so the phone went
                # on showing `New session - <timestamp>` long after the desktop
                # had renamed it -- which reads as being in a different session.
                await mgr.broadcast(
                    {
                        "event": "session_renamed",
                        "data": {
                            "conversation_id": sid_now,
                            "conversation": self.summary_of(mgr, sid_now),
                        },
                    }
                )
            if (
                etype == "session.created"
                and info.get("id")
                and mgr.follow_latest
                and info["id"] != mgr.active_conversation_id
            ):
                # A brand-new session is what "follow latest" exists for: the
                # desktop started one and the phone should move to it.
                await mgr.switch_conversation(info["id"])
        elif etype == "session.deleted":
            info = props.get("info") or props
            self._sessions.pop(info.get("id"), None)
        elif etype in ("message.created", "message.updated", "message.part.created", "message.part.updated"):
            await self._handle_message(mgr, etype, props)
        elif etype in ("permission.updated", "permission.created"):
            await self._handle_permission_ask(mgr, props)
        elif etype == "permission.replied":
            await self._handle_permission_replied(mgr, props)

    # -- messages -> steps -----------------------------------------------------

    async def _handle_message(self, mgr: SessionManager, etype: str, props: dict[str, Any]) -> None:
        if etype in ("message.created", "message.updated"):
            info = props.get("info") or props
            sid = info.get("sessionID")
            if not sid:
                return
            if sid != mgr.active_conversation_id:
                # Changing sessions in the TUI announces nothing -- a message is
                # the only sign the desktop moved. `session.created` covers only
                # a brand-new session, so a phone that followed the empty one
                # `opencode attach` opens at launch sat on "no active steps"
                # while the work went on in a session that already existed.
                if etype == "message.created" and mgr.follow_latest:
                    await mgr.switch_conversation(sid)
                return
            mid = info.get("id")
            if not mid:
                return
            step = self._step_for(info)
            if step is None:
                return

            if "parts" in info:
                by_id = self._parts.setdefault(mid, {})
                for part in info.get("parts") or []:
                    if isinstance(part, dict) and part.get("id"):
                        by_id[part["id"]] = part
            if mid in self._parts:
                self._rebuild_step(step, self._parts[mid])

            await self._emit_step(mgr, step, sid)
            return

        part = props.get("part") or props
        sid = part.get("sessionID")
        if not sid or sid != mgr.active_conversation_id:
            return
        mid = part.get("messageID")
        if not mid:
            return

        step = self._steps.get(mid)
        if step is None:
            # A part arrived before its message (or the message was pruned from
            # our view): rebuild the step from what the part tells us.
            step = TranscriptStep(
                id=mid,
                step_index=len(mgr.active_steps),
                source="MODEL",
                type="PLANNER_RESPONSE",
            )
            self._steps[mid] = step

        by_id = self._parts.setdefault(mid, {})
        if part.get("id"):
            by_id[part["id"]] = part
        self._rebuild_step(step, by_id)
        await self._emit_step(mgr, step, sid)

    def _step_for(self, info: dict[str, Any]) -> TranscriptStep | None:
        """The step for a `message.updated` info, creating it on first sight."""
        mid = info.get("id")
        if not mid:
            return None
        role = info.get("role")
        time_info = info.get("time") if isinstance(info.get("time"), dict) else {}
        created_time = time_info.get("created") or info.get("created_at") or info.get("created")
        step = self._steps.get(mid)
        if step is None:
            step = TranscriptStep(
                id=mid,
                step_index=0,  # renumbered below if it enters the active list
                source="USER_INPUT" if role == "user" else "MODEL",
                type="USER_INPUT" if role == "user" else "PLANNER_RESPONSE",
                created_at=_iso(created_time),
            )
            self._steps[mid] = step
            return step

        # A part may have created the step before its message did; correct the
        # identity rather than leaving a user prompt labelled as model output.
        if role == "user" and step.type != "USER_INPUT":
            step.source, step.type = "USER_INPUT", "USER_INPUT"
        elif role != "user" and step.type != "PLANNER_RESPONSE":
            step.source, step.type = "MODEL", "PLANNER_RESPONSE"
        if not step.created_at:
            step.created_at = _iso(created_time)
        return step

    def _rebuild_step(self, step: TranscriptStep, by_id: dict[str, dict[str, Any]]) -> None:
        """Recompute a step's content, thinking and tool calls from its parts."""
        texts: list[str] = []
        thinkings: list[str] = []
        tools: list[dict[str, Any]] = []

        for part in by_id.values():
            ptype = part.get("type")
            if ptype == "text":
                if part.get("ignored"):
                    continue
                if part.get("synthetic"):
                    # opencode's own scaffolding (system notes), not the model
                    # talking to the user.
                    step.scaffolding = True
                text = part.get("text") or ""
                if text:
                    texts.append(text)
            elif ptype == "reasoning":
                text = part.get("text") or ""
                if text:
                    thinkings.append(text)
            elif ptype == "tool":
                tools.append(self._tool_call(part))

        step.content = "\n\n".join(texts) if texts else None
        step.thinking = "\n\n".join(thinkings) if thinkings else None
        step.tool_calls = tools

    @staticmethod
    def _tool_call(part: dict[str, Any]) -> dict[str, Any]:
        state = part.get("state") or {}
        call: dict[str, Any] = {
            "id": part.get("callID"),
            "name": part.get("tool") or "tool",
            "args": state.get("input") or {},
            "status": _TOOL_STATUS.get(state.get("status") or "", "DONE"),
        }
        if state.get("status") == "completed":
            call["result"] = state.get("output")
        elif state.get("status") == "error":
            call["error"] = state.get("error")
        return call

    async def _emit_step(self, mgr: SessionManager, step: TranscriptStep, sid: str) -> None:
        """Broadcast a step, adding it to the active list on first appearance."""
        idx = next((i for i, s in enumerate(mgr.active_steps) if s is step or (step.id and s.id == step.id)), None)
        if idx is None:
            step.step_index = len(mgr.active_steps)
            mgr.active_steps.append(step)
            event = "step_added"
        else:
            step.step_index = idx
            mgr.active_steps[idx] = step
            event = "step_updated"
        await mgr.broadcast(
            {
                "event": event,
                "data": {"conversation_id": sid, "step": step.model_dump()},
            }
        )

    # -- permissions ------------------------------------------------------------

    async def _handle_permission_ask(self, mgr: SessionManager, props: dict[str, Any]) -> None:
        """A permission request: register it, notify the phone, wait for a tap.

        opencode keeps the request open until someone answers -- in the TUI or
        on the phone -- so there is no timeout to enforce here.
        """
        p = props.get("permission") or props.get("info") or props
        oc_id = p.get("id") or p.get("permissionID")
        sid = p.get("sessionID")
        if not oc_id or not sid or oc_id in self._approval_ids:
            return

        approval_id = str(uuid.uuid4())
        self._approval_ids[oc_id] = approval_id
        self._oc_ids[approval_id] = oc_id

        tool_name = p.get("type") or p.get("tool") or "tool"
        args: dict[str, Any] = {}
        if p.get("pattern") is not None:
            args["pattern"] = p["pattern"]
        if p.get("title"):
            args["title"] = p["title"]
        if p.get("command"):
            args["CommandLine"] = p["command"]
        if p.get("input"):
            args["input"] = p["input"]

        await mgr.register_approval(approval_id, sid, tool_name, args)

        from .push import get_push_manager

        body = str(p.get("title") or p.get("pattern") or p.get("command") or "Action requested")
        if sid != mgr.active_conversation_id:
            # No banner is drawn for a session the phone is not showing, so the
            # notification has to say where to look.
            summary = self._sessions.get(sid)
            body = f"{body}\nin session: {summary.title if summary else sid}"

        get_push_manager().send_notification(
            title=f"Permission Required: {tool_name}",
            body=body,
            data={"approval_id": approval_id, "type": "approval_request"},
        )

    async def _handle_permission_replied(self, mgr: SessionManager, props: dict[str, Any]) -> None:
        """The request was answered (TUI or phone); clear the phone's banner."""
        p = props.get("permission") or props.get("info") or props
        oc_id = p.get("permissionID") or p.get("id")
        response = p.get("response")
        approval_id = self._approval_ids.pop(oc_id, None) if oc_id else None
        if approval_id:
            self._oc_ids.pop(approval_id, None)
        if not approval_id:
            return

        decision = "allow" if response in ("once", "always") else "deny"
        await mgr.resolve_approval(approval_id, ApprovalResponseRequest(decision=decision), source="agent")

    async def deliver_resolution(self, mgr: SessionManager, approval: dict[str, Any], payload: dict[str, Any]) -> None:
        """Carry the phone's decision to opencode's permission endpoint."""
        oc_id = self._oc_ids.get(approval.get("id", ""))
        sid = approval.get("conversation_id")
        if not oc_id or not sid or not self._client:
            return

        response = _RESPONSE.get(payload.get("decision", "deny"), "reject")
        try:
            r = await self._client.post(
                f"/session/{sid}/permissions/{oc_id}",
                json={"response": response},
            )
            if r.status_code >= 300:
                logger.warning("opencode permission reply rejected: %s %s", r.status_code, r.text[:200])
        except Exception as e:
            logger.warning("Could not deliver permission response to opencode: %s", e)

    # -- sessions -----------------------------------------------------------------

    async def _refresh_sessions(self) -> None:
        if not self._client:
            return
        try:
            r = await self._client.get("/session")
            if r.status_code != 200:
                return
            entries = r.json()
            if isinstance(entries, list):
                for info in entries:
                    if isinstance(info, dict):
                        self._upsert_session(info)
        except Exception as e:
            logger.debug("Could not refresh opencode sessions: %s", e)

    def _upsert_session(self, info: dict[str, Any]) -> None:
        item = info.get("info") if isinstance(info.get("info"), dict) else info
        sid = item.get("id")
        if not sid:
            return
        time_info = item.get("time") if isinstance(item.get("time"), dict) else {}
        created_time = time_info.get("created") or item.get("created_at") or item.get("created")
        updated_time = time_info.get("updated") or item.get("updated_at") or item.get("updated")
        existing = self._sessions.get(sid)
        self._sessions[sid] = ConversationSummary(
            id=sid,
            title=(item.get("title") or f"Session {sid[:8]}")[:100],
            created_at=_dt(created_time),
            updated_at=_dt(updated_time),
            step_count=existing.step_count if existing else 0,
            last_user_message=existing.last_user_message if existing else None,
            last_model_response=existing.last_model_response if existing else None,
        )

    def list_conversations(self, mgr: SessionManager) -> list[ConversationSummary]:
        summaries = [s.model_copy() for s in self._sessions.values()]
        for s in summaries:
            s.is_active = s.id == mgr.active_conversation_id
            s.has_pending_approval = any(
                a.get("conversation_id") == s.id and a.get("status") == "pending"
                for a in mgr._pending_approvals.values()
            )
        summaries.sort(key=lambda s: s.updated_at or datetime.min, reverse=True)
        return summaries

    def get_latest_conversation_id(self) -> str | None:
        if not self._sessions:
            return None
        return max(self._sessions.values(), key=lambda s: s.updated_at or datetime.min).id

    def get_transcript_path(self, conversation_id: str) -> None:
        # API-backed: nothing on disk.
        return None

    def is_known_conversation(self, conversation_id: str) -> bool:
        return conversation_id in self._sessions

    def summary_of(self, mgr: SessionManager, conversation_id: str | None) -> dict[str, Any] | None:
        if not conversation_id:
            return None
        summary = self._sessions.get(conversation_id)
        if summary is None:
            return None
        summary_copy = summary.model_copy()
        summary_copy.is_active = conversation_id == mgr.active_conversation_id
        return summary_copy.model_dump(mode="json")

    # -- steps ---------------------------------------------------------------------

    async def load_steps(self, mgr: SessionManager, conversation_id: str) -> list[TranscriptStep]:
        """Full history of a session, via its message list."""
        self._steps = {}
        self._parts = {}
        if not self._client:
            return []
        try:
            r = await self._client.get(f"/session/{conversation_id}/message")
            if r.status_code != 200:
                return []
            entries = r.json()
        except Exception as e:
            logger.debug("Could not load messages for %s: %s", conversation_id, e)
            return []

        steps: list[TranscriptStep] = []
        if isinstance(entries, list):
            for index, entry in enumerate(entries):
                if not isinstance(entry, dict):
                    continue
                info = entry.get("info") if isinstance(entry.get("info"), dict) else entry
                mid = info.get("id") or entry.get("id")
                if not mid:
                    continue
                role = info.get("role") or entry.get("role")
                time_info = info.get("time") if isinstance(info.get("time"), dict) else {}
                created_time = time_info.get("created") or info.get("created_at") or entry.get("created_at")
                step = TranscriptStep(
                    id=mid,
                    step_index=index,
                    source="USER_INPUT" if role == "user" else "MODEL",
                    type="USER_INPUT" if role == "user" else "PLANNER_RESPONSE",
                    created_at=_iso(created_time),
                )
                by_id = self._parts.setdefault(mid, {})
                parts = entry.get("parts") or info.get("parts") or []
                for part in parts:
                    if isinstance(part, dict) and part.get("id"):
                        by_id[part["id"]] = part
                self._rebuild_step(step, by_id)
                self._steps[mid] = step
                steps.append(step)
        return steps

    async def load_conversation(self, mgr: SessionManager, conversation_id: str) -> dict[str, Any] | None:
        if conversation_id not in self._sessions:
            return None
        steps = await self.load_steps(mgr, conversation_id)
        return {
            "id": conversation_id,
            "steps": [s.model_dump() for s in steps],
            "pending_approvals": [],
        }

    # -- interaction -----------------------------------------------------------------

    async def send_prompt(self, mgr: SessionManager, prompt: str, conversation_id: str | None = None) -> str:
        """Send via the API (immune to TUI focus state); fall back to typing.

        Posted to the session the sender named, not to whatever is active by
        the time this runs -- following the desktop moves that on its own.
        """
        sid = conversation_id or mgr.active_conversation_id
        if self._client and sid:
            try:
                r = await self._client.post(
                    f"/session/{sid}/prompt_async",
                    json={"parts": [{"type": "text", "text": prompt}]},
                )
                if r.status_code in (200, 202, 204):
                    return "opencode"
            except Exception as e:
                logger.debug("opencode prompt_async failed, falling back to pty: %s", e)

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
