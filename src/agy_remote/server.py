"""FastAPI server providing REST APIs, WebSockets, E2EE, Web Push, and PWA static assets."""

from __future__ import annotations

import contextlib
import json
import logging
import os
import secrets
import uuid
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import (
    FastAPI,
    File,
    HTTPException,
    Query,
    Request,
    Security,
    UploadFile,
    WebSocket,
    WebSocketDisconnect,
    status,
)
from fastapi.responses import FileResponse, JSONResponse
from fastapi.security.api_key import APIKeyHeader
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError

from .config import (
    RemoteConfig,
    clear_runtime_state,
    get_config,
    is_loopback_host,
    publish_server_registration,
    validate_bind_security,
    withdraw_server_registration,
    write_runtime_state,
)
from .crypto import EnvelopeError, decode_key, decrypt_payload
from .keys import is_known_key
from .mailbox import MailboxError, validate_target
from .models import (
    ApprovalResponseRequest,
    ConversationSummary,
    KeyPressRequest,
    MuteMailboxRequest,
    NewSessionRequest,
    RenameConversationRequest,
    SubmitMetaJobRequest,
    UserPromptRequest,
)
from .pty_runner import get_pty_supervisor
from .push import get_push_manager
from .session_manager import SessionManager
from .spawner import SessionSpawner, SpawnerBusyError, SpawnerError
from .tmux_runner import get_tmux_supervisor
from .version import VERSION

logger = logging.getLogger("agy_remote.server")
STATIC_DIR = Path(__file__).parent / "static"

api_key_header = APIKeyHeader(name="X-Auth-Token", auto_error=False)


#: Extensions accepted by /api/upload, mapped to their magic-byte signatures.
#: SVG is deliberately absent: it is an active-content format that can carry
#: script, and these files land in the workspace the agent operates on.
IMAGE_SIGNATURES: dict[str, tuple[bytes, ...]] = {
    ".png": (b"\x89PNG\r\n\x1a\n",),
    ".jpg": (b"\xff\xd8\xff",),
    ".jpeg": (b"\xff\xd8\xff",),
    ".gif": (b"GIF87a", b"GIF89a"),
    ".webp": (b"RIFF",),
}

MAX_UPLOAD_BYTES = 25 * 1024 * 1024


def looks_like_image(ext: str, content: bytes) -> bool:
    """Verify the bytes actually match the claimed image extension.

    An attacker-supplied extension proves nothing; sniffing the magic bytes
    stops a script or binary being dropped into the workspace as `evil.png`.
    """
    signatures = IMAGE_SIGNATURES.get(ext)
    if not signatures:
        return False
    if not any(content.startswith(sig) for sig in signatures):
        return False
    if ext == ".webp":
        return len(content) >= 12 and content[8:12] == b"WEBP"
    return True


