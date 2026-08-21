"""Data models for agy-remote."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator


class ToolCall(BaseModel):
    """Details of a tool invocation."""

    id: str | None = None
    name: str
    args: dict[str, Any] = Field(default_factory=dict)
    status: str | None = None
    result: Any | None = None
    error: str | None = None


class TranscriptStep(BaseModel):
    """A single step from transcript.jsonl."""

    step_index: int
    source: str = "UNKNOWN"  # e.g., "USER_EXPLICIT", "USER_INPUT", "MODEL", "SYSTEM"
    type: str = "UNKNOWN"  # e.g., "USER_INPUT", "PLANNER_RESPONSE", "TOOL_OUTPUT"
    status: str = "DONE"  # e.g., "DONE", "ERROR", "RUNNING"
    created_at: str | None = None
    content: str | None = None
    thinking: str | None = None
    tool_calls: list[dict[str, Any]] = Field(default_factory=list)
    truncated_fields: list[str] = Field(default_factory=list)
    #: agy talking to itself: checkpoints and system messages, which read like
    #: conversation but are not addressed to the user.
    scaffolding: bool = False


class ConversationSummary(BaseModel):
    """Metadata summary of a conversation session."""

    id: str
    title: str = "Conversation"
    created_at: datetime | None = None
    updated_at: datetime | None = None
    step_count: int = 0
    last_user_message: str | None = None
    last_model_response: str | None = None
    is_active: bool = False
    has_pending_approval: bool = False


class PendingApproval(BaseModel):
    """A tool execution waiting for user permission."""

    id: str
    conversation_id: str
    tool_name: str
    args: dict[str, Any] = Field(default_factory=dict)
    created_at: str = Field(default_factory=lambda: datetime.now().isoformat())
    status: Literal["pending", "allowed", "denied", "force_asked"] = "pending"
    reason: str | None = None


class UserPromptRequest(BaseModel):
    """Request payload to send a prompt to an active session."""

    prompt: str
    conversation_id: str | None = None


class KeyPressRequest(BaseModel):
    """A single named key the phone wants pressed in the supervised session."""

    key: str

    @field_validator("key")
    @classmethod
    def _known_key(cls, value: str) -> str:
        from .keys import is_known_key

        if not is_known_key(value):
            raise ValueError(f"unknown key: {value!r}")
        return value


class ApprovalResponseRequest(BaseModel):
    """Request payload to resolve a tool approval."""

    decision: Literal["allow", "deny", "force_ask"]
    reason: str | None = None
    overwrite_args: dict[str, Any] | None = None


class ServerEvent(BaseModel):
    """Event pushed from server to WebSocket clients."""

    event: (
        str  # "init", "step_added", "step_updated", "approval_request", "approval_resolved", "session_switched", "pong"
    )
    data: dict[str, Any] = Field(default_factory=dict)
