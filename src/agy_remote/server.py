"""FastAPI server providing REST APIs, WebSockets, and PWA static assets."""

from __future__ import annotations

import logging
import uuid
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import (
    FastAPI,
    HTTPException,
    Query,
    Request,
    Security,
    WebSocket,
    WebSocketDisconnect,
    status,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.security.api_key import APIKeyHeader
from fastapi.staticfiles import StaticFiles

from .config import RemoteConfig, get_config
from .models import (
    ApprovalResponseRequest,
    ConversationSummary,
    UserPromptRequest,
)
from .pty_runner import get_pty_supervisor
from .session_manager import SessionManager

logger = logging.getLogger("agy_remote.server")
STATIC_DIR = Path(__file__).parent / "static"

api_key_header = APIKeyHeader(name="X-Auth-Token", auto_error=False)


def create_app(config: RemoteConfig | None = None) -> FastAPI:
    """Factory creating configured FastAPI app."""
    cfg = config or get_config()
    session_mgr = SessionManager(cfg)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
        """Lifespan context manager to start/stop session manager."""
        await session_mgr.start()
        yield
        await session_mgr.stop()

    app = FastAPI(
        title="Antigravity Remote",
        description="Mobile Remote Web PWA for Antigravity CLI",
        version="26.08.21.1",
        lifespan=lifespan,
    )
    app.state.session_manager = session_mgr
    app.state.config = cfg

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    def get_mgr(req: Request) -> SessionManager:
        return getattr(req.app.state, "session_manager", session_mgr)

    def verify_auth(
        request: Request,
        token_query: str | None = Query(None, alias="token"),
        token_header: str | None = Security(api_key_header),
    ) -> bool:
        if not cfg.enable_auth:
            return True
        provided = token_header or token_query
        if provided != cfg.auth_token:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid or missing authentication token",
            )
        return True

    # -------------------------------------------------------------------------
    # REST Endpoints
    # -------------------------------------------------------------------------

    @app.get("/api/status")
    async def get_status(request: Request, token: str | None = Query(None)) -> dict[str, Any]:
        """Return server status and connection info."""
        if cfg.enable_auth and token != cfg.auth_token:
            return {
                "auth_required": True,
                "authenticated": False,
                "version": "v26.08.21.1",
            }

        mgr = get_mgr(request)
        pty = get_pty_supervisor()
        return {
            "auth_required": cfg.enable_auth,
            "authenticated": True,
            "version": "v26.08.21.1",
            "active_conversation_id": mgr.active_conversation_id,
            "supervisor_running": pty is not None and pty.running,
            "primary_mobile_url": cfg.get_primary_mobile_url(),
            "connect_urls": cfg.get_connect_urls(),
            "connected_clients": len(mgr._connected_clients),
        }

    @app.get("/api/conversations")
    async def list_conversations(
        request: Request,
        token: str | None = Query(None),
        token_header: str | None = Security(api_key_header),
    ) -> list[ConversationSummary]:
        """List all discovered Antigravity conversations."""
        verify_auth(request, token, token_header)
        mgr = get_mgr(request)
        return mgr.list_conversations()

    @app.get("/api/conversations/{conversation_id}")
    async def get_conversation(
        conversation_id: str,
        request: Request,
        token: str | None = Query(None),
        token_header: str | None = Security(api_key_header),
    ) -> dict[str, Any]:
        """Get details and step history of a specific conversation."""
        verify_auth(request, token, token_header)
        mgr = get_mgr(request)
        if mgr.active_conversation_id == conversation_id:
            steps = [s.model_dump() for s in mgr.active_steps]
        else:
            path = mgr.get_transcript_path(conversation_id)
            if not path or not path.exists():
                raise HTTPException(status_code=404, detail="Conversation not found")
            temp_mgr = SessionManager(cfg)
            steps = [s.model_dump() for s in await temp_mgr._read_new_steps(path, initial=True)]

        return {
            "id": conversation_id,
            "steps": steps,
            "pending_approvals": mgr.get_active_pending_approvals()
            if mgr.active_conversation_id == conversation_id
            else [],
        }

    @app.post("/api/conversations/{conversation_id}/switch")
    async def switch_conversation_endpoint(
        conversation_id: str,
        request: Request,
        token: str | None = Query(None),
        token_header: str | None = Security(api_key_header),
    ) -> dict[str, Any]:
        """Switch active conversation."""
        verify_auth(request, token, token_header)
        mgr = get_mgr(request)
        success = await mgr.switch_conversation(conversation_id)
        if not success:
            raise HTTPException(status_code=404, detail="Could not switch conversation")
        return {"status": "ok", "active_conversation_id": conversation_id}

    @app.post("/api/prompt")
    async def send_prompt(
        req: UserPromptRequest,
        request: Request,
        token: str | None = Query(None),
        token_header: str | None = Security(api_key_header),
    ) -> dict[str, Any]:
        """Send user prompt from mobile UI into the active session."""
        verify_auth(request, token, token_header)
        pty = get_pty_supervisor()
        if pty and pty.running:
            pty.inject_input(req.prompt)
            return {"status": "ok", "delivered_via": "pty"}

        mgr = get_mgr(request)
        await mgr.broadcast(
            {
                "event": "prompt_sent",
                "data": {
                    "prompt": req.prompt,
                    "conversation_id": req.conversation_id or mgr.active_conversation_id,
                },
            }
        )
        return {
            "status": "ok",
            "delivered_via": "broadcast",
            "message": "Prompt broadcasted. To enable direct CLI typing, launch with 'agy-remote run'.",
        }

    @app.post("/api/approvals/{approval_id}/respond")
    async def respond_approval(
        approval_id: str,
        req: ApprovalResponseRequest,
        request: Request,
        token: str | None = Query(None),
        token_header: str | None = Security(api_key_header),
    ) -> dict[str, Any]:
        """Approve or deny a pending tool call from mobile UI."""
        verify_auth(request, token, token_header)
        mgr = get_mgr(request)
        resolved = await mgr.resolve_approval(approval_id, req)
        if not resolved:
            raise HTTPException(status_code=404, detail="Pending approval not found or expired")
        return {"status": "ok", "decision": req.decision}

    @app.post("/api/hook/pre-tool")
    async def hook_pre_tool(
        request: Request,
        token_header: str | None = Security(api_key_header),
    ) -> JSONResponse:
        """Endpoint called by agy CLI PreToolUse hook."""
        if cfg.enable_auth and token_header != cfg.auth_token:
            raise HTTPException(status_code=401, detail="Unauthorized hook call")

        payload = await request.json()
        tool_call = payload.get("toolCall", {})
        tool_name = tool_call.get("name", "unknown_tool")
        args = tool_call.get("args", {})
        conversation_id = payload.get("conversationId", "default")
        approval_id = str(uuid.uuid4())

        mgr = get_mgr(request)
        decision_payload = await mgr.request_approval(
            approval_id=approval_id,
            conversation_id=conversation_id,
            tool_name=tool_name,
            args=args,
        )
        return JSONResponse(content=decision_payload)

    # -------------------------------------------------------------------------
    # WebSocket Endpoint
    # -------------------------------------------------------------------------

    @app.websocket("/ws")
    async def websocket_endpoint(
        websocket: WebSocket,
        token: str | None = Query(None),
    ) -> None:
        """Bidirectional WebSocket for live updates and mobile interaction."""
        if cfg.enable_auth and token != cfg.auth_token:
            await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
            return

        await websocket.accept()
        mgr = session_mgr
        await mgr.register_client(websocket)

        try:
            while True:
                msg = await websocket.receive_json()
                action = msg.get("action")
                data = msg.get("data", {})

                if action == "ping":
                    await websocket.send_json({"event": "pong"})
                elif action == "send_prompt":
                    prompt_text = data.get("prompt", "")
                    if prompt_text:
                        pty = get_pty_supervisor()
                        if pty and pty.running:
                            pty.inject_input(prompt_text)
                        await mgr.broadcast(
                            {
                                "event": "prompt_sent",
                                "data": {"prompt": prompt_text},
                            }
                        )
                elif action == "approve_tool":
                    approval_id = data.get("approval_id")
                    decision = data.get("decision", "allow")
                    reason = data.get("reason")
                    if approval_id:
                        await mgr.resolve_approval(
                            approval_id,
                            ApprovalResponseRequest(decision=decision, reason=reason),
                        )
                elif action == "switch_conversation":
                    target_id = data.get("conversation_id")
                    if target_id:
                        await mgr.switch_conversation(target_id)
        except (WebSocketDisconnect, Exception):
            pass
        finally:
            mgr.unregister_client(websocket)

    # -------------------------------------------------------------------------
    # PWA Static Files & Fallback
    # -------------------------------------------------------------------------

    if STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

        @app.get("/manifest.json")
        async def manifest() -> FileResponse:
            return FileResponse(STATIC_DIR / "manifest.json", media_type="application/manifest+json")

        @app.get("/sw.js")
        async def service_worker() -> FileResponse:
            return FileResponse(STATIC_DIR / "sw.js", media_type="application/javascript")

        @app.get("/")
        async def index() -> FileResponse:
            return FileResponse(STATIC_DIR / "index.html")

    return app


app = create_app()
