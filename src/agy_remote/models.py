"""Data models for agy-remote."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator


class ToolCall(BaseModel):
    """Details of a tool invocation."""

    id: str | None = None
    name: str
    args: dict[str, Any] = Field(default_factory=dict)
    status: str | None = None
    result: Any | None = None
    error: str | None = None


class TranscriptStep(BaseModel):
    """A single step from a transcript.

    `id` is the agent's own identity for the step (agy: none, so clients fall
    back to `step_index`). It is what lets a client
    replace a step in place when `step_updated` arrives.
    """

    id: str | None = None
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


class SessionRecord(BaseModel):
    """A supervised agent session tracked by the server registry."""

    id: str
    tmux_name: str | None = None
    pane_target: str | None = None
    workdir: Any | None = None  # Path or str
    conversation_id: str | None = None
    busy: bool = False
    created_at: datetime = Field(default_factory=datetime.now)
    last_activity_at: datetime = Field(default_factory=datetime.now)


class UserPromptRequest(BaseModel):
    """Request payload to send a prompt to an active session."""

    prompt: str
    conversation_id: str | None = None


class NewSessionRequest(BaseModel):
    """The phone asks the server to clone a repo and start an agy on it.

    The server is the operator's always-on machine: it owns the clone, the
    tmux session and the supervision, and reports back over events.
    """

    repo_url: str
    branch: str | None = None
    #: The task to hand the agent once the code is there, or None to watch it idle.
    task: str | None = None
    #: Display name; derived from the repo URL when absent.
    name: str | None = None
    #: Default: the server clones (deterministic, no approval round-trip).
    #: True: the clone instruction becomes the seed prompt, so it is an
    #: ordinary tool call the human approves on the phone.
    let_agent_clone: bool = False

    @model_validator(mode="before")
    @classmethod
    def _accept_legacy_field_names(cls, data: Any) -> Any:
        """The plan and the first PWA build named the fields differently."""
        if isinstance(data, dict):
            if not data.get("repo_url") and data.get("repo"):
                data["repo_url"] = data.pop("repo")
            if not data.get("task") and data.get("prompt"):
                data["task"] = data.pop("prompt")
        return data


class KeyPressRequest(BaseModel):
    """A single named key the phone wants pressed in the supervised session."""

    key: str
    conversation_id: str | None = None

    @field_validator("key")
    @classmethod
    def _known_key(cls, value: str) -> str:
        from .keys import is_known_key

        if not is_known_key(value):
            raise ValueError(f"unknown key: {value!r}")
        return value


class ApprovalResponseRequest(BaseModel):
    """Request payload to resolve a tool approval.

    `always` means "approve future matching requests". agy has no such
    outcome, so the server maps it to a plain allow.
    """

    decision: Literal["allow", "deny", "force_ask", "always"]
    reason: str | None = None
    overwrite_args: dict[str, Any] | None = None


class ServerEvent(BaseModel):
    """Event pushed from server to WebSocket clients."""

    event: (
        str  # "init", "step_added", "step_updated", "approval_request", "approval_resolved", "session_switched", "pong"
    )
    data: dict[str, Any] = Field(default_factory=dict)