def create_app(
    config: RemoteConfig | None = None,
    session_mgr: SessionManager | None = None,
) -> FastAPI:
    """Factory creating configured FastAPI app."""
    cfg = config or get_config()
    validate_bind_security(cfg)
    push_mgr = get_push_manager()
    if session_mgr is None:
        session_mgr = SessionManager(cfg, push_manager=push_mgr)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
        """Start/stop the session manager and publish helper credentials."""
        await session_mgr.start()
        # A supervisor that already exists (a re-attach, or a caller that built
        # one first) is mirrored from here. `agy-remote run` builds its
        # supervisor *after* starting the server, so it hands it over itself --
        # this lookup found None and the screen went unmirrored for the life of
        # the process.
        supervisor = get_pty_supervisor()
        if supervisor is not None:
            session_mgr.attach_terminal(supervisor)
        # Let the PreToolUse hook (a separate process) find our token and port.
        write_runtime_state(cfg)
        # And let a hook inside the tmux session we adopted find *us*, rather
        # than whichever server happens to own the shared state file.
        publish_server_registration(cfg)
        owner_pid = os.getpid()
        try:
            yield
        finally:
            clear_runtime_state(owner_pid=owner_pid)
            withdraw_server_registration(cfg.port, owner_pid=owner_pid)
            await session_mgr.stop()

    app = FastAPI(
        title="Antigravity Remote",
        description="Mobile Remote Web PWA with E2EE & Web Push for Antigravity CLI",
        version=VERSION,
        lifespan=lifespan,
    )
    app.state.session_manager = session_mgr
    app.state.config = cfg
    app.state.spawner = SessionSpawner(cfg, session_mgr, push_mgr)

    #: Sent on every response as a second line of defence behind the <meta> CSP
    #: in index.html. No third-party origins are permitted: this page holds the
    #: E2EE key and auth token, so any remote script is a credential thief.
    SECURITY_HEADERS = {
        "Content-Security-Policy": (
            "default-src 'self'; "
            "script-src 'self' 'unsafe-eval'; "
            "style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data: blob:; "
            "manifest-src 'self' data:; "
            "connect-src 'self' ws: wss:; "
            "object-src 'none'; "
            "base-uri 'none'; "
            "form-action 'none'; "
            "frame-ancestors 'none'"
        ),
        "X-Content-Type-Options": "nosniff",
        "X-Frame-Options": "DENY",
        # Keeps the ?token= query string out of any outbound Referer.
        "Referrer-Policy": "no-referrer",
        "Cross-Origin-Opener-Policy": "same-origin",
        "Cache-Control": "no-store",
    }

    @app.middleware("http")
    async def add_security_headers(request: Request, call_next):  # type: ignore[no-untyped-def]
        """Attach hardening headers to every response."""
        response = await call_next(request)
        for header, value in SECURITY_HEADERS.items():
            response.headers.setdefault(header, value)
        return response

    @app.middleware("http")
    async def guard_host_header(request: Request, call_next):  # type: ignore[no-untyped-def]
        """Block DNS-rebinding when the token gate is switched off.

        With auth disabled the bind is loopback-only (see validate_bind_security),
        so a request arriving under any other hostname is a rebound attacker
        domain resolving to 127.0.0.1, not a legitimate client.
        """
        if not cfg.enable_auth:
            host = (request.headers.get("host") or "").rsplit(":", 1)[0]
            if not is_loopback_host(host):
                return JSONResponse(
                    status_code=421,
                    content={"detail": "Unrecognized Host header"},
                )
        return await call_next(request)

    def get_mgr(req: Request) -> SessionManager:
        return getattr(req.app.state, "session_manager", session_mgr)

    def token_ok(provided: str | None) -> bool:
        """Constant-time token check, used by every authenticated entry point.

        The pairing deadline is enforced here, per check, not at startup: a
        boot-time verdict alone would let a long-running server honor an
        expired pairing until its next restart, which is exactly the window
        the TTL exists to close. (A WebSocket authenticated before the
        deadline keeps its connection; new connections are refused.)
        """
        if cfg.pairing_expired():
            logger.info("Refusing expired pairing; restart agy-remote to mint a new QR")
            return False

        return secrets.compare_digest(
            (provided or "").encode("utf-8"),
            cfg.auth_token.encode("utf-8"),
        )

    def verify_auth(
        request: Request,
        token_query: str | None = Query(None, alias="token"),
        token_header: str | None = Security(api_key_header),
    ) -> bool:
        if not cfg.enable_auth:
            return True

        if not token_ok(token_header or token_query):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid or missing authentication token",
            )
        return True

    # -------------------------------------------------------------------------
    # REST Endpoints
    # -------------------------------------------------------------------------

    @app.get("/api/status")
    async def get_status(
        request: Request,
        token: str | None = Query(None),
        token_header: str | None = Security(api_key_header),
    ) -> dict[str, Any]:
        """Return server status, encryption info, and connection links."""
        if cfg.enable_auth and not token_ok(token_header or token):
            # Disclose nothing beyond the fact that a token is needed: version
            # and feature flags are useful reconnaissance for an unauthenticated
            # caller and are not needed to render the login state.
            return {"auth_required": True, "authenticated": False}

        mgr = get_mgr(request)
        pty = get_pty_supervisor()
        tmux = get_tmux_supervisor()
        return {
            "auth_required": cfg.enable_auth,
            "authenticated": True,
            "version": f"v{VERSION}",
            "e2ee_enabled": cfg.e2ee_enabled,
            # Which engine is behind this server. The PWA renders whatever a
            # backend normalizes into one step shape, so without this it can
            # only guess at a name -- and a wrong name makes a session look
            # like something it is not.
            "agent": cfg.agent,
            "active_conversation_id": mgr.active_conversation_id,
            "supervisor_running": (pty is not None and pty.running) or (tmux is not None and tmux.has_session()),
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
            result: dict[str, Any] = {
                "id": conversation_id,
                "steps": steps,
                "pending_approvals": mgr.get_active_pending_approvals(),
            }
        else:
            result = await mgr.backend.load_conversation(mgr, conversation_id)
            if result is None:
                raise HTTPException(status_code=404, detail="Conversation not found")

        return result

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
        success = await mgr.switch_conversation(conversation_id, pin=True)
        if not success:
            raise HTTPException(status_code=404, detail="Could not switch conversation")
        return {"status": "ok", "active_conversation_id": conversation_id}

    @app.post("/api/conversations/{conversation_id}/rename")
    async def rename_conversation_endpoint(
        conversation_id: str,
        request: Request,
        token: str | None = Query(None),
        token_header: str | None = Security(api_key_header),
    ) -> dict[str, Any]:
        """Rename a conversation from the phone, so sessions can be told apart.

        The same sealing rule as the other content endpoints: with E2EE on, an
        unsealed body is never legitimate. The new name goes to every connected
        client as `session_renamed`, so open drawers and headers redraw.
        """
        verify_auth(request, token, token_header)

        mgr = get_mgr(request)
        body = await request.json()
        if cfg.e2ee_enabled:
            if not isinstance(body, dict) or not body.get("encrypted"):
                raise HTTPException(status_code=400, detail="Encrypted body required while E2EE is enabled")
            try:
                body = decrypt_payload(body, decode_key(cfg.e2ee_key), guard=mgr.replay_guard)
            except Exception as e:
                raise HTTPException(status_code=400, detail=f"Could not open envelope: {e}") from e

        try:
            req = RenameConversationRequest.model_validate(body)
        except ValidationError as e:
            raise HTTPException(status_code=422, detail=str(e)) from e
        summary = mgr.rename_conversation(conversation_id, req.title)
        if summary is None:
            raise HTTPException(status_code=404, detail="Conversation not found")

        await mgr.broadcast(
            {
                "event": "session_renamed",
                "data": {"conversation_id": conversation_id, "conversation": summary},
            }
        )
        return {"status": "ok", "conversation": summary}

    @app.post("/api/sessions", status_code=status.HTTP_202_ACCEPTED)
    async def create_session(
        request: Request,
        token: str | None = Query(None),
        token_header: str | None = Security(api_key_header),
    ) -> dict[str, Any]:
        """Clone a repository and start an agy on it, reported over events.

        The reply is the *acceptance*: the clone, the spawn and the readiness
        wait take minutes, and progress arrives as `session_spawning` /
        `session_created` events rather than as a held response.

        The same sealing rule as the other content endpoints: with E2EE on, an
        unsealed body is never legitimate.
        """
        verify_auth(request, token, token_header)

        mgr = get_mgr(request)
        body = await request.json()
        if cfg.e2ee_enabled:
            if not isinstance(body, dict) or not body.get("encrypted"):
                raise HTTPException(status_code=400, detail="Encrypted body required while E2EE is enabled")
            try:
                body = decrypt_payload(body, decode_key(cfg.e2ee_key), guard=mgr.replay_guard)
            except Exception as e:
                raise HTTPException(status_code=400, detail=f"Could not open envelope: {e}") from e

        req = NewSessionRequest.model_validate(body)
        spawner: SessionSpawner = request.app.state.spawner
        try:
            return spawner.create(req)
        except SpawnerBusyError as e:
            raise HTTPException(status_code=409, detail=str(e)) from e
        except SpawnerError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e

    @app.post("/api/sessions/{session_id}/interrupt")
    async def interrupt_session_endpoint(
        session_id: str,
        request: Request,
        token: str | None = Query(None),
        token_header: str | None = Security(api_key_header),
    ) -> dict[str, Any]:
        """Interrupt a running or stalled session with SIGINT (Ctrl+C)."""
        verify_auth(request, token, token_header)
        mgr = get_mgr(request)
        ok = mgr.interrupt_session(session_id)
        if not ok:
            ok = _press_key("interrupt", session_id) == "ok"
        return {"status": "ok" if ok else "failed", "session_id": session_id}

    @app.post("/api/sessions/{session_id}/kill")
    async def kill_session_endpoint(
        session_id: str,
        request: Request,
        token: str | None = Query(None),
        token_header: str | None = Security(api_key_header),
    ) -> dict[str, Any]:
        """Forcefully terminate a running or stalled session."""
        verify_auth(request, token, token_header)
        mgr = get_mgr(request)
        ok = mgr.kill_session(session_id)
        return {"status": "ok" if ok else "failed", "session_id": session_id}

    @app.post("/api/sessions/{conversation_id}/queue/reorder")
    async def reorder_queue_endpoint(
        conversation_id: str,
        request: Request,
        token: str | None = Query(None),
        token_header: str | None = Security(api_key_header),
    ) -> dict[str, Any]:
        """Reorder queued prompts for a conversation."""
        verify_auth(request, token, token_header)
        mgr = get_mgr(request)
        body = await request.json()
        ordered_ids = body.get("ordered_ids", [])
        if not isinstance(ordered_ids, list):
            raise HTTPException(status_code=400, detail="ordered_ids list required")
        ok = await mgr.reorder_queued_prompts(conversation_id, ordered_ids)
        return {"status": "ok" if ok else "failed", "conversation_id": conversation_id}

    # -------------------------------------------------------------------------
    # Unified Multi-Agent & Meta-AGY Endpoints
    # -------------------------------------------------------------------------

    @app.get("/api/agents")
    async def list_agents_endpoint(
        request: Request,
        token: str | None = Query(None),
        token_header: str | None = Security(api_key_header),
    ) -> list[dict[str, Any]]:
        """List all agents (native Antigravity sessions and meta-AGY jobs)."""
        verify_auth(request, token, token_header)
        mgr = get_mgr(request)
        agents = await mgr.list_agents()
        return [a.model_dump(mode="json") for a in agents]

    @app.get("/api/agents/{agent_id}")
    async def get_agent_endpoint(
        agent_id: str,
        request: Request,
        token: str | None = Query(None),
        token_header: str | None = Security(api_key_header),
    ) -> dict[str, Any]:
        """Get details of a specific agent."""
        verify_auth(request, token, token_header)
        mgr = get_mgr(request)
        agent = await mgr.get_agent(agent_id)
        if not agent:
            raise HTTPException(status_code=404, detail=f"Agent {agent_id} not found")
        return agent.model_dump(mode="json")

    @app.get("/api/agents/{agent_id}/output")
    async def get_agent_output_endpoint(
        agent_id: str,
        request: Request,
        offset: int = 0,
        token: str | None = Query(None),
        token_header: str | None = Security(api_key_header),
    ) -> dict[str, Any]:
        """Fetch incremental output for an agent."""
        verify_auth(request, token, token_header)
        mgr = get_mgr(request)
        content, next_offset = await mgr.get_agent_output(agent_id, offset=offset)
        return {
            "agent_id": agent_id,
            "offset": offset,
            "next_offset": next_offset,
            "content": content,
        }

    @app.post("/api/agents/jobs", status_code=status.HTTP_201_CREATED)
    async def submit_meta_job_endpoint(
        request: Request,
        token: str | None = Query(None),
        token_header: str | None = Security(api_key_header),
    ) -> dict[str, Any]:
        """Submit a new task to meta-AGY."""
        verify_auth(request, token, token_header)
        mgr = get_mgr(request)
        body = await request.json()
        if cfg.e2ee_enabled and isinstance(body, dict) and body.get("encrypted"):
            try:
                body = decrypt_payload(body, decode_key(cfg.e2ee_key), guard=mgr.replay_guard)
            except Exception as e:
                raise HTTPException(status_code=400, detail=f"Could not open envelope: {e}") from e

        req = SubmitMetaJobRequest.model_validate(body)
        try:
            job = await mgr.submit_meta_job(
                project=req.project,
                task=req.task,
                provider=req.provider,
                model=req.model,
                context=req.context,
            )
            return job.model_dump(mode="json")
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"Failed to submit meta-AGY job: {exc}") from exc

    @app.post("/api/agents/{agent_id}/cancel")
    async def cancel_agent_endpoint(
        agent_id: str,
        request: Request,
        token: str | None = Query(None),
        token_header: str | None = Security(api_key_header),
    ) -> dict[str, Any]:
        """Stop or cancel an agent."""
        verify_auth(request, token, token_header)
        mgr = get_mgr(request)
        success = await mgr.cancel_agent(agent_id)
        return {"ok": success, "agent_id": agent_id}

    @app.post("/api/agents/{agent_id}/retry")
    async def retry_agent_endpoint(
        agent_id: str,
        request: Request,
        token: str | None = Query(None),
        token_header: str | None = Security(api_key_header),
    ) -> dict[str, Any]:
        """Retry a completed or failed agent job."""
        verify_auth(request, token, token_header)
        mgr = get_mgr(request)
        try:
            job = await mgr.retry_agent(agent_id)
            return job.model_dump(mode="json")
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"Failed to retry agent {agent_id}: {exc}") from exc

    @app.get("/api/mailbox")
    async def get_mailbox(
        request: Request,
        token: str | None = Query(None),
        token_header: str | None = Security(api_key_header),
    ) -> dict[str, Any]:
        """Traffic and loop status across all agent-to-agent mailbox pairs."""
        verify_auth(request, token, token_header)
        mgr = get_mgr(request)
        return {"pairs": mgr.agent_traffic()}

    @app.get("/api/usage")
    async def get_usage(
        request: Request,
        token: str | None = Query(None),
        token_header: str | None = Security(api_key_header),
    ) -> dict[str, Any]:
        """Current context window tokens, cost, model, and execution mode HUD data."""
        verify_auth(request, token, token_header)
        mgr = get_mgr(request)
        return mgr.get_usage_hud()

    @app.get("/api/conversations/{conversation_id}/usage")
    async def get_conversation_usage(
        conversation_id: str,
        request: Request,
        token: str | None = Query(None),
        token_header: str | None = Security(api_key_header),
    ) -> dict[str, Any]:
        """Usage and context stats for a specific conversation."""
        verify_auth(request, token, token_header)
        mgr = get_mgr(request)
        return mgr.get_usage_hud(conversation_id)

    @app.get("/api/push/preferences")
    async def get_push_preferences_endpoint(
        request: Request,
        endpoint: str | None = Query(None),
        token: str | None = Query(None),
        token_header: str | None = Security(api_key_header),
    ) -> dict[str, Any]:
        """Get notification preferences for an endpoint."""
        verify_auth(request, token, token_header)
        return {"preferences": push_mgr.get_preferences(endpoint)}

    @app.post("/api/push/preferences")
    async def update_push_preferences_endpoint(
        request: Request,
        token: str | None = Query(None),
        token_header: str | None = Security(api_key_header),
    ) -> dict[str, Any]:
        """Update notification preferences for an endpoint."""
        verify_auth(request, token, token_header)
        body = await request.json()
        endpoint = body.get("endpoint")
        prefs = body.get("preferences", {})
        if not endpoint or not isinstance(prefs, dict):
            raise HTTPException(status_code=400, detail="endpoint and preferences dict required")
        ok = push_mgr.update_preferences(endpoint, prefs)
        return {"ok": ok, "preferences": push_mgr.get_preferences(endpoint)}

    @app.get("/api/approvals/policy")
    async def get_approval_policy(
        request: Request,
        conversation_id: str | None = Query(None),
        token: str | None = Query(None),
        token_header: str | None = Security(api_key_header),
    ) -> dict[str, Any]:
        """Get the active approval policy ('ask_all', 'auto_reads', 'auto_all')."""
        verify_auth(request, token, token_header)
        mgr = get_mgr(request)
        return {"policy": mgr.get_approval_policy(conversation_id)}

    @app.post("/api/approvals/policy")
    async def set_approval_policy(
        request: Request,
        token: str | None = Query(None),
        token_header: str | None = Security(api_key_header),
    ) -> dict[str, Any]:
        """Set approval policy globally or per session."""
        verify_auth(request, token, token_header)
        mgr = get_mgr(request)
        body = await request.json()
        policy = body.get("policy", "ask_all")
        cid = body.get("conversation_id")
        try:
            mgr.set_approval_policy(policy, cid)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        await mgr.broadcast(
            {
                "event": "approval_policy_changed",
                "data": {"policy": policy, "conversation_id": cid},
            }
        )
        return {"status": "ok", "policy": policy, "conversation_id": cid}

    @app.get("/api/approvals/{approval_id}")
    async def get_approval(
        approval_id: str,
        request: Request,
        token: str | None = Query(None),
        token_header: str | None = Security(api_key_header),
    ) -> dict[str, Any]:
        """A pending approval's data, for the desktop popup to display.

        `tui-approve` runs inside a tmux popup with only ids in its
        environment; this is where it learns what it is offering to approve.
        Answered or unknown ids are 404, so a stale popup cannot re-decide.
        """
        verify_auth(request, token, token_header)
        mgr = get_mgr(request)
        info = mgr.pending_approval(approval_id)
        if info is None:
            raise HTTPException(status_code=404, detail="Unknown or already-resolved approval")
        return info

    @app.post("/api/mailbox/mute")
    async def mute_mailbox(
        req: MuteMailboxRequest,
        request: Request,
        token: str | None = Query(None),
        token_header: str | None = Security(api_key_header),
    ) -> dict[str, Any]:
        """Mute an agent mailbox pair, pausing delivery between them."""
        verify_auth(request, token, token_header)
        mgr = get_mgr(request)
        try:
            a = validate_target(req.a)
            b = validate_target(req.b)
        except MailboxError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        if a == b:
            raise HTTPException(status_code=400, detail="Cannot mute a session talking to itself")
        mgr.mute_mailbox_pair(a, b)
        return {"status": "ok", "pair": {"a": a, "b": b, "muted": True}}

    @app.delete("/api/mailbox/mute")
    async def unmute_mailbox(
        req: MuteMailboxRequest,
        request: Request,
        token: str | None = Query(None),
        token_header: str | None = Security(api_key_header),
    ) -> dict[str, Any]:
        """Unmute an agent mailbox pair, resuming message delivery."""
        verify_auth(request, token, token_header)
        mgr = get_mgr(request)
        try:
            a = validate_target(req.a)
            b = validate_target(req.b)
        except MailboxError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        if a == b:
            raise HTTPException(status_code=400, detail="Cannot unmute a session talking to itself")
        mgr.unmute_mailbox_pair(a, b)
        return {"status": "ok", "pair": {"a": a, "b": b, "muted": False}}

    @app.get("/api/screen")
    async def get_screen(
        request: Request,
        conversation_id: str | None = Query(None),
        token: str | None = Query(None),
        token_header: str | None = Security(api_key_header),
    ) -> dict[str, Any]:
        """The supervised terminal as plain text, or null in watcher mode."""
        verify_auth(request, token, token_header)
        mgr = get_mgr(request)
        mirror = mgr.get_screen_mirror(conversation_id)
        return {"terminal": mirror.snapshot() if mirror else None}

    @app.get("/api/file")
    async def get_file(
        request: Request,
        path: str = Query(...),
        token: str | None = Query(None),
        token_header: str | None = Security(api_key_header),
    ) -> dict[str, Any]:
        """Read a host file the transcript named, for the phone to display.

        The agent references files as [file:///abs/path]; the server runs on
        the machine that has them. `read_host_file` does the security work --
        resolved paths, sanctioned roots only, size-capped -- so the endpoint
        only maps its verdicts onto status codes.
        """
        verify_auth(request, token, token_header)
        mgr = get_mgr(request)
        try:
            return mgr.read_host_file(path)
        except PermissionError as e:
            raise HTTPException(status_code=403, detail=str(e)) from e
        except FileNotFoundError as e:
            raise HTTPException(status_code=404, detail=f"File not found: {e}") from e
        except ValueError as e:
            raise HTTPException(status_code=415, detail=str(e)) from e

    @app.get("/api/git-diff")
    async def git_diff(
        request: Request,
        conversation_id: str | None = Query(None),
        token: str | None = Query(None),
        token_header: str | None = Security(api_key_header),
    ) -> dict[str, Any]:
        """The named session's git working-tree diff, for the phone's pane.

        The workdir is whatever the session registry holds -- the request
        names a session, never a path -- and `working_tree_diff` does the
        security and sanity work. Verdicts map like /api/file's do.
        """
        verify_auth(request, token, token_header)
        mgr = get_mgr(request)
        try:
            return mgr.working_tree_diff(conversation_id)
        except LookupError as e:
            raise HTTPException(status_code=404, detail=str(e)) from e
        except PermissionError as e:
            raise HTTPException(status_code=403, detail=str(e)) from e
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e

    @app.get("/api/files")
    async def list_files(
        request: Request,
        path: str | None = Query(None),
        conversation_id: str | None = Query(None),
        token: str | None = Query(None),
        token_header: str | None = Security(api_key_header),
    ) -> dict[str, Any]:
        """List one directory of the session's project, for the phone's file tree.

        With no path the session's workdir is the root, so the request never
        names a location outside what the session already sanctioned.
        `list_host_dir` does the security work; verdicts map like /api/file's.
        """
        verify_auth(request, token, token_header)
        mgr = get_mgr(request)
        try:
            return mgr.list_host_dir(path, conversation_id)
        except LookupError as e:
            raise HTTPException(status_code=404, detail=str(e)) from e
        except PermissionError as e:
            raise HTTPException(status_code=403, detail=str(e)) from e
        except FileNotFoundError as e:
            raise HTTPException(status_code=404, detail=f"Directory not found: {e}") from e
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e

    def _press_key(key: str, conversation_id: str | None = None) -> str:
        """Deliver a key to whichever supervisor is live, if any."""
        sup = session_mgr.get_supervisor(conversation_id)
        if sup is not None and hasattr(sup, "send_key"):
            return "ok" if sup.send_key(key) else "refused"

        tmux = get_tmux_supervisor()
        if tmux and tmux.has_session():
            return "ok" if tmux.send_key(key) else "refused"

        pty = get_pty_supervisor()
        if pty and pty.running:
            return "ok" if pty.send_key(key) else "refused"

        return "no_session"

    @app.post("/api/key")
    async def send_key(
        req: KeyPressRequest,
        request: Request,
        token: str | None = Query(None),
        token_header: str | None = Security(api_key_header),
    ) -> dict[str, Any]:
        """Press a named key -- Shift+Tab, Esc, an arrow -- in the live session.

        agy's execution mode, its panels and its selection lists are reachable
        only by keystroke; a prompt line cannot express any of them.
        """
        verify_auth(request, token, token_header)
        return {"status": _press_key(req.key, req.conversation_id)}

    @app.post("/api/prompt")
    async def send_prompt(
        request: Request,
        token: str | None = Query(None),
        token_header: str | None = Security(api_key_header),
    ) -> dict[str, Any]:
        """Send user prompt from mobile UI into active session.

        The same sealing rule as the WebSocket: with E2EE on, an unsealed body
        is never legitimate. This is the fallback the PWA uses when its socket
        is dead, and accepting bare JSON here shipped prompt content payload-
        plaintext across the hops the AES-GCM layer exists to protect --
        holding the token must not be enough to bypass it.
        """
        verify_auth(request, token, token_header)

        mgr = get_mgr(request)
        body = await request.json()
        if cfg.e2ee_enabled:
            if not isinstance(body, dict) or not body.get("encrypted"):
                raise HTTPException(status_code=400, detail="Encrypted body required while E2EE is enabled")
            try:
                body = decrypt_payload(body, decode_key(cfg.e2ee_key), guard=mgr.replay_guard)
            except Exception as e:
                raise HTTPException(status_code=400, detail=f"Could not open envelope: {e}") from e

        req = UserPromptRequest.model_validate(body)
        # A human's own words: any loop this session was caught in is broken.
        # A prompt without a target goes to the session on screen, so that is
        # whose loop this one breaks.
        mgr.note_human_prompt(req.conversation_id or mgr.active_conversation_id)
        result = await mgr.submit_prompt(req.prompt, req.conversation_id)
        if result["status"] == "queued":
            return {
                "status": "queued",
                "prompt_id": result["prompt_id"],
                "message": "Agent is mid-turn; prompt queued and will be delivered when it finishes.",
            }

        delivered_via = result["delivered_via"]
        if delivered_via == "broadcast":
            return {
                "status": "ok",
                "delivered_via": "broadcast",
                "message": "Prompt broadcasted. To enable direct CLI typing, launch with 'agy-remote run'.",
            }
        return {"status": "ok", "delivered_via": delivered_via}

    @app.post("/api/upload")
    async def upload_file(
        request: Request,
        file: UploadFile = File(...),
        token: str | None = Query(None),
        token_header: str | None = Security(api_key_header),
    ) -> dict[str, Any]:
        """Upload image/screenshot from mobile camera or gallery into workspace with strict sanitization."""
        verify_auth(request, token, token_header)
        upload_dir = (Path.cwd() / ".agents" / "uploads").resolve()
        upload_dir.mkdir(parents=True, exist_ok=True)

        raw_filename = Path(file.filename or "image.jpg").name
        ext = Path(raw_filename).suffix.lower()
        if ext not in IMAGE_SIGNATURES:
            raise HTTPException(status_code=400, detail="Invalid file type. Only image uploads are allowed.")

        content = await file.read(MAX_UPLOAD_BYTES + 1)
        if len(content) > MAX_UPLOAD_BYTES:
            raise HTTPException(status_code=413, detail="File too large (max 25MB).")

        if not looks_like_image(ext, content):
            raise HTTPException(status_code=400, detail="File content does not match its image extension.")

        filename = f"mobile_{uuid.uuid4().hex[:8]}_{raw_filename}"
        dest = (upload_dir / filename).resolve()
        if not dest.is_relative_to(upload_dir):
            raise HTTPException(status_code=400, detail="Invalid target filename.")

        with open(dest, "wb") as f:
            f.write(content)
        dest.chmod(0o600)

        return {
            "status": "ok",
            "filename": filename,
            "relative_path": f".agents/uploads/{filename}",
            "absolute_path": str(dest),
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
        if cfg.enable_auth and not token_ok(token_header):
            raise HTTPException(status_code=401, detail="Unauthorized hook call")

        mgr = get_mgr(request)
        if mgr.backend.name != "agy":
            # Only agy speaks this hook protocol. A stale hook firing at a
            # server fronting something else must not mint phantom approvals.
            raise HTTPException(status_code=400, detail="This server is not fronting an agy session")

        payload = await request.json()
        tool_call = payload.get("toolCall", {})
        tool_name = tool_call.get("name", "unknown_tool")
        args = tool_call.get("args", {})
        if isinstance(args, str):
            with contextlib.suppress(Exception):
                args = json.loads(args)

        conversation_id = payload.get("conversationId")
        if not conversation_id or conversation_id == "default":
            conversation_id = mgr.supervised_conversation_id or mgr.active_conversation_id or "default"

        approval_id = str(uuid.uuid4())

        # A mixed install breaks the approval protocol silently: a new run's
        # skip-permissions marker means nothing to an old hook and vice versa.
        # The approval still proceeds -- refusing it would strand the agent --
        # but the drift must be visible somewhere other than the symptoms.
        hook_version = (request.headers.get("X-Agy-Remote-Version") or "").lstrip("v")
        if hook_version and hook_version != VERSION:
            logger.warning(
                "PreToolUse hook reports v%s but this server runs v%s: "
                "a mixed agy-remote install. Upgrade the stale side "
                "(usually: uv tool upgrade agy-remote) and restart the session.",
                hook_version,
                VERSION,
            )

        # If this server is supervising a session, bind to its reported conversation ID
        if conversation_id and conversation_id != "default":
            await mgr.bind_supervised_conversation(conversation_id)

        # Only buzz a phone about a decision the phone is actually going to be
        # asked for. Suppress push if a connected client is already actively
        # focused on this session (presence suppression).
        if mgr.can_hold_approval(conversation_id) and not mgr.is_client_focused(conversation_id):
            push_mgr.send_notification(
                title=f"Permission Required: {tool_name}",
                body=f"{tool_name}: {args.get('CommandLine') or args.get('TargetFile') or 'Action requested'}",
                data={
                    "approval_id": approval_id,
                    "conversation_id": conversation_id,
                    "type": "approval_request",
                },
            )

        origin_pane = payload.get("tmux_pane") or payload.get("tmuxPane") or request.headers.get("X-Tmux-Pane") or None

        decision_payload = await mgr.request_approval(
            approval_id=approval_id,
            conversation_id=conversation_id,
            tool_name=tool_name,
            args=args,
            origin_pane=origin_pane,
        )
        return JSONResponse(content=decision_payload)

    # -------------------------------------------------------------------------
    # Web Push Notification Endpoints
    # -------------------------------------------------------------------------

    @app.get("/api/push/vapid-public-key")
    async def get_vapid_key() -> dict[str, str]:
        """Get public VAPID key for browser push subscription."""
        return {"public_key": push_mgr.public_key}

    @app.post("/api/push/subscribe")
    async def subscribe_push(
        request: Request,
        token: str | None = Query(None),
        token_header: str | None = Security(api_key_header),
    ) -> dict[str, str]:
        """Register a browser push subscription."""
        verify_auth(request, token, token_header)
        sub_data = await request.json()
        push_mgr.add_subscription(sub_data)
        return {"status": "subscribed"}

    # -------------------------------------------------------------------------
    # WebSocket Endpoint with E2EE
    # -------------------------------------------------------------------------

    @app.websocket("/ws")
    async def websocket_endpoint(
        websocket: WebSocket,
        token: str | None = Query(None),
        device: str | None = Query(None),
    ) -> None:
        """Bidirectional WebSocket for live updates with E2EE envelope support."""
        if cfg.enable_auth and not token_ok(token):
            await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
            return

        await websocket.accept()
        mgr = session_mgr
        # The per-device identity the peer count is deduped by. A client may
        # hold several sockets (reconnect races, a suspended reload); they are
        # all one device, and the badge must say so.
        await mgr.register_client(websocket, device_id=device[:128] if device else None)

        raw_key_bytes = decode_key(cfg.e2ee_key) if cfg.e2ee_enabled else None

        try:
            while True:
                raw_msg = await websocket.receive_json()
                # Whatever this frame turns out to be, it proves the client is
                # alive -- the reaper judges by the last thing it said.
                mgr.note_client_activity(websocket)

                async def reject(reason: str) -> None:
                    """Say so, rather than dropping the frame in silence.

                    A prompt the server threw away used to look exactly like
                    one it accepted: the phone sent into an open socket, got
                    nothing back and cleared the input. The reply is sealed
                    like any other, so it tells an attacker nothing they could
                    not already see from the connection staying open.
                    """
                    logger.warning("Rejected WS frame: %s", reason)
                    await mgr.send_to(websocket, {"event": "frame_rejected", "data": {"reason": reason}})

                if raw_key_bytes is not None:
                    # E2EE is on, so an unsealed frame is never legitimate:
                    # accepting one would let anyone holding only the token
                    # downgrade out of encryption and drive the agent.
                    if not raw_msg.get("encrypted"):
                        await reject("encrypted frame required while E2EE is enabled")
                        continue
                    try:
                        msg = decrypt_payload(raw_msg, raw_key_bytes, guard=mgr.replay_guard)
                    except EnvelopeError as e:
                        await reject(str(e))
                        continue
                    except Exception as e:
                        await reject(f"could not decrypt: {e}")
                        continue
                else:
                    msg = raw_msg

                if not isinstance(msg, dict):
                    continue

                action = msg.get("action")
                data = msg.get("data", {})

                if action == "ping":
                    await mgr.send_to(websocket, {"event": "pong"})
                elif action == "focus_state":
                    focused = bool(data.get("focused", False))
                    conv_id = data.get("conversation_id")
                    mgr.set_client_focus(websocket, focused, conv_id)
                elif action == "send_prompt":
                    prompt_text = data.get("prompt", "")
                    if prompt_text:
                        # A human's own words: any loop the target session was
                        # caught in is broken (a bare prompt targets the
                        # session on screen).
                        mgr.note_human_prompt(data.get("conversation_id") or mgr.active_conversation_id)
                        # submit_prompt is the one door both entry points use:
                        # idle conversations go straight out (prompt_sent), a
                        # conversation mid-turn queues (prompt_queued).
                        await mgr.submit_prompt(prompt_text, data.get("conversation_id"))
                elif action == "cancel_prompt":
                    prompt_id = data.get("prompt_id")
                    if isinstance(prompt_id, str):
                        await mgr.cancel_queued_prompt(prompt_id)
                elif action == "request_screen":
                    # A client revealing the panel wants the screen now, not at
                    # the next redraw -- a still terminal never sends one.
                    conv_id = data.get("conversation_id")
                    mirror = mgr.get_screen_mirror(conv_id)
                    if mirror is not None:
                        await mgr.send_to(websocket, {"event": "terminal_screen", "data": mirror.snapshot()})
                elif action == "send_key":
                    key = data.get("key")
                    conv_id = data.get("conversation_id")
                    if isinstance(key, str) and is_known_key(key):
                        _press_key(key, conv_id)
                    else:
                        logger.warning("Refused unknown key press: %r", key)
                elif action == "approve_tool":
                    approval_id = data.get("approval_id")
                    decision = data.get("decision", "deny")
                    reason = data.get("reason")
                    if approval_id:
                        try:
                            response = ApprovalResponseRequest(decision=decision, reason=reason)
                        except ValidationError as e:
                            logger.warning("Rejected malformed approval response: %s", e)
                            continue
                        await mgr.resolve_approval(approval_id, response)
                elif action == "set_approval_policy":
                    policy = data.get("policy", "ask_all")
                    conv_id = data.get("conversation_id")
                    with contextlib.suppress(ValueError):
                        mgr.set_approval_policy(policy, conv_id)
                        await mgr.broadcast(
                            {
                                "event": "approval_policy_changed",
                                "data": {"policy": policy, "conversation_id": conv_id},
                            }
                        )
                elif action == "interrupt_session":
                    conv_id = data.get("conversation_id")
                    ok = mgr.interrupt_session(conv_id)
                    if not ok:
                        _press_key("interrupt", conv_id)
                elif action == "kill_session":
                    conv_id = data.get("conversation_id")
                    mgr.kill_session(conv_id)
                elif action == "reorder_prompt_queue":
                    conv_id = data.get("conversation_id")
                    ordered_ids = data.get("ordered_ids", [])
                    if conv_id and isinstance(ordered_ids, list):
                        await mgr.reorder_queued_prompts(conv_id, ordered_ids)
                elif action == "switch_conversation":
                    target_id = data.get("conversation_id")
                    # Only switch to an id that resolves to a real
                    # conversation, so a crafted id cannot leave the manager
                    # pointing at nothing.
                    if isinstance(target_id, str) and mgr.backend.is_known_conversation(target_id):
                        await mgr.switch_conversation(target_id, pin=True)
                elif action == "get_agents":
                    agents = await mgr.list_agents()
                    await mgr.send_to(
                        websocket,
                        {"event": "agent_updated", "data": {"agents": [a.model_dump(mode="json") for a in agents]}},
                    )
                elif action == "get_agent_output":
                    agent_id = data.get("agent_id")
                    offset = int(data.get("offset", 0))
                    if agent_id:
                        content, next_offset = await mgr.get_agent_output(agent_id, offset)
                        await mgr.send_to(
                            websocket,
                            {
                                "event": "agent_output",
                                "data": {
                                    "agent_id": agent_id,
                                    "offset": offset,
                                    "next_offset": next_offset,
                                    "content": content,
                                },
                            },
                        )
                elif action == "replay":
                    since_seq = int(data.get("since_seq", 0))
                    replayed = await mgr.replay_since(websocket, since_seq)
                    if not replayed:
                        init_payload = mgr._init_data()
                        await mgr.send_to(websocket, init_payload)
                elif action == "get_usage":
                    conv_id = data.get("conversation_id")
                    usage = mgr.get_usage_hud(conv_id)
                    await mgr.send_to(websocket, {"event": "usage_updated", "data": usage})
        except WebSocketDisconnect:
            pass
        except Exception as e:
            logger.debug("WebSocket loop terminated: %s", e)
        finally:
            mgr.unregister_client(websocket)
            # The count is the alarm, so a device leaving has to clear it.
            with contextlib.suppress(Exception):
                await mgr.announce_peers()

    # -------------------------------------------------------------------------
    # PWA Static Files & Fallback
    # -------------------------------------------------------------------------

    if STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

        @app.get("/manifest.json")
        async def manifest() -> FileResponse:
            """The PWA manifest, with no credentials in it, for anyone.

            An earlier fix put `?token=...#key=...` into an authenticated
            manifest's `start_url` so the installed app would launch paired.
            That was the first code path ever to place the E2EE key in a
            response body -- the key rides only in the QR *fragment* precisely
            because fragments never traverse the wire, and on the documented
            plaintext-LAN topologies the payload layer keyed by it is the only
            protection. It also converted token-knowledge into key-knowledge on
            request, defeating the downgrade defence outright.

            The paired install is built client-side instead: the PWA assembles
            a manifest from the credentials it already holds in localStorage
            and hands it to the browser as a data: URI, so nothing secret is
            ever served. This static file is the anonymous fallback.
            """
            return FileResponse(STATIC_DIR / "manifest.json", media_type="application/manifest+json")

        @app.get("/sw.js")
        async def service_worker() -> FileResponse:
            return FileResponse(STATIC_DIR / "sw.js", media_type="application/javascript")

        @app.get("/")
        async def index() -> FileResponse:
            return FileResponse(STATIC_DIR / "index.html")

    return app


def __getattr__(name: str) -> Any:
    """Build the ASGI app lazily on first access.

    `uvicorn agy_remote.server:app` still works, but merely importing this
    module no longer constructs a server. Eager construction meant an unsafe
    configuration blew up as an import traceback before the CLI could print a
    readable message, and every import touched the brain dir and VAPID keys.
    """
    if name == "app":
        global _app_singleton
        try:
            return _app_singleton
        except NameError:
            _app_singleton = create_app()
            return _app_singleton
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
